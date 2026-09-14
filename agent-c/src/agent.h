/*
 * PerfLens Device Agent — shared declarations
 *
 * The agent is split into focused modules:
 *   util.c     — logging, dynamic buffers, string/JSON helpers
 *   subproc.c  — signals, child tracking, fork/exec helpers, pipelines
 *   perfcmd.c  — the perf command lines every probe and collection runs
 *   wire.c     — TCP framing, streaming zstd sink
 *   probe.c    — platform detection, perf capability probing
 *   procs.c    — /proc process listing
 *   collect.c  — round-based and continuous collection loops
 *   metrics.c  — device health metrics collector + thread
 *   commands.c — command handlers + dispatch
 *   auth.c     — pairing-code generation and comparison
 *   update.c   — self-update from GitHub releases
 *   main.c     — agent state, session loop, run modes, CLI
 *
 * License: MIT (same as PerfLens project)
 */

#ifndef PERFLENS_AGENT_H
#define PERFLENS_AGENT_H

#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200809L
#endif
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <arpa/inet.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <ifaddrs.h>
#include <poll.h>
#include <limits.h>
#include <netdb.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "vendor/zstd/zstd.h"

/* --------------------------------------------------------------------------
 * Constants
 * -------------------------------------------------------------------------- */

#define LOG_PREFIX       "[perflens-agent]"
#define PERF_PATH_MAX    1024

/* Version is injected by the Makefile (-DAGENT_VERSION=\"x.y.z\") */
#ifndef AGENT_VERSION
#define AGENT_VERSION    "dev"
#endif

/* Self-update: release assets are named perflens-agent-linux-<arch>.
 * Override the base URL with PERFLENS_UPDATE_URL (e.g. corporate mirror). */
#define UPDATE_URL_BASE \
    "https://github.com/harshithsunku/perflens/releases/latest/download"
#define DEFAULT_PORT     9999
#define DEFAULT_FREQ     99
#define DEFAULT_DURATION 8
#define MAX_DURATION     300      /* the UI offers up to this; the server relays it */
#define DEFAULT_MAX_FREQ 10000    /* when perf_event_max_sample_rate is unreadable */
#define MAX_EVENTS       16
#define MAX_CMD_ARGS     32
#define INITIAL_BUF_SIZE (256 * 1024)     /* 256 KB initial read buffer */
#define SMALL_BUF_SIZE   (4 * 1024)       /* stderr captures, stat output */
#define MAX_BUF_SIZE     (64 * 1024 * 1024)  /* 64 MB cap */
#define IO_CHUNK         (64 * 1024)      /* pipe read chunk for streamed output */
#define RECONNECT_MAX    30.0
#define ZSTD_LEVEL       1

/* Continuous mode cuts a chunk at the interval deadline, or as soon as the
 * uncompressed text crosses this size, whichever comes first. Measured on an
 * 8-core target with the 25-thread test workload: ~3.4 MB/s of perf script
 * text, so the 64 MB cap above was reached after ~19 s and every chunk of a
 * longer interval was lost -- silently, since the sticky sink error skipped
 * the send and logged nothing. */
#define CHUNK_SOFT_LIMIT (16 * 1024 * 1024)

/* Server -> agent frames are JSON commands, a few hundred bytes each. Anything
 * near the data cap is a corrupt stream or a hostile peer, and a peer that
 * has not authenticated yet must not be able to make the agent allocate
 * 64 MB per frame. */
#define MAX_CMD_FRAME    (64 * 1024)
#define MAX_CMD_QUEUE    64

/* SIGTERM -> SIGKILL escalation for perf children on stop/teardown. A child
 * stuck in uninterruptible I/O used to block the collection thread in
 * waitpid() forever, and stop -- which joins that thread -- with it. */
#define CHILD_GRACE_MS   3000

/* Bound on a blocked send() during a session (SO_SNDTIMEO and
 * TCP_USER_TIMEOUT). Keepalive only probes an idle connection; with a chunk
 * in flight Linux retransmits for ~15 minutes before send() fails, and a
 * peer that is alive but not reading never fails it at all. Override with
 * PERFLENS_SEND_TIMEOUT_MS (the protocol tests shorten it). */
#define SEND_TIMEOUT_MS  60000

