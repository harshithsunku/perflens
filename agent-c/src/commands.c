/*
 * PerfLens Device Agent — command handlers + dispatch
 */

#include "agent.h"

static int append_effective_events(char *resp, size_t cap, int n,
                                   const struct agent_state *a);

/* --------------------------------------------------------------------------
 * Command handlers
 * -------------------------------------------------------------------------- */

static void cmd_ping(struct agent_state *a, const char *cmd_id,
                     const char *json)
{
    (void)json;
    char resp[256];
    snprintf(resp, sizeof(resp), "{\"id\":\"%s\",\"ok\":true}", cmd_id);
    agent_send_response(a, resp);
}

static void cmd_status(struct agent_state *a, const char *cmd_id,
                       const char *json)
{
    (void)json;
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

    char esc_pv[256];
    json_escape(esc_pv, sizeof(esc_pv), a->platform.perf_version);

    char resp[4096];
    int n = snprintf(resp, sizeof(resp),
        "{\"id\":\"%s\",\"ok\":true,\"state\":\"%s\",\"pid\":%d,"
        "\"frequency\":%d,\"duration\":%d,"
        "\"agent_version\":\"" AGENT_VERSION "\","
        "\"platform\":{\"arch\":\"%s\",\"kernel\":\"%s\","
        "\"perf_version\":\"%s\",\"perf_event_paranoid\":%d}",
        cmd_id, state_str, pid, freq, dur,
        a->platform.arch, a->platform.kernel,
        esc_pv, a->platform.perf_event_paranoid);

    if (a->caps) {
        n += snprintf(resp + n, sizeof(resp) - (size_t)n,
            ",\"capabilities\":{\"record_events\":[");
        for (int i = 0; i < a->caps->record_event_count; i++) {
            if (i > 0) n += snprintf(resp + n, sizeof(resp) - (size_t)n, ",");
            n += snprintf(resp + n, sizeof(resp) - (size_t)n,
                "\"%s\"", a->caps->record_events[i]);
        }
        n += snprintf(resp + n, sizeof(resp) - (size_t)n,
            "],\"stat_only_events\":[");
        for (int i = 0; i < a->caps->stat_only_event_count; i++) {
            if (i > 0) n += snprintf(resp + n, sizeof(resp) - (size_t)n, ",");
            n += snprintf(resp + n, sizeof(resp) - (size_t)n,
                "\"%s\"", a->caps->stat_only_events[i]);
        }
        n += snprintf(resp + n, sizeof(resp) - (size_t)n,
            "],\"callgraph_method\":\"%s\",\"pipe_mode\":%s},"
            "\"events\":[",
            a->caps->callgraph, a->caps->pipe_mode ? "true" : "false");
        n = append_effective_events(resp, sizeof(resp), n, a);
        n += snprintf(resp + n, sizeof(resp) - (size_t)n, "]");
    }

    snprintf(resp + n, sizeof(resp) - (size_t)n, "}");
    agent_send_response(a, resp);
}

static void cmd_list_processes(struct agent_state *a, const char *cmd_id,
                               const char *json)
{
    (void)json;
    struct proc_entry *procs = malloc(sizeof(struct proc_entry) * MAX_PROC_RESULT);
    if (!procs) {
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,\"error\":\"out of memory\"}", cmd_id);
        agent_send_response(a, resp);
        return;
    }

    int count = do_list_processes(procs, MAX_PROC_RESULT);

    char *resp = malloc(JSON_BUF_SIZE);
    if (!resp) { free(procs); return; }

    int n = snprintf(resp, JSON_BUF_SIZE,
        "{\"id\":\"%s\",\"ok\":true,\"processes\":[", cmd_id);

    /* Reserve enough for a worst-case entry (escaped comm + cmdline
     * + format) so a full buffer stops cleanly instead of truncating
     * mid-entry into invalid JSON. */
    for (int i = 0; i < count && (size_t)n + 768 < JSON_BUF_SIZE; i++) {
        char esc_comm[128], esc_cmdline[512];
        json_escape(esc_comm, sizeof(esc_comm), procs[i].comm);
        json_escape(esc_cmdline, sizeof(esc_cmdline), procs[i].cmdline);

        if (i > 0) resp[n++] = ',';
        n += snprintf(resp + n, JSON_BUF_SIZE - (size_t)n,
            "{\"pid\":%d,\"comm\":\"%s\",\"cmdline\":\"%s\",\"cpu\":%.1f}",
            procs[i].pid, esc_comm, esc_cmdline, procs[i].cpu);
    }

    snprintf(resp + n, JSON_BUF_SIZE - (size_t)n, "]}");
    agent_send_response(a, resp);

    free(resp);
    free(procs);
}

