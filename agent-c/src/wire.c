/*
 * PerfLens Device Agent — TCP framing + streaming zstd sink
 */

#include "agent.h"

/* --------------------------------------------------------------------------
 * TCP helpers
 * -------------------------------------------------------------------------- */

/* Detect a dead peer even when idle: without keepalive a dropped network
 * path leaves the agent blocked in recv() forever (so --server mode never
 * reconnects). ~2 minutes to declare the connection dead. */
void tcp_enable_keepalive(int fd)
{
    int on = 1;
    setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &on, sizeof(on));
#ifdef TCP_KEEPIDLE
    int idle = 60, intvl = 10, cnt = 6;
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPIDLE, &idle, sizeof(idle));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPINTVL, &intvl, sizeof(intvl));
    setsockopt(fd, IPPROTO_TCP, TCP_KEEPCNT, &cnt, sizeof(cnt));
#endif
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

int tcp_send_frame(int fd, const void *payload,
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
int tcp_recv_frame(int fd, char **payload, uint32_t *out_len,
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

    /* Server→agent frames are small JSON commands. A huge length means a
     * corrupt stream or a stray client — don't try to allocate it. */
    if (len > MAX_BUF_SIZE) {
        agent_warn("Oversized frame (%u bytes) — dropping connection", len);
        return -1;
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
 * Compression sink (in-process zstd, streaming)
 *
 * perf script output is compressed as it is read from the pipe, so the raw
 * text (up to MAX_BUF_SIZE) never sits in memory — only the compressed
 * stream, typically 20-40x smaller. Falls back to raw buffering when a
 * zstd context cannot be created.
 * -------------------------------------------------------------------------- */

void sink_init(struct sink *s, int want_compress)
{
    memset(s, 0, sizeof(*s));
    buf_init(&s->out);
    if (!want_compress) return;

    s->zcs = ZSTD_createCStream();
    if (s->zcs && !ZSTD_isError(
            ZSTD_CCtx_setParameter(s->zcs, ZSTD_c_compressionLevel,
                                   ZSTD_LEVEL))) {
        s->compress = 1;
    } else {
        if (s->zcs) { ZSTD_freeCStream(s->zcs); s->zcs = NULL; }
        agent_warn("zstd stream init failed — sending uncompressed");
    }
}

int sink_write(struct sink *s, const void *data, size_t len)
{
    if (s->error) return -1;
    if (len == 0) return 0;
    /* Same decompressed-size cap the buffered path always enforced */
    if (s->raw_len + len > MAX_BUF_SIZE) { s->error = 1; return -1; }
    s->raw_len += len;

    if (!s->compress) {
        if (buf_ensure(&s->out, s->out.len + len) < 0) { s->error = 1; return -1; }
        memcpy(s->out.data + s->out.len, data, len);
        s->out.len += len;
        return 0;
    }

    ZSTD_inBuffer in = { data, len, 0 };
    while (in.pos < in.size) {
        if (buf_ensure(&s->out, s->out.len + 4096) < 0) { s->error = 1; return -1; }
        ZSTD_outBuffer ob = { s->out.data, s->out.cap, s->out.len };
        size_t r = ZSTD_compressStream2(s->zcs, &ob, &in, ZSTD_e_continue);
        if (ZSTD_isError(r)) {
            agent_warn("zstd stream error: %s", ZSTD_getErrorName(r));
            s->error = 1;
            return -1;
        }
        s->out.len = ob.pos;
    }
    return 0;
}

/* Flush the zstd frame epilogue. Returns 0 on success. */
int sink_finish(struct sink *s)
{
    if (s->error) return -1;
    if (!s->compress) return 0;

    ZSTD_inBuffer in = { NULL, 0, 0 };
    size_t r;
    do {
        if (buf_ensure(&s->out, s->out.len + 4096) < 0) { s->error = 1; return -1; }
        ZSTD_outBuffer ob = { s->out.data, s->out.cap, s->out.len };
        r = ZSTD_compressStream2(s->zcs, &ob, &in, ZSTD_e_end);
        if (ZSTD_isError(r)) {
            agent_warn("zstd stream error: %s", ZSTD_getErrorName(r));
            s->error = 1;
            return -1;
        }
        s->out.len = ob.pos;
    } while (r != 0);
    return 0;
}

void sink_free(struct sink *s)
{
    if (s->zcs) { ZSTD_freeCStream(s->zcs); s->zcs = NULL; }
    buf_free(&s->out);
}

/* Like run_cmd(), but the child's stdout is streamed through a sink
 * instead of buffered whole. stderr is captured into err as usual. */
int run_cmd_to_sink(char *const argv[], struct sink *sink,
                           struct buf *err, int timeout_sec)
{
    if (err) err->len = 0;

    int out_fd, err_fd;
    pid_t pid = fork_cmd(argv, &out_fd, &err_fd);
    if (pid < 0) return -1;

    char *chunk = malloc(IO_CHUNK);
    if (!chunk) {
        kill(pid, SIGKILL);
        close(out_fd); close(err_fd);
        int ws;
        do { } while (waitpid(pid, &ws, 0) < 0 && errno == EINTR);
        untrack_child(pid);
        return -1;
    }

    struct pollfd fds[2];
    fds[0].fd = out_fd; fds[0].events = POLLIN;
    fds[1].fd = err_fd; fds[1].events = POLLIN;
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

        /* stdout -> sink */
        if (fds[0].fd >= 0 && (fds[0].revents & (POLLIN | POLLHUP))) {
            ssize_t n = read(fds[0].fd, chunk, IO_CHUNK);
            if (n > 0) {
                if (sink_write(sink, chunk, (size_t)n) < 0) {
                    /* Over the cap or compression failed — stop reading;
                     * the child gets EPIPE and exits, rc reflects it. */
                    close(fds[0].fd); fds[0].fd = -1; open_fds--;
                }
            } else {
                close(fds[0].fd); fds[0].fd = -1; open_fds--;
            }
        }

        /* stderr -> err buf (or discard) */
        if (fds[1].fd >= 0 && (fds[1].revents & (POLLIN | POLLHUP))) {
            if (!err) {
                ssize_t n = read(fds[1].fd, chunk, IO_CHUNK);
                if (n <= 0) { close(fds[1].fd); fds[1].fd = -1; open_fds--; }
            } else if (buf_ensure(err, err->len + 4096) < 0) {
                close(fds[1].fd); fds[1].fd = -1; open_fds--;
            } else {
                ssize_t n = read(fds[1].fd, err->data + err->len,
                                 err->cap - err->len);
                if (n > 0) {
                    err->len += (size_t)n;
                } else {
                    close(fds[1].fd); fds[1].fd = -1; open_fds--;
                }
            }
        }
    }

    if (fds[0].fd >= 0) close(fds[0].fd);
    if (fds[1].fd >= 0) close(fds[1].fd);
    free(chunk);

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