/* Pairing-code authentication.
 *
 * The agent always holds a secret: --token/PERFLENS_TOKEN when given, or one
 * generated at startup and printed to the log for the operator to copy into
 * the server. A peer must present it before any command runs, so there is no
 * unauthenticated state and --listen can safely keep its 0.0.0.0 default.
 *
 * The code is 16 random bytes as lowercase hex. That is 128 bits, so the
 * failure cap below is defence in depth rather than the thing standing
 * between an attacker and the agent. */
#define TOKEN_BYTES       16
#define TOKEN_HEX_LEN     (TOKEN_BYTES * 2)   /* 32 chars, +1 for NUL */
#define AUTH_TIMEOUT_SECS 10    /* peer must authenticate within this */
#define AUTH_MAX_FAILURES 3     /* wrong codes before the session is dropped */

/* Wire protocol flags (5-byte header: 4-byte length + 1-byte flag) */
#define FLAG_DATA_RAW     0   /* agent -> server: raw perf data */
#define FLAG_DATA_ZSTD    1   /* agent -> server: zstd-compressed perf data */
#define FLAG_CMD_REQUEST  2   /* server -> agent: JSON command */
#define FLAG_CMD_RESPONSE 3   /* agent -> server: JSON response */
#define FLAG_METRICS      4   /* agent -> server: JSON health metrics */

/* Agent states */
#define AGENT_IDLE       0
#define AGENT_PROFILING  1
#define AGENT_PAUSED     2
#define AGENT_PROBING    3   /* a start or reprobe is probing capabilities */

/* Process list limits */
#define MAX_PROCS        4096
#define MAX_PROC_RESULT  200

/* JSON response buffer */
#define JSON_BUF_SIZE    (128 * 1024)

/* Normalized field set for 'perf script -F'. Ensures consistent output
 * format across kernel versions. Requires perf >= ~3.12.
 *
 * 'symoff' is load-bearing: without it perf prints a bare symbol name, and
 * the server can only place a sample at its function's declaration line.
 * With it every frame carries 'func+0x<offset>', which resolves exactly.
 * If a perf is too old to know the field, the probe rejects the whole list
 * and falls back to the default output format -- which prints the offset
 * anyway, so both branches keep line-level annotation working. */
#define SCRIPT_FIELDS    "comm,tid,pid,time,period,event,ip,sym,symoff,dso"

/* --------------------------------------------------------------------------
 * Shared types
 * -------------------------------------------------------------------------- */

/* Dynamic buffer */
struct buf {
    char  *data;
    size_t len;
    size_t cap;
};

/* Streaming compression sink (wire.c) */
struct sink {
    int compress;           /* 1 = zstd streaming, 0 = raw buffering */
    ZSTD_CStream *zcs;
    struct buf out;         /* compressed (or raw) output */
    size_t raw_len;         /* total uncompressed bytes consumed */
    int error;              /* sticky failure flag */
};

struct platform_info {
    char arch[128];
    char kernel[128];
    char perf_version[128];
    int  perf_event_paranoid;
};

struct capabilities {
    char  *record_events[MAX_EVENTS];
    int    record_event_count;
    char  *stat_only_events[MAX_EVENTS];
    int    stat_only_event_count;
    char  *all_events[MAX_EVENTS * 2];
    int    all_event_count;
    char   callgraph[8];        /* "fp", "dwarf", "lbr", or "" */
    char   script_fields[128];  /* SCRIPT_FIELDS or "" */
    int    pipe_mode;           /* record -o - | script -i - works */
};

struct proc_entry {
    int  pid;
    char comm[64];
    char cmdline[256];
    double cpu;
};

/* Command queue (thread-safe, condition variable based) */
struct cmd_entry {
    char *json;
    struct cmd_entry *next;
};

struct cmd_queue {
    struct cmd_entry *head;
    struct cmd_entry *tail;
    int len;
    pthread_mutex_t lock;
    pthread_cond_t cond;
};

/* What a start or reprobe asked for. The command thread validates it and
 * hands it to the collection thread, which probes (that takes seconds to
 * minutes), answers the command, and -- for start -- goes on to collect.
 * Meanwhile ping, status and stop keep being answered. */
struct start_job {
    int  pid;
    int  frequency;
    int  duration;
    int  then_start;            /* 1 = start, 0 = reprobe */
    char cmd_id[80];
    char req_events[512];       /* comma list the peer asked for, or "" */
};

struct agent_state {
    /* Socket (protected by sock_lock) */
    int sock_fd;
    pthread_mutex_t sock_lock;

