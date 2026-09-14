/*
 * PerfLens Device Agent — platform detection + perf capability probing
 */

#include "agent.h"

/* --------------------------------------------------------------------------
 * Platform detection
 * -------------------------------------------------------------------------- */

static const char *CANDIDATE_EVENTS[] = {
    "cycles", "instructions", "cache-misses", "cache-references",
    "branch-misses", "branch-instructions", "page-faults",
    "context-switches", "cpu-migrations",
    NULL
};

/* Software sampling events, probed only when the PMU yields no record event.
 *
 * Plenty of embedded hardware ships without a wired-up ARM PMU.
 * `perf list hw sw` then offers software events only, every hardware
 * candidate above fails, and the three that survive are all stat-only —
 * which leaves zero record events and a `start` that can never succeed.
 * cpu-clock samples on a timer and needs no PMU at all.
 *
 * Deliberately a fallback rather than two more candidates: where counters do
 * exist these add a seventh and eighth stream of samples measuring what
 * `cycles` already covers, so listing them outright silently raised every
 * existing target from six record events to eight. They cost nothing when
 * they are not needed, and are the difference between profiling and not
 * when they are. */
static const char *FALLBACK_EVENTS[] = { "cpu-clock", "task-clock", NULL };

static const char *CALLGRAPH_METHODS[] = { "fp", "dwarf", "lbr", NULL };

static const char *SKIP_PATTERNS[] = {
    "not supported", "invalid event", "unknown", NULL
};

char g_perf[PERF_PATH_MAX] = "perf";

/* Point every later probe and collection at another perf binary.
 *
 * Some targets install perf under a vendor prefix that is not on PATH, and
 * launching the agent with PATH= was the only workaround. A bare name still
 * goes through PATH; anything with a slash must be absolute, because a path
 * relative to the agent's working directory is never what a remote operator
 * means. The candidate runs with profiling arguments for as long as the agent
 * lives and a peer can choose it over the wire, so it has to identify itself
 * as perf rather than merely exist. On failure the current perf stays. */
int perf_use(const char *path, char *err, size_t errlen)
{
    if (!path || !path[0]) {
        snprintf(err, errlen, "empty perf path");
        return -1;
    }
    if (strlen(path) >= sizeof(g_perf)) {
        snprintf(err, errlen, "perf path is longer than %d bytes",
                 PERF_PATH_MAX - 1);
        return -1;
    }
    if (strchr(path, '/') && path[0] != '/') {
        snprintf(err, errlen, "%s: not an absolute path", path);
        return -1;
    }
    if (path[0] == '/' && access(path, X_OK) != 0) {
        snprintf(err, errlen, "%s: %s", path, strerror(errno));
        return -1;
    }

    char *argv[] = { (char *)path, "--version", NULL };
    struct buf out;
    buf_init(&out);
    int rc = run_cmd(argv, &out, NULL, 5);
    int is_perf = rc == 0 && out.len >= 13 &&
                  memcmp(out.data, "perf version ", 13) == 0;
    buf_free(&out);
    if (!is_perf) {
        snprintf(err, errlen, "%s: does not identify as perf "
                 "(\"%s --version\" must print \"perf version\")", path, path);
        return -1;
    }

    snprintf(g_perf, sizeof(g_perf), "%s", path);
    return 0;
}

void detect_platform(struct platform_info *info)
{
    struct utsname uts;
    uname(&uts);
    snprintf(info->arch, sizeof(info->arch), "%s", uts.machine);
    snprintf(info->kernel, sizeof(info->kernel), "%s", uts.release);

    /* perf version */
    char *argv[] = { g_perf, "--version", NULL };
    struct buf out;
    buf_init(&out);
    int rc = run_cmd(argv, &out, NULL, 5);
    if (rc == 0 && out.len > 0) {
        size_t cplen = out.len < sizeof(info->perf_version) - 1
                     ? out.len : sizeof(info->perf_version) - 1;
        memcpy(info->perf_version, out.data, cplen);
        info->perf_version[cplen] = '\0';
        /* Strip trailing newline */
        char *nl = strchr(info->perf_version, '\n');
        if (nl) *nl = '\0';
    } else {
        snprintf(info->perf_version, sizeof(info->perf_version), "unknown");
        agent_warn("perf not found or not working as \"%s\". If it is "
                   "installed outside PATH, pass --perf /path/to/perf (or set "
                   "PERFLENS_PERF), or set the path from the PerfLens UI.",
                   g_perf);
    }
    buf_free(&out);

    /* perf_event_paranoid */
    info->perf_event_paranoid = -1;
    FILE *f = fopen("/proc/sys/kernel/perf_event_paranoid", "r");
    if (f) {
        if (fscanf(f, "%d", &info->perf_event_paranoid) != 1)
            info->perf_event_paranoid = -1;
        fclose(f);
    }

    agent_log("Platform: arch=%s, kernel=%s, perf=%s (%s), "
              "perf_event_paranoid=%d",
              info->arch, info->kernel, info->perf_version, g_perf,
              info->perf_event_paranoid);

    if (info->perf_event_paranoid > 1) {
        agent_warn("perf_event_paranoid=%d (>1). "
                   "Some events may be unavailable. "
                   "Consider: sudo sysctl kernel.perf_event_paranoid=1",
                   info->perf_event_paranoid);
    }
}

