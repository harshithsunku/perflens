/*
 * PerfLens Device Agent — command handlers + dispatch
 */

#include "agent.h"

/* --------------------------------------------------------------------------
 * Response helpers
 *
 * Every response carries the request's id back, and the id is whatever the
 * peer sent -- so it is escaped here, once, rather than printed raw at
 * forty call sites (where a quote or backslash in it used to produce
 * invalid JSON, before authentication too). Dispatch already refuses ids
 * outside [A-Za-z0-9_.:-]{1,63}, so the escape is belt and braces.
 * -------------------------------------------------------------------------- */

static void send_ok(struct agent_state *a, const char *id)
{
    char esc[160], resp[256];
    json_escape(esc, sizeof(esc), id);
    snprintf(resp, sizeof(resp), "{\"id\":\"%s\",\"ok\":true}", esc);
    agent_send_response(a, resp);
}

static void send_error(struct agent_state *a, const char *id,
                       const char *fmt, ...)
{
    char msg[512], esc_msg[1100], esc_id[160], resp[1400];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);
    json_escape(esc_id, sizeof(esc_id), id);
    json_escape(esc_msg, sizeof(esc_msg), msg);
    snprintf(resp, sizeof(resp),
             "{\"id\":\"%s\",\"ok\":false,\"error\":\"%s\"}", esc_id, esc_msg);
    agent_send_response(a, resp);
}

/* Send a response assembled in a wbuf, or an error if it did not fit:
 * a truncated JSON document is worse than no document. */
static void send_built(struct agent_state *a, const char *id, struct wbuf *w)
{
    if (w->truncated) {
        agent_warn("Response to %s exceeded %zu bytes; not sent", id, w->cap);
        send_error(a, id, "response too large");
        return;
    }
    agent_send_response(a, w->p);
}

static void begin_ok(struct wbuf *w, const char *id)
{
    char esc[160];
    json_escape(esc, sizeof(esc), id);
    wbuf_addf(w, "{\"id\":\"%s\",\"ok\":true", esc);
}

static void append_event_array(struct wbuf *w, char *const *events, int count)
{
    for (int i = 0; i < count; i++) {
        char esc[128];
        json_escape(esc, sizeof(esc), events[i]);
        wbuf_addf(w, "%s\"%s\"", i > 0 ? "," : "", esc);
    }
}

/* The effective record-event list (selection, or all probed) as quoted
 * JSON array items. */
static void append_effective_events(struct wbuf *w, const struct agent_state *a)
{
    if (a->sel_events[0]) {
        char tmp[512];
        snprintf(tmp, sizeof(tmp), "%s", a->sel_events);
        char *save = NULL;
        int first = 1;
        for (char *tok = strtok_r(tmp, ",", &save); tok;
             tok = strtok_r(NULL, ",", &save)) {
            char esc[128];
            json_escape(esc, sizeof(esc), tok);
            wbuf_addf(w, "%s\"%s\"", first ? "" : ",", esc);
            first = 0;
        }
    } else if (a->caps) {
        append_event_array(w, a->caps->record_events, a->caps->record_event_count);
    }
}

static void append_capabilities(struct wbuf *w, const struct capabilities *caps)
{
    wbuf_add(w, "\"record_events\":[");
    append_event_array(w, caps->record_events, caps->record_event_count);
    wbuf_add(w, "],\"stat_only_events\":[");
    append_event_array(w, caps->stat_only_events, caps->stat_only_event_count);
    wbuf_addf(w, "],\"callgraph_method\":\"%s\",\"pipe_mode\":%s",
              caps->callgraph, caps->pipe_mode ? "true" : "false");
}

static int max_sample_rate(void)
{
    long v = read_int_file("/proc/sys/kernel/perf_event_max_sample_rate");
    return (v > 0 && v <= 1000000) ? (int)v : DEFAULT_MAX_FREQ;
}