    /* Agent state (protected by state_lock) */
    int state;
    pthread_mutex_t state_lock;

    /* Config (frequency and duration protected by state_lock) */
    int pid;
    int frequency;
    int duration;

    /* Start time of the profiled process (field 22 of /proc/<pid>/stat) as
     * of `start`, so a reused PID is noticed instead of profiled. 0 when it
     * could not be read. */
    unsigned long long pid_start;

    /* The shared secret a peer must present before any command runs. Either
     * --token/PERFLENS_TOKEN (borrowed, points into argv or the environment)
     * or generated_token below. Never written to the socket. */
    const char *token;
    char generated_token[TOKEN_HEX_LEN + 1];
    int token_is_generated;     /* the operator has to copy it from the log */

    /* Per-session auth state. Reset on every session: run_listen and
     * run_connect both loop, and a sticky flag would let one authenticated
     * session authorize its successor. */
    atomic_int authed;
    int auth_failures;

    /* Record-event selection: comma-joined subset of the probed record
     * events actually sampled (set at start; empty = all probed) */
    char sel_events[512];

    /* Probed state */
    struct platform_info platform;
    struct capabilities *caps;

    /* Collection thread */
    pthread_t collect_thread;
    int collect_thread_active;
    atomic_int collect_stop;
    struct start_job job;       /* read by the thread at start */

    /* Woken by stop/pause/resume so a sleeping loop reacts at once */
    pthread_mutex_t wake_lock;
    pthread_cond_t  wake;

    /* Per-session disconnect signal */
    atomic_int session_done;

    /* Metrics thread */
    pthread_t metrics_thread;
    int metrics_thread_active;
    int metrics_enabled;
    int metrics_interval;       /* seconds */
    int metrics_network;        /* include network stats */
    int metrics_disk;           /* include disk I/O stats (off by default) */
    int metrics_threads;        /* include per-thread stats (off by default) */

    /* Command queue */
    struct cmd_queue cmdq;
};

/* --------------------------------------------------------------------------
 * Globals (defined in subproc.c)
 * -------------------------------------------------------------------------- */

extern volatile sig_atomic_t g_shutdown;
extern struct agent_state *g_agent;        /* for signal handler */
extern volatile int g_agent_sock_fd;       /* socket mirror for signal handler */

/* --------------------------------------------------------------------------
 * util.c
 * -------------------------------------------------------------------------- */

void agent_log(const char *fmt, ...);
void agent_warn(const char *fmt, ...);
/* Only with PERFLENS_LOG=debug: the per-chunk and per-round lines, which
 * otherwise grow a RAM-backed /tmp by about a megabyte a day. */
void agent_debug(const char *fmt, ...);

void buf_init(struct buf *b);
void buf_free(struct buf *b);
int  buf_ensure(struct buf *b, size_t needed);
/* Like buf_ensure, but the first allocation is `initial` rather than
 * INITIAL_BUF_SIZE -- for buffers that only ever hold a few hundred bytes. */
int  buf_ensure_small(struct buf *b, size_t needed, size_t initial);

int  str_contains_lower(const char *haystack, size_t len, const char *needle);
int  is_stat_only(const char *event);

size_t json_escape(char *dst, size_t cap, const char *src);

/* Key lookups scan [json, end) -- pass end=NULL for the whole string. A
 * match must be a key (followed by ':'), never a string value that happens
 * to equal the name, and never something in a later sibling object. */
const char *json_object_end(const char *obj);
int  json_get_str_n(const char *json, const char *end, const char *key,
                    char *buf, size_t buflen);
int  json_get_int_n(const char *json, const char *end, const char *key,
                    int *out);
int  json_get_bool_n(const char *json, const char *end, const char *key,
                     int *out);
const char *json_find_object_n(const char *json, const char *end,
                               const char *key);
const char *json_find_array_n(const char *json, const char *end,
                              const char *key);
int  json_get_str(const char *json, const char *key, char *buf, size_t buflen);
int  json_get_int(const char *json, const char *key, int *out);
int  json_get_bool(const char *json, const char *key, int *out);
const char *json_find_object(const char *json, const char *key);
const char *json_find_array(const char *json, const char *key);

/* Command ids the agent will echo: [A-Za-z0-9_.:-]{1,63}. Anything else is
 * answered with an empty id, so a quote or backslash in an id can never turn
 * a response into invalid JSON. */
