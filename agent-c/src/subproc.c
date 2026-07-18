/*
 * PerfLens Device Agent — signals, child tracking, subprocess helpers
 */

#include "agent.h"

/* --------------------------------------------------------------------------
 * Globals
 * -------------------------------------------------------------------------- */

volatile sig_atomic_t g_shutdown = 0;
#define MAX_TRACKED_CHILDREN 8
static volatile pid_t g_child_pids[MAX_TRACKED_CHILDREN];
struct agent_state *g_agent = NULL;  /* for signal handler */
volatile int g_agent_sock_fd = -1;   /* mirror of agent sock_fd for signal handler */

/* --------------------------------------------------------------------------
 * Child process tracking
 *
 * Fixed slots claimed/released with CAS so track/untrack/kill are safe from
 * the collection thread, the command thread, and the signal handler
 * concurrently — no mutex (the signal handler can't take one).
 * -------------------------------------------------------------------------- */

void track_child(pid_t pid)
{
    for (int i = 0; i < MAX_TRACKED_CHILDREN; i++) {
        if (__sync_bool_compare_and_swap(&g_child_pids[i], 0, pid))
            return;
    }
    agent_warn("child pid %d not tracked (all slots busy)", (int)pid);
}

void untrack_child(pid_t pid)
{
    for (int i = 0; i < MAX_TRACKED_CHILDREN; i++) {
        if (__sync_bool_compare_and_swap(&g_child_pids[i], pid, 0))
            return;
    }
}

/* Async-signal-safe: only volatile reads + kill(2). */
void kill_tracked_children(void)
{
    for (int i = 0; i < MAX_TRACKED_CHILDREN; i++) {
        pid_t p = g_child_pids[i];
        if (p > 0)
            kill(p, SIGTERM);
    }
}

/* --------------------------------------------------------------------------
 * Signal handling
 * -------------------------------------------------------------------------- */

static void signal_handler(int sig)
{
    (void)sig;
    g_shutdown = 1;
    kill_tracked_children();
    /* Unblock recv thread by shutting down socket */
    if (g_agent_sock_fd >= 0)
        shutdown(g_agent_sock_fd, SHUT_RDWR);
}

void install_signal_handlers(void)
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

/* Block SIGINT/SIGTERM in worker threads — only main thread handles signals */
void block_signals_in_thread(void)
{
    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGINT);
    sigaddset(&mask, SIGTERM);
    pthread_sigmask(SIG_BLOCK, &mask, NULL);
}

/* Forked children inherit the forking thread's blocked-signal mask, and
 * execvp preserves it — a perf child forked from a worker thread would
 * never see our SIGTERM. Reset the mask before exec. */
void unblock_signals_in_child(void)
{
    sigset_t empty;
    sigemptyset(&empty);
    sigprocmask(SIG_SETMASK, &empty, NULL);
}

/* --------------------------------------------------------------------------
 * Subprocess helper: run_cmd()
 *
 * Runs argv[0..] with fork/exec, captures stdout and stderr into caller-
 * provided buffers. Returns the child's exit code, or -1 on error/timeout.
 * Uses poll() for timeout — no SIGALRM interference.
 * -------------------------------------------------------------------------- */