static int agent_busy(struct agent_state *a)
{
    pthread_mutex_lock(&a->state_lock);
    int busy = a->state == AGENT_PROFILING || a->state == AGENT_PAUSED;
    pthread_mutex_unlock(&a->state_lock);
    return busy;
}

/* --------------------------------------------------------------------------
 * Command handlers
 * -------------------------------------------------------------------------- */

static void cmd_ping(struct agent_state *a, const char *cmd_id,
                     const char *args, const char *aend)
{
    send_ok(a, cmd_id);
}

static void cmd_status(struct agent_state *a, const char *cmd_id,
                       const char *args, const char *aend)
{
    const char *state_str;
    int st, pid, freq, dur;

    pthread_mutex_lock(&a->state_lock);
    st = a->state;
    pid = a->pid;
    freq = a->frequency;
    dur = a->duration;
    pthread_mutex_unlock(&a->state_lock);

    switch (st) {
    case AGENT_PROFILING: state_str = "profiling"; break;
    case AGENT_PAUSED:    state_str = "paused";    break;
    default:              state_str = "idle";       break;
    }

    char esc_pv[256], esc_path[PERF_PATH_MAX * 2];
    char esc_arch[256], esc_kernel[256];
    json_escape(esc_pv, sizeof(esc_pv), a->platform.perf_version);
    json_escape(esc_path, sizeof(esc_path), g_perf);
    json_escape(esc_arch, sizeof(esc_arch), a->platform.arch);
    json_escape(esc_kernel, sizeof(esc_kernel), a->platform.kernel);

    char storage[8192 + PERF_PATH_MAX * 2];
    struct wbuf w;
    wbuf_init(&w, storage, sizeof(storage));
    begin_ok(&w, cmd_id);
    wbuf_addf(&w,
        ",\"state\":\"%s\",\"pid\":%d,\"frequency\":%d,\"duration\":%d,"
        "\"agent_version\":\"" AGENT_VERSION "\","
        "\"platform\":{\"arch\":\"%s\",\"kernel\":\"%s\","
        "\"perf_version\":\"%s\",\"perf_path\":\"%s\","
        "\"perf_event_paranoid\":%d}",
        state_str, pid, freq, dur, esc_arch, esc_kernel,
        esc_pv, esc_path, a->platform.perf_event_paranoid);

    if (a->caps) {
        wbuf_add(&w, ",\"capabilities\":{");
        append_capabilities(&w, a->caps);
        wbuf_add(&w, "},\"events\":[");
        append_effective_events(&w, a);
        wbuf_add(&w, "]");
    }
    wbuf_add(&w, "}");
    send_built(a, cmd_id, &w);
}

static void cmd_list_processes(struct agent_state *a, const char *cmd_id,
                               const char *args, const char *aend)
{
    struct proc_entry *procs = malloc(sizeof(struct proc_entry) * MAX_PROC_RESULT);
    char *storage = malloc(JSON_BUF_SIZE);
    if (!procs || !storage) {
        free(procs);
        free(storage);
        send_error(a, cmd_id, "out of memory");
        return;
    }

    int count = do_list_processes(procs, MAX_PROC_RESULT);

    struct wbuf w;
    wbuf_init(&w, storage, JSON_BUF_SIZE);
    begin_ok(&w, cmd_id);
    wbuf_add(&w, ",\"processes\":[");

    /* Reserve enough for a worst-case entry (escaped comm + cmdline
     * + format) so a full buffer stops cleanly instead of truncating
     * mid-entry into invalid JSON. */
    for (int i = 0; i < count && w.len + 1024 < w.cap; i++) {
        char esc_comm[128], esc_cmdline[512];
        json_escape(esc_comm, sizeof(esc_comm), procs[i].comm);
        json_escape(esc_cmdline, sizeof(esc_cmdline), procs[i].cmdline);
        wbuf_addf(&w, "%s{\"pid\":%d,\"comm\":\"%s\",\"cmdline\":\"%s\",\"cpu\":%.1f}",
                  i > 0 ? "," : "", procs[i].pid, esc_comm, esc_cmdline,
                  procs[i].cpu);
    }
    wbuf_add(&w, "]}");
    send_built(a, cmd_id, &w);

    free(storage);
    free(procs);
}