int  json_valid_id(const char *id);

/* Bounded string builder for JSON responses. Every append clamps to the
 * buffer and records the overflow, instead of the `n += snprintf` idiom
 * that writes past the end once n exceeds the capacity. */
struct wbuf {
    char  *p;
    size_t cap;
    size_t len;
    int    truncated;
};
void wbuf_init(struct wbuf *w, char *storage, size_t cap);
void wbuf_addf(struct wbuf *w, const char *fmt, ...);
void wbuf_add(struct wbuf *w, const char *s);

int  process_exists(int pid);
/* Field 22 of /proc/<pid>/stat (start time in clock ticks since boot), or 0
 * when unreadable. Two processes never share a pid and a start time. */
unsigned long long process_start_time(int pid);
long read_int_file(const char *path);
/* $TMPDIR, or /tmp. Embedded targets often keep /tmp tiny and RAM-backed. */
const char *agent_tmpdir(void);
/* Remove perflens-* temp files of our uid older than an hour, left behind
 * by an agent that was SIGKILLed mid-round. */
void sweep_stale_tmpfiles(void);

/* --------------------------------------------------------------------------
 * subproc.c
 * -------------------------------------------------------------------------- */

#define CHILD_NICE       1   /* fork_cmd flag: run the child at nice 5 */
#define CHILD_KEEP_STDIN 2   /* pipeline stage b reads its predecessor */

void track_child(pid_t pid);
void untrack_child(pid_t pid);
void kill_tracked_children(void);
void install_signal_handlers(void);
void block_signals_in_thread(void);

/* Wait up to grace_ms for a child that has been signalled, then SIGKILL its
 * whole process group and reap it. Returns the wait status. */
int   reap_child(pid_t pid, int grace_ms);
/* Signal a child and the workload it spawned (perf record -- sleep N). */
void  kill_child_group(pid_t pid, int sig);

/* `a` may be NULL. Otherwise a stop or a lost session cancels the wait:
 * the child is signalled and reaped, and -1 is returned. */
int   run_cmd(char *const argv[], struct buf *out, struct buf *err,
              int timeout_sec, const struct agent_state *a);
pid_t fork_cmd(char *const argv[], int *out_fd_p, int *err_fd_p, int flags);
int   fork_pipeline(char *const argv_a[], char *const argv_b[],
                    pid_t *pid_a_p, pid_t *pid_b_p,
                    int *a_err_p, int *b_out_p, int *b_err_p);
int   run_pipeline_once(char *const argv_a[], char *const argv_b[],
                        struct buf *out, int timeout_sec,
                        const struct agent_state *a);

/* --------------------------------------------------------------------------
 * perfcmd.c — one place that knows what a perf command line looks like
 * -------------------------------------------------------------------------- */

/* Join events[0..count) with commas into dst, appending `extra` (may be
 * NULL) as one more item. Returns dst. */
char *join_events(char *dst, size_t cap, char *const *events, int count,
                  const char *extra);

/* Each builder fills argv (capacity `cap`, NULL-terminated) and returns the
 * argument count. `output` is a file, or "-" for a pipe. `sleep_secs` (may
 * be NULL) appends `-- sleep N`; without it the record runs until signalled. */
int build_record_argv(char **argv, int cap, const char *events,
                      const char *pid_str, const char *freq_str,
                      const char *output, const char *callgraph,
                      const char *sleep_secs);
int build_script_argv(char **argv, int cap, const char *fields,
                      const char *input);
/* csv=1 adds `-x ,` (one line per event, machine-readable), which is how
 * the probe asks about every candidate in a single run. */
int build_stat_argv(char **argv, int cap, const char *events,
                    const char *pid_str, const char *sleep_secs, int csv);

/* --------------------------------------------------------------------------
 * wire.c
 * -------------------------------------------------------------------------- */

/* Keepalive, TCP_NODELAY, and the send bounds, for a session socket. */
void tcp_session_opts(int fd);
int  tcp_send_frame(int fd, const void *payload, size_t payload_len,
                    uint8_t flag);
int  tcp_recv_frame(int fd, char **payload, uint32_t *out_len,
                    uint8_t *out_flag);

void sink_init(struct sink *s, int want_compress);
int  sink_write(struct sink *s, const void *data, size_t len);
int  sink_finish(struct sink *s);
void sink_free(struct sink *s);
void sink_reset(struct sink *s);
/* `a` may be NULL (headless mode); otherwise the read loop stops early when
 * the session is stopping, instead of waiting for the child to close its
 * pipes -- which a stuck perf never does. */
