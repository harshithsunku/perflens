/*
 * PerfLens Device Agent — C implementation
 *
 * Functionally identical to agent/perflens_agent.py, compiles to a single
 * statically linked binary with zero runtime dependencies on the target.
 *
 * Usage:
 *   perflens-agent --pid PID [--server HOST] [--port PORT]
 *                  [--frequency HZ] [--duration SECS]
 *
 * Build:
 *   make                              # native build
 *   make CROSS=aarch64-linux-gnu-     # cross-compile for ARM64
 *
 * Architecture:
 *   1. Platform detection (uname, perf_event_paranoid)
 *   2. Capability probing (events, call-graph modes, perf script -F)
 *   3. Collection loop: perf record + perf stat -> perf script -> compress -> TCP send
 *   4. TCP wire protocol: 5-byte header (4B big-endian length + 1B flag)
 *   5. Reconnect with exponential backoff on connection loss
 *   6. Signal handling: SIGINT/SIGTERM -> graceful shutdown
 *
 * License: MIT (same as PerfLens project)
 */

#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#endif
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <arpa/inet.h>
#include <errno.h>
#include <getopt.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "vendor/zstd/zstd.h"

/* --------------------------------------------------------------------------
 * Constants
 * -------------------------------------------------------------------------- */

#define LOG_PREFIX       "[perflens-agent]"
#define PERF             "perf"
#define DEFAULT_PORT     9999
#define DEFAULT_FREQ     99
#define DEFAULT_DURATION 8
#define MAX_EVENTS       16
#define MAX_CMD_ARGS     32
#define INITIAL_BUF_SIZE (256 * 1024)     /* 256 KB initial read buffer */
#define MAX_BUF_SIZE     (64 * 1024 * 1024)  /* 64 MB cap */
#define RECONNECT_MAX    30.0
#define ZSTD_LEVEL       1

/* Normalized field set for 'perf script -F'. Ensures consistent output
 * format across kernel versions. Requires perf >= ~3.12. */
#define SCRIPT_FIELDS    "comm,pid,time,period,event,ip,sym,dso"

static const char *CANDIDATE_EVENTS[] = {
    "cycles", "instructions", "cache-misses", "cache-references",
    "branch-misses", "branch-instructions", "page-faults",
    "context-switches", "cpu-migrations",
    NULL
};

/* Events that can only be used with perf stat, not perf record */
static const char *STAT_ONLY_EVENTS[] = {
    "page-faults", "context-switches", "cpu-migrations",
    NULL
};

static const char *CALLGRAPH_METHODS[] = { "fp", "dwarf", "lbr", NULL };

static const char *SKIP_PATTERNS[] = {
    "not supported", "invalid event", "unknown", NULL
};

/* --------------------------------------------------------------------------
 * Globals
 * -------------------------------------------------------------------------- */

static volatile sig_atomic_t g_shutdown = 0;
static volatile pid_t g_child_pids[8];
static volatile int g_child_count = 0;

/* --------------------------------------------------------------------------
 * Logging
 * -------------------------------------------------------------------------- */

static void agent_log(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "%s ", LOG_PREFIX);
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    fflush(stderr);
    va_end(ap);
}

static void agent_warn(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "%s WARNING: ", LOG_PREFIX);
    vfprintf(stderr, fmt, ap);
    fprintf(stderr, "\n");
    fflush(stderr);
    va_end(ap);
}

/* --------------------------------------------------------------------------
 * Signal handling
 * -------------------------------------------------------------------------- */

static void signal_handler(int sig)
{
    (void)sig;
    g_shutdown = 1;
    /* Kill tracked child processes */
    for (int i = 0; i < g_child_count; i++) {
        if (g_child_pids[i] > 0)
            kill(g_child_pids[i], SIGTERM);
    }
}

static void install_signal_handlers(void)
{
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = signal_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;  /* no SA_RESTART — we want blocking calls to fail with EINTR */
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);

    /* Ignore SIGPIPE — we handle send errors via return codes */
    sa.sa_handler = SIG_IGN;
    sigaction(SIGPIPE, &sa, NULL);
}

static void track_child(pid_t pid)
{
    if (g_child_count < (int)(sizeof(g_child_pids)/sizeof(g_child_pids[0])))
        g_child_pids[g_child_count++] = pid;
}

static void untrack_child(pid_t pid)
{
    for (int i = 0; i < g_child_count; i++) {
        if (g_child_pids[i] == pid) {
            g_child_pids[i] = g_child_pids[--g_child_count];
            return;
        }
    }
}

