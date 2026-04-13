/*
 * PerfLens Device Agent — C implementation
 *
 * Functionally identical to agent/perflens_agent.py, compiles to a single
 * statically linked binary with zero runtime dependencies on the target.
 *
 * Usage:
 *   perflens-agent --listen [--port PORT]
 *   perflens-agent --server HOST [--port PORT]
 *   perflens-agent --output FILE --pid PID [options]
 *
 * Build:
 *   make                              # native build
 *   make CROSS=aarch64-linux-gnu-     # cross-compile for ARM64
 *
 * Architecture:
 *   1. Platform detection (uname, perf_event_paranoid)
 *   2. Interactive protocol: hello handshake + bidirectional commands
 *   3. Server-driven profiling: start/stop/pause/resume via commands
 *   4. Collection: perf record + perf stat -> perf script -> compress -> send
 *   5. TCP wire protocol: 5-byte header (4B big-endian length + 1B flag)
 *   6. Daemon behavior: --listen re-accepts, --server reconnects with backoff
 *   7. Signal handling: SIGINT/SIGTERM -> graceful shutdown
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
#include <dirent.h>
#include <errno.h>
#include <getopt.h>
#include <poll.h>
#include <pthread.h>
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

/* Wire protocol flags (5-byte header: 4-byte length + 1-byte flag) */
#define FLAG_DATA_RAW     0   /* agent -> server: raw perf data */
#define FLAG_DATA_ZSTD    1   /* agent -> server: zstd-compressed perf data */
#define FLAG_CMD_REQUEST  2   /* server -> agent: JSON command */
#define FLAG_CMD_RESPONSE 3   /* agent -> server: JSON response */

/* Agent states */
#define AGENT_IDLE       0
#define AGENT_PROFILING  1
#define AGENT_PAUSED     2

/* Process list limits */
#define MAX_PROCS        4096
#define MAX_PROC_RESULT  200

/* JSON response buffer */
#define JSON_BUF_SIZE    (128 * 1024)

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
 * Forward declarations
 * -------------------------------------------------------------------------- */

struct agent_state;
static void *collection_thread_fn(void *arg);

/* --------------------------------------------------------------------------
 * Globals
 * -------------------------------------------------------------------------- */

static volatile sig_atomic_t g_shutdown = 0;
static volatile pid_t g_child_pids[8];
static volatile int g_child_count = 0;
static struct agent_state *g_agent = NULL;  /* for signal handler */
static volatile int g_agent_sock_fd = -1;   /* mirror of agent sock_fd for signal handler */

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
    /* Unblock recv thread by shutting down socket */
    if (g_agent_sock_fd >= 0)
        shutdown(g_agent_sock_fd, SHUT_RDWR);
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

/* Block SIGINT/SIGTERM in worker threads — only main thread handles signals */
static void block_signals_in_thread(void)
{
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGINT);
    sigaddset(&mask, SIGTERM);
    pthread_sigmask(SIG_BLOCK, &mask, NULL);
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
 * Minimal JSON helpers
 *
 * Sufficient for the well-defined PerfLens wire protocol. Not a general
 * JSON parser — only handles the command/response structures used here.
 * -------------------------------------------------------------------------- */

/* Escape a string for JSON output. Returns bytes written (excluding NUL). */
static size_t json_escape(char *dst, size_t cap, const char *src)
{
    size_t pos = 0;
    for (; *src && pos + 2 < cap; src++) {
        switch (*src) {
        case '"':  dst[pos++] = '\\'; dst[pos++] = '"';  break;
        case '\\': dst[pos++] = '\\'; dst[pos++] = '\\'; break;
        case '\n': dst[pos++] = '\\'; dst[pos++] = 'n';  break;
        case '\r': dst[pos++] = '\\'; dst[pos++] = 'r';  break;
        case '\t': dst[pos++] = '\\'; dst[pos++] = 't';  break;
        default:
            if ((unsigned char)*src >= 0x20)
                dst[pos++] = *src;
            break;
        }
    }
    dst[pos] = '\0';
    return pos;
}