int  run_cmd_to_sink(char *const argv[], struct sink *sink,
                     struct buf *err, int timeout_sec, int flags,
                     const struct agent_state *a);

/* --------------------------------------------------------------------------
 * auth.c
 * -------------------------------------------------------------------------- */

/* Fill out[] with TOKEN_HEX_LEN lowercase hex chars from /dev/urandom.
 * Returns 0 on success, -1 on failure — callers must fail closed rather than
 * fall back to a predictable source. out_cap must be > TOKEN_HEX_LEN. */
int  agent_generate_token(char *out, size_t out_cap);

/* Constant-time string equality. Compares the full length of both strings
 * regardless of where they differ, so a peer cannot learn the secret one
 * character at a time from response timing. */
int  agent_consttime_eq(const char *a, const char *b);

/* --------------------------------------------------------------------------
 * probe.c
 * -------------------------------------------------------------------------- */

/* The perf binary every probe and collection runs: "perf" from PATH unless
 * --perf, PERFLENS_PERF or verify_perf {perf} chose another. */
extern char g_perf[PERF_PATH_MAX];
int  perf_use(const char *path, char *err, size_t errlen);
void detect_platform(struct platform_info *info);
/* Returns 0, or -1 when cancelled part-way (caps is then incomplete). */
int  probe_capabilities(int pid, struct capabilities *caps,
                        const struct agent_state *a);
/* Everything in caps depends on the kernel, the perf build and the
 * permissions -- not on the pid, except that recording another user's
 * process may be refused. One short record answers that. */
int  probe_pid_records(const struct capabilities *caps, int pid,
                       const struct agent_state *a);
void free_capabilities(struct capabilities *caps);
/* Does this perf script output carry call chains (indented frame lines)? */
int  callchains_present(const struct buf *out);

/* --------------------------------------------------------------------------
 * procs.c
 * -------------------------------------------------------------------------- */

int do_list_processes(struct proc_entry *result, int max_results);

/* --------------------------------------------------------------------------
 * collect.c
 * -------------------------------------------------------------------------- */

char *collect_one_round(const struct capabilities *caps, const char *events,
                        int pid, int frequency, int duration,
                        int want_compress, size_t *out_len,
                        size_t *out_raw_len, uint8_t *out_flag,
                        const struct agent_state *a);
/* The collection loop proper (rounds or continuous), run on the
 * collection thread once capabilities are known. */
void  collection_run(struct agent_state *a);
/* Sleep up to ms, waking early on stop/pause/resume, session end, or
 * shutdown. */
void  session_sleep(struct agent_state *a, int ms);
void  agent_wake(struct agent_state *a);
/* True once the collection thread has been told to stop, or the session
 * ended. NULL means headless mode, which only stops on a signal. */
static inline int collection_cancelled(const struct agent_state *a)
{
    return a && (a->collect_stop || a->session_done);
}

/* --------------------------------------------------------------------------
 * metrics.c
 * -------------------------------------------------------------------------- */

void *metrics_thread_fn(void *arg);

/* --------------------------------------------------------------------------
 * commands.c
 * -------------------------------------------------------------------------- */

void dispatch_command(struct agent_state *a, const char *json);
/* The collection thread's entry: probe as the job requires, answer the
 * start or reprobe, then collect. */
void *collection_thread_fn(void *arg);

/* Defined in main.c; called by cmd_auth once a peer has proved itself.
 * Idempotent — metrics must not stream before authentication. */
void start_metrics_thread(struct agent_state *a);

/* --------------------------------------------------------------------------
 * update.c
 * -------------------------------------------------------------------------- */

int self_update(char *msg, size_t msglen);

/* --------------------------------------------------------------------------
 * main.c — send helpers shared with worker modules
 * -------------------------------------------------------------------------- */

int agent_send_frame(struct agent_state *a, const void *payload,
                     size_t len, uint8_t flag);
int agent_send_response(struct agent_state *a, const char *json);
int agent_send_metrics(struct agent_state *a, const char *json, size_t len);

/* Takes ownership of json. Returns -1 (and frees it) when the queue is full. */
int  cmdq_push(struct cmd_queue *q, char *json);

#endif /* PERFLENS_AGENT_H */