/* --------------------------------------------------------------------------
 * Dynamic buffer
 * -------------------------------------------------------------------------- */

struct buf {
    char  *data;
    size_t len;
    size_t cap;
};

static void buf_init(struct buf *b)
{
    b->data = NULL;
    b->len  = 0;
    b->cap  = 0;
}

static void buf_free(struct buf *b)
{
    free(b->data);
    b->data = NULL;
    b->len  = 0;
    b->cap  = 0;
}

static int buf_ensure(struct buf *b, size_t needed)
{
    if (b->cap >= needed) return 0;
    size_t newcap = b->cap ? b->cap : INITIAL_BUF_SIZE;
    while (newcap < needed) newcap *= 2;
    if (newcap > MAX_BUF_SIZE) return -1;
    char *p = realloc(b->data, newcap);
    if (!p) return -1;
    b->data = p;
    b->cap  = newcap;
    return 0;
}

/* --------------------------------------------------------------------------
 * Subprocess helper: run_cmd()
 *
 * Runs argv[0..] with fork/exec, captures stdout and stderr into caller-
 * provided buffers. Returns the child's exit code, or -1 on error/timeout.
 * Uses poll() for timeout — no SIGALRM interference.
 * -------------------------------------------------------------------------- */

static int run_cmd(char *const argv[], struct buf *out, struct buf *err,
                   int timeout_sec)
{
    int stdout_pipe[2] = {-1, -1};
    int stderr_pipe[2] = {-1, -1};

    if (out) { out->len = 0; }
    if (err) { err->len = 0; }

    if (pipe(stdout_pipe) < 0 || pipe(stderr_pipe) < 0) {
        agent_warn("pipe() failed: %s", strerror(errno));
        return -1;
    }

    pid_t pid = fork();
    if (pid < 0) {
        agent_warn("fork() failed: %s", strerror(errno));
        close(stdout_pipe[0]); close(stdout_pipe[1]);
        close(stderr_pipe[0]); close(stderr_pipe[1]);
        return -1;
    }

    if (pid == 0) {
        /* Child */
        close(stdout_pipe[0]);
        close(stderr_pipe[0]);
        dup2(stdout_pipe[1], STDOUT_FILENO);
        dup2(stderr_pipe[1], STDERR_FILENO);
        close(stdout_pipe[1]);
        close(stderr_pipe[1]);

        /* Close stdin to prevent perf from reading terminal */
        close(STDIN_FILENO);

        execvp(argv[0], argv);
        _exit(127);
    }

    /* Parent */
    close(stdout_pipe[1]);
    close(stderr_pipe[1]);
    track_child(pid);

    struct pollfd fds[2];
    fds[0].fd = stdout_pipe[0]; fds[0].events = POLLIN;
    fds[1].fd = stderr_pipe[0]; fds[1].events = POLLIN;
    int open_fds = 2;

    struct timespec start;
    clock_gettime(CLOCK_MONOTONIC, &start);

    while (open_fds > 0 && !g_shutdown) {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        int elapsed_ms = (int)((now.tv_sec - start.tv_sec) * 1000 +
                               (now.tv_nsec - start.tv_nsec) / 1000000);
        int remaining_ms = timeout_sec * 1000 - elapsed_ms;
        if (remaining_ms <= 0) {
            agent_warn("Command timed out after %ds, killing", timeout_sec);
            kill(pid, SIGKILL);
            break;
        }

        int ret = poll(fds, 2, remaining_ms < 500 ? remaining_ms : 500);
        if (ret < 0) {
            if (errno == EINTR) continue;
            break;
        }

        for (int i = 0; i < 2; i++) {
            if (fds[i].fd < 0) continue;
            if (!(fds[i].revents & (POLLIN | POLLHUP))) continue;

            struct buf *target = (i == 0) ? out : err;
            if (!target) {
                /* Drain and discard */
                char discard[4096];
                ssize_t n = read(fds[i].fd, discard, sizeof(discard));
                if (n <= 0) { close(fds[i].fd); fds[i].fd = -1; open_fds--; }
                continue;
            }

            if (buf_ensure(target, target->len + 4096) < 0) {
                close(fds[i].fd); fds[i].fd = -1; open_fds--;
                continue;
            }
            ssize_t n = read(fds[i].fd, target->data + target->len,
                             target->cap - target->len);
            if (n > 0) {
                target->len += (size_t)n;
            } else {
                close(fds[i].fd); fds[i].fd = -1; open_fds--;
            }
        }
    }

    /* Close any remaining pipe fds */
    if (fds[0].fd >= 0) close(fds[0].fd);
    if (fds[1].fd >= 0) close(fds[1].fd);

    int status = 0;
    int rc;
    do {
        rc = waitpid(pid, &status, 0);
    } while (rc < 0 && errno == EINTR);

    untrack_child(pid);

    if (WIFEXITED(status))
        return WEXITSTATUS(status);
    return -1;
}

