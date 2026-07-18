/*
 * PerfLens Device Agent — collection loops (rounds + continuous pipeline)
 */

#include "agent.h"

/* --------------------------------------------------------------------------
 * Collection: one round of perf record + perf stat + perf script
 * -------------------------------------------------------------------------- */

/* Runs one round of perf record + stat + script. Returns a malloc'd
 * payload ready to send (zstd-compressed when want_compress and the
 * stream initialized, raw otherwise), or NULL on failure/no data.
 * out_len is the payload size, out_raw_len the uncompressed size,
 * out_flag the wire flag matching the payload encoding. */
char *collect_one_round(const struct capabilities *caps, const char *events,
                        int pid, int frequency, int duration,
                        int want_compress, size_t *out_len,
                        size_t *out_raw_len, uint8_t *out_flag)
{
    if (caps->record_event_count == 0) return NULL;

    /* Create temp file for perf.data */
    char tmpl[] = "/tmp/perflens-data-XXXXXX";
    int fd = mkstemp(tmpl);
    if (fd < 0) {
        agent_warn("mkstemp failed: %s", strerror(errno));
        return NULL;
    }
    close(fd);

    char pid_str[16], freq_str[16], dur_str[16];
    snprintf(pid_str, sizeof(pid_str), "%d", pid);
    snprintf(freq_str, sizeof(freq_str), "%d", frequency);
    snprintf(dur_str, sizeof(dur_str), "%d", duration);

    int timeout = duration + 10;

    /* Record events: caller-selected subset, or all probed */
    char rec_events[512];
    if (events && events[0]) {
        snprintf(rec_events, sizeof(rec_events), "%s", events);
    } else {
        rec_events[0] = '\0';
        for (int i = 0; i < caps->record_event_count; i++) {
            if (i > 0) strncat(rec_events, ",", sizeof(rec_events) - strlen(rec_events) - 1);
            strncat(rec_events, caps->record_events[i],
                    sizeof(rec_events) - strlen(rec_events) - 1);
        }
    }

    /* Build all events string for stat: "cycles,...,task-clock" */
    char all_events[512];
    all_events[0] = '\0';
    for (int i = 0; i < caps->all_event_count; i++) {
        if (i > 0) strncat(all_events, ",", sizeof(all_events) - strlen(all_events) - 1);
        strncat(all_events, caps->all_events[i],
                sizeof(all_events) - strlen(all_events) - 1);
    }
    strncat(all_events, ",task-clock", sizeof(all_events) - strlen(all_events) - 1);

    /* --- Fork perf record and perf stat concurrently --- */

    /* Build perf record argv */
    char *argv_rec[MAX_CMD_ARGS];
    int ri = 0;
    argv_rec[ri++] = PERF; argv_rec[ri++] = "record";
    argv_rec[ri++] = "-e"; argv_rec[ri++] = rec_events;
    argv_rec[ri++] = "-p"; argv_rec[ri++] = pid_str;
    argv_rec[ri++] = "-F"; argv_rec[ri++] = freq_str;
    argv_rec[ri++] = "-o"; argv_rec[ri++] = tmpl;
    if (caps->callgraph[0]) {
        argv_rec[ri++] = "--call-graph";
        argv_rec[ri++] = (char *)caps->callgraph;
    }
    argv_rec[ri++] = "--"; argv_rec[ri++] = "sleep"; argv_rec[ri++] = dur_str;
    argv_rec[ri] = NULL;

    /* Build perf stat argv */
    char *argv_stat[MAX_CMD_ARGS];
    int si = 0;
    argv_stat[si++] = PERF; argv_stat[si++] = "stat";
    argv_stat[si++] = "-e"; argv_stat[si++] = all_events;
    argv_stat[si++] = "-p"; argv_stat[si++] = pid_str;
    argv_stat[si++] = "--"; argv_stat[si++] = "sleep"; argv_stat[si++] = dur_str;
    argv_stat[si] = NULL;

    /* Fork both children before waiting for either */
    struct buf rec_err, stat_err;
    buf_init(&rec_err); buf_init(&stat_err);

    int rec_out_fd, rec_err_fd, stat_out_fd, stat_err_fd;
    pid_t rec_pid = fork_cmd(argv_rec, &rec_out_fd, &rec_err_fd);
    if (rec_pid < 0) {
        buf_free(&rec_err); buf_free(&stat_err);
        unlink(tmpl);
        return NULL;
    }

    pid_t stat_pid = fork_cmd(argv_stat, &stat_out_fd, &stat_err_fd);
    if (stat_pid < 0) {
        kill(rec_pid, SIGKILL);
        int ws; do { } while (waitpid(rec_pid, &ws, 0) < 0 && errno == EINTR);
        untrack_child(rec_pid);
        close(rec_out_fd); close(rec_err_fd);
        buf_free(&rec_err); buf_free(&stat_err);
        unlink(tmpl);
        return NULL;
    }

    /* Poll all 4 pipe fds: rec stdout (discard), rec stderr (capture),
     *                       stat stdout (discard), stat stderr (capture) */
    struct pollfd pfds[4];
    pfds[0].fd = rec_out_fd;  pfds[0].events = POLLIN;
    pfds[1].fd = rec_err_fd;  pfds[1].events = POLLIN;
    pfds[2].fd = stat_out_fd; pfds[2].events = POLLIN;
    pfds[3].fd = stat_err_fd; pfds[3].events = POLLIN;
    struct buf *targets[4] = { NULL, &rec_err, NULL, &stat_err };
    int open_pfds = 4;

    struct timespec poll_start;
    clock_gettime(CLOCK_MONOTONIC, &poll_start);

    while (open_pfds > 0 && !g_shutdown) {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        int elapsed_ms = (int)((now.tv_sec - poll_start.tv_sec) * 1000 +
                               (now.tv_nsec - poll_start.tv_nsec) / 1000000);
        int remaining_ms = timeout * 1000 - elapsed_ms;
        if (remaining_ms <= 0) {
            agent_warn("Record+stat timed out after %ds, killing", timeout);
            kill(rec_pid, SIGKILL);
            kill(stat_pid, SIGKILL);
            break;
        }

        int ret = poll(pfds, 4, remaining_ms < 500 ? remaining_ms : 500);
        if (ret < 0) {
            if (errno == EINTR) continue;
            break;
        }

        for (int i = 0; i < 4; i++) {
            if (pfds[i].fd < 0) continue;
            if (!(pfds[i].revents & (POLLIN | POLLHUP))) continue;

            struct buf *target = targets[i];
            if (!target) {
                char discard[4096];
                ssize_t n = read(pfds[i].fd, discard, sizeof(discard));
                if (n <= 0) { close(pfds[i].fd); pfds[i].fd = -1; open_pfds--; }
                continue;
            }

            if (buf_ensure(target, target->len + 4096) < 0) {
                close(pfds[i].fd); pfds[i].fd = -1; open_pfds--;
                continue;
            }
            ssize_t n = read(pfds[i].fd, target->data + target->len,
                             target->cap - target->len);
            if (n > 0) {
                target->len += (size_t)n;
            } else {
                close(pfds[i].fd); pfds[i].fd = -1; open_pfds--;
            }
        }
    }

    /* Close any remaining pipe fds */
    for (int i = 0; i < 4; i++)
        if (pfds[i].fd >= 0) close(pfds[i].fd);

    /* Wait for both children */
    int rec_status = 0, stat_status = 0, wrc;
    do { wrc = waitpid(rec_pid, &rec_status, 0); } while (wrc < 0 && errno == EINTR);
    untrack_child(rec_pid);
    do { wrc = waitpid(stat_pid, &stat_status, 0); } while (wrc < 0 && errno == EINTR);
    untrack_child(stat_pid);

    int rc_rec = WIFEXITED(rec_status) ? WEXITSTATUS(rec_status) : -1;
    int rc_stat = WIFEXITED(stat_status) ? WEXITSTATUS(stat_status) : -1;

    if (rc_rec != 0) {
        char msg[256] = "";
        if (rec_err.len > 0) {
            size_t cplen = rec_err.len < sizeof(msg) - 1 ? rec_err.len : sizeof(msg) - 1;
            memcpy(msg, rec_err.data, cplen);
            msg[cplen] = '\0';
            /* Strip trailing newline */
            char *nl = strrchr(msg, '\n');
            if (nl) *nl = '\0';
        }
        agent_log("perf record failed (rc=%d): %s", rc_rec, msg);
        buf_free(&rec_err); buf_free(&stat_err);
        unlink(tmpl);
        return NULL;
    }

    /* Run perf script, streaming its stdout through the sink so the raw
     * text is never held in memory whole */
    char *argv_script[MAX_CMD_ARGS];
    int sci = 0;
    argv_script[sci++] = PERF; argv_script[sci++] = "script";
    if (caps->script_fields[0]) {
        argv_script[sci++] = "-F";
        argv_script[sci++] = (char *)caps->script_fields;
    }
    argv_script[sci++] = "-i"; argv_script[sci++] = tmpl;
    argv_script[sci] = NULL;

    struct sink sk;
    sink_init(&sk, want_compress);

    struct buf script_err;
    buf_init(&script_err);
    int rc_script = run_cmd_to_sink(argv_script, &sk, &script_err, timeout);

    if (rc_script != 0 || sk.error) {
        char msg[256] = "";
        if (script_err.len > 0) {
            size_t cplen = script_err.len < sizeof(msg) - 1
                         ? script_err.len : sizeof(msg) - 1;
            memcpy(msg, script_err.data, cplen);
            msg[cplen] = '\0';
        }
        agent_log("perf script failed (rc=%d%s): %s", rc_script,
                  sk.error ? ", output cap or compression error" : "", msg);
        sink_free(&sk);
        buf_free(&script_err);
        buf_free(&rec_err); buf_free(&stat_err);
        unlink(tmpl);
        return NULL;
    }
    buf_free(&script_err);

    /* Append stat marker + stat stderr into the same stream */
    if (rc_stat == 0 && stat_err.len > 0) {
        const char *marker = "\n### PERF_STAT ###\n";
        sink_write(&sk, marker, strlen(marker));
        sink_write(&sk, stat_err.data, stat_err.len);
    }

    if (sink_finish(&sk) < 0) {
        sink_free(&sk);
        buf_free(&rec_err); buf_free(&stat_err);
        unlink(tmpl);
        return NULL;
    }

    buf_free(&rec_err);
    buf_free(&stat_err);
    unlink(tmpl);

    *out_len = sk.out.len;
    *out_raw_len = sk.raw_len;
    *out_flag = sk.compress ? FLAG_DATA_ZSTD : FLAG_DATA_RAW;

    /* Hand the output buffer to the caller; release only the zstd context */
    char *result = sk.out.data;
    if (sk.zcs) ZSTD_freeCStream(sk.zcs);
    return result;
}