static void cmd_verify_pid(struct agent_state *a, const char *cmd_id,
                           const char *json)
{
    const char *args = json_find_object(json, "args");
    int pid = -1;
    if (args) json_get_int(args, "pid", &pid);

    if (pid < 0) {
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,\"error\":\"pid required\"}", cmd_id);
        agent_send_response(a, resp);
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

    char resp[1024];
    snprintf(resp, sizeof(resp),
        "{\"id\":\"%s\",\"ok\":true,\"exists\":%s,\"pid\":%d,"
        "\"info\":{\"comm\":\"%s\",\"cmdline\":\"%s\"}}",
        cmd_id, exists ? "true" : "false", pid, esc_comm, esc_cmdline);
    agent_send_response(a, resp);
}

static void cmd_verify_perf(struct agent_state *a, const char *cmd_id,
                            const char *json)
{
    (void)json;

    char *argv[] = { PERF, "--version", NULL };
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
        char resp[512];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":true,\"available\":false,"
            "\"error\":\"perf not found or not working\"}", cmd_id);
        agent_send_response(a, resp);
        return;
    }

    /* Quick functional check against self */
    int self_pid = (int)getpid();
    char pid_str[16];
    snprintf(pid_str, sizeof(pid_str), "%d", self_pid);
    char *argv_check[] = { PERF, "stat", "-e", "cycles", "-p", pid_str,
                           "--", "sleep", "0", NULL };
    struct buf errbuf;
    buf_init(&errbuf);
    int functional = (run_cmd(argv_check, NULL, &errbuf, 10) == 0);

    char err_msg[256] = "";
    if (!functional && errbuf.len > 0) {
        size_t cplen = errbuf.len < sizeof(err_msg) - 1
                     ? errbuf.len : sizeof(err_msg) - 1;
        memcpy(err_msg, errbuf.data, cplen);
        err_msg[cplen] = '\0';
    }
    buf_free(&errbuf);

    char esc_version[256], esc_err[512];
    json_escape(esc_version, sizeof(esc_version), version);
    json_escape(esc_err, sizeof(esc_err), err_msg);

    char resp[1024];
    if (err_msg[0]) {
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":true,\"available\":true,"
            "\"version\":\"%s\",\"functional\":%s,"
            "\"error\":\"%s\",\"perf_event_paranoid\":%d}",
            cmd_id, esc_version,
            functional ? "true" : "false",
            esc_err, a->platform.perf_event_paranoid);
    } else {
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":true,\"available\":true,"
            "\"version\":\"%s\",\"functional\":%s,"
            "\"error\":null,\"perf_event_paranoid\":%d}",
            cmd_id, esc_version,
            functional ? "true" : "false",
            a->platform.perf_event_paranoid);
    }
    agent_send_response(a, resp);
}

