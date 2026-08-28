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

void detect_platform(struct platform_info *info)
{
    struct utsname uts;
    uname(&uts);
    snprintf(info->arch, sizeof(info->arch), "%s", uts.machine);
    snprintf(info->kernel, sizeof(info->kernel), "%s", uts.release);

    /* perf version */
    char *argv[] = { PERF, "--version", NULL };
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

    agent_log("Platform: arch=%s, kernel=%s, perf=%s, perf_event_paranoid=%d",
              info->arch, info->kernel, info->perf_version,
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

static int event_works(const char *event, int pid)
{
    char pid_str[16], *argv[10];
    snprintf(pid_str, sizeof(pid_str), "%d", pid);

    int i = 0;
    argv[i++] = PERF; argv[i++] = "stat"; argv[i++] = "-e"; argv[i++] = (char *)event;
    argv[i++] = "-p"; argv[i++] = pid_str; argv[i++] = "--"; argv[i++] = "sleep";
    argv[i++] = "1";  argv[i++] = NULL;

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

static int callgraph_works(const char *method, int pid)
{
    char tmpfile[] = "/tmp/perflens-probe-XXXXXX.data";
    /* mkstemp needs the template to end with XXXXXX, so fix up */
    char tmpl[] = "/tmp/perflens-probe-XXXXXX";
    int fd = mkstemp(tmpl);
    if (fd < 0) return 0;
    close(fd);
    /* Rename with .data suffix for perf */
    snprintf(tmpfile, sizeof(tmpfile), "%s", tmpl);

    char pid_str[16], freq_str[8];
    snprintf(pid_str, sizeof(pid_str), "%d", pid);
    snprintf(freq_str, sizeof(freq_str), "99");

    /* perf record */
    char *argv_rec[] = {
        PERF, "record", "-e", "cycles", "-p", pid_str,
        "--call-graph", (char *)method, "-F", freq_str, "-o", tmpfile,
        "--", "sleep", "2", NULL
    };
    int rc = run_cmd(argv_rec, NULL, NULL, 15);
    if (rc != 0) { unlink(tmpfile); return 0; }

    /* perf script */
    char *argv_script[] = { PERF, "script", "-i", tmpfile, NULL };
    struct buf out;
    buf_init(&out);
    rc = run_cmd(argv_script, &out, NULL, 15);
    int result = (rc == 0 && out.len > 0);
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
    char tmpl[] = "/tmp/perflens-probe-XXXXXX";
    int fd = mkstemp(tmpl);
    if (fd < 0) return 0;
    close(fd);

    char pid_str[16];
    snprintf(pid_str, sizeof(pid_str), "%d", pid);
    char *argv[] = {
        PERF, "record", "-e", (char *)event, "-p", pid_str,
        "-F", "99", "-o", tmpl, "--", "sleep", "1", NULL
    };
    int rc = run_cmd(argv, NULL, NULL, 15);
    unlink(tmpl);
    return rc == 0;
}

static int script_fields_work(int pid, const char *event)
{
    char tmpl[] = "/tmp/perflens-probe-XXXXXX";
    int fd = mkstemp(tmpl);
    if (fd < 0) return 0;
    close(fd);

    char pid_str[16];
    snprintf(pid_str, sizeof(pid_str), "%d", pid);

    char *argv_rec[] = {
        PERF, "record", "-e", (char *)event, "-p", pid_str,
        "-F", "99", "-o", tmpl, "--", "sleep", "1", NULL
    };
    int rc = run_cmd(argv_rec, NULL, NULL, 15);
    if (rc != 0) { unlink(tmpl); return 0; }

    char *argv_script[] = {
        PERF, "script", "-F", SCRIPT_FIELDS, "-i", tmpl, NULL
    };
    struct buf out;
    buf_init(&out);
    rc = run_cmd(argv_script, &out, NULL, 15);
    int result = (rc == 0 && out.len > 0);
    buf_free(&out);
    unlink(tmpl);
    return result;
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
 * A sample line carries "<event>:"; a call-chain frame line does not. So
 * more lines than samples means the chains survived. That holds for both the
 * -F field list and the default output format, since both print the event
 * name followed by a colon. */
static int callchains_present(const struct buf *out, const char *event)
{
    char needle[64];
    snprintf(needle, sizeof(needle), "%s:", event);
    size_t nlen = strlen(needle);
    if (!out->data || nlen == 0) return 0;

    long lines = 0, samples = 0;
    const char *p = out->data, *end = out->data + out->len;
    while (p < end) {
        const char *nl = memchr(p, '\n', (size_t)(end - p));
        size_t len = nl ? (size_t)(nl - p) : (size_t)(end - p);
        if (len > 0) {
            lines++;
            for (size_t i = 0; i + nlen <= len; i++) {
                if (memcmp(p + i, needle, nlen) == 0) { samples++; break; }
            }
        }
        if (!nl) break;
        p = nl + 1;
    }
    return samples > 0 && lines > samples;
}

/* Probe continuous pipe mode with the exact argv shapes collection will
 * use. Pipe mode is old but the least uniform corner of perf across the
 * kernel range we support — it must be probed, never assumed. */
static int pipe_mode_works(const struct capabilities *caps, int pid)
{
    char pid_str[16];
    snprintf(pid_str, sizeof(pid_str), "%d", pid);

    char *argv_rec[16];
    int ri = 0;
    argv_rec[ri++] = PERF; argv_rec[ri++] = "record";
    argv_rec[ri++] = "-e"; argv_rec[ri++] = caps->record_events[0];
    argv_rec[ri++] = "-p"; argv_rec[ri++] = pid_str;
    argv_rec[ri++] = "-F"; argv_rec[ri++] = "99";
    argv_rec[ri++] = "-o"; argv_rec[ri++] = "-";
    if (caps->callgraph[0]) {
        argv_rec[ri++] = "--call-graph";
        argv_rec[ri++] = (char *)caps->callgraph;
    }
    argv_rec[ri++] = "--"; argv_rec[ri++] = "sleep"; argv_rec[ri++] = "2";
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

    struct buf out;
    buf_init(&out);
    int rc = run_pipeline_once(argv_rec, argv_script, &out, 20);
    int ok = (rc == 0 && out.len > 0);
    if (ok && caps->callgraph[0] &&
        !callchains_present(&out, caps->record_events[0])) {
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

    if (caps->record_event_count == 0)
        agent_warn("No record events available. Profiling may not produce useful data.");

    /* Probe call-graph methods */
    agent_log("Probing call-graph methods...");
    caps->callgraph[0] = '\0';
    for (int i = 0; CALLGRAPH_METHODS[i]; i++) {
        if (g_shutdown) return;
        agent_log("  Trying --call-graph %s...", CALLGRAPH_METHODS[i]);
        if (callgraph_works(CALLGRAPH_METHODS[i], pid)) {
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
    if (caps->record_event_count > 0) {
        agent_log("Probing perf script -F support...");
        if (script_fields_work(pid, caps->record_events[0])) {
            snprintf(caps->script_fields, sizeof(caps->script_fields),
                     "%s", SCRIPT_FIELDS);
            agent_log("  perf script -F supported, using: %s", caps->script_fields);
        } else {
            agent_log("  perf script -F not supported, using default output format");
        }
    }

    /* Probe continuous pipe mode (record -o - | script -i -) */
    caps->pipe_mode = 0;
    if (caps->record_event_count > 0 && !g_shutdown) {
        agent_log("Probing pipe mode (continuous collection)...");
        if (pipe_mode_works(caps, pid)) {
            caps->pipe_mode = 1;
            agent_log("  pipe mode supported — continuous collection enabled");
        } else {
            agent_log("  pipe mode not supported — using per-round collection");
        }
    }

    /* Log summary */
    char rec_list[512] = "(none)";
    if (caps->record_event_count > 0) {
        rec_list[0] = '\0';
        for (int i = 0; i < caps->record_event_count; i++) {
            if (i > 0) strncat(rec_list, ",", sizeof(rec_list) - strlen(rec_list) - 1);
            strncat(rec_list, caps->record_events[i],
                    sizeof(rec_list) - strlen(rec_list) - 1);
        }
    }
    agent_log("Record events: %s", rec_list);

    char stat_list[512] = "(none)";
    if (caps->stat_only_event_count > 0) {
        stat_list[0] = '\0';
        for (int i = 0; i < caps->stat_only_event_count; i++) {
            if (i > 0) strncat(stat_list, ",", sizeof(stat_list) - strlen(stat_list) - 1);
            strncat(stat_list, caps->stat_only_events[i],
                    sizeof(stat_list) - strlen(stat_list) - 1);
        }
    }
    agent_log("Stat-only events: %s", stat_list);
}

void free_capabilities(struct capabilities *caps)
{
    for (int i = 0; i < caps->record_event_count; i++)
        free(caps->record_events[i]);
    for (int i = 0; i < caps->stat_only_event_count; i++)
        free(caps->stat_only_events[i]);
    /* all_events are aliases — don't double-free */
}