static void cmd_verify_pid(struct agent_state *a, const char *cmd_id,
                           const char *args, const char *aend)
{
    int pid = -1;
    if (args) json_get_int_n(args, aend, "pid", &pid);

    if (pid < 0) {
        send_error(a, cmd_id, "pid required");
        return;
    }

    int exists = process_exists(pid);
    char comm[64] = "", cmdline[256] = "";

    if (exists) {
        char path[64];
        FILE *f;

        snprintf(path, sizeof(path), "/proc/%d/comm", pid);
        f = fopen(path, "r");
        if (f) {
            if (fgets(comm, sizeof(comm), f)) {
                char *nl = strchr(comm, '\n');
                if (nl) *nl = '\0';
            }
            fclose(f);
        }

        snprintf(path, sizeof(path), "/proc/%d/cmdline", pid);
        f = fopen(path, "r");
        if (f) {
            size_t n = fread(cmdline, 1, sizeof(cmdline) - 1, f);
            fclose(f);
            cmdline[n] = '\0';
            for (size_t j = 0; j < n; j++) {
                if (cmdline[j] == '\0') cmdline[j] = ' ';
            }
        }
    }

    char esc_comm[128], esc_cmdline[512];
    json_escape(esc_comm, sizeof(esc_comm), comm);
    json_escape(esc_cmdline, sizeof(esc_cmdline), cmdline);

    char storage[1024];
    struct wbuf w;
    wbuf_init(&w, storage, sizeof(storage));
    begin_ok(&w, cmd_id);
    wbuf_addf(&w, ",\"exists\":%s,\"pid\":%d,"
              "\"info\":{\"comm\":\"%s\",\"cmdline\":\"%s\"}}",
              exists ? "true" : "false", pid, esc_comm, esc_cmdline);
    send_built(a, cmd_id, &w);
}