static void cmd_reprobe(struct agent_state *a, const char *cmd_id,
                        const char *json)
{
    /* The collection thread reads a->caps while running — re-probing now
     * would free it out from under it. */
    pthread_mutex_lock(&a->state_lock);
    if (a->state == AGENT_PROFILING || a->state == AGENT_PAUSED) {
        pthread_mutex_unlock(&a->state_lock);
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,"
            "\"error\":\"cannot reprobe while profiling — stop first\"}",
            cmd_id);
        agent_send_response(a, resp);
        return;
    }
    pthread_mutex_unlock(&a->state_lock);

    const char *args = json_find_object(json, "args");
    int pid = a->pid;
    if (args) json_get_int(args, "pid", &pid);

    if (pid < 0) {
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,\"error\":\"pid required\"}", cmd_id);
        agent_send_response(a, resp);
        return;
    }

    if (!process_exists(pid)) {
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,\"error\":\"process %d not found\"}",
            cmd_id, pid);
        agent_send_response(a, resp);
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
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,\"error\":\"out of memory\"}", cmd_id);
        agent_send_response(a, resp);
        return;
    }

    probe_capabilities(pid, caps);

    pthread_mutex_lock(&a->state_lock);
    a->caps = caps;
    a->pid = pid;
    pthread_mutex_unlock(&a->state_lock);

    char resp[2048];
    int n = snprintf(resp, sizeof(resp),
        "{\"id\":\"%s\",\"ok\":true,\"record_events\":[", cmd_id);
    for (int i = 0; i < caps->record_event_count; i++) {
        if (i > 0) n += snprintf(resp + n, sizeof(resp) - (size_t)n, ",");
        n += snprintf(resp + n, sizeof(resp) - (size_t)n,
            "\"%s\"", caps->record_events[i]);
    }
    n += snprintf(resp + n, sizeof(resp) - (size_t)n,
        "],\"stat_only_events\":[");
    for (int i = 0; i < caps->stat_only_event_count; i++) {
        if (i > 0) n += snprintf(resp + n, sizeof(resp) - (size_t)n, ",");
        n += snprintf(resp + n, sizeof(resp) - (size_t)n,
            "\"%s\"", caps->stat_only_events[i]);
    }
    n += snprintf(resp + n, sizeof(resp) - (size_t)n,
        "],\"callgraph_method\":\"%s\",\"pipe_mode\":%s}",
        caps->callgraph, caps->pipe_mode ? "true" : "false");
    agent_send_response(a, resp);
}

/* Append the effective record-event list (selection, or all probed) as
 * quoted JSON array items. Returns the new offset. */
static int append_effective_events(char *resp, size_t cap, int n,
                                   const struct agent_state *a)
{
    if (a->sel_events[0]) {
        char tmp[512];
        snprintf(tmp, sizeof(tmp), "%s", a->sel_events);
        char *save = NULL;
        int first = 1;
        for (char *tok = strtok_r(tmp, ",", &save); tok;
             tok = strtok_r(NULL, ",", &save)) {
            n += snprintf(resp + n, cap - (size_t)n, "%s\"%s\"",
                          first ? "" : ",", tok);
            first = 0;
        }
    } else if (a->caps) {
        for (int i = 0; i < a->caps->record_event_count; i++)
            n += snprintf(resp + n, cap - (size_t)n, "%s\"%s\"",
                          i > 0 ? "," : "", a->caps->record_events[i]);
    }
    return n;
}