/* --------------------------------------------------------------------------
 * Continuous collection (pipe mode)
 *
 * One long-lived `perf record -o - | perf script -i -` pipeline instead
 * of discrete rounds: no sampling dead time while perf script runs, and
 * symbol tables are parsed once per pipeline instead of once per round.
 * The symbolized stream is cut into chunks every `duration` seconds at
 * sample boundaries and shipped through the streaming sink. perf stat
 * still runs as one-shot rounds; each completed stat is appended to the
 * next chunk as a PERF_STAT section.
 * -------------------------------------------------------------------------- */

/* Feed carry contents up to the last complete sample boundary into the
 * sink. Callchain output separates samples with blank lines; flat output
 * is one sample per line. Returns 0, or -1 on sink error. */
static int carry_feed(struct buf *carry, struct sink *sk, int have_callgraph)
{
    if (carry->len == 0) return 0;

    size_t cut = 0;
    if (have_callgraph) {
        for (size_t i = carry->len; i >= 2; i--) {
            if (carry->data[i - 1] == '\n' && carry->data[i - 2] == '\n') {
                cut = i;
                break;
            }
        }
    } else {
        for (size_t i = carry->len; i >= 1; i--) {
            if (carry->data[i - 1] == '\n') { cut = i; break; }
        }
    }
    /* Defensive: never let a boundary-less stream pin the carry forever */
    if (cut == 0 && carry->len > 1024 * 1024) cut = carry->len;
    if (cut == 0) return 0;

    if (sink_write(sk, carry->data, cut) < 0) return -1;
    memmove(carry->data, carry->data + cut, carry->len - cut);
    carry->len -= cut;
    return 0;
}