/* --------------------------------------------------------------------------
 * Non-blocking fork helper (for concurrent subprocesses)
 *
 * Forks argv[0..] and returns immediately with the child pid.  Caller gets
 * stdout and stderr read-end fds to poll.  Returns -1 on error.
 * -------------------------------------------------------------------------- */

static pid_t fork_cmd(char *const argv[], int *out_fd_p, int *err_fd_p)
{
    int stdout_pipe[2] = {-1, -1};
    int stderr_pipe[2] = {-1, -1};

    if (pipe(stdout_pipe) < 0 || pipe(stderr_pipe) < 0) {
        agent_warn("pipe() failed: %s", strerror(errno));
        if (stdout_pipe[0] >= 0) { close(stdout_pipe[0]); close(stdout_pipe[1]); }
        return -1;
    }

    pid_t pid = fork();
    if (pid < 0) {
        agent_warn("fork() failed: %s", strerror(errno));
        close(stdout_pipe[0]); close(stdout_pipe[1]);
        close(stderr_pipe[0]); close(stderr_pipe[1]);
        return -1;
    }

    if (pid == 0) {
        /* Child */
        close(stdout_pipe[0]);
        close(stderr_pipe[0]);
        dup2(stdout_pipe[1], STDOUT_FILENO);
        dup2(stderr_pipe[1], STDERR_FILENO);
        close(stdout_pipe[1]);
        close(stderr_pipe[1]);
        close(STDIN_FILENO);
        execvp(argv[0], argv);
        _exit(127);
    }

    /* Parent */
    close(stdout_pipe[1]);
    close(stderr_pipe[1]);
    track_child(pid);

    *out_fd_p = stdout_pipe[0];
    *err_fd_p = stderr_pipe[0];
    return pid;
}

/* --------------------------------------------------------------------------
 * String helpers
 * -------------------------------------------------------------------------- */

static int str_contains_lower(const char *haystack, size_t len, const char *needle)
{
    size_t nlen = strlen(needle);
    if (nlen > len) return 0;
    for (size_t i = 0; i <= len - nlen; i++) {
        size_t j;
        for (j = 0; j < nlen; j++) {
            char c = haystack[i + j];
            if (c >= 'A' && c <= 'Z') c += 32;
            if (c != needle[j]) break;
        }
        if (j == nlen) return 1;
    }
    return 0;
}

static int is_stat_only(const char *event)
{
    for (int i = 0; STAT_ONLY_EVENTS[i]; i++)
        if (strcmp(event, STAT_ONLY_EVENTS[i]) == 0) return 1;
    return 0;
}

/* --------------------------------------------------------------------------
 * Platform detection
 * -------------------------------------------------------------------------- */

struct platform_info {
    char arch[128];
    char kernel[128];
    char perf_version[128];
    int  perf_event_paranoid;
};

static void detect_platform(struct platform_info *info)
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

struct capabilities {
    char  *record_events[MAX_EVENTS];
    int    record_event_count;
    char  *stat_only_events[MAX_EVENTS];
    int    stat_only_event_count;
    char  *all_events[MAX_EVENTS * 2];
    int    all_event_count;
    char   callgraph[8];        /* "fp", "dwarf", "lbr", or "" */
    char   script_fields[128];  /* SCRIPT_FIELDS or "" */
};

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