static void cmd_start(struct agent_state *a, const char *cmd_id,
                      const char *json)
{
    pthread_mutex_lock(&a->state_lock);
    if (a->state == AGENT_PROFILING || a->state == AGENT_PAUSED) {
        int paused = (a->state == AGENT_PAUSED);
        pthread_mutex_unlock(&a->state_lock);
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,\"error\":\"%s\"}",
            cmd_id,
            paused ? "already profiling (paused — use resume or stop)"
                   : "already profiling");
        agent_send_response(a, resp);
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

    const char *args = json_find_object(json, "args");
    int pid = a->pid;
    int freq = a->frequency;
    int dur = a->duration;

    if (args) {
        json_get_int(args, "pid", &pid);
        json_get_int(args, "frequency", &freq);
        json_get_int(args, "duration", &dur);
    }

    if (pid < 0) {
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,\"error\":\"pid required\"}", cmd_id);
        agent_send_response(a, resp);
        return;
    }

    if (!process_exists(pid)) {
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,\"error\":\"process %d not found\"}",
            cmd_id, pid);
        agent_send_response(a, resp);
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
            char resp[256];
            snprintf(resp, sizeof(resp),
                "{\"id\":\"%s\",\"ok\":false,\"error\":\"out of memory\"}",
                cmd_id);
            agent_send_response(a, resp);
            return;
        }
        probe_capabilities(pid, caps);
        a->caps = caps;
    }

    pthread_mutex_lock(&a->state_lock);
    a->pid = pid;
    a->frequency = freq;
    a->duration = dur;
    pthread_mutex_unlock(&a->state_lock);

    if (a->caps->record_event_count == 0) {
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,"
            "\"error\":\"no perf record events available for PID %d\"}",
            cmd_id, pid);
        agent_send_response(a, resp);
        return;
    }

    /* Optional args.events: record only this subset of the probed
     * events. Unknown names are dropped; absent/empty means all. */
    a->sel_events[0] = '\0';
    const char *arr = args ? json_find_array(args, "events") : NULL;
    if (arr) {
        const char *p = arr + 1;
        while (*p && *p != ']') {
            if (*p != '"') { p++; continue; }
            char name[64];
            size_t ni = 0;
            p++;
            while (*p && *p != '"' && ni + 1 < sizeof(name))
                name[ni++] = *p++;
            name[ni] = '\0';
            if (*p == '"') p++;
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
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,"
            "\"error\":\"thread creation failed\"}", cmd_id);
        agent_send_response(a, resp);
        return;
    }

    /* Build success response */
    char resp[2048];
    int n = snprintf(resp, sizeof(resp),
        "{\"id\":\"%s\",\"ok\":true,\"pid\":%d,"
        "\"frequency\":%d,\"duration\":%d,\"events\":[",
        cmd_id, pid, freq, dur);
    n = append_effective_events(resp, sizeof(resp), n, a);
    snprintf(resp + n, sizeof(resp) - (size_t)n,
        "],\"callgraph\":\"%s\",\"mode\":\"%s\"}",
        a->caps->callgraph,
        a->caps->pipe_mode ? "continuous" : "rounds");
    agent_send_response(a, resp);
}

static void cmd_stop(struct agent_state *a, const char *cmd_id,
                     const char *json)
{
    (void)json;
    pthread_mutex_lock(&a->state_lock);
    if (a->state != AGENT_PROFILING && a->state != AGENT_PAUSED) {
        pthread_mutex_unlock(&a->state_lock);
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,\"error\":\"not profiling\"}", cmd_id);
        agent_send_response(a, resp);
        return;
    }
    pthread_mutex_unlock(&a->state_lock);

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

    char resp[256];
    snprintf(resp, sizeof(resp), "{\"id\":\"%s\",\"ok\":true}", cmd_id);
    agent_send_response(a, resp);
}

static void cmd_pause(struct agent_state *a, const char *cmd_id,
                      const char *json)
{
    (void)json;
    pthread_mutex_lock(&a->state_lock);
    if (a->state != AGENT_PROFILING) {
        pthread_mutex_unlock(&a->state_lock);
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,\"error\":\"not profiling\"}", cmd_id);
        agent_send_response(a, resp);
        return;
    }
    a->state = AGENT_PAUSED;
    pthread_mutex_unlock(&a->state_lock);

    /* Kill active perf subprocesses to stop collecting immediately */
    kill_tracked_children();

    char resp[256];
    snprintf(resp, sizeof(resp), "{\"id\":\"%s\",\"ok\":true}", cmd_id);
    agent_send_response(a, resp);
}

static void cmd_resume(struct agent_state *a, const char *cmd_id,
                       const char *json)
{
    (void)json;
    pthread_mutex_lock(&a->state_lock);
    if (a->state != AGENT_PAUSED) {
        pthread_mutex_unlock(&a->state_lock);
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,\"error\":\"not paused\"}", cmd_id);
        agent_send_response(a, resp);
        return;
    }
    a->state = AGENT_PROFILING;
    pthread_mutex_unlock(&a->state_lock);

    char resp[256];
    snprintf(resp, sizeof(resp), "{\"id\":\"%s\",\"ok\":true}", cmd_id);
    agent_send_response(a, resp);
}