static void cmd_verify_perf(struct agent_state *a, const char *cmd_id,
                            const char *args, const char *aend)
{
    /* args.perf points the agent at a perf outside PATH. Adopting it changes
     * the binary every later probe and collection runs, and the probed
     * capabilities belong to the old one, so it is refused mid-collection
     * exactly as reprobe is. A candidate that fails validation leaves the
     * current perf in place and says why. */
    char want[PERF_PATH_MAX + 1] = "";
    char adopt_err[PERF_PATH_MAX + 128] = "";
    if (args && json_get_str_n(args, aend, "perf", want, sizeof(want)) == 0 &&
        want[0] && strcmp(want, g_perf) != 0) {
        if (agent_busy(a)) {
            send_error(a, cmd_id,
                       "cannot change perf while profiling — stop first");
            return;
        }
        if (perf_use(want, adopt_err, sizeof(adopt_err)) == 0) {
            agent_log("Using perf: %s", g_perf);
            detect_platform(&a->platform);
            if (a->caps) {
                free_capabilities(a->caps);
                free(a->caps);
                a->caps = NULL;
            }
        }
    }

    char esc_path[PERF_PATH_MAX * 2];
    json_escape(esc_path, sizeof(esc_path), g_perf);

    char storage[4096 + PERF_PATH_MAX * 3];
    struct wbuf w;
    wbuf_init(&w, storage, sizeof(storage));
    begin_ok(&w, cmd_id);

    if (adopt_err[0]) {
        char esc_adopt[sizeof(adopt_err) * 2];
        json_escape(esc_adopt, sizeof(esc_adopt), adopt_err);
        wbuf_addf(&w, ",\"available\":false,\"path\":\"%s\",\"error\":\"%s\"}",
                  esc_path, esc_adopt);
        send_built(a, cmd_id, &w);
        return;
    }

    char *argv[] = { g_perf, "--version", NULL };
    struct buf out;
    buf_init(&out);
    int rc = run_cmd(argv, &out, NULL, 5);

    char version[256] = "";
    if (rc == 0 && out.len > 0) {
        size_t cplen = out.len < sizeof(version) - 1
                     ? out.len : sizeof(version) - 1;
        memcpy(version, out.data, cplen);
        version[cplen] = '\0';
        char *nl = strchr(version, '\n');
        if (nl) *nl = '\0';
    }
    buf_free(&out);

    if (!version[0]) {
        wbuf_addf(&w, ",\"available\":false,\"path\":\"%s\","
                  "\"error\":\"perf not found or not working\"}", esc_path);
        send_built(a, cmd_id, &w);
        return;
    }

    /* Quick functional check against self. Counts the first event this
     * target is known to record, or cpu-clock, which exists without a PMU:
     * `-e cycles` exited 0 with `<not supported>` on a PMU-less target and
     * reported the perf functional when it was not. */
    const char *ev = (a->caps && a->caps->record_event_count > 0)
                   ? a->caps->record_events[0] : "cpu-clock";
    char pid_str[16];
    snprintf(pid_str, sizeof(pid_str), "%d", (int)getpid());
    char *argv_check[MAX_CMD_ARGS];
    build_stat_argv(argv_check, MAX_CMD_ARGS, ev, pid_str, "0");
    struct buf errbuf;
    buf_init(&errbuf);
    int functional = (run_cmd(argv_check, NULL, &errbuf, 10) == 0);
    if (functional && errbuf.len > 0 &&
        (str_contains_lower(errbuf.data, errbuf.len, "not supported") ||
         str_contains_lower(errbuf.data, errbuf.len, "not counted")))
        functional = 0;

    char err_msg[256] = "";
    if (!functional && errbuf.len > 0) {
        size_t cplen = errbuf.len < sizeof(err_msg) - 1
                     ? errbuf.len : sizeof(err_msg) - 1;
        memcpy(err_msg, errbuf.data, cplen);
        err_msg[cplen] = '\0';
    }
    buf_free(&errbuf);

    char esc_version[512], esc_err[512];
    json_escape(esc_version, sizeof(esc_version), version);
    json_escape(esc_err, sizeof(esc_err), err_msg);

    wbuf_addf(&w, ",\"available\":true,\"path\":\"%s\",\"version\":\"%s\","
              "\"functional\":%s,", esc_path, esc_version,
              functional ? "true" : "false");
    if (err_msg[0])
        wbuf_addf(&w, "\"error\":\"%s\",", esc_err);
    else
        wbuf_add(&w, "\"error\":null,");
    wbuf_addf(&w, "\"perf_event_paranoid\":%d}", a->platform.perf_event_paranoid);
    send_built(a, cmd_id, &w);
}

static void cmd_reprobe(struct agent_state *a, const char *cmd_id,
                        const char *args, const char *aend)
{
    /* The collection thread reads a->caps while running — re-probing now
     * would free it out from under it. */
    if (agent_busy(a)) {
        send_error(a, cmd_id, "cannot reprobe while profiling — stop first");
        return;
    }

    int pid = a->pid;
    if (args) json_get_int_n(args, aend, "pid", &pid);

    if (pid < 0) {
        send_error(a, cmd_id, "pid required");
        return;
    }

    if (!process_exists(pid)) {
        send_error(a, cmd_id, "process %d not found", pid);
        return;
    }

    agent_log("Re-probing capabilities for PID %d...", pid);

    if (a->caps) {
        free_capabilities(a->caps);
        free(a->caps);
        a->caps = NULL;
    }

    struct capabilities *caps = malloc(sizeof(*caps));
    if (!caps) {
        send_error(a, cmd_id, "out of memory");
        return;
    }

    probe_capabilities(pid, caps);

    pthread_mutex_lock(&a->state_lock);
    a->caps = caps;
    a->pid = pid;
    a->pid_start = process_start_time(pid);
    pthread_mutex_unlock(&a->state_lock);

    char storage[4096];
    struct wbuf w;
    wbuf_init(&w, storage, sizeof(storage));
    begin_ok(&w, cmd_id);
    wbuf_add(&w, ",");
    append_capabilities(&w, caps);
    wbuf_add(&w, "}");
    send_built(a, cmd_id, &w);
}