int run_cmd(char *const argv[], struct buf *out, struct buf *err,
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

        unblock_signals_in_child();
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

pid_t fork_cmd(char *const argv[], int *out_fd_p, int *err_fd_p)
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
        unblock_signals_in_child();
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
 * Two-stage pipeline helper (a | b)
 *
 * Used for continuous collection: perf record -o - | perf script -i -.
 * Both children are tracked so stop/pause/signal handling reaches them.
 * -------------------------------------------------------------------------- */

static void close_pipe_pair(int p[2])
{
    if (p[0] >= 0) close(p[0]);
    if (p[1] >= 0) close(p[1]);
}

/* Fork a's stdout into b's stdin. On success returns 0 and gives the
 * parent read fds for a's stderr, b's stdout, and b's stderr. */
int fork_pipeline(char *const argv_a[], char *const argv_b[],
                         pid_t *pid_a_p, pid_t *pid_b_p,
                         int *a_err_p, int *b_out_p, int *b_err_p)
{
    int link_p[2] = {-1, -1}, aerr[2] = {-1, -1};
    int bout[2] = {-1, -1}, berr[2] = {-1, -1};

    if (pipe(link_p) < 0 || pipe(aerr) < 0 ||
        pipe(bout) < 0 || pipe(berr) < 0) {
        agent_warn("pipe() failed: %s", strerror(errno));
        close_pipe_pair(link_p); close_pipe_pair(aerr);
        close_pipe_pair(bout); close_pipe_pair(berr);
        return -1;
    }

    pid_t pa = fork();
    if (pa < 0) {
        agent_warn("fork() failed: %s", strerror(errno));
        close_pipe_pair(link_p); close_pipe_pair(aerr);
        close_pipe_pair(bout); close_pipe_pair(berr);
        return -1;
    }
    if (pa == 0) {
        dup2(link_p[1], STDOUT_FILENO);
        dup2(aerr[1], STDERR_FILENO);
        close_pipe_pair(link_p); close_pipe_pair(aerr);
        close_pipe_pair(bout); close_pipe_pair(berr);
        close(STDIN_FILENO);
        unblock_signals_in_child();
        execvp(argv_a[0], argv_a);
        _exit(127);
    }

    pid_t pb = fork();
    if (pb < 0) {
        agent_warn("fork() failed: %s", strerror(errno));
        kill(pa, SIGKILL);
        int ws;
        do { } while (waitpid(pa, &ws, 0) < 0 && errno == EINTR);
        close_pipe_pair(link_p); close_pipe_pair(aerr);
        close_pipe_pair(bout); close_pipe_pair(berr);
        return -1;
    }
    if (pb == 0) {
        dup2(link_p[0], STDIN_FILENO);
        dup2(bout[1], STDOUT_FILENO);
        dup2(berr[1], STDERR_FILENO);
        close_pipe_pair(link_p); close_pipe_pair(aerr);
        close_pipe_pair(bout); close_pipe_pair(berr);
        /* Stage b is the CPU-heavy symbolizer — yield to the profiled
         * workload so the profiler doesn't skew what it measures. */
        if (nice(5) < 0) { /* best effort */ }
        unblock_signals_in_child();
        execvp(argv_b[0], argv_b);
        _exit(127);
    }

    /* Parent keeps only the read ends it polls */
    close_pipe_pair(link_p);
    close(aerr[1]); close(bout[1]); close(berr[1]);
    track_child(pa);
    track_child(pb);

    *pid_a_p = pa; *pid_b_p = pb;
    *a_err_p = aerr[0]; *b_out_p = bout[0]; *b_err_p = berr[0];
    return 0;
}

/* Run a pipeline to completion, capturing b's stdout. Returns b's exit
 * code, or -1 on error/timeout. Used by the pipe-mode capability probe. */
int run_pipeline_once(char *const argv_a[], char *const argv_b[],
                             struct buf *out, int timeout_sec)
{
    pid_t pid_a, pid_b;
    int a_err_fd, b_out_fd, b_err_fd;

    if (fork_pipeline(argv_a, argv_b, &pid_a, &pid_b,
                      &a_err_fd, &b_out_fd, &b_err_fd) < 0)
        return -1;

    if (out) out->len = 0;

    struct pollfd fds[3];
    fds[0].fd = b_out_fd; fds[0].events = POLLIN;
    fds[1].fd = b_err_fd; fds[1].events = POLLIN;
    fds[2].fd = a_err_fd; fds[2].events = POLLIN;
    int open_fds = 3;

    struct timespec start;
    clock_gettime(CLOCK_MONOTONIC, &start);

    while (open_fds > 0 && !g_shutdown) {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        int elapsed_ms = (int)((now.tv_sec - start.tv_sec) * 1000 +
                               (now.tv_nsec - start.tv_nsec) / 1000000);
        int remaining_ms = timeout_sec * 1000 - elapsed_ms;
        if (remaining_ms <= 0) {
            kill(pid_a, SIGKILL);
            kill(pid_b, SIGKILL);
            break;
        }

        int ret = poll(fds, 3, remaining_ms < 500 ? remaining_ms : 500);
        if (ret < 0) {
            if (errno == EINTR) continue;
            break;
        }

        for (int i = 0; i < 3; i++) {
            if (fds[i].fd < 0) continue;
            if (!(fds[i].revents & (POLLIN | POLLHUP))) continue;

            if (i == 0 && out) {
                if (buf_ensure(out, out->len + 4096) < 0) {
                    close(fds[i].fd); fds[i].fd = -1; open_fds--;
                    continue;
                }
                ssize_t n = read(fds[i].fd, out->data + out->len,
                                 out->cap - out->len);
                if (n > 0) {
                    out->len += (size_t)n;
                } else {
                    close(fds[i].fd); fds[i].fd = -1; open_fds--;
                }
            } else {
                char discard[4096];
                ssize_t n = read(fds[i].fd, discard, sizeof(discard));
                if (n <= 0) { close(fds[i].fd); fds[i].fd = -1; open_fds--; }
            }
        }
    }

    for (int i = 0; i < 3; i++)
        if (fds[i].fd >= 0) close(fds[i].fd);

    int status_a = 0, status_b = 0;
    int rc;
    do { rc = waitpid(pid_a, &status_a, 0); } while (rc < 0 && errno == EINTR);
    untrack_child(pid_a);
    do { rc = waitpid(pid_b, &status_b, 0); } while (rc < 0 && errno == EINTR);
    untrack_child(pid_b);

    if (WIFEXITED(status_b))
        return WEXITSTATUS(status_b);
    return -1;
}

