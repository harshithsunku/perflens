/*
 * PerfLens Device Agent — the perf command lines
 *
 * Every probe and both collection loops used to assemble their own argv,
 * nine copies in all, and they drifted: the call-graph probe kept asking for
 * `-e cycles` after the event list had learnt to fall back to cpu-clock, and
 * the pipe-mode probe had to promise by hand that it used "the exact argv
 * shapes collection will use". These builders are that promise.
 */

#include "agent.h"

char *join_events(char *dst, size_t cap, char *const *events, int count,
                  const char *extra)
{
    if (cap == 0) return dst;
    dst[0] = '\0';
    size_t len = 0;
    for (int i = 0; i < count; i++) {
        const char *ev = events[i];
        size_t need = strlen(ev) + (len ? 1 : 0);
        if (len + need + 1 > cap) break;
        if (len) dst[len++] = ',';
        memcpy(dst + len, ev, strlen(ev) + 1);
        len += strlen(ev);
    }
    if (extra && *extra) {
        size_t need = strlen(extra) + (len ? 1 : 0);
        if (len + need + 1 <= cap) {
            if (len) dst[len++] = ',';
            memcpy(dst + len, extra, strlen(extra) + 1);
        }
    }
    return dst;
}

static int put(char **argv, int cap, int n, const char *s)
{
    if (n + 1 < cap) argv[n] = (char *)s;
    return n + 1;
}

int build_record_argv(char **argv, int cap, const char *events,
                      const char *pid_str, const char *freq_str,
                      const char *output, const char *callgraph,
                      const char *sleep_secs)
{
    int n = 0;
    n = put(argv, cap, n, g_perf);
    n = put(argv, cap, n, "record");
    n = put(argv, cap, n, "-e");
    n = put(argv, cap, n, events);
    n = put(argv, cap, n, "-p");
    n = put(argv, cap, n, pid_str);
    n = put(argv, cap, n, "-F");
    n = put(argv, cap, n, freq_str);
    n = put(argv, cap, n, "-o");
    n = put(argv, cap, n, output);
    if (callgraph && callgraph[0]) {
        n = put(argv, cap, n, "--call-graph");
        n = put(argv, cap, n, callgraph);
    }
    if (sleep_secs && sleep_secs[0]) {
        n = put(argv, cap, n, "--");
        n = put(argv, cap, n, "sleep");
        n = put(argv, cap, n, sleep_secs);
    }
    if (n < cap) argv[n] = NULL;
    return n;
}

int build_script_argv(char **argv, int cap, const char *fields,
                      const char *input)
{
    int n = 0;
    n = put(argv, cap, n, g_perf);
    n = put(argv, cap, n, "script");
    if (fields && fields[0]) {
        n = put(argv, cap, n, "-F");
        n = put(argv, cap, n, fields);
    }
    n = put(argv, cap, n, "-i");
    n = put(argv, cap, n, input);
    if (n < cap) argv[n] = NULL;
    return n;
}

int build_stat_argv(char **argv, int cap, const char *events,
                    const char *pid_str, const char *sleep_secs, int csv)
{
    int n = 0;
    n = put(argv, cap, n, g_perf);
    n = put(argv, cap, n, "stat");
    if (csv) {
        n = put(argv, cap, n, "-x");
        n = put(argv, cap, n, ",");
    }
    n = put(argv, cap, n, "-e");
    n = put(argv, cap, n, events);
    n = put(argv, cap, n, "-p");
    n = put(argv, cap, n, pid_str);
    n = put(argv, cap, n, "--");
    n = put(argv, cap, n, "sleep");
    n = put(argv, cap, n, sleep_secs);
    if (n < cap) argv[n] = NULL;
    return n;
}