/* Find a JSON string value by key. Returns 0 on success, -1 if not found. */
static int json_get_str(const char *json, const char *key, char *buf, size_t buflen)
{
    char pattern[256];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(json, pattern);
    if (!p) return -1;

    p += strlen(pattern);
    while (*p == ' ' || *p == '\t' || *p == ':') p++;
    if (*p != '"') return -1;
    p++;

    size_t i = 0;
    while (*p && *p != '"' && i + 1 < buflen) {
        if (*p == '\\' && *(p + 1)) {
            p++;
            switch (*p) {
            case '"':  buf[i++] = '"';  break;
            case '\\': buf[i++] = '\\'; break;
            case 'n':  buf[i++] = '\n'; break;
            case 'r':  buf[i++] = '\r'; break;
            case 't':  buf[i++] = '\t'; break;
            default:   buf[i++] = *p;   break;
            }
        } else {
            buf[i++] = *p;
        }
        p++;
    }
    buf[i] = '\0';
    return 0;
}

/* Find a JSON integer value by key. Returns 0 on success, -1 if not found. */
static int json_get_int(const char *json, const char *key, int *out)
{
    char pattern[256];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(json, pattern);
    if (!p) return -1;

    p += strlen(pattern);
    while (*p == ' ' || *p == '\t' || *p == ':') p++;

    char *end;
    long val = strtol(p, &end, 10);
    if (end == p) return -1;
    *out = (int)val;
    return 0;
}

/* Find a nested JSON object by key. Returns pointer to '{' or NULL. */
static const char *json_find_object(const char *json, const char *key)
{
    char pattern[256];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(json, pattern);
    if (!p) return NULL;

    p += strlen(pattern);
    while (*p == ' ' || *p == '\t' || *p == ':') p++;
    if (*p != '{') return NULL;
    return p;
}

/* --------------------------------------------------------------------------
 * TCP helpers
 * -------------------------------------------------------------------------- */

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

static int tcp_send_frame(int fd, const void *payload,
                          size_t payload_len, uint8_t flag)
{
    /* 5-byte header: 4-byte big-endian length + 1-byte flag */
    uint32_t len_be = htonl((uint32_t)payload_len);

    if (tcp_send_all(fd, &len_be, 4) < 0 ||
        tcp_send_all(fd, &flag, 1) < 0 ||
        tcp_send_all(fd, payload, payload_len) < 0)
        return -1;
    return 0;
}

/* Receive exactly n bytes. Returns 0 on success, -1 on error/disconnect. */
static int tcp_recv_exactly(int fd, void *buf, size_t n)
{
    char *p = (char *)buf;
    size_t remaining = n;
    while (remaining > 0) {
        ssize_t r = recv(fd, p, remaining, 0);
        if (r < 0) {
            if (errno == EINTR && !g_shutdown) continue;
            return -1;
        }
        if (r == 0) return -1;  /* disconnect */
        p += r;
        remaining -= (size_t)r;
    }
    return 0;
}