static void probe_capabilities(int pid, struct capabilities *caps)
{
    memset(caps, 0, sizeof(*caps));

    agent_log("Probing perf event support...");
    for (int i = 0; CANDIDATE_EVENTS[i]; i++) {
        if (g_shutdown) return;
        const char *ev = CANDIDATE_EVENTS[i];
        if (event_works(ev, pid)) {
            char *dup = strdup(ev);
            if (!dup) continue;
            if (is_stat_only(ev)) {
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
            agent_log("  %s: supported", ev);
        } else {
            agent_log("  %s: not available, skipping", ev);
        }
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

static void free_capabilities(struct capabilities *caps)
{
    for (int i = 0; i < caps->record_event_count; i++)
        free(caps->record_events[i]);
    for (int i = 0; i < caps->stat_only_event_count; i++)
        free(caps->stat_only_events[i]);
    /* all_events are aliases — don't double-free */
}

/* --------------------------------------------------------------------------
 * TCP connection with reconnect
 * -------------------------------------------------------------------------- */

struct tcp_conn {
    int    fd;
    char   host[256];
    int    port;
};

static void tcp_close(struct tcp_conn *c)
{
    if (c->fd >= 0) {
        close(c->fd);
        c->fd = -1;
    }
}

static int tcp_connect(struct tcp_conn *c)
{
    double delay = 1.0;

    while (!g_shutdown) {
        int s = socket(AF_INET, SOCK_STREAM, 0);
        if (s < 0) {
            agent_warn("socket() failed: %s", strerror(errno));
            return -1;
        }

        struct sockaddr_in addr;
        memset(&addr, 0, sizeof(addr));
        addr.sin_family = AF_INET;
        addr.sin_port = htons((uint16_t)c->port);
        if (inet_pton(AF_INET, c->host, &addr.sin_addr) != 1) {
            agent_warn("Invalid server address: %s", c->host);
            close(s);
            return -1;
        }

        /* Set connect timeout via SO_SNDTIMEO */
        struct timeval tv = { .tv_sec = 30, .tv_usec = 0 };
        setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

        if (connect(s, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
            c->fd = s;
            agent_log("Connected to %s:%d", c->host, c->port);
            return 0;
        }

        agent_log("Connection failed (%s), retrying in %.0fs...",
                  strerror(errno), delay);
        close(s);

        /* Sleep with shutdown check */
        struct timespec ts;
        ts.tv_sec  = (time_t)delay;
        ts.tv_nsec = (long)((delay - (double)ts.tv_sec) * 1e9);
        nanosleep(&ts, NULL);

        if (delay < RECONNECT_MAX) delay *= 2;
        if (delay > RECONNECT_MAX) delay = RECONNECT_MAX;
    }
    return -1;
}

static int tcp_send_all(int fd, const void *data, size_t len)
{
    const char *p = (const char *)data;
    size_t remaining = len;
    while (remaining > 0) {
        ssize_t n = send(fd, p, remaining, MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        p += n;
        remaining -= (size_t)n;
    }
    return 0;
}

static int tcp_send_frame(struct tcp_conn *c, const void *payload,
                          size_t payload_len, uint8_t flag)
{
    /* 5-byte header: 4-byte big-endian length + 1-byte compression flag */
    uint32_t len_be = htonl((uint32_t)payload_len);

    if (tcp_send_all(c->fd, &len_be, 4) < 0 ||
        tcp_send_all(c->fd, &flag, 1) < 0 ||
        tcp_send_all(c->fd, payload, payload_len) < 0)
        return -1;
    return 0;
}

static int tcp_send_with_retry(struct tcp_conn *c, const void *payload,
                               size_t payload_len, uint8_t flag)
{
    if (tcp_send_frame(c, payload, payload_len, flag) == 0)
        return 0;

    agent_log("Send failed (%s), reconnecting...", strerror(errno));
    tcp_close(c);
    if (tcp_connect(c) < 0)
        return -1;

    if (tcp_send_frame(c, payload, payload_len, flag) == 0)
        return 0;

    agent_log("Retry send also failed (%s)", strerror(errno));
    tcp_close(c);
    return -1;
}

/* --------------------------------------------------------------------------
 * Compression (in-process zstd, no subprocess)
 * -------------------------------------------------------------------------- */

static int compress_data(const char *input, size_t input_len,
                         char **output, size_t *output_len)
{
    size_t bound = ZSTD_compressBound(input_len);
    char *cbuf = malloc(bound);
    if (!cbuf) return -1;

    size_t csize = ZSTD_compress(cbuf, bound, input, input_len, ZSTD_LEVEL);
    if (ZSTD_isError(csize)) {
        agent_warn("Compression failed: %s", ZSTD_getErrorName(csize));
        free(cbuf);
        return -1;
    }

    *output = cbuf;
    *output_len = csize;
    return 0;
}

/* --------------------------------------------------------------------------
 * Collection: one round of perf record + perf stat + perf script
 * -------------------------------------------------------------------------- */

static char *collect_one_round(const struct capabilities *caps,
                               int pid, int frequency, int duration,
                               size_t *out_len)
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

    /* Build record events string: "cycles,instructions,..." */
    char rec_events[512];
    rec_events[0] = '\0';
    for (int i = 0; i < caps->record_event_count; i++) {
        if (i > 0) strncat(rec_events, ",", sizeof(rec_events) - strlen(rec_events) - 1);
        strncat(rec_events, caps->record_events[i],
                sizeof(rec_events) - strlen(rec_events) - 1);
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

    /* Run perf script */
    char *argv_script[MAX_CMD_ARGS];
    int sci = 0;
    argv_script[sci++] = PERF; argv_script[sci++] = "script";
    if (caps->script_fields[0]) {
        argv_script[sci++] = "-F";
        argv_script[sci++] = (char *)caps->script_fields;
    }
    argv_script[sci++] = "-i"; argv_script[sci++] = tmpl;
    argv_script[sci] = NULL;

    struct buf script_out, script_err;
    buf_init(&script_out); buf_init(&script_err);
    int rc_script = run_cmd(argv_script, &script_out, &script_err, timeout);

    if (rc_script != 0) {
        char msg[256] = "";
        if (script_err.len > 0) {
            size_t cplen = script_err.len < sizeof(msg) - 1
                         ? script_err.len : sizeof(msg) - 1;
            memcpy(msg, script_err.data, cplen);
            msg[cplen] = '\0';
        }
        agent_log("perf script failed (rc=%d): %s", rc_script, msg);
        buf_free(&script_out); buf_free(&script_err);
        buf_free(&rec_err); buf_free(&stat_err);
        unlink(tmpl);
        return NULL;
    }
    buf_free(&script_err);

    /* Combine: script output + stat marker + stat stderr */
    size_t total = script_out.len;
    int have_stat = (rc_stat == 0 && stat_err.len > 0);
    if (have_stat)
        total += 1 + strlen("\n### PERF_STAT ###\n") + stat_err.len;

    char *combined = malloc(total + 1);
    if (!combined) {
        buf_free(&script_out); buf_free(&rec_err); buf_free(&stat_err);
        unlink(tmpl);
        return NULL;
    }

    memcpy(combined, script_out.data, script_out.len);
    size_t pos = script_out.len;

    if (have_stat) {
        const char *marker = "\n### PERF_STAT ###\n";
        size_t mlen = strlen(marker);
        memcpy(combined + pos, marker, mlen);
        pos += mlen;
        memcpy(combined + pos, stat_err.data, stat_err.len);
        pos += stat_err.len;
    }
    combined[pos] = '\0';

    *out_len = pos;

    buf_free(&script_out);
    buf_free(&rec_err);
    buf_free(&stat_err);
    unlink(tmpl);

    return combined;
}

/* --------------------------------------------------------------------------
 * Process liveness check
 * -------------------------------------------------------------------------- */

static int process_exists(int pid)
{
    if (kill(pid, 0) == 0) return 1;
    if (errno == EPERM)    return 1;  /* exists but we lack permission */
    return 0;
}

/* --------------------------------------------------------------------------
 * Usage / help
 * -------------------------------------------------------------------------- */

static void print_usage(const char *prog)
{
    fprintf(stderr,
        "usage: %s --pid PID [--server HOST] [--port PORT]\n"
        "                    [--frequency HZ] [--duration SECS]\n"
        "\n"
        "PerfLens Device Agent (C)\n"
        "\n"
        "  --pid PID         Process to profile (required)\n"
        "  --server HOST     Server IP to stream data to (omit for stdout mode)\n"
        "  --port PORT       Server TCP port (default: %d)\n"
        "  --frequency HZ    Sampling frequency in Hz (default: %d)\n"
        "  --duration SECS   Duration of each collection in seconds (default: %d)\n"
        "  --help            Show this help message\n",
        prog, DEFAULT_PORT, DEFAULT_FREQ, DEFAULT_DURATION);
}

/* --------------------------------------------------------------------------
 * Main
 * -------------------------------------------------------------------------- */

int main(int argc, char *argv[])
{
    int pid = -1;
    char *server = NULL;
    int port = DEFAULT_PORT;
    int frequency = DEFAULT_FREQ;
    int duration = DEFAULT_DURATION;

    static struct option long_opts[] = {
        {"pid",       required_argument, NULL, 'p'},
        {"server",    required_argument, NULL, 's'},
        {"port",      required_argument, NULL, 'P'},
        {"frequency", required_argument, NULL, 'f'},
        {"duration",  required_argument, NULL, 'd'},
        {"help",      no_argument,       NULL, 'h'},
        {NULL, 0, NULL, 0}
    };

    int opt;
    while ((opt = getopt_long(argc, argv, "p:s:P:f:d:h", long_opts, NULL)) != -1) {
        switch (opt) {
        case 'p': pid       = atoi(optarg); break;
        case 's': server    = optarg;       break;
        case 'P': port      = atoi(optarg); break;
        case 'f': frequency = atoi(optarg); break;
        case 'd': duration  = atoi(optarg); break;
        case 'h': print_usage(argv[0]); return 0;
        default:  print_usage(argv[0]); return 1;
        }
    }

    if (pid < 0) {
        fprintf(stderr, "Error: --pid is required\n\n");
        print_usage(argv[0]);
        return 1;
    }

    /* Verify process exists */
    if (!process_exists(pid)) {
        agent_log("Error: process %d not found", pid);
        return 1;
    }

    install_signal_handlers();

    /* Platform detection */
    struct platform_info pinfo;
    detect_platform(&pinfo);

    /* Capability probing */
    struct capabilities caps;
    probe_capabilities(pid, &caps);

    if (g_shutdown) {
        free_capabilities(&caps);
        return 0;
    }

    /* --- Stdout mode --- */
    if (!server) {
        agent_log("Collecting perf data for PID %d (stdout mode)", pid);
        size_t out_len = 0;
        char *output = collect_one_round(&caps, pid, frequency, duration, &out_len);
        if (output && out_len > 0) {
            fwrite(output, 1, out_len, stdout);
            fflush(stdout);
            agent_log("Done. Output %zu bytes.", out_len);
            free(output);
        } else {
            agent_log("No data collected.");
            free_capabilities(&caps);
            return 1;
        }
        free_capabilities(&caps);
        return 0;
    }

    /* --- TCP streaming mode --- */
    struct tcp_conn conn;
    conn.fd = -1;
    snprintf(conn.host, sizeof(conn.host), "%s", server);
    conn.port = port;

    agent_log("Connecting to %s:%d...", conn.host, conn.port);
    if (tcp_connect(&conn) < 0) {
        free_capabilities(&caps);
        return 1;
    }

    /* Collection loop */
    int round_num = 0;
    while (!g_shutdown) {
        round_num++;

        if (!process_exists(pid)) {
            agent_log("Process %d has exited", pid);
            break;
        }

        agent_log("Round %d: collecting (%ds)...", round_num, duration);
        size_t raw_len = 0;
        char *raw = collect_one_round(&caps, pid, frequency, duration, &raw_len);

        if (g_shutdown) { free(raw); break; }

        if (!raw || raw_len == 0) {
            agent_log("Round %d: no data, retrying...", round_num);
            free(raw);
            sleep(1);
            continue;
        }

        /* Compress */
        char *payload = NULL;
        size_t payload_len = 0;
        uint8_t flag = 0;

        char *compressed = NULL;
        size_t compressed_len = 0;
        if (compress_data(raw, raw_len, &compressed, &compressed_len) == 0) {
            payload = compressed;
            payload_len = compressed_len;
            flag = 1;
            double ratio = (compressed_len > 0) ? (double)raw_len / (double)compressed_len : 0;
            agent_log("Round %d: perf script %zu bytes, compressed %zu bytes (ratio %.1fx)",
                      round_num, raw_len, compressed_len, ratio);
        } else {
            payload = raw;
            payload_len = raw_len;
            flag = 0;
            agent_log("Round %d: perf script %zu bytes (uncompressed)", round_num, raw_len);
        }

        /* Send */
        if (tcp_send_with_retry(&conn, payload, payload_len, flag) == 0) {
            agent_log("Round %d: sent successfully", round_num);
        } else {
            agent_log("Round %d: send failed after reconnect: %s",
                      round_num, strerror(errno));
            if (g_shutdown) {
                free(raw);
                if (compressed) free(compressed);
                break;
            }
            sleep(1);
        }

        free(raw);
        if (compressed) free(compressed);
    }

    /* Cleanup */
    agent_log("Shutting down.");
    tcp_close(&conn);
    free_capabilities(&caps);
    return 0;
}