/* --------------------------------------------------------------------------
 * Capability probing
 * -------------------------------------------------------------------------- */

/* A perf.data path in the agent's temp dir, or -1. */
static int make_probe_file(char *tmpl, size_t cap)
{
    snprintf(tmpl, cap, "%s/perflens-probe-XXXXXX", agent_tmpdir());
    int fd = mkstemp(tmpl);
    if (fd < 0) return -1;
    close(fd);
    return 0;
}

static int event_works(const char *event, int pid)
{
    char pid_str[16], *argv[MAX_CMD_ARGS];
    snprintf(pid_str, sizeof(pid_str), "%d", pid);
    build_stat_argv(argv, MAX_CMD_ARGS, event, pid_str, "1");

    struct buf dummy, err_buf;
    buf_init(&dummy); buf_init(&err_buf);
    int rc = run_cmd(argv, &dummy, &err_buf, 10);
    buf_free(&dummy);

    if (rc != 0) { buf_free(&err_buf); return 0; }

    for (int j = 0; SKIP_PATTERNS[j]; j++) {
        if (str_contains_lower(err_buf.data, err_buf.len, SKIP_PATTERNS[j])) {
            buf_free(&err_buf);
            return 0;
        }
    }
    buf_free(&err_buf);
    return 1;
}

/* Does this perf script output actually carry call chains?
 *
 * Pipe mode can emit samples while silently dropping their stacks. Measured
 * on perf 4.4, on a big-endian ARMv7 target: the same capture written to a
 * file gave ~10 frames per sample, while `record -o - | script -i -` gave
 * exactly one leaf frame each. Both exit 0 with non-empty output, so "it
 * produced something" cannot tell them apart — and taking pipe mode on that
 * evidence flattens every flame graph to a single level, with nothing
 * reporting an error.
 *
 * A call-chain frame is printed on its own indented line. A sample header is
 * not indented, and without a chain its ip and symbol sit on that same line.
 * So any indented line means the chains survived. That holds for both the -F
 * field list and the default output format -- and, unlike matching
 * "<event>:", for the PMU-qualified names a hybrid CPU prints
 * ("cpu_core/cycles/:"), which never contain "cycles:" and so turned
 * continuous mode off on every hybrid x86 machine.
 *
 * The same test now decides the call-graph probe, which used to accept any
 * non-empty output -- the trap this function was written to close. */
int callchains_present(const struct buf *out)
{
    if (!out->data) return 0;

    long samples = 0, frames = 0;
    const char *p = out->data, *end = out->data + out->len;
    while (p < end) {
        const char *nl = memchr(p, '\n', (size_t)(end - p));
        size_t len = nl ? (size_t)(nl - p) : (size_t)(end - p);
        if (len > 0) {
            if (*p == ' ' || *p == '\t') frames++;
            else samples++;
        }
        if (!nl) break;
        p = nl + 1;
    }
    return samples > 0 && frames > 0;
}

/* Record two seconds of `event` with `method`, then check that perf script
 * prints stacks under the samples. Uses the first event the target can
 * record -- it was `cycles` unconditionally, which on a PMU-less target
 * only worked because that perf happened to fall back to cpu-clock itself. */