/* Receive one frame. Caller must free *payload. Returns 0 on success. */
static int tcp_recv_frame(int fd, char **payload, uint32_t *out_len,
                          uint8_t *out_flag)
{
    uint8_t header[5];
    if (tcp_recv_exactly(fd, header, 5) < 0)
        return -1;

    uint32_t len;
    memcpy(&len, header, 4);
    len = ntohl(len);
    *out_flag = header[4];
    *out_len = len;

    if (len == 0) {
        *payload = NULL;
        return 0;
    }

    char *data = malloc(len + 1);
    if (!data) return -1;

    if (tcp_recv_exactly(fd, data, len) < 0) {
        free(data);
        return -1;
    }
    data[len] = '\0';
    *payload = data;
    return 0;
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
 * Process listing (for list_processes command)
 * -------------------------------------------------------------------------- */

struct proc_entry {
    int  pid;
    char comm[64];
    char cmdline[256];
    double cpu;
};

struct proc_snap {
    int pid;
    unsigned long ticks;
};

static unsigned long read_total_cpu(void)
{
    FILE *f = fopen("/proc/stat", "r");
    if (!f) return 0;

    char line[512];
    if (!fgets(line, sizeof(line), f)) { fclose(f); return 0; }
    fclose(f);

    unsigned long total = 0, val;
    char *p = line;
    if (strncmp(p, "cpu", 3) != 0) return 0;
    p += 3;
    while (*p) {
        while (*p == ' ') p++;
        if (*p == '\0' || *p == '\n') break;
        char *end;
        val = strtoul(p, &end, 10);
        if (end == p) break;
        total += val;
        p = end;
    }
    return total;
}

static int read_proc_ticks(int pid, unsigned long *ticks)
{
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/stat", pid);
    FILE *f = fopen(path, "r");
    if (!f) return -1;

    char line[1024];
    if (!fgets(line, sizeof(line), f)) { fclose(f); return -1; }
    fclose(f);

    /* Skip past (comm) which may contain spaces or parens */
    char *p = strrchr(line, ')');
    if (!p) return -1;
    p++;

    /* Now at field 3 (state). Need fields 14 (utime) and 15 (stime). */
    int field = 3;
    unsigned long utime = 0, stime = 0;
    while (*p) {
        while (*p == ' ') p++;
        if (field == 14) {
            utime = strtoul(p, NULL, 10);
        } else if (field == 15) {
            stime = strtoul(p, NULL, 10);
            break;
        }
        while (*p && *p != ' ') p++;
        field++;
    }

    *ticks = utime + stime;
    return 0;
}

static int cmp_proc_cpu(const void *a, const void *b)
{
    const struct proc_entry *pa = (const struct proc_entry *)a;
    const struct proc_entry *pb = (const struct proc_entry *)b;
    if (pb->cpu > pa->cpu) return 1;
    if (pb->cpu < pa->cpu) return -1;
    return 0;
}

static int do_list_processes(struct proc_entry *result, int max_results)
{
    struct proc_snap *snap1 = malloc(sizeof(struct proc_snap) * MAX_PROCS);
    if (!snap1) return 0;
    int snap1_count = 0;

    unsigned long total1 = read_total_cpu();

    DIR *d = opendir("/proc");
    if (!d) { free(snap1); return 0; }

    struct dirent *ent;
    while ((ent = readdir(d)) != NULL && snap1_count < MAX_PROCS) {
        char *end;
        int pid = (int)strtol(ent->d_name, &end, 10);
        if (*end != '\0' || pid <= 0) continue;

        unsigned long ticks;
        if (read_proc_ticks(pid, &ticks) == 0) {
            snap1[snap1_count].pid = pid;
            snap1[snap1_count].ticks = ticks;
            snap1_count++;
        }
    }
    closedir(d);

    usleep(500000);

    unsigned long total2 = read_total_cpu();
    unsigned long total_delta = total2 - total1;
    if (total_delta == 0) total_delta = 1;

    /* Collect ALL processes first, then sort and return top max_results */
    struct proc_entry *all = malloc(sizeof(struct proc_entry) * (size_t)snap1_count);
    if (!all) { free(snap1); return 0; }

    int count = 0;
    for (int i = 0; i < snap1_count; i++) {
        int pid = snap1[i].pid;
        unsigned long ticks2;
        if (read_proc_ticks(pid, &ticks2) < 0) continue;

        unsigned long delta = ticks2 - snap1[i].ticks;
        double cpu_pct = ((double)delta / (double)total_delta) * 100.0;

        struct proc_entry *e = &all[count];
        e->pid = pid;
        e->cpu = cpu_pct;

        char path[64];
        FILE *f;

        snprintf(path, sizeof(path), "/proc/%d/comm", pid);
        f = fopen(path, "r");
        if (f) {
            if (fgets(e->comm, sizeof(e->comm), f)) {
                char *nl = strchr(e->comm, '\n');
                if (nl) *nl = '\0';
            } else {
                strcpy(e->comm, "?");
            }
            fclose(f);
        } else {
            strcpy(e->comm, "?");
        }

        snprintf(path, sizeof(path), "/proc/%d/cmdline", pid);
        f = fopen(path, "r");
        if (f) {
            size_t n = fread(e->cmdline, 1, sizeof(e->cmdline) - 1, f);
            fclose(f);
            e->cmdline[n] = '\0';
            for (size_t j = 0; j < n; j++) {
                if (e->cmdline[j] == '\0') e->cmdline[j] = ' ';
            }
            while (n > 0 && e->cmdline[n - 1] == ' ')
                e->cmdline[--n] = '\0';
        } else {
            e->cmdline[0] = '\0';
        }

        count++;
    }

    free(snap1);
    qsort(all, (size_t)count, sizeof(struct proc_entry), cmp_proc_cpu);

    int ret = count < max_results ? count : max_results;
    memcpy(result, all, sizeof(struct proc_entry) * (size_t)ret);
    free(all);
    return ret;
}

/* --------------------------------------------------------------------------
 * Command queue (thread-safe, condition variable based)
 * -------------------------------------------------------------------------- */

struct cmd_entry {
    char *json;
    struct cmd_entry *next;
};

struct cmd_queue {
    struct cmd_entry *head;
    struct cmd_entry *tail;
    pthread_mutex_t lock;
    pthread_cond_t cond;
};

static void cmdq_init(struct cmd_queue *q)
{
    q->head = NULL;
    q->tail = NULL;
    pthread_mutex_init(&q->lock, NULL);
    pthread_cond_init(&q->cond, NULL);
}

static void cmdq_destroy(struct cmd_queue *q)
{
    struct cmd_entry *e = q->head;
    while (e) {
        struct cmd_entry *next = e->next;
        free(e->json);
        free(e);
        e = next;
    }
    pthread_mutex_destroy(&q->lock);
    pthread_cond_destroy(&q->cond);
}

static void cmdq_push(struct cmd_queue *q, const char *json)
{
    struct cmd_entry *e = malloc(sizeof(*e));
    if (!e) return;
    e->json = strdup(json);
    e->next = NULL;

    pthread_mutex_lock(&q->lock);
    if (q->tail) {
        q->tail->next = e;
    } else {
        q->head = e;
    }
    q->tail = e;
    pthread_cond_signal(&q->cond);
    pthread_mutex_unlock(&q->lock);
}

/* Pop with timeout (ms). Returns JSON string (caller frees) or NULL. */
static char *cmdq_pop(struct cmd_queue *q, int timeout_ms)
{
    pthread_mutex_lock(&q->lock);

    while (!q->head) {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        ts.tv_sec += timeout_ms / 1000;
        ts.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
        if (ts.tv_nsec >= 1000000000L) {
            ts.tv_sec++;
            ts.tv_nsec -= 1000000000L;
        }

        int rc = pthread_cond_timedwait(&q->cond, &q->lock, &ts);
        if (rc == ETIMEDOUT || !q->head) {
            pthread_mutex_unlock(&q->lock);
            return NULL;
        }
    }

    struct cmd_entry *e = q->head;
    q->head = e->next;
    if (!q->head) q->tail = NULL;

    pthread_mutex_unlock(&q->lock);

    char *json = e->json;
    free(e);
    return json;
}

static void cmdq_drain(struct cmd_queue *q)
{
    pthread_mutex_lock(&q->lock);
    struct cmd_entry *e = q->head;
    while (e) {
        struct cmd_entry *next = e->next;
        free(e->json);
        free(e);
        e = next;
    }
    q->head = NULL;
    q->tail = NULL;
    pthread_mutex_unlock(&q->lock);
}

/* --------------------------------------------------------------------------
 * Agent state
 * -------------------------------------------------------------------------- */

struct agent_state {
    /* Socket (protected by sock_lock) */
    int sock_fd;
    pthread_mutex_t sock_lock;

    /* Agent state (protected by state_lock) */
    int state;
    pthread_mutex_t state_lock;

    /* Config */
    int pid;
    int frequency;
    int duration;

    /* Probed state */
    struct platform_info platform;
    struct capabilities *caps;

    /* Collection thread */
    pthread_t collect_thread;
    int collect_thread_active;
    volatile int collect_stop;

    /* Per-session disconnect signal */
    volatile int session_done;

    /* Command queue */
    struct cmd_queue cmdq;
};

static void agent_state_init(struct agent_state *a)
{
    a->sock_fd = -1;
    pthread_mutex_init(&a->sock_lock, NULL);
    a->state = AGENT_IDLE;
    pthread_mutex_init(&a->state_lock, NULL);
    a->pid = -1;
    a->frequency = DEFAULT_FREQ;
    a->duration = DEFAULT_DURATION;
    memset(&a->platform, 0, sizeof(a->platform));
    a->caps = NULL;
    a->collect_thread_active = 0;
    a->collect_stop = 0;
    a->session_done = 0;
    cmdq_init(&a->cmdq);
}

/* --------------------------------------------------------------------------
 * Send helpers (thread-safe via sock_lock)
 * -------------------------------------------------------------------------- */

static int agent_send_frame(struct agent_state *a, const void *payload,
                            size_t len, uint8_t flag)
{
    pthread_mutex_lock(&a->sock_lock);
    int rc = tcp_send_frame(a->sock_fd, payload, len, flag);
    pthread_mutex_unlock(&a->sock_lock);
    return rc;
}

static int agent_send_response(struct agent_state *a, const char *json)
{
    return agent_send_frame(a, json, strlen(json), FLAG_CMD_RESPONSE);
}

static int agent_send_data(struct agent_state *a, const void *data,
                           size_t len, uint8_t flag)
{
    return agent_send_frame(a, data, len, flag);
}

/* --------------------------------------------------------------------------
 * Receiver thread
 * -------------------------------------------------------------------------- */

static void *recv_thread_fn(void *arg)
{
    struct agent_state *a = (struct agent_state *)arg;
    block_signals_in_thread();

    while (!g_shutdown && !a->session_done) {
        char *payload = NULL;
        uint32_t len = 0;
        uint8_t flag = 0;

        if (tcp_recv_frame(a->sock_fd, &payload, &len, &flag) < 0) {
            if (!g_shutdown)
                agent_log("Server disconnected");
            a->session_done = 1;
            break;
        }

        if (len == 0) {
            free(payload);
            continue;
        }

        if (flag == FLAG_CMD_REQUEST) {
            cmdq_push(&a->cmdq, payload);
        } else {
            agent_log("Unexpected flag %d from server", flag);
        }

        free(payload);
    }

    return NULL;
}

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
            "],\"callgraph_method\":\"%s\"}", a->caps->callgraph);
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

    for (int i = 0; i < count && (size_t)n + 512 < JSON_BUF_SIZE; i++) {
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
        "],\"callgraph_method\":\"%s\"}", caps->callgraph);
    agent_send_response(a, resp);
}