static void collect_pipeline_loop(struct agent_state *a)
{
    const struct capabilities *caps = a->caps;
    char pid_str[16];
    snprintf(pid_str, sizeof(pid_str), "%d", a->pid);

    /* Event lists (same construction as round mode; record honors the
     * caller-selected subset) */
    char rec_events[512] = "";
    if (a->sel_events[0]) {
        snprintf(rec_events, sizeof(rec_events), "%s", a->sel_events);
    } else {
        for (int i = 0; i < caps->record_event_count; i++) {
            if (i > 0) strncat(rec_events, ",", sizeof(rec_events) - strlen(rec_events) - 1);
            strncat(rec_events, caps->record_events[i],
                    sizeof(rec_events) - strlen(rec_events) - 1);
        }
    }
    char all_events[512] = "";
    for (int i = 0; i < caps->all_event_count; i++) {
        if (i > 0) strncat(all_events, ",", sizeof(all_events) - strlen(all_events) - 1);
        strncat(all_events, caps->all_events[i],
                sizeof(all_events) - strlen(all_events) - 1);
    }
    strncat(all_events, ",task-clock", sizeof(all_events) - strlen(all_events) - 1);

    int chunk_num = 0;

    while (!a->collect_stop && !g_shutdown && !a->session_done) {
        int st;
        pthread_mutex_lock(&a->state_lock);
        st = a->state;
        pthread_mutex_unlock(&a->state_lock);

        if (st == AGENT_PAUSED) {
            int wait_ms = 1000;
            struct timespec tick = {0, 200000000L};
            while (wait_ms > 0 && !a->collect_stop && !g_shutdown && !a->session_done) {
                nanosleep(&tick, NULL);
                wait_ms -= 200;
            }
            continue;
        }

        if (!process_exists(a->pid)) {
            agent_log("Process %d exited", a->pid);
            pthread_mutex_lock(&a->state_lock);
            a->state = AGENT_IDLE;
            pthread_mutex_unlock(&a->state_lock);
            return;
        }

        int freq = a->frequency;
        char freq_str[16];
        snprintf(freq_str, sizeof(freq_str), "%d", freq);

        char *argv_rec[MAX_CMD_ARGS];
        int ri = 0;
        argv_rec[ri++] = PERF; argv_rec[ri++] = "record";
        argv_rec[ri++] = "-e"; argv_rec[ri++] = rec_events;
        argv_rec[ri++] = "-p"; argv_rec[ri++] = pid_str;
        argv_rec[ri++] = "-F"; argv_rec[ri++] = freq_str;
        argv_rec[ri++] = "-o"; argv_rec[ri++] = "-";
        if (caps->callgraph[0]) {
            argv_rec[ri++] = "--call-graph";
            argv_rec[ri++] = (char *)caps->callgraph;
        }
        argv_rec[ri] = NULL;

        char *argv_script[8];
        int sci = 0;
        argv_script[sci++] = PERF; argv_script[sci++] = "script";
        if (caps->script_fields[0]) {
            argv_script[sci++] = "-F";
            argv_script[sci++] = (char *)caps->script_fields;
        }
        argv_script[sci++] = "-i"; argv_script[sci++] = "-";
        argv_script[sci] = NULL;

        pid_t rec_pid, script_pid;
        int rec_err_fd, script_out_fd, script_err_fd;
        if (fork_pipeline(argv_rec, argv_script, &rec_pid, &script_pid,
                          &rec_err_fd, &script_out_fd, &script_err_fd) < 0) {
            int wait_ms = 1000;
            struct timespec tick = {0, 200000000L};
            while (wait_ms > 0 && !a->collect_stop && !g_shutdown && !a->session_done) {
                nanosleep(&tick, NULL);
                wait_ms -= 200;
            }
            continue;
        }
        agent_log("Continuous pipeline started (pid %d, %d Hz)", a->pid, freq);

        char *chunk_buf = malloc(IO_CHUNK);
        struct buf carry;
        buf_init(&carry);
        struct sink sk;
        sink_init(&sk, 1);

        /* One-shot perf stat round state */
        pid_t stat_pid = -1;
        int stat_ofd = -1, stat_efd = -1;
        struct buf stat_out;
        buf_init(&stat_out);
        int stat_done = 0;

        /* Last diagnostic line from perf record, for EOF logging */
        char rec_diag[256] = "";

        struct timespec chunk_start;
        clock_gettime(CLOCK_MONOTONIC, &chunk_start);

        int pipeline_eof = 0;
        int restart = 0;
        int send_failed = 0;

        while (chunk_buf && !a->collect_stop && !g_shutdown &&
               !a->session_done && !pipeline_eof && !restart && !send_failed) {
            pthread_mutex_lock(&a->state_lock);
            st = a->state;
            pthread_mutex_unlock(&a->state_lock);
            if (st == AGENT_PAUSED || a->frequency != freq) {
                restart = 1;
                break;
            }

            int dur = a->duration;
            if (dur < 1) dur = 1;

            /* Start a stat round if none is running */
            if (stat_pid < 0 && !stat_done) {
                char dur_str[16];
                snprintf(dur_str, sizeof(dur_str), "%d", dur);
                char *argv_stat[MAX_CMD_ARGS];
                int si = 0;
                argv_stat[si++] = PERF; argv_stat[si++] = "stat";
                argv_stat[si++] = "-e"; argv_stat[si++] = all_events;
                argv_stat[si++] = "-p"; argv_stat[si++] = pid_str;
                argv_stat[si++] = "--"; argv_stat[si++] = "sleep";
                argv_stat[si++] = dur_str;
                argv_stat[si] = NULL;
                stat_out.len = 0;
                stat_pid = fork_cmd(argv_stat, &stat_ofd, &stat_efd);
                if (stat_pid < 0) { stat_ofd = -1; stat_efd = -1; }
            }

            struct pollfd pfds[5];
            pfds[0].fd = script_out_fd; pfds[0].events = POLLIN;
            pfds[1].fd = script_err_fd; pfds[1].events = POLLIN;
            pfds[2].fd = rec_err_fd;    pfds[2].events = POLLIN;
            pfds[3].fd = stat_ofd;      pfds[3].events = POLLIN;
            pfds[4].fd = stat_efd;      pfds[4].events = POLLIN;

            int ret = poll(pfds, 5, 200);
            if (ret < 0) {
                if (errno == EINTR) continue;
                break;
            }

            /* Symbolized samples: script stdout -> carry -> sink */
            if (pfds[0].fd >= 0 && (pfds[0].revents & (POLLIN | POLLHUP))) {
                ssize_t n = read(script_out_fd, chunk_buf, IO_CHUNK);
                if (n > 0) {
                    if (buf_ensure(&carry, carry.len + (size_t)n) == 0) {
                        memcpy(carry.data + carry.len, chunk_buf, (size_t)n);
                        carry.len += (size_t)n;
                    }
                    carry_feed(&carry, &sk, caps->callgraph[0] != '\0');
                } else {
                    pipeline_eof = 1;
                }
            }

            /* script stderr: discard */
            if (pfds[1].fd >= 0 && (pfds[1].revents & (POLLIN | POLLHUP))) {
                ssize_t n = read(script_err_fd, chunk_buf, IO_CHUNK);
                if (n <= 0) { close(script_err_fd); script_err_fd = -1; }
            }

            /* record stderr: keep the latest line for diagnostics */
            if (pfds[2].fd >= 0 && (pfds[2].revents & (POLLIN | POLLHUP))) {
                ssize_t n = read(rec_err_fd, chunk_buf, IO_CHUNK);
                if (n > 0) {
                    size_t cplen = (size_t)n < sizeof(rec_diag) - 1
                                 ? (size_t)n : sizeof(rec_diag) - 1;
                    memcpy(rec_diag, chunk_buf, cplen);
                    rec_diag[cplen] = '\0';
                } else {
                    close(rec_err_fd);
                    rec_err_fd = -1;
                }
            }

            /* stat stdout: discard (results arrive on stderr) */
            if (pfds[3].fd >= 0 && (pfds[3].revents & (POLLIN | POLLHUP))) {
                ssize_t n = read(stat_ofd, chunk_buf, IO_CHUNK);
                if (n <= 0) { close(stat_ofd); stat_ofd = -1; }
            }

            /* stat stderr: capture */
            if (pfds[4].fd >= 0 && (pfds[4].revents & (POLLIN | POLLHUP))) {
                ssize_t n = read(stat_efd, chunk_buf, IO_CHUNK);
                if (n > 0) {
                    if (buf_ensure(&stat_out, stat_out.len + (size_t)n) == 0) {
                        memcpy(stat_out.data + stat_out.len, chunk_buf, (size_t)n);
                        stat_out.len += (size_t)n;
                    }
                } else {
                    close(stat_efd);
                    stat_efd = -1;
                }
            }

            /* Reap the stat round once both its pipes hit EOF */
            if (stat_pid >= 0 && stat_ofd < 0 && stat_efd < 0) {
                int ws, wrc;
                do { wrc = waitpid(stat_pid, &ws, 0); } while (wrc < 0 && errno == EINTR);
                untrack_child(stat_pid);
                if (WIFEXITED(ws) && WEXITSTATUS(ws) == 0 && stat_out.len > 0)
                    stat_done = 1;
                else
                    stat_out.len = 0;
                stat_pid = -1;
            }

            /* Chunk flush: deadline reached, or stream ended */
            struct timespec now;
            clock_gettime(CLOCK_MONOTONIC, &now);
            double elapsed = (double)(now.tv_sec - chunk_start.tv_sec) +
                             (double)(now.tv_nsec - chunk_start.tv_nsec) / 1e9;
            if (elapsed >= (double)dur || pipeline_eof) {
                if (pipeline_eof && carry.len > 0) {
                    /* Stream is over — the tail is complete output */
                    sink_write(&sk, carry.data, carry.len);
                    carry.len = 0;
                }
                if (stat_done) {
                    const char *marker = "\n### PERF_STAT ###\n";
                    sink_write(&sk, marker, strlen(marker));
                    sink_write(&sk, stat_out.data, stat_out.len);
                    stat_done = 0;
                    stat_out.len = 0;
                }
                if (sk.raw_len > 0 && sink_finish(&sk) == 0 &&
                    !a->collect_stop && !a->session_done && !g_shutdown) {
                    chunk_num++;
                    uint8_t flag = sk.compress ? FLAG_DATA_ZSTD : FLAG_DATA_RAW;
                    if (agent_send_data(a, sk.out.data, sk.out.len, flag) == 0) {
                        agent_log("Chunk %d: %zu bytes, compressed %zu "
                                  "(ratio %.1fx)",
                                  chunk_num, sk.raw_len, sk.out.len,
                                  sk.out.len > 0
                                      ? (double)sk.raw_len / (double)sk.out.len
                                      : 0.0);
                    } else {
                        agent_log("Chunk %d: send failed: %s",
                                  chunk_num, strerror(errno));
                        send_failed = 1;
                    }
                }
                sink_free(&sk);
                sink_init(&sk, 1);
                clock_gettime(CLOCK_MONOTONIC, &chunk_start);
            }
        }

        /* Teardown pipeline and any in-flight stat round */
        kill(rec_pid, SIGTERM);
        kill(script_pid, SIGTERM);
        if (stat_pid >= 0) kill(stat_pid, SIGTERM);

        if (script_out_fd >= 0) close(script_out_fd);
        if (script_err_fd >= 0) close(script_err_fd);
        if (rec_err_fd >= 0) close(rec_err_fd);
        if (stat_ofd >= 0) close(stat_ofd);
        if (stat_efd >= 0) close(stat_efd);

        int ws, wrc;
        do { wrc = waitpid(rec_pid, &ws, 0); } while (wrc < 0 && errno == EINTR);
        untrack_child(rec_pid);
        do { wrc = waitpid(script_pid, &ws, 0); } while (wrc < 0 && errno == EINTR);
        untrack_child(script_pid);
        if (stat_pid >= 0) {
            do { wrc = waitpid(stat_pid, &ws, 0); } while (wrc < 0 && errno == EINTR);
            untrack_child(stat_pid);
        }

        free(chunk_buf);
        sink_free(&sk);
        buf_free(&carry);
        buf_free(&stat_out);

        if (send_failed) return;

        if (pipeline_eof && !restart &&
            !a->collect_stop && !g_shutdown && !a->session_done) {
            if (!process_exists(a->pid)) {
                agent_log("Process %d exited", a->pid);
                pthread_mutex_lock(&a->state_lock);
                a->state = AGENT_IDLE;
                pthread_mutex_unlock(&a->state_lock);
                return;
            }
            agent_log("Pipeline ended unexpectedly (%s), restarting in 1s...",
                      rec_diag[0] ? rec_diag : "no diagnostic");
            int wait_ms = 1000;
            struct timespec tick = {0, 200000000L};
            while (wait_ms > 0 && !a->collect_stop && !g_shutdown && !a->session_done) {
                nanosleep(&tick, NULL);
                wait_ms -= 200;
            }
        }
    }
}