static int callgraph_works(const char *method, const char *event, int pid)
{
    char tmpfile[PATH_MAX];
    if (make_probe_file(tmpfile, sizeof(tmpfile)) < 0) return 0;

    char pid_str[16], *argv_rec[MAX_CMD_ARGS], *argv_script[MAX_CMD_ARGS];
    snprintf(pid_str, sizeof(pid_str), "%d", pid);
    build_record_argv(argv_rec, MAX_CMD_ARGS, event, pid_str, "99",
                      tmpfile, method, "2");
    int rc = run_cmd(argv_rec, NULL, NULL, 15);
    if (rc != 0) { unlink(tmpfile); return 0; }

    build_script_argv(argv_script, MAX_CMD_ARGS, NULL, tmpfile);
    struct buf out;
    buf_init(&out);
    rc = run_cmd(argv_script, &out, NULL, 15);
    int result = (rc == 0 && callchains_present(&out));
    buf_free(&out);
    unlink(tmpfile);
    return result;
}

/* Can this event actually drive `perf record`?
 *
 * event_works() probes with `perf stat`, and stat accepting an event does not
 * mean record will take it — that inference is what the hardcoded
 * STAT_ONLY_EVENTS list was papering over. Ask record directly, so the
 * advertised record set is measured rather than assumed. Costs one short
 * record per candidate that stat already accepted, and only for events not
 * already known to be stat-only. */
static int event_records(const char *event, int pid)
{
    char tmpl[PATH_MAX];
    if (make_probe_file(tmpl, sizeof(tmpl)) < 0) return 0;

    char pid_str[16], *argv[MAX_CMD_ARGS];
    snprintf(pid_str, sizeof(pid_str), "%d", pid);
    build_record_argv(argv, MAX_CMD_ARGS, event, pid_str, "99", tmpl,
                      NULL, "1");
    int rc = run_cmd(argv, NULL, NULL, 15);
    unlink(tmpl);
    return rc == 0;
}

static int script_fields_work(int pid, const char *event)
{
    char tmpl[PATH_MAX];
    if (make_probe_file(tmpl, sizeof(tmpl)) < 0) return 0;

    char pid_str[16], *argv_rec[MAX_CMD_ARGS], *argv_script[MAX_CMD_ARGS];
    snprintf(pid_str, sizeof(pid_str), "%d", pid);
    build_record_argv(argv_rec, MAX_CMD_ARGS, event, pid_str, "99", tmpl,
                      NULL, "1");
    int rc = run_cmd(argv_rec, NULL, NULL, 15);
    if (rc != 0) { unlink(tmpl); return 0; }

    build_script_argv(argv_script, MAX_CMD_ARGS, SCRIPT_FIELDS, tmpl);
    struct buf out;
    buf_init(&out);
    rc = run_cmd(argv_script, &out, NULL, 15);
    int result = (rc == 0 && out.len > 0);
    buf_free(&out);
    unlink(tmpl);
    return result;
}

/* Probe continuous pipe mode with the exact argv shapes collection will
 * use. Pipe mode is old but the least uniform corner of perf across the
 * kernel range we support — it must be probed, never assumed. */
static int pipe_mode_works(const struct capabilities *caps, int pid)
{
    char pid_str[16], *argv_rec[MAX_CMD_ARGS], *argv_script[MAX_CMD_ARGS];
    snprintf(pid_str, sizeof(pid_str), "%d", pid);
    build_record_argv(argv_rec, MAX_CMD_ARGS, caps->record_events[0], pid_str,
                      "99", "-", caps->callgraph, "2");
    build_script_argv(argv_script, MAX_CMD_ARGS, caps->script_fields, "-");

    struct buf out;
    buf_init(&out);
    int rc = run_pipeline_once(argv_rec, argv_script, &out, 20);
    int ok = (rc == 0 && out.len > 0);
    if (ok && caps->callgraph[0] && !callchains_present(&out)) {
        agent_log("  pipe mode produced samples but no call chains, "
                  "falling back to discrete rounds");
        ok = 0;
    }
    buf_free(&out);
    return ok;
}

/* Probe one NULL-terminated event list, sorting each survivor into the
 * record or stat-only bucket. */