static void cmd_start(struct agent_state *a, const char *cmd_id,
                      const char *json)
{
    pthread_mutex_lock(&a->state_lock);
    if (a->state == AGENT_PROFILING) {
        pthread_mutex_unlock(&a->state_lock);
        char resp[256];
        snprintf(resp, sizeof(resp),
            "{\"id\":\"%s\",\"ok\":false,\"error\":\"already profiling\"}",
            cmd_id);
        agent_send_response(a, resp);
        return;
    }
    pthread_mutex_unlock(&a->state_lock);

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
    for (int i = 0; i < a->caps->record_event_count; i++) {
        if (i > 0) n += snprintf(resp + n, sizeof(resp) - (size_t)n, ",");
        n += snprintf(resp + n, sizeof(resp) - (size_t)n,
            "\"%s\"", a->caps->record_events[i]);
    }
    snprintf(resp + n, sizeof(resp) - (size_t)n,
        "],\"callgraph\":\"%s\"}", a->caps->callgraph);
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
    for (int i = 0; i < g_child_count; i++) {
        if (g_child_pids[i] > 0)
            kill(g_child_pids[i], SIGTERM);
    }

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
    for (int i = 0; i < g_child_count; i++) {
        if (g_child_pids[i] > 0)
            kill(g_child_pids[i], SIGTERM);
    }

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

/* --------------------------------------------------------------------------
 * Command dispatch
 * -------------------------------------------------------------------------- */

typedef void (*cmd_handler_fn)(struct agent_state *, const char *, const char *);

struct cmd_dispatch_entry {
    const char   *name;
    cmd_handler_fn handler;
};

static const struct cmd_dispatch_entry CMD_TABLE[] = {
    { "ping",            cmd_ping },
    { "status",          cmd_status },
    { "list_processes",  cmd_list_processes },
    { "verify_pid",      cmd_verify_pid },
    { "verify_perf",     cmd_verify_perf },
    { "reprobe",         cmd_reprobe },
    { "start",           cmd_start },
    { "stop",            cmd_stop },
    { "pause",           cmd_pause },
    { "resume",          cmd_resume },
    { "configure",       cmd_configure },
    { NULL, NULL },
};

static void dispatch_command(struct agent_state *a, const char *json)
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

/* --------------------------------------------------------------------------
 * Collection loop thread
 * -------------------------------------------------------------------------- */

static void *collection_thread_fn(void *arg)
{
    struct agent_state *a = (struct agent_state *)arg;
    block_signals_in_thread();

    int round_num = 0;

    while (!a->collect_stop && !g_shutdown && !a->session_done) {
        int st;
        pthread_mutex_lock(&a->state_lock);
        st = a->state;
        pthread_mutex_unlock(&a->state_lock);

        if (st == AGENT_PAUSED) {
            struct timespec ns = { .tv_sec = 1, .tv_nsec = 0 };
            nanosleep(&ns, NULL);
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

        size_t raw_len = 0;
        char *raw = collect_one_round(a->caps, a->pid, a->frequency,
                                      a->duration, &raw_len);

        if (a->collect_stop || g_shutdown || a->session_done) {
            free(raw);
            break;
        }

        if (!raw || raw_len == 0) {
            agent_log("Round %d: no data", round_num);
            free(raw);
            struct timespec ns = { .tv_sec = 1, .tv_nsec = 0 };
            nanosleep(&ns, NULL);
            continue;
        }

        /* Compress */
        char *compressed = NULL;
        size_t compressed_len = 0;
        uint8_t flag;
        const void *payload;
        size_t payload_len;

        if (compress_data(raw, raw_len, &compressed, &compressed_len) == 0) {
            payload = compressed;
            payload_len = compressed_len;
            flag = FLAG_DATA_ZSTD;
            double ratio = compressed_len > 0
                ? (double)raw_len / (double)compressed_len : 0;
            agent_log("Round %d: perf script %zu bytes, "
                      "compressed %zu bytes (ratio %.1fx)",
                      round_num, raw_len, compressed_len, ratio);
        } else {
            payload = raw;
            payload_len = raw_len;
            flag = FLAG_DATA_RAW;
            agent_log("Round %d: perf script %zu bytes (uncompressed)",
                      round_num, raw_len);
        }

        /* Send */
        if (agent_send_data(a, payload, payload_len, flag) == 0) {
            agent_log("Round %d: sent successfully", round_num);
        } else {
            agent_log("Round %d: send failed: %s", round_num, strerror(errno));
            free(raw);
            free(compressed);
            break;
        }

        free(raw);
        free(compressed);
    }

    pthread_mutex_lock(&a->state_lock);
    if (a->state == AGENT_PROFILING || a->state == AGENT_PAUSED)
        a->state = AGENT_IDLE;
    pthread_mutex_unlock(&a->state_lock);

    agent_log("Collection loop ended");
    return NULL;
}

/* --------------------------------------------------------------------------
 * Interactive session (shared by --listen and --server modes)
 * -------------------------------------------------------------------------- */

static void run_interactive(struct agent_state *a)
{
    /* Fresh per-session state */
    a->session_done = 0;
    a->collect_stop = 0;

    /* Send hello handshake (agent always sends hello first) */
    char esc_pv[256];
    json_escape(esc_pv, sizeof(esc_pv), a->platform.perf_version);

    char hello[1024];
    snprintf(hello, sizeof(hello),
        "{\"type\":\"hello\",\"version\":1,\"agent\":\"perflens\","
        "\"platform\":{\"arch\":\"%s\",\"kernel\":\"%s\","
        "\"perf_version\":\"%s\",\"perf_event_paranoid\":%d}}",
        a->platform.arch, a->platform.kernel,
        esc_pv, a->platform.perf_event_paranoid);

    if (agent_send_response(a, hello) < 0) {
        agent_log("Failed to send hello: %s", strerror(errno));
        return;
    }

    /* Start receiver thread */
    pthread_t recv_tid;
    if (pthread_create(&recv_tid, NULL, recv_thread_fn, a) != 0) {
        agent_warn("Failed to create recv thread");
        return;
    }

    /* Command processing loop */
    while (!g_shutdown && !a->session_done) {
        char *json = cmdq_pop(&a->cmdq, 1000);
        if (!json) continue;

        dispatch_command(a, json);
        free(json);
    }

    /* --- Cleanup session --- */
    agent_log("Ending interactive session");

    /* Stop collection */
    a->collect_stop = 1;
    for (int i = 0; i < g_child_count; i++) {
        if (g_child_pids[i] > 0)
            kill(g_child_pids[i], SIGTERM);
    }
    if (a->collect_thread_active) {
        pthread_join(a->collect_thread, NULL);
        a->collect_thread_active = 0;
    }

    /* Close socket (wakes recv thread if blocked in recv) */
    if (a->sock_fd >= 0) {
        close(a->sock_fd);
        a->sock_fd = -1;
        g_agent_sock_fd = -1;
    }

    /* Wait for recv thread */
    pthread_join(recv_tid, NULL);

    /* Reset state */
    pthread_mutex_lock(&a->state_lock);
    a->state = AGENT_IDLE;
    pthread_mutex_unlock(&a->state_lock);

    /* Drain command queue */
    cmdq_drain(&a->cmdq);
}

/* --------------------------------------------------------------------------
 * Local IP helper (for listen mode display)
 * -------------------------------------------------------------------------- */

static void get_local_ip(char *buf, size_t buflen)
{
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) { snprintf(buf, buflen, "127.0.0.1"); return; }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(80);
    inet_pton(AF_INET, "8.8.8.8", &addr.sin_addr);

    if (connect(s, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(s);
        snprintf(buf, buflen, "127.0.0.1");
        return;
    }

    struct sockaddr_in local;
    socklen_t len = sizeof(local);
    getsockname(s, (struct sockaddr *)&local, &len);
    close(s);

    inet_ntop(AF_INET, &local.sin_addr, buf, (socklen_t)buflen);
}

/* --------------------------------------------------------------------------
 * Run modes
 * -------------------------------------------------------------------------- */

static void run_listen(struct agent_state *a, int port)
{
    detect_platform(&a->platform);

    int listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd < 0) {
        agent_log("socket() failed: %s", strerror(errno));
        return;
    }

    int reuse = 1;
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons((uint16_t)port);

    if (bind(listen_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        agent_log("bind() failed: %s", strerror(errno));
        close(listen_fd);
        return;
    }

    if (listen(listen_fd, 1) < 0) {
        agent_log("listen() failed: %s", strerror(errno));
        close(listen_fd);
        return;
    }

    agent_log("Listening on port %d", port);
    agent_log("Waiting for server connection...");

    char local_ip[INET_ADDRSTRLEN];
    get_local_ip(local_ip, sizeof(local_ip));
    agent_log("  Connect from server: %s:%d", local_ip, port);

    while (!g_shutdown) {
        /* Accept with poll timeout for shutdown check */
        struct pollfd pfd;
        pfd.fd = listen_fd;
        pfd.events = POLLIN;
        int ret = poll(&pfd, 1, 2000);
        if (ret < 0) {
            if (errno == EINTR) continue;
            break;
        }
        if (ret == 0) continue;

        struct sockaddr_in client_addr;
        socklen_t client_len = sizeof(client_addr);
        int conn_fd = accept(listen_fd, (struct sockaddr *)&client_addr,
                             &client_len);
        if (conn_fd < 0) {
            if (errno == EINTR) continue;
            agent_warn("accept() failed: %s", strerror(errno));
            continue;
        }

        char client_ip[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &client_addr.sin_addr, client_ip,
                  sizeof(client_ip));
        agent_log("Server connected from %s:%d",
                  client_ip, ntohs(client_addr.sin_port));

        a->sock_fd = conn_fd;
        g_agent_sock_fd = conn_fd;
        run_interactive(a);

        if (!g_shutdown)
            agent_log("Session ended, waiting for new connection...");
    }

    close(listen_fd);
}

static void run_connect(struct agent_state *a, const char *host, int port)
{
    detect_platform(&a->platform);

    while (!g_shutdown) {
        /* Connect with exponential backoff */
        double delay = 1.0;
        int sock = -1;

        while (!g_shutdown) {
            sock = socket(AF_INET, SOCK_STREAM, 0);
            if (sock < 0) {
                agent_warn("socket() failed: %s", strerror(errno));
                return;
            }

            struct sockaddr_in addr;
            memset(&addr, 0, sizeof(addr));
            addr.sin_family = AF_INET;
            addr.sin_port = htons((uint16_t)port);
            if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
                agent_warn("Invalid server address: %s", host);
                close(sock);
                return;
            }

            /* Connect timeout */
            struct timeval tv;
            tv.tv_sec = 30;
            tv.tv_usec = 0;
            setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

            if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) == 0) {
                agent_log("Connected to %s:%d", host, port);
                break;
            }

            agent_log("Connection failed (%s), retrying in %.0fs...",
                      strerror(errno), delay);
            close(sock);
            sock = -1;

            /* Sleep with shutdown check */
            struct timespec ns;
            ns.tv_sec = (time_t)delay;
            ns.tv_nsec = (long)((delay - (double)ns.tv_sec) * 1e9);
            nanosleep(&ns, NULL);

            if (delay < RECONNECT_MAX) delay *= 2;
            if (delay > RECONNECT_MAX) delay = RECONNECT_MAX;
        }

        if (sock < 0) continue;

        /* Clear connect timeout for recv/send during session */
        struct timeval no_tv;
        no_tv.tv_sec = 0;
        no_tv.tv_usec = 0;
        setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &no_tv, sizeof(no_tv));

        a->sock_fd = sock;
        g_agent_sock_fd = sock;
        run_interactive(a);

        if (!g_shutdown)
            agent_log("Session ended, reconnecting...");
    }
}

