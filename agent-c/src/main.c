/*
 * PerfLens Device Agent — agent state, session loop, run modes, CLI
 */

#include "agent.h"

/* --------------------------------------------------------------------------
 * Command queue (thread-safe, condition variable based)
 * -------------------------------------------------------------------------- */

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

void cmdq_push(struct cmd_queue *q, const char *json)
{
    struct cmd_entry *e = malloc(sizeof(*e));
    if (!e) return;
    e->json = strdup(json);
    if (!e->json) { free(e); return; }
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

static void agent_state_init(struct agent_state *a)
{
    a->sock_fd = -1;
    pthread_mutex_init(&a->sock_lock, NULL);
    a->state = AGENT_IDLE;
    pthread_mutex_init(&a->state_lock, NULL);
    a->pid = -1;
    a->frequency = DEFAULT_FREQ;
    a->duration = DEFAULT_DURATION;
    a->token = NULL;
    a->sel_events[0] = '\0';
    memset(&a->platform, 0, sizeof(a->platform));
    a->caps = NULL;
    a->collect_thread_active = 0;
    a->collect_stop = 0;
    a->session_done = 0;
    a->metrics_thread_active = 0;
    a->metrics_enabled = 1;
    a->metrics_interval = 2;
    a->metrics_network = 1;
    a->metrics_disk = 0;    /* extra cost on embedded targets — opt-in */
    a->metrics_threads = 0; /* opt-in, same reasoning */
    cmdq_init(&a->cmdq);
}

/* --------------------------------------------------------------------------
 * Send helpers (thread-safe via sock_lock)
 * -------------------------------------------------------------------------- */

int agent_send_frame(struct agent_state *a, const void *payload,
                            size_t len, uint8_t flag)
{
    pthread_mutex_lock(&a->sock_lock);
    int rc = tcp_send_frame(a->sock_fd, payload, len, flag);
    pthread_mutex_unlock(&a->sock_lock);
    return rc;
}

int agent_send_response(struct agent_state *a, const char *json)
{
    return agent_send_frame(a, json, strlen(json), FLAG_CMD_RESPONSE);
}

int agent_send_data(struct agent_state *a, const void *data,
                           size_t len, uint8_t flag)
{
    return agent_send_frame(a, data, len, flag);
}

int agent_send_metrics(struct agent_state *a, const char *json,
                       size_t len)
{
    return agent_send_frame(a, json, len, FLAG_METRICS);
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

    char token_field[300] = "";
    if (a->token && a->token[0]) {
        char esc_tok[256];
        json_escape(esc_tok, sizeof(esc_tok), a->token);
        snprintf(token_field, sizeof(token_field),
                 ",\"token\":\"%s\"", esc_tok);
    }

    char hello[1536];
    snprintf(hello, sizeof(hello),
        "{\"type\":\"hello\",\"version\":1,\"agent\":\"perflens\","
        "\"agent_version\":\"%s\"%s,"
        "\"platform\":{\"arch\":\"%s\",\"kernel\":\"%s\","
        "\"perf_version\":\"%s\",\"perf_event_paranoid\":%d}}",
        AGENT_VERSION, token_field,
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

    /* Start metrics thread */
    a->metrics_thread_active = 0;
    if (pthread_create(&a->metrics_thread, NULL, metrics_thread_fn, a) == 0) {
        a->metrics_thread_active = 1;
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
    kill_tracked_children();
    if (a->collect_thread_active) {
        pthread_join(a->collect_thread, NULL);
        a->collect_thread_active = 0;
    }
    if (a->metrics_thread_active) {
        pthread_join(a->metrics_thread, NULL);
        a->metrics_thread_active = 0;
    }

    /* shutdown() reliably wakes a recv thread blocked in recv(); close()
     * alone does not, and would let the fd number be reused while the
     * recv thread still reads from it. Close only after the join. */
    if (a->sock_fd >= 0)
        shutdown(a->sock_fd, SHUT_RDWR);

    /* Wait for recv thread */
    pthread_join(recv_tid, NULL);

    if (a->sock_fd >= 0) {
        close(a->sock_fd);
        a->sock_fd = -1;
        g_agent_sock_fd = -1;
    }

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

        tcp_enable_keepalive(conn_fd);
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
            /* Resolve every attempt — DNS may change between retries.
             * Numeric addresses first: they need no NSS, which matters in
             * glibc-static builds where DNS resolution may be unavailable. */
            struct addrinfo hints, *res = NULL;
            memset(&hints, 0, sizeof(hints));
            hints.ai_family = AF_INET;
            hints.ai_socktype = SOCK_STREAM;
            hints.ai_flags = AI_NUMERICHOST;
            char port_str[16];
            snprintf(port_str, sizeof(port_str), "%d", port);

            int gai = getaddrinfo(host, port_str, &hints, &res);
            if (gai != 0) {
                hints.ai_flags = 0;
                gai = getaddrinfo(host, port_str, &hints, &res);
            }
            if (gai != 0 || !res) {
                agent_log("Cannot resolve %s (%s), retrying in %.0fs...",
                          host, gai_strerror(gai), delay);
            } else {
                sock = socket(res->ai_family, res->ai_socktype,
                              res->ai_protocol);
                if (sock < 0) {
                    /* Transient (fd exhaustion etc.) — retry, don't exit */
                    agent_warn("socket() failed (%s), retrying in %.0fs...",
                               strerror(errno), delay);
                    freeaddrinfo(res);
                } else {
                    /* Connect timeout */
                    struct timeval tv;
                    tv.tv_sec = 30;
                    tv.tv_usec = 0;
                    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

                    int crc = connect(sock, res->ai_addr,
                                      (socklen_t)res->ai_addrlen);
                    freeaddrinfo(res);

                    if (crc == 0) {
                        agent_log("Connected to %s:%d", host, port);
                        break;
                    }

                    agent_log("Connection failed (%s), retrying in %.0fs...",
                              strerror(errno), delay);
                    close(sock);
                    sock = -1;
                }
            }

            /* Sleep with shutdown check (200ms ticks) */
            {
                int wait_ms = (int)(delay * 1000);
                struct timespec tick = {0, 200000000L};
                while (wait_ms > 0 && !g_shutdown) {
                    nanosleep(&tick, NULL);
                    wait_ms -= 200;
                }
            }

            if (delay < RECONNECT_MAX) delay *= 2;
            if (delay > RECONNECT_MAX) delay = RECONNECT_MAX;
        }

        if (sock < 0) continue;

        /* Clear connect timeout for recv/send during session */
        struct timeval no_tv;
        no_tv.tv_sec = 0;
        no_tv.tv_usec = 0;
        setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &no_tv, sizeof(no_tv));

        tcp_enable_keepalive(sock);
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
        "PerfLens Device Agent %s\n"
        "\n"
        "Modes:\n"
        "  --listen          Listen for server connections (daemon)\n"
        "  --server HOST     Connect to server (daemon; HOST may be a hostname)\n"
        "  --output FILE     Headless: collect and write to file ('-' for stdout)\n"
        "\n"
        "Options:\n"
        "  --pid PID         Process to profile (required for --output)\n"
        "  --port PORT       TCP port (default: %d)\n"
        "  --frequency HZ    Sampling frequency in Hz (default: %d)\n"
        "  --duration SECS   Duration of each collection in seconds (default: %d)\n"
        "  --rounds N        Collection rounds in --output mode (default: 1)\n"
        "  --token SECRET    Shared secret sent to the server in the hello\n"
        "                    (or set PERFLENS_TOKEN)\n"
        "  --update          Self-update from the latest GitHub release and exit\n"
        "                    (override base URL with PERFLENS_UPDATE_URL)\n"
        "  --version         Print version and exit\n"
        "  --help            Show this help message\n",
        prog, prog, prog, AGENT_VERSION,
        DEFAULT_PORT, DEFAULT_FREQ, DEFAULT_DURATION);
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
    int rounds = 1;
    int listen_mode = 0;
    int do_update = 0;
    char *output = NULL;
    const char *token = getenv("PERFLENS_TOKEN");

    enum { OPT_ROUNDS = 1000, OPT_TOKEN, OPT_UPDATE, OPT_VERSION };
    static struct option long_opts[] = {
        {"pid",       required_argument, NULL, 'p'},
        {"server",    required_argument, NULL, 's'},
        {"port",      required_argument, NULL, 'P'},
        {"frequency", required_argument, NULL, 'f'},
        {"duration",  required_argument, NULL, 'd'},
        {"listen",    no_argument,       NULL, 'l'},
        {"output",    required_argument, NULL, 'o'},
        {"rounds",    required_argument, NULL, OPT_ROUNDS},
        {"token",     required_argument, NULL, OPT_TOKEN},
        {"update",    no_argument,       NULL, OPT_UPDATE},
        {"version",   no_argument,       NULL, OPT_VERSION},
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
        case OPT_ROUNDS:  rounds = atoi(optarg); break;
        case OPT_TOKEN:   token  = optarg;       break;
        case OPT_UPDATE:  do_update = 1;         break;
        case OPT_VERSION:
            printf("perflens-agent %s\n", AGENT_VERSION);
            return 0;
        case 'h': print_usage(argv[0]); return 0;
        default:  print_usage(argv[0]); return 1;
        }
    }

    if (rounds < 1) rounds = 1;

    install_signal_handlers();

    if (do_update) {
        char msg[512];
        int rc = self_update(msg, sizeof(msg));
        agent_log("Self-update: %s", msg);
        return rc == 0 ? 0 : 1;
    }

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

        agent_log("Collecting perf data for PID %d (headless mode, %d round%s)",
                  pid, rounds, rounds == 1 ? "" : "s");

        FILE *f = NULL;
        if (strcmp(output, "-") == 0) {
            f = stdout;
        } else {
            f = fopen(output, "w");
            if (!f) {
                agent_log("Error: cannot open %s: %s", output, strerror(errno));
                free_capabilities(&caps);
                return 1;
            }
        }

        size_t total_len = 0;
        for (int r = 1; r <= rounds && !g_shutdown; r++) {
            if (!process_exists(pid)) {
                agent_log("Process %d exited", pid);
                break;
            }
            if (rounds > 1)
                agent_log("Round %d/%d...", r, rounds);
            size_t out_len = 0, raw_len = 0;
            uint8_t flag = FLAG_DATA_RAW;
            char *data = collect_one_round(&caps, NULL, pid, frequency,
                                           duration, 0,
                                           &out_len, &raw_len, &flag);
            if (data && out_len > 0) {
                fwrite(data, 1, out_len, f);
                fflush(f);
                total_len += out_len;
            } else {
                agent_log("Round %d: no data", r);
            }
            free(data);
        }

        if (f != stdout)
            fclose(f);
        free_capabilities(&caps);

        if (total_len == 0) {
            agent_log("No data collected.");
            return 1;
        }
        agent_log("Done. Output %zu bytes to %s.", total_len,
                  strcmp(output, "-") == 0 ? "stdout" : output);
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
    agent.token = token;
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
