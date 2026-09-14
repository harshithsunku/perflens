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
 *
 * Every child is the leader of its own process group (see child_setup), so
 * a signal to -pid reaches the workload perf itself spawned (`perf record
 * -- sleep N`) as well. Killing perf alone with SIGKILL used to leave one
 * orphaned sleep per round.
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

void kill_child_group(pid_t pid, int sig)
{
    if (pid <= 0) return;
    /* The group first (perf and its workload), then the pid itself in case
     * the child has not reached setpgid() yet. */
    kill(-pid, sig);
    kill(pid, sig);
}

/* Async-signal-safe: only volatile reads + kill(2). */
void kill_tracked_children(void)
{
    for (int i = 0; i < MAX_TRACKED_CHILDREN; i++) {
        pid_t p = g_child_pids[i];
        if (p > 0)
            kill_child_group(p, SIGTERM);
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

/* --------------------------------------------------------------------------
 * The child side of every fork
 *
 * Runs between fork() and exec(), so only async-signal-safe calls plus
 * setenv/unsetenv (which glibc and musl implement without locks the parent's
 * other threads could be holding, given the environment is never mutated
 * elsewhere in the agent).
 *
 *   - Its own process group, so stop/pause/teardown can signal perf and the
 *     `sleep` workload perf started together.
 *   - LC_ALL=C: perf stat prints big numbers with the locale's thousands
 *     grouping (`--big-num` is the default), and the server's parser only
 *     strips ','. A de_DE or fr_FR device printed 1.234.567 or 1 234 567.
 *   - stdin from /dev/null rather than closed, so the first file perf opens
 *     does not land on fd 0.
 *   - Signals unblocked: forked children inherit the forking thread's mask,
 *     and execvp preserves it — a perf child forked from a worker thread
 *     would never see our SIGTERM.
 *   - Optionally nice 5: perf script is the CPU-heavy symbolizer, and it
 *     must yield to the workload it is measuring.
 * -------------------------------------------------------------------------- */

static void child_setup(int flags)
{
    setpgid(0, 0);
    setenv("LC_ALL", "C", 1);
    unsetenv("LANGUAGE");

    if (!(flags & CHILD_KEEP_STDIN)) {
        int devnull = open("/dev/null", O_RDONLY);
        if (devnull >= 0) {
            dup2(devnull, STDIN_FILENO);
            if (devnull != STDIN_FILENO) close(devnull);
        } else {
            close(STDIN_FILENO);
        }
    }

    if (flags & CHILD_NICE) {
        if (nice(5) < 0) { /* best effort */ }
    }

    sigset_t empty;
    sigemptyset(&empty);
    sigprocmask(SIG_SETMASK, &empty, NULL);
}

/* fork() with the agent's signal handlers kept out of the child.
 *
 * Until it execs, a child shares the parent's handlers -- and the SIGTERM
 * handler calls shutdown() on the session socket, which the child still
 * holds a copy of. A stop that signalled a child forked from the command
 * thread a moment earlier therefore tore down the parent's own connection:
 * both ends saw EOF in the same millisecond. So: block SIGTERM and SIGINT
 * across the fork, and in the child restore the default dispositions before
 * child_setup() unblocks anything. SIGPIPE goes back to default too -- perf
 * is meant to die when the reader of its pipe goes away. */
static pid_t do_fork(void)
{
    sigset_t block, old;
    sigemptyset(&block);
    sigaddset(&block, SIGTERM);
    sigaddset(&block, SIGINT);
    pthread_sigmask(SIG_BLOCK, &block, &old);
    pid_t pid = fork();
    if (pid == 0) {
        signal(SIGTERM, SIG_DFL);
        signal(SIGINT, SIG_DFL);
        signal(SIGPIPE, SIG_DFL);
        return 0;
    }
    pthread_sigmask(SIG_SETMASK, &old, NULL);
    return pid;
}

/* Parent side: claim the group too, so kill(-pid) cannot race the child's
 * own setpgid(). EACCES after the exec is expected and harmless. */
static void parent_after_fork(pid_t pid)
{
    setpgid(pid, pid);
    track_child(pid);
}

static int make_pipe(int fds[2])
{
    if (pipe2(fds, O_CLOEXEC) < 0) {
        agent_warn("pipe() failed: %s", strerror(errno));
        fds[0] = fds[1] = -1;
        return -1;
    }
    return 0;
}

static void close_pipe_pair(int p[2])
{
    if (p[0] >= 0) close(p[0]);
    if (p[1] >= 0) close(p[1]);
}

/* --------------------------------------------------------------------------
 * Reaping with a grace period
 * -------------------------------------------------------------------------- */

int reap_child(pid_t pid, int grace_ms)
{
    int status = 0;
    struct timespec tick = {0, 50000000L};  /* 50 ms */
    int waited = 0;
    while (waited <= grace_ms) {
        pid_t r = waitpid(pid, &status, WNOHANG);
        if (r == pid) return status;
        if (r < 0 && errno != EINTR) return status;
        if (waited == grace_ms) break;
        nanosleep(&tick, NULL);
        waited += 50;
        if (waited > grace_ms) waited = grace_ms;
    }
    kill_child_group(pid, SIGKILL);
    pid_t r;
    do { r = waitpid(pid, &status, 0); } while (r < 0 && errno == EINTR);
    return status;
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

    if (make_pipe(stdout_pipe) < 0 || make_pipe(stderr_pipe) < 0) {
        close_pipe_pair(stdout_pipe);
        close_pipe_pair(stderr_pipe);
        return -1;
    }

    pid_t pid = do_fork();
    if (pid < 0) {
        agent_warn("fork() failed: %s", strerror(errno));
        close_pipe_pair(stdout_pipe);
        close_pipe_pair(stderr_pipe);
        return -1;
    }

    if (pid == 0) {
        /* Child: dup2 clears O_CLOEXEC on the target descriptors */
        dup2(stdout_pipe[1], STDOUT_FILENO);
        dup2(stderr_pipe[1], STDERR_FILENO);
        child_setup(0);
        execvp(argv[0], argv);
        _exit(127);
    }

    /* Parent */
    close(stdout_pipe[1]);
    close(stderr_pipe[1]);
    parent_after_fork(pid);

    struct pollfd fds[2];
    fds[0].fd = stdout_pipe[0]; fds[0].events = POLLIN;
    fds[1].fd = stderr_pipe[0]; fds[1].events = POLLIN;
    int open_fds = 2;
    int killed = 0;

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
            kill_child_group(pid, SIGKILL);
            killed = 1;
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

            if (buf_ensure_small(target, target->len + 4096, SMALL_BUF_SIZE) < 0) {
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

    int status = reap_child(pid, killed ? 0 : CHILD_GRACE_MS);
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

pid_t fork_cmd(char *const argv[], int *out_fd_p, int *err_fd_p, int flags)
{
    int stdout_pipe[2] = {-1, -1};
    int stderr_pipe[2] = {-1, -1};

    if (make_pipe(stdout_pipe) < 0 || make_pipe(stderr_pipe) < 0) {
        close_pipe_pair(stdout_pipe);
        close_pipe_pair(stderr_pipe);
        return -1;
    }

    pid_t pid = do_fork();
    if (pid < 0) {
        agent_warn("fork() failed: %s", strerror(errno));
        close_pipe_pair(stdout_pipe);
        close_pipe_pair(stderr_pipe);
        return -1;
    }

    if (pid == 0) {
        dup2(stdout_pipe[1], STDOUT_FILENO);
        dup2(stderr_pipe[1], STDERR_FILENO);
        child_setup(flags);
        execvp(argv[0], argv);
        _exit(127);
    }

    /* Parent */
    close(stdout_pipe[1]);
    close(stderr_pipe[1]);
    parent_after_fork(pid);

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

/* Fork a's stdout into b's stdin. On success returns 0 and gives the
 * parent read fds for a's stderr, b's stdout, and b's stderr. */
int fork_pipeline(char *const argv_a[], char *const argv_b[],
                         pid_t *pid_a_p, pid_t *pid_b_p,
                         int *a_err_p, int *b_out_p, int *b_err_p)
{
    int link_p[2] = {-1, -1}, aerr[2] = {-1, -1};
    int bout[2] = {-1, -1}, berr[2] = {-1, -1};

    if (make_pipe(link_p) < 0 || make_pipe(aerr) < 0 ||
        make_pipe(bout) < 0 || make_pipe(berr) < 0) {
        close_pipe_pair(link_p); close_pipe_pair(aerr);
        close_pipe_pair(bout); close_pipe_pair(berr);
        return -1;
    }

    pid_t pa = do_fork();
    if (pa < 0) {
        agent_warn("fork() failed: %s", strerror(errno));
        close_pipe_pair(link_p); close_pipe_pair(aerr);
        close_pipe_pair(bout); close_pipe_pair(berr);
        return -1;
    }
    if (pa == 0) {
        dup2(link_p[1], STDOUT_FILENO);
        dup2(aerr[1], STDERR_FILENO);
        child_setup(0);
        execvp(argv_a[0], argv_a);
        _exit(127);
    }
    parent_after_fork(pa);

    pid_t pb = do_fork();
    if (pb < 0) {
        agent_warn("fork() failed: %s", strerror(errno));
        kill_child_group(pa, SIGKILL);
        reap_child(pa, 0);
        untrack_child(pa);
        close_pipe_pair(link_p); close_pipe_pair(aerr);
        close_pipe_pair(bout); close_pipe_pair(berr);
        return -1;
    }
    if (pb == 0) {
        dup2(link_p[0], STDIN_FILENO);
        dup2(bout[1], STDOUT_FILENO);
        dup2(berr[1], STDERR_FILENO);
        /* Stage b is the CPU-heavy symbolizer — yield to the profiled
         * workload so the profiler doesn't skew what it measures. */
        child_setup(CHILD_NICE | CHILD_KEEP_STDIN);
        execvp(argv_b[0], argv_b);
        _exit(127);
    }
    parent_after_fork(pb);

    /* Parent keeps only the read ends it polls */
    close_pipe_pair(link_p);
    close(aerr[1]); close(bout[1]); close(berr[1]);

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
    int killed = 0;

    struct timespec start;
    clock_gettime(CLOCK_MONOTONIC, &start);

    while (open_fds > 0 && !g_shutdown) {
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        int elapsed_ms = (int)((now.tv_sec - start.tv_sec) * 1000 +
                               (now.tv_nsec - start.tv_nsec) / 1000000);
        int remaining_ms = timeout_sec * 1000 - elapsed_ms;
        if (remaining_ms <= 0) {
            kill_child_group(pid_a, SIGKILL);
            kill_child_group(pid_b, SIGKILL);
            killed = 1;
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

    int grace = killed ? 0 : CHILD_GRACE_MS;
    reap_child(pid_a, grace);
    untrack_child(pid_a);
    int status_b = reap_child(pid_b, grace);
    untrack_child(pid_b);

    if (WIFEXITED(status_b))
        return WEXITSTATUS(status_b);
    return -1;
}