/* --------------------------------------------------------------------------
 * Usage / help
 * -------------------------------------------------------------------------- */

static void print_usage(const char *prog)
{
    fprintf(stderr,
        "usage: %s --listen [--port PORT]\n"
        "       %s --server HOST [--port PORT]\n"
        "       %s --output FILE --pid PID [options]\n"
        "\n"
        "PerfLens Device Agent (C)\n"
        "\n"
        "Modes:\n"
        "  --listen          Listen for server connections (daemon)\n"
        "  --server HOST     Connect to server (daemon)\n"
        "  --output FILE     Headless: collect once, write to file ('-' for stdout)\n"
        "\n"
        "Options:\n"
        "  --pid PID         Process to profile (required for --output)\n"
        "  --port PORT       TCP port (default: %d)\n"
        "  --frequency HZ    Sampling frequency in Hz (default: %d)\n"
        "  --duration SECS   Duration of each collection in seconds (default: %d)\n"
        "  --help            Show this help message\n",
        prog, prog, prog, DEFAULT_PORT, DEFAULT_FREQ, DEFAULT_DURATION);
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
    int listen_mode = 0;
    char *output = NULL;

    static struct option long_opts[] = {
        {"pid",       required_argument, NULL, 'p'},
        {"server",    required_argument, NULL, 's'},
        {"port",      required_argument, NULL, 'P'},
        {"frequency", required_argument, NULL, 'f'},
        {"duration",  required_argument, NULL, 'd'},
        {"listen",    no_argument,       NULL, 'l'},
        {"output",    required_argument, NULL, 'o'},
        {"help",      no_argument,       NULL, 'h'},
        {NULL, 0, NULL, 0}
    };

    int opt;
    while ((opt = getopt_long(argc, argv, "p:s:P:f:d:lo:h",
                              long_opts, NULL)) != -1) {
        switch (opt) {
        case 'p': pid       = atoi(optarg); break;
        case 's': server    = optarg;       break;
        case 'P': port      = atoi(optarg); break;
        case 'f': frequency = atoi(optarg); break;
        case 'd': duration  = atoi(optarg); break;
        case 'l': listen_mode = 1;          break;
        case 'o': output    = optarg;       break;
        case 'h': print_usage(argv[0]); return 0;
        default:  print_usage(argv[0]); return 1;
        }
    }

    install_signal_handlers();

    /* --- Headless mode: --output --- */
    if (output) {
        if (pid < 0) {
            fprintf(stderr, "Error: --pid is required for --output mode\n\n");
            print_usage(argv[0]);
            return 1;
        }
        if (!process_exists(pid)) {
            agent_log("Error: process %d not found", pid);
            return 1;
        }

        struct platform_info pinfo;
        detect_platform(&pinfo);

        struct capabilities caps;
        probe_capabilities(pid, &caps);

        if (g_shutdown) { free_capabilities(&caps); return 0; }

        if (caps.record_event_count == 0) {
            agent_log("Error: no perf record events available for PID %d", pid);
            free_capabilities(&caps);
            return 1;
        }

        agent_log("Collecting perf data for PID %d (headless mode)", pid);
        size_t out_len = 0;
        char *data = collect_one_round(&caps, pid, frequency, duration, &out_len);
        if (data && out_len > 0) {
            if (strcmp(output, "-") == 0) {
                fwrite(data, 1, out_len, stdout);
                fflush(stdout);
            } else {
                FILE *f = fopen(output, "w");
                if (f) {
                    fwrite(data, 1, out_len, f);
                    fclose(f);
                    agent_log("Written %zu bytes to %s", out_len, output);
                } else {
                    agent_log("Error: cannot open %s: %s", output, strerror(errno));
                }
            }
            agent_log("Done. Output %zu bytes.", out_len);
            free(data);
        } else {
            agent_log("No data collected.");
            free_capabilities(&caps);
            return 1;
        }
        free_capabilities(&caps);
        return 0;
    }

    /* --- Interactive modes: --listen or --server --- */
    if (!listen_mode && !server) {
        fprintf(stderr,
            "Error: use --listen, --server HOST, or --output FILE\n\n");
        print_usage(argv[0]);
        return 1;
    }

    struct agent_state agent;
    agent_state_init(&agent);
    agent.frequency = frequency;
    agent.duration = duration;
    if (pid >= 0) agent.pid = pid;

    /* Register global pointer for signal handler */
    g_agent = &agent;

    if (listen_mode) {
        run_listen(&agent, port);
    } else {
        run_connect(&agent, server, port);
    }

    /* Cleanup */
    if (agent.caps) {
        free_capabilities(agent.caps);
        free(agent.caps);
    }
    cmdq_destroy(&agent.cmdq);
    pthread_mutex_destroy(&agent.sock_lock);
    pthread_mutex_destroy(&agent.state_lock);

    agent_log("Shutting down.");
    return 0;
}
