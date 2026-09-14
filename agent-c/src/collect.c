/*
 * PerfLens Device Agent — collection loops (rounds + continuous pipeline)
 */

#include "agent.h"

/* Same pid, same start time. kill(pid, 0) alone would happily keep
 * profiling whatever process the kernel handed the recycled pid to. */
static int target_alive(const struct agent_state *a)
{
    if (!process_exists(a->pid)) return 0;
    if (a->pid_start == 0) return 1;
    unsigned long long now = process_start_time(a->pid);
    return now == 0 || now == a->pid_start;
}

static void copy_first_line(char *dst, size_t cap, const struct buf *b)
{
    dst[0] = '\0';
    if (!b || b->len == 0) return;
    size_t n = b->len < cap - 1 ? b->len : cap - 1;
    memcpy(dst, b->data, n);
    dst[n] = '\0';
    char *nl = strchr(dst, '\n');
    if (nl) *nl = '\0';
}

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
                        size_t *out_raw_len, uint8_t *out_flag,
                        const struct agent_state *a)
{
    if (caps->record_event_count == 0) return NULL;

    /* Create temp file for perf.data */
    char tmpl[PATH_MAX];
    snprintf(tmpl, sizeof(tmpl), "%s/perflens-data-XXXXXX", agent_tmpdir());
    int fd = mkstemp(tmpl);
    if (fd < 0) {
        agent_warn("mkstemp failed in %s: %s", agent_tmpdir(), strerror(errno));
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
    if (events && events[0])
        snprintf(rec_events, sizeof(rec_events), "%s", events);
    else
        join_events(rec_events, sizeof(rec_events), caps->record_events,
                    caps->record_event_count, NULL);

    /* Everything perf stat can count, plus the task clock */
    char all_events[512];
    join_events(all_events, sizeof(all_events), caps->all_events,
                caps->all_event_count, "task-clock");

    char *argv_rec[MAX_CMD_ARGS], *argv_stat[MAX_CMD_ARGS];
    build_record_argv(argv_rec, MAX_CMD_ARGS, rec_events, pid_str, freq_str,
                      tmpl, caps->callgraph, dur_str);
    build_stat_argv(argv_stat, MAX_CMD_ARGS, all_events, pid_str, dur_str);

    /* --- Fork perf record and perf stat concurrently --- */
    struct buf rec_err, stat_err;
    buf_init(&rec_err); buf_init(&stat_err);

    int rec_out_fd, rec_err_fd, stat_out_fd, stat_err_fd;
    pid_t rec_pid = fork_cmd(argv_rec, &rec_out_fd, &rec_err_fd, 0);
    if (rec_pid < 0) {
        unlink(tmpl);
        return NULL;
    }

    pid_t stat_pid = fork_cmd(argv_stat, &stat_out_fd, &stat_err_fd, 0);
    if (stat_pid < 0) {
        kill_child_group(rec_pid, SIGKILL);
        reap_child(rec_pid, 0);
        untrack_child(rec_pid);
        close(rec_out_fd); close(rec_err_fd);
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
    int killed = 0;

    struct timespec poll_start;
    clock_gettime(CLOCK_MONOTONIC, &poll_start);

    /* Stops early on cancel too: a perf that ignores SIGTERM keeps its
     * pipes open, and waiting for EOF would hold `stop` for the whole
     * round timeout. reap_child() below then escalates to SIGKILL. */
    while (open_pfds > 0 && !g_shutdown && !collection_cancelled(a)) {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        int elapsed_ms = (int)((now.tv_sec - poll_start.tv_sec) * 1000 +
                               (now.tv_nsec - poll_start.tv_nsec) / 1000000);
        int remaining_ms = timeout * 1000 - elapsed_ms;
        if (remaining_ms <= 0) {
            agent_warn("Record+stat timed out after %ds, killing", timeout);
            kill_child_group(rec_pid, SIGKILL);
            kill_child_group(stat_pid, SIGKILL);
            killed = 1;
            break;
        }

        int ret = poll(pfds, 4, remaining_ms < 200 ? remaining_ms : 200);
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

            if (buf_ensure_small(target, target->len + 4096, SMALL_BUF_SIZE) < 0) {
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

    /* Wait for both children -- bounded: a perf stuck flushing a slow
     * RAM-backed /tmp used to hang this thread, and stop with it. */
    int grace = killed ? 0 : CHILD_GRACE_MS;
    int rec_status = reap_child(rec_pid, grace);
    untrack_child(rec_pid);
    int stat_status = reap_child(stat_pid, grace);
    untrack_child(stat_pid);

    int rc_rec = WIFEXITED(rec_status) ? WEXITSTATUS(rec_status) : -1;
    int rc_stat = WIFEXITED(stat_status) ? WEXITSTATUS(stat_status) : -1;

    if (collection_cancelled(a)) {
        buf_free(&rec_err); buf_free(&stat_err);
        unlink(tmpl);
        return NULL;
    }

    if (rc_rec != 0) {
        char msg[256];
        copy_first_line(msg, sizeof(msg), &rec_err);
        agent_log("perf record failed (rc=%d): %s", rc_rec, msg);
        buf_free(&rec_err); buf_free(&stat_err);
        unlink(tmpl);
        return NULL;
    }

    /* Run perf script, streaming its stdout through the sink so the raw
     * text is never held in memory whole. Nice 5: it is the CPU-heavy
     * symbolizer, and on the single-core targets that run rounds mode it
     * competes with the very workload it measures. */
    char *argv_script[MAX_CMD_ARGS];
    build_script_argv(argv_script, MAX_CMD_ARGS, caps->script_fields, tmpl);

    struct sink sk;
    sink_init(&sk, want_compress);

    struct buf script_err;
    buf_init(&script_err);
    int rc_script = run_cmd_to_sink(argv_script, &sk, &script_err, timeout,
                                    CHILD_NICE, a);

    if (rc_script != 0 || sk.error) {
        char msg[256];
        copy_first_line(msg, sizeof(msg), &script_err);
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
        agent_warn("Round dropped: compression failed");
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
 * The symbolized stream is cut into chunks every `duration` seconds -- or
 * at CHUNK_SOFT_LIMIT raw bytes, whichever comes first -- at sample
 * boundaries and shipped through the streaming sink. perf stat runs as
 * back-to-back one-shot rounds of the same length, so every interval is
 * counted; each completed round queues up and rides the next chunk as a
 * PERF_STAT section.
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

static void short_sleep(struct agent_state *a, int ms)
{
    struct timespec tick = {0, 200000000L};
    while (ms > 0 && !a->collect_stop && !g_shutdown && !a->session_done) {
        nanosleep(&tick, NULL);
        ms -= 200;
    }
}

static int agent_state_now(struct agent_state *a)
{
    pthread_mutex_lock(&a->state_lock);
    int st = a->state;
    pthread_mutex_unlock(&a->state_lock);
    return st;
}

static void collect_pipeline_loop(struct agent_state *a)
{
    const struct capabilities *caps = a->caps;
    const char *marker = "\n### PERF_STAT ###\n";
    char pid_str[16];
    snprintf(pid_str, sizeof(pid_str), "%d", a->pid);

    /* Event lists (same construction as round mode; record honors the
     * caller-selected subset) */
    char rec_events[512];
    if (a->sel_events[0])
        snprintf(rec_events, sizeof(rec_events), "%s", a->sel_events);
    else
        join_events(rec_events, sizeof(rec_events), caps->record_events,
                    caps->record_event_count, NULL);
    char all_events[512];
    join_events(all_events, sizeof(all_events), caps->all_events,
                caps->all_event_count, "task-clock");

    int chunk_num = 0;
    int chunks_dropped = 0;
    unsigned long long raw_total = 0, sent_total = 0;

    while (!a->collect_stop && !g_shutdown && !a->session_done) {
        if (agent_state_now(a) == AGENT_PAUSED) {
            short_sleep(a, 1000);
            continue;
        }

        if (!target_alive(a)) {
            agent_log("Process %d exited", a->pid);
            pthread_mutex_lock(&a->state_lock);
            a->state = AGENT_IDLE;
            pthread_mutex_unlock(&a->state_lock);
            return;
        }

        pthread_mutex_lock(&a->state_lock);
        int freq = a->frequency;
        pthread_mutex_unlock(&a->state_lock);
        char freq_str[16];
        snprintf(freq_str, sizeof(freq_str), "%d", freq);

        char *argv_rec[MAX_CMD_ARGS], *argv_script[MAX_CMD_ARGS];
        build_record_argv(argv_rec, MAX_CMD_ARGS, rec_events, pid_str,
                          freq_str, "-", caps->callgraph, NULL);
        build_script_argv(argv_script, MAX_CMD_ARGS, caps->script_fields, "-");

        pid_t rec_pid, script_pid;
        int rec_err_fd, script_out_fd, script_err_fd;
        if (fork_pipeline(argv_rec, argv_script, &rec_pid, &script_pid,
                          &rec_err_fd, &script_out_fd, &script_err_fd) < 0) {
            short_sleep(a, 1000);
            continue;
        }
        agent_log("Continuous pipeline started (pid %d, %d Hz)", a->pid, freq);

        char *chunk_buf = malloc(IO_CHUNK);    /* stderr and stat reads */
        struct buf carry;                      /* script output, fed at sample boundaries */
        buf_init(&carry);
        struct sink sk;
        sink_init(&sk, 1);

        /* perf stat rounds: one always running, results queued for the
         * next flush. A round used to start only after the previous
         * result had been *attached*, which measured every other interval. */
        pid_t stat_pid = -1;
        int stat_ofd = -1, stat_efd = -1;
        struct buf stat_out, stat_queue;
        buf_init(&stat_out);
        buf_init(&stat_queue);

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
            int st = a->state;
            int dur = a->duration;
            int now_freq = a->frequency;
            pthread_mutex_unlock(&a->state_lock);
            if (st == AGENT_PAUSED || now_freq != freq) {
                restart = 1;
                break;
            }
            if (dur < 1) dur = 1;

            /* Start a stat round whenever none is running */
            if (stat_pid < 0) {
                char dur_str[16];
                snprintf(dur_str, sizeof(dur_str), "%d", dur);
                char *argv_stat[MAX_CMD_ARGS];
                build_stat_argv(argv_stat, MAX_CMD_ARGS, all_events, pid_str,
                                dur_str);
                stat_out.len = 0;
                stat_pid = fork_cmd(argv_stat, &stat_ofd, &stat_efd, 0);
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

            int flush_now = 0;

            /* Symbolized samples: script stdout -> carry tail -> sink */
            if (pfds[0].fd >= 0 && (pfds[0].revents & (POLLIN | POLLHUP))) {
                if (buf_ensure(&carry, carry.len + IO_CHUNK) < 0) {
                    /* Out of memory. Ship what the sink already holds and
                     * drop the carry: a torn sample the parser skips beats
                     * skipping bytes and carrying the tear forward. */
                    agent_warn("Out of memory buffering perf script output; "
                               "dropping %zu buffered bytes", carry.len);
                    carry.len = 0;
                    flush_now = 1;
                    ssize_t n = read(script_out_fd, chunk_buf, IO_CHUNK);
                    if (n <= 0) pipeline_eof = 1;
                } else {
                    ssize_t n = read(script_out_fd, carry.data + carry.len,
                                     carry.cap - carry.len);
                    if (n > 0) {
                        carry.len += (size_t)n;
                        if (carry_feed(&carry, &sk, caps->callgraph[0] != '\0') < 0) {
                            /* The sink refused: over the hard cap, or zstd
                             * failed. Unreachable in practice now that
                             * chunks flush at the soft limit, but it used
                             * to be silent, and every later write failed. */
                            chunks_dropped++;
                            agent_warn("Chunk dropped: %zu raw bytes exceeded "
                                       "the %d MB cap or compression failed "
                                       "(%d dropped so far)",
                                       sk.raw_len, MAX_BUF_SIZE / (1024 * 1024),
                                       chunks_dropped);
                            sink_reset(&sk);
                            if (carry.len > MAX_BUF_SIZE / 2) carry.len = 0;
                            clock_gettime(CLOCK_MONOTONIC, &chunk_start);
                        }
                        if (sk.raw_len >= CHUNK_SOFT_LIMIT) flush_now = 1;
                    } else {
                        pipeline_eof = 1;
                    }
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
                    if (buf_ensure_small(&stat_out, stat_out.len + (size_t)n,
                                         SMALL_BUF_SIZE) == 0) {
                        memcpy(stat_out.data + stat_out.len, chunk_buf, (size_t)n);
                        stat_out.len += (size_t)n;
                    }
                } else {
                    close(stat_efd);
                    stat_efd = -1;
                }
            }

            /* Reap the stat round once both its pipes hit EOF, queue its
             * output, and let the next iteration start the next round. */
            if (stat_pid >= 0 && stat_ofd < 0 && stat_efd < 0) {
                int ws = reap_child(stat_pid, 1000);
                untrack_child(stat_pid);
                stat_pid = -1;
                if (WIFEXITED(ws) && WEXITSTATUS(ws) == 0 && stat_out.len > 0) {
                    size_t need = stat_queue.len + strlen(marker) + stat_out.len;
                    if (buf_ensure_small(&stat_queue, need, SMALL_BUF_SIZE) == 0) {
                        memcpy(stat_queue.data + stat_queue.len, marker, strlen(marker));
                        stat_queue.len += strlen(marker);
                        memcpy(stat_queue.data + stat_queue.len, stat_out.data, stat_out.len);
                        stat_queue.len += stat_out.len;
                    }
                }
                stat_out.len = 0;
            }

            /* Chunk flush: deadline, size, or stream end */
            struct timespec now;
            clock_gettime(CLOCK_MONOTONIC, &now);
            double elapsed = (double)(now.tv_sec - chunk_start.tv_sec) +
                             (double)(now.tv_nsec - chunk_start.tv_nsec) / 1e9;
            if (elapsed >= (double)dur || pipeline_eof || flush_now) {
                if (pipeline_eof && carry.len > 0) {
                    /* Stream is over — the tail is complete output */
                    sink_write(&sk, carry.data, carry.len);
                    carry.len = 0;
                }
                if (stat_queue.len > 0) {
                    sink_write(&sk, stat_queue.data, stat_queue.len);
                    stat_queue.len = 0;
                }
                if (sk.raw_len > 0 && !a->collect_stop && !a->session_done &&
                    !g_shutdown) {
                    if (sink_finish(&sk) == 0) {
                        chunk_num++;
                        uint8_t flag = sk.compress ? FLAG_DATA_ZSTD : FLAG_DATA_RAW;
                        if (agent_send_frame(a, sk.out.data, sk.out.len, flag) == 0) {
                            raw_total += sk.raw_len;
                            sent_total += sk.out.len;
                            agent_debug("Chunk %d: %zu bytes, compressed %zu "
                                        "(ratio %.1fx)%s",
                                        chunk_num, sk.raw_len, sk.out.len,
                                        sk.out.len > 0
                                            ? (double)sk.raw_len / (double)sk.out.len
                                            : 0.0,
                                        flush_now ? ", size flush" : "");
                            if (chunk_num == 1 || chunk_num % 100 == 0)
                                agent_log("%d chunk%s sent: %.1f MB of perf "
                                          "script text, %.1f MB on the wire",
                                          chunk_num, chunk_num == 1 ? "" : "s",
                                          (double)raw_total / 1048576.0,
                                          (double)sent_total / 1048576.0);
                        } else {
                            agent_log("Chunk %d: send failed: %s",
                                      chunk_num, strerror(errno));
                            send_failed = 1;
                        }
                    } else {
                        chunks_dropped++;
                        agent_warn("Chunk dropped: compression failed "
                                   "(%d dropped so far)", chunks_dropped);
                    }
                }
                sink_reset(&sk);
                clock_gettime(CLOCK_MONOTONIC, &chunk_start);
            }
        }

        /* Teardown pipeline and any in-flight stat round */
        kill_child_group(rec_pid, SIGTERM);
        kill_child_group(script_pid, SIGTERM);
        if (stat_pid >= 0) kill_child_group(stat_pid, SIGTERM);

        if (script_out_fd >= 0) close(script_out_fd);
        if (script_err_fd >= 0) close(script_err_fd);
        if (rec_err_fd >= 0) close(rec_err_fd);
        if (stat_ofd >= 0) close(stat_ofd);
        if (stat_efd >= 0) close(stat_efd);

        reap_child(rec_pid, CHILD_GRACE_MS);
        untrack_child(rec_pid);
        reap_child(script_pid, CHILD_GRACE_MS);
        untrack_child(script_pid);
        if (stat_pid >= 0) {
            reap_child(stat_pid, CHILD_GRACE_MS);
            untrack_child(stat_pid);
        }

        free(chunk_buf);
        sink_free(&sk);
        buf_free(&carry);
        buf_free(&stat_out);
        buf_free(&stat_queue);

        if (send_failed) return;

        if (pipeline_eof && !restart &&
            !a->collect_stop && !g_shutdown && !a->session_done) {
            if (!target_alive(a)) {
                agent_log("Process %d exited", a->pid);
                pthread_mutex_lock(&a->state_lock);
                a->state = AGENT_IDLE;
                pthread_mutex_unlock(&a->state_lock);
                return;
            }
            /* pause kills the pipeline, and its EOF usually lands here
             * before the loop above sees AGENT_PAUSED: not unexpected. */
            if (agent_state_now(a) == AGENT_PAUSED) continue;
            agent_log("Pipeline ended unexpectedly (%s), restarting in 1s...",
                      rec_diag[0] ? rec_diag : "no diagnostic");
            short_sleep(a, 1000);
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
    unsigned long long raw_total = 0, sent_total = 0;

    while (!a->collect_stop && !g_shutdown && !a->session_done) {
        if (agent_state_now(a) == AGENT_PAUSED) {
            short_sleep(a, 1000);
            continue;
        }

        if (!target_alive(a)) {
            agent_log("Process %d exited", a->pid);
            pthread_mutex_lock(&a->state_lock);
            a->state = AGENT_IDLE;
            pthread_mutex_unlock(&a->state_lock);
            break;
        }

        pthread_mutex_lock(&a->state_lock);
        int freq = a->frequency;
        int dur = a->duration;
        pthread_mutex_unlock(&a->state_lock);

        round_num++;
        agent_debug("Round %d: collecting (%ds)...", round_num, dur);

        size_t payload_len = 0, raw_len = 0;
        uint8_t flag = FLAG_DATA_RAW;
        char *payload = collect_one_round(a->caps, a->sel_events,
                                          a->pid, freq, dur, 1,
                                          &payload_len, &raw_len, &flag, a);

        if (a->collect_stop || g_shutdown || a->session_done) {
            free(payload);
            break;
        }

        if (!payload || raw_len == 0) {
            agent_debug("Round %d: no data", round_num);
            free(payload);
            short_sleep(a, 1000);
            continue;
        }

        /* Send */
        if (agent_send_frame(a, payload, payload_len, flag) == 0) {
            raw_total += raw_len;
            sent_total += payload_len;
            agent_debug("Round %d: perf script %zu bytes, sent %zu bytes%s",
                        round_num, raw_len, payload_len,
                        flag == FLAG_DATA_ZSTD ? " (zstd)" : "");
            if (round_num == 1 || round_num % 100 == 0)
                agent_log("%d round%s sent: %.1f MB of perf script text, "
                          "%.1f MB on the wire", round_num,
                          round_num == 1 ? "" : "s",
                          (double)raw_total / 1048576.0,
                          (double)sent_total / 1048576.0);
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