static void cmd_start(struct agent_state *a, const char *cmd_id,
                      const char *args, const char *aend)
{
    pthread_mutex_lock(&a->state_lock);
    if (a->state == AGENT_PROFILING || a->state == AGENT_PAUSED) {
        int paused = (a->state == AGENT_PAUSED);
        pthread_mutex_unlock(&a->state_lock);
        send_error(a, cmd_id, "%s",
                   paused ? "already profiling (paused — use resume or stop)"
                          : "already profiling");
        return;
    }
    pthread_mutex_unlock(&a->state_lock);

    /* A previous collection thread may have ended on its own (e.g. target
     * process exited set state back to IDLE) without anyone joining it. */
    if (a->collect_thread_active) {
        a->collect_stop = 1;
        pthread_join(a->collect_thread, NULL);
        a->collect_thread_active = 0;
    }

    int pid = a->pid;
    int freq = a->frequency;
    int dur = a->duration;

    if (args) {
        json_get_int_n(args, aend, "pid", &pid);
        json_get_int_n(args, aend, "frequency", &freq);
        json_get_int_n(args, aend, "duration", &dur);
    }

    if (pid < 0) {
        send_error(a, cmd_id, "pid required");
        return;
    }

    /* Validate like configure does. duration 0 spun rounds mode through
     * record and script back to back; a frequency past the kernel's
     * perf_event_max_sample_rate makes perf fail or throttle every round. */
    int max_freq = max_sample_rate();
    if (freq < 1 || freq > max_freq) {
        send_error(a, cmd_id, "frequency must be between 1 and %d Hz "
                   "(perf_event_max_sample_rate)", max_freq);
        return;
    }
    if (dur < 1 || dur > MAX_DURATION) {
        send_error(a, cmd_id, "duration must be between 1 and %d seconds",
                   MAX_DURATION);
        return;
    }

    if (!process_exists(pid)) {
        send_error(a, cmd_id, "process %d not found", pid);
        return;
    }

    /* Probe capabilities if needed (deferred — no PID at startup) */
    if (!a->caps || a->pid != pid) {
        if (a->caps) {
            free_capabilities(a->caps);
            free(a->caps);
            a->caps = NULL;
        }
        struct capabilities *caps = malloc(sizeof(*caps));
        if (!caps) {
            send_error(a, cmd_id, "out of memory");
            return;
        }
        probe_capabilities(pid, caps);
        a->caps = caps;
    }

    pthread_mutex_lock(&a->state_lock);
    a->pid = pid;
    a->pid_start = process_start_time(pid);
    a->frequency = freq;
    a->duration = dur;
    pthread_mutex_unlock(&a->state_lock);

    if (a->caps->record_event_count == 0) {
        send_error(a, cmd_id, "no perf record events available for PID %d", pid);
        return;
    }

    /* Optional args.events: record only this subset of the probed
     * events. Unknown names are dropped; absent/empty means all. */
    a->sel_events[0] = '\0';
    const char *arr = args ? json_find_array_n(args, aend, "events") : NULL;
    if (arr) {
        const char *arr_end = json_object_end(arr);
        if (!arr_end) arr_end = aend;
        const char *p = arr + 1;
        while (p < arr_end && *p && *p != ']') {
            if (*p != '"') { p++; continue; }
            char name[64];
            size_t ni = 0;
            p++;
            while (p < arr_end && *p && *p != '"' && ni + 1 < sizeof(name)) {
                if (*p == '\\' && p + 1 < arr_end) p++;
                name[ni++] = *p++;
            }
            name[ni] = '\0';
            while (p < arr_end && *p && *p != '"') p++;
            if (p < arr_end && *p == '"') p++;
            for (int i = 0; i < a->caps->record_event_count; i++) {
                if (strcmp(name, a->caps->record_events[i]) == 0) {
                    if (a->sel_events[0])
                        strncat(a->sel_events, ",",
                                sizeof(a->sel_events) - strlen(a->sel_events) - 1);
                    strncat(a->sel_events, name,
                            sizeof(a->sel_events) - strlen(a->sel_events) - 1);
                    break;
                }
            }
        }
        if (a->sel_events[0])
            agent_log("Recording selected events: %s", a->sel_events);
        else
            agent_log("No valid events in selection — recording all probed");
    }

    /* Start collection thread */
    a->collect_stop = 0;

    pthread_mutex_lock(&a->state_lock);
    a->state = AGENT_PROFILING;
    pthread_mutex_unlock(&a->state_lock);

    if (pthread_create(&a->collect_thread, NULL, collection_thread_fn, a) == 0) {
        a->collect_thread_active = 1;
    } else {
        agent_warn("Failed to create collection thread");
        pthread_mutex_lock(&a->state_lock);
        a->state = AGENT_IDLE;
        pthread_mutex_unlock(&a->state_lock);
        send_error(a, cmd_id, "thread creation failed");
        return;
    }

    /* Build success response */
    char storage[4096];
    struct wbuf w;
    wbuf_init(&w, storage, sizeof(storage));
    begin_ok(&w, cmd_id);
    wbuf_addf(&w, ",\"pid\":%d,\"frequency\":%d,\"duration\":%d,\"events\":[",
              pid, freq, dur);
    append_effective_events(&w, a);
    wbuf_addf(&w, "],\"callgraph\":\"%s\",\"mode\":\"%s\"}",
              a->caps->callgraph,
              a->caps->pipe_mode ? "continuous" : "rounds");
    send_built(a, cmd_id, &w);
}

