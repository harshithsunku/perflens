/*
 * PerfLens Device Agent — shared declarations
 *
 * The agent is split into focused modules:
 *   util.c     — logging, dynamic buffers, string/JSON helpers
 *   subproc.c  — signals, child tracking, fork/exec helpers, pipelines
 *   wire.c     — TCP framing, streaming zstd sink
 *   probe.c    — platform detection, perf capability probing
 *   procs.c    — /proc process listing
 *   collect.c  — round-based and continuous collection loops
 *   metrics.c  — device health metrics collector + thread
 *   commands.c — command handlers + dispatch
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
#include <getopt.h>
#include <poll.h>
#include <limits.h>
#include <netdb.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
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
#define MAX_EVENTS       16
#define MAX_CMD_ARGS     32
#define INITIAL_BUF_SIZE (256 * 1024)     /* 256 KB initial read buffer */
#define MAX_BUF_SIZE     (64 * 1024 * 1024)  /* 64 MB cap */
#define IO_CHUNK         (64 * 1024)      /* pipe read chunk for streamed output */
#define RECONNECT_MAX    30.0
#define ZSTD_LEVEL       1

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

/* Process list limits */
#define MAX_PROCS        4096
#define MAX_PROC_RESULT  200

/* JSON response buffer */
#define JSON_BUF_SIZE    (128 * 1024)

/* Normalized field set for 'perf script -F'. Ensures consistent output
 * format across kernel versions. Requires perf >= ~3.12. */
#define SCRIPT_FIELDS    "comm,tid,pid,time,period,event,ip,sym,dso"

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
    pthread_mutex_t lock;
    pthread_cond_t cond;
};

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
    const char *token;          /* optional shared secret, sent in hello */

    /* Record-event selection: comma-joined subset of the probed record
     * events actually sampled (set at start; empty = all probed) */
    char sel_events[512];

    /* Probed state */
    struct platform_info platform;
    struct capabilities *caps;

    /* Collection thread */
    pthread_t collect_thread;
    int collect_thread_active;
    volatile int collect_stop;

    /* Per-session disconnect signal */
    volatile int session_done;

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

void buf_init(struct buf *b);
void buf_free(struct buf *b);
int  buf_ensure(struct buf *b, size_t needed);

int  str_contains_lower(const char *haystack, size_t len, const char *needle);
int  is_stat_only(const char *event);

size_t json_escape(char *dst, size_t cap, const char *src);
int  json_get_str(const char *json, const char *key, char *buf, size_t buflen);
int  json_get_int(const char *json, const char *key, int *out);
int  json_get_bool(const char *json, const char *key, int *out);
const char *json_find_object(const char *json, const char *key);
const char *json_find_array(const char *json, const char *key);

int  process_exists(int pid);
long read_int_file(const char *path);

/* --------------------------------------------------------------------------
 * subproc.c
 * -------------------------------------------------------------------------- */

void track_child(pid_t pid);
void untrack_child(pid_t pid);
void kill_tracked_children(void);
void install_signal_handlers(void);
void block_signals_in_thread(void);
void unblock_signals_in_child(void);

int   run_cmd(char *const argv[], struct buf *out, struct buf *err,
              int timeout_sec);
pid_t fork_cmd(char *const argv[], int *out_fd_p, int *err_fd_p);
int   fork_pipeline(char *const argv_a[], char *const argv_b[],
                    pid_t *pid_a_p, pid_t *pid_b_p,
                    int *a_err_p, int *b_out_p, int *b_err_p);
int   run_pipeline_once(char *const argv_a[], char *const argv_b[],
                        struct buf *out, int timeout_sec);

/* --------------------------------------------------------------------------
 * wire.c
 * -------------------------------------------------------------------------- */

void tcp_enable_keepalive(int fd);
int  tcp_send_frame(int fd, const void *payload, size_t payload_len,
                    uint8_t flag);
int  tcp_recv_frame(int fd, char **payload, uint32_t *out_len,
                    uint8_t *out_flag);

void sink_init(struct sink *s, int want_compress);
int  sink_write(struct sink *s, const void *data, size_t len);
int  sink_finish(struct sink *s);
void sink_free(struct sink *s);
int  run_cmd_to_sink(char *const argv[], struct sink *sink,
                     struct buf *err, int timeout_sec);

/* --------------------------------------------------------------------------
 * probe.c
 * -------------------------------------------------------------------------- */

void detect_platform(struct platform_info *info);
void probe_capabilities(int pid, struct capabilities *caps);
void free_capabilities(struct capabilities *caps);

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
                        size_t *out_raw_len, uint8_t *out_flag);
void *collection_thread_fn(void *arg);

/* --------------------------------------------------------------------------
 * metrics.c
 * -------------------------------------------------------------------------- */

void *metrics_thread_fn(void *arg);

/* --------------------------------------------------------------------------
 * commands.c
 * -------------------------------------------------------------------------- */

void dispatch_command(struct agent_state *a, const char *json);

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
int agent_send_data(struct agent_state *a, const void *data,
                    size_t len, uint8_t flag);
int agent_send_metrics(struct agent_state *a, const char *json, size_t len);

void cmdq_push(struct cmd_queue *q, const char *json);

#endif /* PERFLENS_AGENT_H */