/* --------------------------------------------------------------------------
 * Collection loop thread
 * -------------------------------------------------------------------------- */

void *collection_thread_fn(void *arg)
{
    struct agent_state *a = (struct agent_state *)arg;
    block_signals_in_thread();

    if (a->caps && a->caps->pipe_mode) {
        collect_pipeline_loop(a);
        pthread_mutex_lock(&a->state_lock);
        if (a->state == AGENT_PROFILING || a->state == AGENT_PAUSED)
            a->state = AGENT_IDLE;
        pthread_mutex_unlock(&a->state_lock);
        agent_log("Collection loop ended");
        return NULL;
    }

    int round_num = 0;

    while (!a->collect_stop && !g_shutdown && !a->session_done) {
        int st;
        pthread_mutex_lock(&a->state_lock);
        st = a->state;
        pthread_mutex_unlock(&a->state_lock);

        if (st == AGENT_PAUSED) {
            int wait_ms = 1000;
            struct timespec tick = {0, 200000000L};
            while (wait_ms > 0 && !a->collect_stop && !g_shutdown && !a->session_done) {
                nanosleep(&tick, NULL);
                wait_ms -= 200;
            }
            continue;
        }

        if (!process_exists(a->pid)) {
            agent_log("Process %d exited", a->pid);
            pthread_mutex_lock(&a->state_lock);
            a->state = AGENT_IDLE;
            pthread_mutex_unlock(&a->state_lock);
            break;
        }

        round_num++;
        agent_log("Round %d: collecting (%ds)...", round_num, a->duration);

        size_t payload_len = 0, raw_len = 0;
        uint8_t flag = FLAG_DATA_RAW;
        char *payload = collect_one_round(a->caps, a->sel_events,
                                          a->pid, a->frequency,
                                          a->duration, 1,
                                          &payload_len, &raw_len, &flag);

        if (a->collect_stop || g_shutdown || a->session_done) {
            free(payload);
            break;
        }

        if (!payload || raw_len == 0) {
            agent_log("Round %d: no data", round_num);
            free(payload);
            int wait_ms = 1000;
            struct timespec tick = {0, 200000000L};
            while (wait_ms > 0 && !a->collect_stop && !g_shutdown && !a->session_done) {
                nanosleep(&tick, NULL);
                wait_ms -= 200;
            }
            continue;
        }

        if (flag == FLAG_DATA_ZSTD) {
            double ratio = payload_len > 0
                ? (double)raw_len / (double)payload_len : 0;
            agent_log("Round %d: perf script %zu bytes, "
                      "compressed %zu bytes (ratio %.1fx)",
                      round_num, raw_len, payload_len, ratio);
        } else {
            agent_log("Round %d: perf script %zu bytes (uncompressed)",
                      round_num, raw_len);
        }

        /* Send */
        if (agent_send_data(a, payload, payload_len, flag) == 0) {
            agent_log("Round %d: sent successfully", round_num);
        } else {
            agent_log("Round %d: send failed: %s", round_num, strerror(errno));
            free(payload);
            break;
        }

        free(payload);
    }

    pthread_mutex_lock(&a->state_lock);
    if (a->state == AGENT_PROFILING || a->state == AGENT_PAUSED)
        a->state = AGENT_IDLE;
    pthread_mutex_unlock(&a->state_lock);

    agent_log("Collection loop ended");
    return NULL;
}