static void cmd_stop(struct agent_state *a, const char *cmd_id,
                     const char *args, const char *aend)
{
    if (!agent_busy(a)) {
        send_error(a, cmd_id, "not profiling");
        return;
    }

    a->collect_stop = 1;

    /* Kill active perf subprocesses for immediate stop */
    kill_tracked_children();

    if (a->collect_thread_active) {
        pthread_join(a->collect_thread, NULL);
        a->collect_thread_active = 0;
    }

    pthread_mutex_lock(&a->state_lock);
    a->state = AGENT_IDLE;
    pthread_mutex_unlock(&a->state_lock);

    send_ok(a, cmd_id);
}

static void cmd_pause(struct agent_state *a, const char *cmd_id,
                      const char *args, const char *aend)
{
    pthread_mutex_lock(&a->state_lock);
    if (a->state != AGENT_PROFILING) {
        pthread_mutex_unlock(&a->state_lock);
        send_error(a, cmd_id, "not profiling");
        return;
    }
    a->state = AGENT_PAUSED;
    pthread_mutex_unlock(&a->state_lock);

    /* Kill active perf subprocesses to stop collecting immediately */
    kill_tracked_children();

    send_ok(a, cmd_id);
}

static void cmd_resume(struct agent_state *a, const char *cmd_id,
                       const char *args, const char *aend)
{
    pthread_mutex_lock(&a->state_lock);
    if (a->state != AGENT_PAUSED) {
        pthread_mutex_unlock(&a->state_lock);
        send_error(a, cmd_id, "not paused");
        return;
    }
    a->state = AGENT_PROFILING;
    pthread_mutex_unlock(&a->state_lock);

    send_ok(a, cmd_id);
}