static void probe_event_list(const char **events, int pid,
                             struct capabilities *caps)
{
    for (int i = 0; events[i]; i++) {
        if (g_shutdown) return;
        const char *ev = events[i];
        if (!event_works(ev, pid)) {
            agent_log("  %s: not available, skipping", ev);
            continue;
        }
        char *dup = strdup(ev);
        if (!dup) continue;
        int stat_only = is_stat_only(ev) || !event_records(ev, pid);
        if (stat_only) {
            if (caps->stat_only_event_count < MAX_EVENTS)
                caps->stat_only_events[caps->stat_only_event_count++] = dup;
            else
                free(dup);
        } else {
            if (caps->record_event_count < MAX_EVENTS)
                caps->record_events[caps->record_event_count++] = dup;
            else
                free(dup);
        }
        agent_log("  %s: supported (%s)", ev,
                  stat_only ? "stat only" : "record");
    }
}

void probe_capabilities(int pid, struct capabilities *caps)
{
    memset(caps, 0, sizeof(*caps));

    agent_log("Probing perf event support...");
    probe_event_list(CANDIDATE_EVENTS, pid, caps);

    /* Nothing the PMU offers can drive `perf record`. Try the software
     * sampling events before giving up -- on a PMU-less target they are the
     * only thing that works. */
    if (caps->record_event_count == 0 && !g_shutdown) {
        agent_log("No hardware record events; trying software sampling...");
        probe_event_list(FALLBACK_EVENTS, pid, caps);
    }

    /* Build combined all_events list */
    for (int i = 0; i < caps->record_event_count; i++)
        caps->all_events[caps->all_event_count++] = caps->record_events[i];
    for (int i = 0; i < caps->stat_only_event_count; i++)
        caps->all_events[caps->all_event_count++] = caps->stat_only_events[i];

    if (caps->record_event_count == 0) {
        agent_warn("No record events available. Profiling may not produce useful data.");
        /* Nothing can be recorded, so there is nothing to probe a call-graph
         * method or pipe mode with: each attempt would just time out. */
        return;
    }

    /* Probe call-graph methods */
    agent_log("Probing call-graph methods...");
    caps->callgraph[0] = '\0';
    for (int i = 0; CALLGRAPH_METHODS[i]; i++) {
        if (g_shutdown) return;
        agent_log("  Trying --call-graph %s...", CALLGRAPH_METHODS[i]);
        if (callgraph_works(CALLGRAPH_METHODS[i], caps->record_events[0], pid)) {
            snprintf(caps->callgraph, sizeof(caps->callgraph), "%s",
                     CALLGRAPH_METHODS[i]);
            agent_log("  Using call-graph method: %s", caps->callgraph);
            break;
        } else {
            agent_log("  %s: failed", CALLGRAPH_METHODS[i]);
        }
    }
    if (caps->callgraph[0] == '\0')
        agent_warn("No call-graph method works. Will collect flat profiles (no stacks).");

    /* Probe perf script -F support */
    caps->script_fields[0] = '\0';
    agent_log("Probing perf script -F support...");
    if (script_fields_work(pid, caps->record_events[0])) {
        snprintf(caps->script_fields, sizeof(caps->script_fields),
                 "%s", SCRIPT_FIELDS);
        agent_log("  perf script -F supported, using: %s", caps->script_fields);
    } else {
        agent_log("  perf script -F not supported, using default output format");
    }

    /* Probe continuous pipe mode (record -o - | script -i -) */
    caps->pipe_mode = 0;
    if (!g_shutdown) {
        agent_log("Probing pipe mode (continuous collection)...");
        if (pipe_mode_works(caps, pid)) {
            caps->pipe_mode = 1;
            agent_log("  pipe mode supported — continuous collection enabled");
        } else {
            agent_log("  pipe mode not supported — using per-round collection");
        }
    }

    /* Log summary */
    char list[512];
    agent_log("Record events: %s",
              caps->record_event_count
                  ? join_events(list, sizeof(list), caps->record_events,
                                caps->record_event_count, NULL)
                  : "(none)");
    agent_log("Stat-only events: %s",
              caps->stat_only_event_count
                  ? join_events(list, sizeof(list), caps->stat_only_events,
                                caps->stat_only_event_count, NULL)
                  : "(none)");
}

void free_capabilities(struct capabilities *caps)
{
    for (int i = 0; i < caps->record_event_count; i++)
        free(caps->record_events[i]);
    for (int i = 0; i < caps->stat_only_event_count; i++)
        free(caps->stat_only_events[i]);
    /* all_events are aliases — don't double-free */
}