static void cmd_configure(struct agent_state *a, const char *cmd_id,
                          const char *json)
{
    const char *args = json_find_object(json, "args");
    int freq = -1, dur = -1;

    if (args) {
        json_get_int(args, "frequency", &freq);
        json_get_int(args, "duration", &dur);
    }

    pthread_mutex_lock(&a->state_lock);
    if (freq > 0) a->frequency = freq;
    if (dur > 0) a->duration = dur;
    freq = a->frequency;
    dur = a->duration;
    pthread_mutex_unlock(&a->state_lock);

    char resp[256];
    snprintf(resp, sizeof(resp),
        "{\"id\":\"%s\",\"ok\":true,\"frequency\":%d,\"duration\":%d}",
        cmd_id, freq, dur);
    agent_send_response(a, resp);
}

static void cmd_configure_metrics(struct agent_state *a, const char *cmd_id,
                                  const char *json)
{
    const char *args = json_find_object(json, "args");
    int val;

    pthread_mutex_lock(&a->state_lock);
    if (args) {
        if (json_get_bool(args, "enabled", &val) == 0)
            a->metrics_enabled = val;
        if (json_get_int(args, "interval", &val) == 0 && val > 0)
            a->metrics_interval = val;
        if (json_get_bool(args, "network", &val) == 0)
            a->metrics_network = val;
        if (json_get_bool(args, "disk", &val) == 0)
            a->metrics_disk = val;
        if (json_get_bool(args, "threads", &val) == 0)
            a->metrics_threads = val;
    }
    int enabled  = a->metrics_enabled;
    int interval = a->metrics_interval;
    int network  = a->metrics_network;
    int disk     = a->metrics_disk;
    int threads  = a->metrics_threads;
    pthread_mutex_unlock(&a->state_lock);

    char resp[256];
    snprintf(resp, sizeof(resp),
        "{\"id\":\"%s\",\"ok\":true,\"metrics_enabled\":%s,"
        "\"interval\":%d,\"network\":%s,\"disk\":%s,\"threads\":%s}",
        cmd_id,
        enabled ? "true" : "false",
        interval,
        network ? "true" : "false",
        disk ? "true" : "false",
        threads ? "true" : "false");
    agent_send_response(a, resp);
}

static void cmd_update(struct agent_state *a, const char *cmd_id,
                       const char *json)
{
    (void)json;
    char msg[512];
    int rc = self_update(msg, sizeof(msg));
    agent_log("Self-update: %s", msg);

    char esc[1024];
    json_escape(esc, sizeof(esc), msg);
    char resp[2048];
    snprintf(resp, sizeof(resp),
        "{\"id\":\"%s\",\"ok\":%s,\"message\":\"%s\","
        "\"running_version\":\"%s\"}",
        cmd_id, rc == 0 ? "true" : "false", esc, AGENT_VERSION);
    agent_send_response(a, resp);
}

/* --------------------------------------------------------------------------
 * Command dispatch
 * -------------------------------------------------------------------------- */

typedef void (*cmd_handler_fn)(struct agent_state *, const char *, const char *);

struct cmd_dispatch_entry {
    const char   *name;
    cmd_handler_fn handler;
};

static const struct cmd_dispatch_entry CMD_TABLE[] = {
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
    char cmd[64] = "", cmd_id[64] = "";
    json_get_str(json, "cmd", cmd, sizeof(cmd));
    json_get_str(json, "id", cmd_id, sizeof(cmd_id));

    if (!cmd[0]) {
        agent_log("Received command with no 'cmd' field");
        return;
    }

    for (int i = 0; CMD_TABLE[i].name; i++) {
        if (strcmp(cmd, CMD_TABLE[i].name) == 0) {
            CMD_TABLE[i].handler(a, cmd_id, json);
            return;
        }
    }

    /* Unknown command */
    char esc_cmd[64];
    json_escape(esc_cmd, sizeof(esc_cmd), cmd);
    char resp[256];
    snprintf(resp, sizeof(resp),
        "{\"id\":\"%s\",\"ok\":false,\"error\":\"unknown command: %s\"}",
        cmd_id, esc_cmd);
    agent_send_response(a, resp);
}