static void cmd_configure(struct agent_state *a, const char *cmd_id,
                          const char *args, const char *aend)
{
    int freq = -1, dur = -1;

    if (args) {
        json_get_int_n(args, aend, "frequency", &freq);
        json_get_int_n(args, aend, "duration", &dur);
    }

    int max_freq = max_sample_rate();
    if (freq != -1 && (freq < 1 || freq > max_freq)) {
        send_error(a, cmd_id, "frequency must be between 1 and %d Hz "
                   "(perf_event_max_sample_rate)", max_freq);
        return;
    }
    if (dur != -1 && (dur < 1 || dur > MAX_DURATION)) {
        send_error(a, cmd_id, "duration must be between 1 and %d seconds",
                   MAX_DURATION);
        return;
    }

    pthread_mutex_lock(&a->state_lock);
    if (freq > 0) a->frequency = freq;
    if (dur > 0) a->duration = dur;
    freq = a->frequency;
    dur = a->duration;
    pthread_mutex_unlock(&a->state_lock);

    char storage[256];
    struct wbuf w;
    wbuf_init(&w, storage, sizeof(storage));
    begin_ok(&w, cmd_id);
    wbuf_addf(&w, ",\"frequency\":%d,\"duration\":%d}", freq, dur);
    send_built(a, cmd_id, &w);
}

static void cmd_configure_metrics(struct agent_state *a, const char *cmd_id,
                                  const char *args, const char *aend)
{
    int val;

    pthread_mutex_lock(&a->state_lock);
    if (args) {
        if (json_get_bool_n(args, aend, "enabled", &val) == 0)
            a->metrics_enabled = val;
        if (json_get_int_n(args, aend, "interval", &val) == 0 &&
            val >= 1 && val <= 3600)
            a->metrics_interval = val;
        if (json_get_bool_n(args, aend, "network", &val) == 0)
            a->metrics_network = val;
        if (json_get_bool_n(args, aend, "disk", &val) == 0)
            a->metrics_disk = val;
        if (json_get_bool_n(args, aend, "threads", &val) == 0)
            a->metrics_threads = val;
    }
    int enabled  = a->metrics_enabled;
    int interval = a->metrics_interval;
    int network  = a->metrics_network;
    int disk     = a->metrics_disk;
    int threads  = a->metrics_threads;
    pthread_mutex_unlock(&a->state_lock);

    char storage[256];
    struct wbuf w;
    wbuf_init(&w, storage, sizeof(storage));
    begin_ok(&w, cmd_id);
    wbuf_addf(&w, ",\"metrics_enabled\":%s,\"interval\":%d,\"network\":%s,"
              "\"disk\":%s,\"threads\":%s}",
              enabled ? "true" : "false", interval,
              network ? "true" : "false", disk ? "true" : "false",
              threads ? "true" : "false");
    send_built(a, cmd_id, &w);
}

/* The pairing handshake. The server presents the code the operator read off
 * this agent's log (or configured with --token); nothing is sent back but a
 * verdict. The code itself never travels agent -> server. */
static void cmd_auth(struct agent_state *a, const char *cmd_id,
                     const char *args, const char *aend)
{
    if (a->authed) {
        char storage[256];
        struct wbuf w;
        wbuf_init(&w, storage, sizeof(storage));
        begin_ok(&w, cmd_id);
        wbuf_add(&w, ",\"already\":true}");
        send_built(a, cmd_id, &w);
        return;
    }

    if (!a->token || !a->token[0]) {
        /* Only reachable if a session started tokenless and the server sent
         * auth anyway; nothing to check against. */
        send_error(a, cmd_id, "agent has no pairing code configured");
        return;
    }

    char presented[256] = "";
    if (args)
        json_get_str_n(args, aend, "token", presented, sizeof(presented));

    if (!agent_consttime_eq(presented, a->token)) {
        a->auth_failures++;
        agent_log("Rejected pairing code (attempt %d of %d)",
                  a->auth_failures, AUTH_MAX_FAILURES);
        if (a->token_is_generated)
            agent_log("  Pairing code: %s", a->token);

        send_error(a, cmd_id, "auth failed");

        /* 128 bits of entropy makes brute force irrelevant; this just stops a
         * peer sitting on the single --listen slot guessing indefinitely. */
        if (a->auth_failures >= AUTH_MAX_FAILURES) {
            agent_log("Too many failed pairing attempts — closing session.");
            a->session_done = 1;
        }
        return;
    }

    a->authed = 1;
    agent_log("Server authenticated.");

    /* Metrics were held back until the peer proved itself. */
    start_metrics_thread(a);

    send_ok(a, cmd_id);
}

static void cmd_update(struct agent_state *a, const char *cmd_id,
                       const char *args, const char *aend)
{
    /* The one command that fetches and executes new code. Refuse it on a
     * session with no pairing code at all: such a session is only reachable
     * in --server mode, and `perflens-agent --update` over ssh (or
     * `perflens push-agent`) covers that case without exposing the primitive
     * to whatever the agent happened to connect to. */
    if (!a->token || !a->token[0]) {
        send_error(a, cmd_id, "update requires a configured pairing code");
        return;
    }

    char msg[512];
    int rc = self_update(msg, sizeof(msg));
    agent_log("Self-update: %s", msg);

    char esc[1024], esc_id[160];
    json_escape(esc, sizeof(esc), msg);
    json_escape(esc_id, sizeof(esc_id), cmd_id);
    char resp[2048];
    snprintf(resp, sizeof(resp),
        "{\"id\":\"%s\",\"ok\":%s,\"message\":\"%s\","
        "\"running_version\":\"%s\"}",
        esc_id, rc == 0 ? "true" : "false", esc, AGENT_VERSION);
    agent_send_response(a, resp);
}

/* --------------------------------------------------------------------------
 * Command dispatch
 * -------------------------------------------------------------------------- */

typedef void (*cmd_handler_fn)(struct agent_state *, const char *,
                               const char *, const char *);

struct cmd_dispatch_entry {
    const char   *name;
    cmd_handler_fn handler;
};

static const struct cmd_dispatch_entry CMD_TABLE[] = {
    { "auth",               cmd_auth },
    { "ping",               cmd_ping },
    { "status",             cmd_status },
    { "list_processes",     cmd_list_processes },
    { "verify_pid",         cmd_verify_pid },
    { "verify_perf",        cmd_verify_perf },
    { "reprobe",            cmd_reprobe },
    { "start",              cmd_start },
    { "stop",               cmd_stop },
    { "pause",              cmd_pause },
    { "resume",             cmd_resume },
    { "configure",          cmd_configure },
    { "configure_metrics",  cmd_configure_metrics },
    { "update",             cmd_update },
    { NULL, NULL },
};

void dispatch_command(struct agent_state *a, const char *json)
{
    char cmd[64] = "", cmd_id[80] = "";
    json_get_str(json, "cmd", cmd, sizeof(cmd));
    json_get_str(json, "id", cmd_id, sizeof(cmd_id));

    /* An id is echoed into every response. One outside the accepted
     * alphabet is answered with no id rather than escaped into something
     * the peer did not send. */
    if (cmd_id[0] && !json_valid_id(cmd_id)) {
        agent_warn("Command id rejected (only [A-Za-z0-9_.:-], up to 63 chars)");
        cmd_id[0] = '\0';
    }

    if (!cmd[0]) {
        agent_log("Received command with no 'cmd' field");
        return;
    }

    /* The authentication gate.
     *
     * Placed here rather than in the receiver thread so it covers every entry
     * in CMD_TABLE, and every future one, by construction. Answering instead
     * of dropping is deliberate: silence is indistinguishable from a stalled
     * link, and this is exactly the case an operator running an older server
     * needs to be able to diagnose. */
    if (!a->authed && strcmp(cmd, "auth") != 0) {
        send_error(a, cmd_id, "unauthenticated");
        return;
    }

    /* Command parameters live in args; lookups are bounded to that object,
     * so a `pid` in a later sibling can never stand in for a missing one. */
    const char *args = json_find_object(json, "args");
    const char *aend = args ? json_object_end(args) : NULL;
    if (args && !aend) args = NULL;    /* unterminated: ignore it */

    for (int i = 0; CMD_TABLE[i].name; i++) {
        if (strcmp(cmd, CMD_TABLE[i].name) == 0) {
            CMD_TABLE[i].handler(a, cmd_id, args, aend);
            return;
        }
    }

    send_error(a, cmd_id, "unknown command: %s", cmd);
}
