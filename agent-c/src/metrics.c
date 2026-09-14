/*
 * PerfLens Device Agent — device health metrics
 *
 * Every /proc and /sys file the collector reads is opened once and re-read
 * with pread() each tick (procfs and sysfs attributes support it), where it
 * used to open, parse and close ~30 files every two seconds -- and the
 * /proc/meminfo parse alone ran ~350 sscanf() calls. The parsers take a
 * buffer, so agent-c/tests/test_metrics.c can feed them captured input.
 */

#include "agent.h"
#include <sys/statvfs.h>

#define MAX_THERMAL_ZONES 32
#define FD_COUNT_EVERY    5     /* readdir of /proc/<pid>/fd is the costly one */
#define METRICS_FRAME_CAP (256 * 1024)

/* A file re-read in place each tick. */
struct proc_file {
    int  fd;
    char path[128];
};

static void pf_init(struct proc_file *f, const char *path)
{
    f->fd = -1;
    snprintf(f->path, sizeof(f->path), "%s", path);
}

static void pf_close(struct proc_file *f)
{
    if (f->fd >= 0) close(f->fd);
    f->fd = -1;
}

/* Read the whole file into b (NUL-terminated). Returns b->len, or -1. A
 * file that fails to read is closed and reopened next time, which is what
 * a /proc/<pid> file needs once the process is gone and back. */
static ssize_t pf_read(struct proc_file *f, struct buf *b)
{
    if (f->fd < 0) {
        f->fd = open(f->path, O_RDONLY | O_CLOEXEC);
        if (f->fd < 0) return -1;
    }
    b->len = 0;
    for (;;) {
        if (buf_ensure_small(b, b->len + 4096 + 1, SMALL_BUF_SIZE) < 0) return -1;
        ssize_t n = pread(f->fd, b->data + b->len, 4096, (off_t)b->len);
        if (n < 0) {
            if (errno == EINTR) continue;
            pf_close(f);
            return -1;
        }
        if (n == 0) break;
        b->len += (size_t)n;
    }
    b->data[b->len] = '\0';
    return (ssize_t)b->len;
}

static long pf_read_long(struct proc_file *f)
{
    char tmp[64];
    if (f->fd < 0) {
        f->fd = open(f->path, O_RDONLY | O_CLOEXEC);
        if (f->fd < 0) return -1;
    }
    ssize_t n = pread(f->fd, tmp, sizeof(tmp) - 1, 0);
    if (n <= 0) { pf_close(f); return -1; }
    tmp[n] = '\0';
    char *end;
    long v = strtol(tmp, &end, 10);
    return end == tmp ? -1 : v;
}

typedef struct {
    int pid;
    int include_network;

    /* CPU delta state */
    unsigned long prev_cpu[8];   /* user,nice,sys,idle,iowait,irq,softirq,steal */
    int prev_cpu_valid;
    unsigned long (*prev_per_core)[8];
    unsigned long (*curr_per_core)[8];
    int core_cap;                /* rows allocated in the two arrays */
    int num_cores;

    /* Process CPU delta */
    unsigned long prev_proc_ticks;
    double prev_proc_time;
    int prev_proc_valid;
    int tick;
    int last_fds;

    /* Warn-once flags */
    int warned_temp;
    int warned_freq;
    int warned_proc_fd;

    long page_size;
    long clk_tck;

    /* Files kept open */
    struct proc_file f_stat, f_meminfo, f_loadavg, f_uptime, f_netdev;
    struct proc_file f_diskstats;
    struct proc_file f_pstat, f_pstatus, f_oom, f_pio;
    struct proc_file *f_freq;     /* one per core, lazily opened */
    int freq_cap;

    /* Thermal: the zone typed as a CPU/SoC sensor if there is one,
     * otherwise the hottest of all zones each tick. thermal_zone0 alone is
     * a PMIC, battery or board sensor on many SoCs. */
    struct proc_file f_temp[MAX_THERMAL_ZONES];
    int  temp_zones;
    int  temp_chosen;             /* index, or -1 for "max of all" */

    struct buf text;              /* scratch for pf_read */
} metrics_collector_t;

/* --------------------------------------------------------------------------
 * Parsers (pure: text in, numbers out)
 * -------------------------------------------------------------------------- */

static int parse_cpu_fields(const char *line, unsigned long *out)
{
    /* Skip "cpu" or "cpuN " prefix, parse up to 8 integers */
    const char *p = line;
    while (*p && *p != ' ') p++;
    int i;
    for (i = 0; i < 8; i++) {
        while (*p == ' ') p++;
        if (*p == '\0' || *p == '\n') break;
        out[i] = strtoul(p, (char **)&p, 10);
    }
    for (; i < 8; i++) out[i] = 0;
    return 1;
}

struct stat_totals {
    unsigned long cpu[8];
    unsigned long ctxt, intr;
    int procs_running, procs_blocked;
    int num_cores;               /* highest cpuN seen + 1 */
};

/* Parse /proc/stat. Per-core rows go into per_core[0..cap); cores past the
 * cap are still counted in num_cores. A hole (offline core) leaves zeros
 * in its row. */
static void parse_proc_stat(const char *text, struct stat_totals *t,
                            unsigned long (*per_core)[8], int cap)
{
    memset(t, 0, sizeof(*t));
    if (per_core) memset(per_core, 0, sizeof(per_core[0]) * (size_t)cap);
    const char *p = text;
    while (p && *p) {
        const char *nl = strchr(p, '\n');
        size_t len = nl ? (size_t)(nl - p) : strlen(p);
        if (strncmp(p, "cpu ", 4) == 0) {
            parse_cpu_fields(p, t->cpu);
        } else if (strncmp(p, "cpu", 3) == 0 && p[3] >= '0' && p[3] <= '9') {
            int cid = atoi(p + 3);
            if (cid >= 0) {
                if (cid + 1 > t->num_cores) t->num_cores = cid + 1;
                if (per_core && cid < cap) parse_cpu_fields(p, per_core[cid]);
            }
        } else if (strncmp(p, "ctxt ", 5) == 0) {
            t->ctxt = strtoul(p + 5, NULL, 10);
        } else if (strncmp(p, "intr ", 5) == 0) {
            t->intr = strtoul(p + 5, NULL, 10);
        } else if (strncmp(p, "procs_running ", 14) == 0) {
            t->procs_running = atoi(p + 14);
        } else if (strncmp(p, "procs_blocked ", 14) == 0) {
            t->procs_blocked = atoi(p + 14);
        }
        (void)len;
        if (!nl) break;
        p = nl + 1;
    }
}

static double calc_cpu_pct(const unsigned long *prev, const unsigned long *curr)
{
    unsigned long p_idle = prev[3] + prev[4];
    unsigned long c_idle = curr[3] + curr[4];
    unsigned long p_total = 0, c_total = 0;
    for (int i = 0; i < 8; i++) { p_total += prev[i]; c_total += curr[i]; }
    if (c_total < p_total || c_idle < p_idle) return 0.0;   /* counter reset */
    unsigned long d_total = c_total - p_total;
    unsigned long d_idle = c_idle - p_idle;
    if (d_total == 0) return 0.0;
    return 100.0 * (double)(d_total - d_idle) / (double)d_total;
}

struct meminfo {
    unsigned long total, available, free, buffers, cached, swap_total, swap_free;
    int have_available;
};

/* "Key:   12345 kB" lines, matched by prefix -- no sscanf per candidate. */
static unsigned long meminfo_value(const char *text, const char *key)
{
    size_t klen = strlen(key);
    const char *p = text;
    while (p && *p) {
        if (strncmp(p, key, klen) == 0 && p[klen] == ':')
            return strtoul(p + klen + 1, NULL, 10);
        const char *nl = strchr(p, '\n');
        if (!nl) break;
        p = nl + 1;
    }
    return 0;
}

static void parse_meminfo(const char *text, struct meminfo *m)
{
    m->total = meminfo_value(text, "MemTotal");
    m->free = meminfo_value(text, "MemFree");
    m->buffers = meminfo_value(text, "Buffers");
    m->cached = meminfo_value(text, "Cached");
    m->swap_total = meminfo_value(text, "SwapTotal");
    m->swap_free = meminfo_value(text, "SwapFree");
    m->have_available = strstr(text, "MemAvailable:") != NULL;
    m->available = m->have_available ? meminfo_value(text, "MemAvailable")
                                     /* kernels before 3.14: page cache is
                                      * reclaimable, so it is not "used" */
                                     : m->free + m->buffers + m->cached;
    if (m->total && m->available > m->total) m->available = m->total;
}

/* Prefer a zone whose type names the CPU or SoC. Returns -1 for none. */
static int choose_thermal_zone(const char *const *types, int n)
{
    static const char *const PREFERRED[] = {
        "x86_pkg_temp", "cpu", "soc", "core", "tsens", "mtktscpu", NULL
    };
    for (int k = 0; PREFERRED[k]; k++)
        for (int i = 0; i < n; i++)
            if (types[i] && str_contains_lower(types[i], strlen(types[i]),
                                               PREFERRED[k]))
                return i;
    return -1;
}

/* --------------------------------------------------------------------------
 * Collector setup
 * -------------------------------------------------------------------------- */

static void metrics_init(metrics_collector_t *mc)
{
    memset(mc, 0, sizeof(*mc));
    mc->page_size = sysconf(_SC_PAGESIZE);
    if (mc->page_size <= 0) mc->page_size = 4096;
    mc->clk_tck = sysconf(_SC_CLK_TCK);
    if (mc->clk_tck <= 0) mc->clk_tck = 100;
    mc->include_network = 1;
    mc->last_fds = -1;
    buf_init(&mc->text);

    pf_init(&mc->f_stat, "/proc/stat");
    pf_init(&mc->f_meminfo, "/proc/meminfo");
    pf_init(&mc->f_loadavg, "/proc/loadavg");
    pf_init(&mc->f_uptime, "/proc/uptime");
    pf_init(&mc->f_netdev, "/proc/net/dev");
    pf_init(&mc->f_diskstats, "/proc/diskstats");
    pf_init(&mc->f_pstat, "");
    pf_init(&mc->f_pstatus, "");
    pf_init(&mc->f_oom, "");
    pf_init(&mc->f_pio, "");

    /* Thermal zones, once */
    char *types[MAX_THERMAL_ZONES] = {0};
    for (int i = 0; i < MAX_THERMAL_ZONES; i++) {
        char path[128];
        snprintf(path, sizeof(path), "/sys/class/thermal/thermal_zone%d/type", i);
        FILE *f = fopen(path, "r");
        if (!f) break;
        char t[64] = "";
        if (fgets(t, sizeof(t), f)) {
            char *nl = strchr(t, '\n');
            if (nl) *nl = '\0';
        }
        fclose(f);
        types[i] = strdup(t);
        snprintf(path, sizeof(path), "/sys/class/thermal/thermal_zone%d/temp", i);
        pf_init(&mc->f_temp[i], path);
        mc->temp_zones = i + 1;
    }
    mc->temp_chosen = choose_thermal_zone((const char *const *)types,
                                          mc->temp_zones);
    if (mc->temp_zones)
        agent_log("Metrics: %d thermal zone%s, reporting %s", mc->temp_zones,
                  mc->temp_zones == 1 ? "" : "s",
                  mc->temp_chosen >= 0 ? types[mc->temp_chosen]
                                       : "the hottest zone");
    for (int i = 0; i < MAX_THERMAL_ZONES; i++) free(types[i]);
}

static void metrics_free(metrics_collector_t *mc)
{
    pf_close(&mc->f_stat); pf_close(&mc->f_meminfo); pf_close(&mc->f_loadavg);
    pf_close(&mc->f_uptime); pf_close(&mc->f_netdev); pf_close(&mc->f_diskstats);
    pf_close(&mc->f_pstat); pf_close(&mc->f_pstatus); pf_close(&mc->f_oom);
    pf_close(&mc->f_pio);
    for (int i = 0; i < mc->freq_cap; i++) pf_close(&mc->f_freq[i]);
    free(mc->f_freq);
    for (int i = 0; i < mc->temp_zones; i++) pf_close(&mc->f_temp[i]);
    free(mc->prev_per_core);
    free(mc->curr_per_core);
    buf_free(&mc->text);
}

static void metrics_set_pid(metrics_collector_t *mc, int pid)
{
    if (pid != mc->pid) {
        mc->pid = pid;
        mc->prev_proc_valid = 0;
        mc->last_fds = -1;
        char path[128];
        pf_close(&mc->f_pstat); pf_close(&mc->f_pstatus);
        pf_close(&mc->f_oom); pf_close(&mc->f_pio);
        snprintf(path, sizeof(path), "/proc/%d/stat", pid);   pf_init(&mc->f_pstat, path);
        snprintf(path, sizeof(path), "/proc/%d/status", pid); pf_init(&mc->f_pstatus, path);
        snprintf(path, sizeof(path), "/proc/%d/oom_score", pid); pf_init(&mc->f_oom, path);
        snprintf(path, sizeof(path), "/proc/%d/io", pid);     pf_init(&mc->f_pio, path);
    }
}

static int ensure_cores(metrics_collector_t *mc, int n)
{
    if (n <= mc->core_cap) return 0;
    int cap = mc->core_cap ? mc->core_cap : 16;
    while (cap < n) cap *= 2;
    unsigned long (*p)[8] = realloc(mc->prev_per_core, sizeof(*p) * (size_t)cap);
    if (!p) return -1;
    memset(p + mc->core_cap, 0, sizeof(*p) * (size_t)(cap - mc->core_cap));
    mc->prev_per_core = p;
    unsigned long (*c)[8] = realloc(mc->curr_per_core, sizeof(*c) * (size_t)cap);
    if (!c) return -1;
    mc->curr_per_core = c;
    struct proc_file *f = realloc(mc->f_freq, sizeof(*f) * (size_t)cap);
    if (!f) return -1;
    for (int i = mc->freq_cap; i < cap; i++) {
        char path[128];
        snprintf(path, sizeof(path),
                 "/sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq", i);
        pf_init(&f[i], path);
    }
    mc->f_freq = f;
    mc->freq_cap = cap;
    mc->core_cap = cap;
    return 0;
}

static double get_timestamp(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

/* --------------------------------------------------------------------------
 * Frames
 * -------------------------------------------------------------------------- */

static int collect_system_metrics(metrics_collector_t *mc, struct wbuf *w)
{
    double ts = get_timestamp();

    if (pf_read(&mc->f_stat, &mc->text) < 0) return -1;
    struct stat_totals probe;
    parse_proc_stat(mc->text.data, &probe, NULL, 0);
    if (ensure_cores(mc, probe.num_cores) < 0) return -1;
    struct stat_totals t;
    parse_proc_stat(mc->text.data, &t, mc->curr_per_core, mc->core_cap);
    int num_cores = t.num_cores;
    mc->num_cores = num_cores;

    /* CPU overall % */
    double cpu_overall = -1.0;
    int has_overall = 0;
    if (mc->prev_cpu_valid) {
        cpu_overall = calc_cpu_pct(mc->prev_cpu, t.cpu);
        has_overall = 1;
    }

    wbuf_addf(w, "{\"ts\":%.3f,\"type\":\"system\",\"cpu\":{\"overall_pct\":", ts);
    if (has_overall) wbuf_addf(w, "%.1f", cpu_overall);
    else wbuf_add(w, "null");

    /* Per-core % -- an offline core (no row, zeros) reads 0.0 */
    wbuf_add(w, ",\"per_core\":[");
    for (int i = 0; i < num_cores; i++) {
        double pct = 0.0;
        if (mc->prev_cpu_valid) {
            unsigned long z[8] = {0};
            if (memcmp(mc->prev_per_core[i], z, sizeof(z)) != 0)
                pct = calc_cpu_pct(mc->prev_per_core[i], mc->curr_per_core[i]);
        }
        wbuf_addf(w, "%s%.1f", i ? "," : "", pct);
    }
    wbuf_add(w, "]");
    memcpy(mc->prev_cpu, t.cpu, sizeof(t.cpu));
    memcpy(mc->prev_per_core, mc->curr_per_core,
           sizeof(mc->prev_per_core[0]) * (size_t)mc->core_cap);
    mc->prev_cpu_valid = 1;

    /* CPU frequency. A missing core is null, not the end of the list: the
     * loop used to stop at the first offline core and drop every later one. */
    int has_freq = 0;
    for (int i = 0; i < num_cores; i++) {
        long v = pf_read_long(&mc->f_freq[i]);
        if (v >= 0) { has_freq = 1; break; }
    }
    if (has_freq) {
        wbuf_add(w, ",\"freq_mhz\":[");
        for (int i = 0; i < num_cores; i++) {
            long v = pf_read_long(&mc->f_freq[i]);
            if (v >= 0) wbuf_addf(w, "%s%ld", i ? "," : "", v / 1000);
            else wbuf_addf(w, "%snull", i ? "," : "");
        }
        wbuf_add(w, "]");
    } else if (!mc->warned_freq) {
        mc->warned_freq = 1;
        agent_warn("Metrics: cpufreq not available (will not warn again)");
    }
    wbuf_addf(w, ",\"num_cores\":%d}", num_cores);

    /* Memory */
    struct meminfo m;
    memset(&m, 0, sizeof(m));
    if (pf_read(&mc->f_meminfo, &mc->text) >= 0)
        parse_meminfo(mc->text.data, &m);
    unsigned long mem_used = m.total > m.available ? m.total - m.available : 0;
    double mem_pct = m.total > 0 ? 100.0 * (double)mem_used / (double)m.total : 0.0;
    wbuf_addf(w, ",\"mem\":{\"total_kb\":%lu,\"used_kb\":%lu,\"available_kb\":%lu,"
              "\"buffers_kb\":%lu,\"cached_kb\":%lu,"
              "\"swap_total_kb\":%lu,\"swap_used_kb\":%lu,\"used_pct\":%.1f}",
              m.total, mem_used, m.available, m.buffers, m.cached,
              m.swap_total, m.swap_total > m.swap_free ? m.swap_total - m.swap_free : 0,
              mem_pct);

    /* Load average */
    double load_1m = 0, load_5m = 0, load_15m = 0;
    if (pf_read(&mc->f_loadavg, &mc->text) >= 0)
        sscanf(mc->text.data, "%lf %lf %lf", &load_1m, &load_5m, &load_15m);
    wbuf_addf(w, ",\"load\":{\"avg_1m\":%.2f,\"avg_5m\":%.2f,\"avg_15m\":%.2f}",
              load_1m, load_5m, load_15m);

    /* Temperature */
    long temp_raw = -1;
    if (mc->temp_chosen >= 0) {
        temp_raw = pf_read_long(&mc->f_temp[mc->temp_chosen]);
    } else {
        for (int i = 0; i < mc->temp_zones; i++) {
            long v = pf_read_long(&mc->f_temp[i]);
            if (v > temp_raw) temp_raw = v;
        }
    }
    if (temp_raw >= 0) {
        wbuf_addf(w, ",\"temp_c\":%d", (int)(temp_raw / 1000));
    } else if (!mc->warned_temp) {
        mc->warned_temp = 1;
        agent_warn("Metrics: no thermal zone readable (will not warn again)");
    }

    /* Uptime */
    double uptime = 0;
    if (pf_read(&mc->f_uptime, &mc->text) >= 0)
        uptime = strtod(mc->text.data, NULL);

    wbuf_addf(w, ",\"uptime_sec\":%lu,\"context_switches\":%lu,\"interrupts\":%lu,"
              "\"procs_running\":%d,\"procs_blocked\":%d}",
              (unsigned long)uptime, t.ctxt, t.intr,
              t.procs_running, t.procs_blocked);
    return w->truncated ? -1 : 0;
}

static int collect_process_metrics(metrics_collector_t *mc, struct wbuf *w)
{
    if (mc->pid <= 0) return -1;
    if (pf_read(&mc->f_pstat, &mc->text) < 0) return -1;
    char *raw = mc->text.data;

    /* Find last ')' to handle comm with spaces/parens */
    char *pend = strrchr(raw, ')');
    if (!pend) return -1;
    char *pstart = strchr(raw, '(');
    char comm[256] = "";
    if (pstart && pend > pstart) {
        size_t clen = (size_t)(pend - pstart - 1);
        if (clen >= sizeof(comm)) clen = sizeof(comm) - 1;
        memcpy(comm, pstart + 1, clen);
        comm[clen] = '\0';
    }

    /* Fields after comm (starting at field 3) */
    char *p = pend + 2;
    char proc_state = 'S';
    unsigned long minflt = 0, majflt = 0, utime = 0, stime = 0;
    unsigned long vsize = 0;
    long rss_pages = 0;
    int num_threads = 0;

    /* Parse fields: state(3) minflt(10) majflt(12) utime(14) stime(15) threads(20) vsize(23) rss(24) */
    int field = 3;
    while (*p && field <= 24) {
        while (*p == ' ') p++;
        if (*p == '\0') break;
        if (field == 3) {
            /* Field 3 is a single character (R/S/D/Z/T/etc.) */
            proc_state = *p;
            p++;
        } else {
            char *end;
            unsigned long val = strtoul(p, &end, 10);
            if (end == p) { p++; field++; continue; } /* skip unparseable */
            if (field == 10) minflt = val;
            else if (field == 12) majflt = val;
            else if (field == 14) utime = val;
            else if (field == 15) stime = val;
            else if (field == 20) num_threads = (int)val;
            else if (field == 23) vsize = val;
            else if (field == 24) rss_pages = (long)val;
            p = end;
        }
        field++;
    }

    long rss_kb = rss_pages * mc->page_size / 1024;
    long vsize_kb = (long)(vsize / 1024);

    /* CPU % delta: 100 = one core, the convention `top` uses */
    double now = get_timestamp();
    unsigned long total_ticks = utime + stime;
    char cpu_str[32];
    if (mc->prev_proc_valid) {
        double dt = now - mc->prev_proc_time;
        if (dt > 0 && total_ticks >= mc->prev_proc_ticks) {
            double pct = 100.0 * (double)(total_ticks - mc->prev_proc_ticks) /
                         (dt * (double)mc->clk_tck);
            snprintf(cpu_str, sizeof(cpu_str), "%.1f", pct);
        } else {
            snprintf(cpu_str, sizeof(cpu_str), "null");
        }
    } else {
        snprintf(cpu_str, sizeof(cpu_str), "null");
    }
    mc->prev_proc_ticks = total_ticks;
    mc->prev_proc_time = now;
    mc->prev_proc_valid = 1;

    /* Context switches from /proc/<pid>/status */
    long vol_csw = 0, invol_csw = 0;
    if (pf_read(&mc->f_pstatus, &mc->text) >= 0) {
        const char *v = strstr(mc->text.data, "\nvoluntary_ctxt_switches:");
        if (v) vol_csw = strtol(v + 25, NULL, 10);
        const char *nv = strstr(mc->text.data, "nonvoluntary_ctxt_switches:");
        if (nv) invol_csw = strtol(nv + 27, NULL, 10);
    }

    /* FD count: a readdir of /proc/<pid>/fd, once every few ticks */
    if (mc->last_fds < 0 || mc->tick % FD_COUNT_EVERY == 0) {
        char path[64];
        snprintf(path, sizeof(path), "/proc/%d/fd", mc->pid);
        DIR *d = opendir(path);
        if (d) {
            int fds = 0;
            struct dirent *ent;
            while ((ent = readdir(d)) != NULL) {
                if (ent->d_name[0] != '.') fds++;
            }
            closedir(d);
            mc->last_fds = fds;
        } else if (!mc->warned_proc_fd) {
            mc->warned_proc_fd = 1;
            agent_warn("Metrics: cannot read /proc/%d/fd (will not warn again)", mc->pid);
        }
    }
    int fds = mc->last_fds < 0 ? 0 : mc->last_fds;

    /* OOM score */
    long oom = pf_read_long(&mc->f_oom);
    if (oom < 0) oom = 0;

    /* Escape comm for JSON */
    char esc_comm[512];
    json_escape(esc_comm, sizeof(esc_comm), comm);

    double ts = get_timestamp();
    wbuf_addf(w,
        "{\"ts\":%.3f,\"type\":\"process\","
        "\"pid\":%d,\"comm\":\"%s\",\"state\":\"%c\","
        "\"cpu_pct\":%s,\"rss_kb\":%ld,\"vsize_kb\":%ld,"
        "\"threads\":%d,\"fds\":%d,"
        "\"voluntary_csw\":%ld,\"involuntary_csw\":%ld,"
        "\"minor_faults\":%lu,\"major_faults\":%lu,"
        "\"oom_score\":%ld}",
        ts, mc->pid, esc_comm, proc_state,
        cpu_str, rss_kb, vsize_kb,
        num_threads, fds,
        vol_csw, invol_csw,
        minflt, majflt, oom);
    return w->truncated ? -1 : 0;
}

static int collect_network_metrics(metrics_collector_t *mc, struct wbuf *w)
{
    if (pf_read(&mc->f_netdev, &mc->text) < 0) return -1;

    int count = 0;
    wbuf_addf(w, "{\"ts\":%.3f,\"type\":\"network\",\"interfaces\":{",
              get_timestamp());

    /* Skip 2 header lines */
    const char *p = mc->text.data;
    for (int i = 0; i < 2 && p; i++) {
        p = strchr(p, '\n');
        if (p) p++;
    }

    while (p && *p) {
        const char *nl = strchr(p, '\n');
        size_t len = nl ? (size_t)(nl - p) : strlen(p);
        char line[512];
        if (len >= sizeof(line)) len = sizeof(line) - 1;
        memcpy(line, p, len);
        line[len] = '\0';
        p = nl ? nl + 1 : NULL;

        char iface[32];
        char *colon = strchr(line, ':');
        if (!colon) continue;
        char *s = line;
        while (*s == ' ') s++;
        size_t ilen = (size_t)(colon - s);
        if (ilen >= sizeof(iface)) ilen = sizeof(iface) - 1;
        memcpy(iface, s, ilen);
        iface[ilen] = '\0';
        if (strcmp(iface, "lo") == 0) continue;

        char *q = colon + 1;
        unsigned long fields[16] = {0};
        for (int fi = 0; fi < 16 && *q; fi++) {
            while (*q == ' ') q++;
            fields[fi] = strtoul(q, &q, 10);
        }
        char esc_iface[64];
        json_escape(esc_iface, sizeof(esc_iface), iface);
        wbuf_addf(w,
            "%s\"%s\":{\"rx_bytes\":%lu,\"rx_packets\":%lu,\"rx_errors\":%lu,"
            "\"rx_drops\":%lu,\"tx_bytes\":%lu,\"tx_packets\":%lu,\"tx_errors\":%lu}",
            count > 0 ? "," : "", esc_iface,
            fields[0], fields[1], fields[2], fields[3],
            fields[8], fields[9], fields[10]);
        count++;
    }
    if (count == 0) return -1;
    wbuf_add(w, "}}");
    return w->truncated ? -1 : 0;
}

/* Disk I/O (opt-in via configure_metrics {"disk": true}): cumulative
 * counters from /proc/diskstats plus per-process /proc/<pid>/io. The
 * server/UI computes rates from consecutive snapshots, like network. */
static int collect_disk_metrics(metrics_collector_t *mc, struct wbuf *w)
{
    if (pf_read(&mc->f_diskstats, &mc->text) < 0) return -1;

    wbuf_addf(w, "{\"ts\":%.3f,\"type\":\"disk\",\"devices\":{", get_timestamp());
    int count = 0;
    char included[8][64];  /* whole-disk names already emitted */

    const char *p = mc->text.data;
    while (p && *p && count < 8) {
        const char *nl = strchr(p, '\n');
        size_t len = nl ? (size_t)(nl - p) : strlen(p);
        char line[512];
        if (len >= sizeof(line)) len = sizeof(line) - 1;
        memcpy(line, p, len);
        line[len] = '\0';
        p = nl ? nl + 1 : NULL;

        unsigned int major, minor;
        char name[64];
        unsigned long rd_ios, rd_merges, rd_sectors, rd_ms;
        unsigned long wr_ios, wr_merges, wr_sectors, wr_ms;
        int n = sscanf(line, "%u %u %63s %lu %lu %lu %lu %lu %lu %lu %lu",
                       &major, &minor, name,
                       &rd_ios, &rd_merges, &rd_sectors, &rd_ms,
                       &wr_ios, &wr_merges, &wr_sectors, &wr_ms);
        if (n < 11) continue;
        if (strncmp(name, "loop", 4) == 0 || strncmp(name, "ram", 3) == 0 ||
            strncmp(name, "zram", 4) == 0)
            continue;
        if (rd_ios == 0 && wr_ios == 0) continue;  /* never-used device */
        /* Skip partitions: the kernel lists the whole disk first (sda
         * before sda1, mmcblk0 before mmcblk0p1), so anything prefixed
         * by an already-included name is a partition of it. */
        int is_part = 0;
        for (int i = 0; i < count; i++) {
            if (strncmp(name, included[i], strlen(included[i])) == 0) {
                is_part = 1;
                break;
            }
        }
        if (is_part) continue;

        char esc_name[128];
        json_escape(esc_name, sizeof(esc_name), name);
        wbuf_addf(w,
            "%s\"%s\":{\"reads\":%lu,\"read_bytes\":%llu,"
            "\"writes\":%lu,\"write_bytes\":%llu,"
            "\"read_ms\":%lu,\"write_ms\":%lu}",
            count > 0 ? "," : "", esc_name,
            rd_ios, (unsigned long long)rd_sectors * 512,
            wr_ios, (unsigned long long)wr_sectors * 512,
            rd_ms, wr_ms);
        snprintf(included[count], sizeof(included[count]), "%s", name);
        count++;
    }
    if (count == 0) return -1;
    wbuf_add(w, "}");

    /* Per-process I/O — readable only for same-uid processes (or root) */
    if (mc->pid > 0 && pf_read(&mc->f_pio, &mc->text) >= 0) {
        unsigned long long rb = 0, wb = 0, syscr = 0, syscw = 0;
        const char *t = mc->text.data;
        const char *k;
        if ((k = strstr(t, "read_bytes:"))) rb = strtoull(k + 11, NULL, 10);
        if ((k = strstr(t, "write_bytes:"))) wb = strtoull(k + 12, NULL, 10);
        if ((k = strstr(t, "syscr:"))) syscr = strtoull(k + 6, NULL, 10);
        if ((k = strstr(t, "syscw:"))) syscw = strtoull(k + 6, NULL, 10);
        wbuf_addf(w, ",\"proc\":{\"read_bytes\":%llu,\"write_bytes\":%llu,"
                  "\"syscr\":%llu,\"syscw\":%llu}", rb, wb, syscr, syscw);
    }
    wbuf_add(w, "}");
    return w->truncated ? -1 : 0;
}

/* Per-thread stats (opt-in via configure_metrics {"threads": true}):
 * tid, comm, state, and cumulative CPU ticks for every thread of the
 * profiled process. The UI computes per-thread CPU%% from consecutive
 * snapshots using the included clk_tck. Capped at 64 threads. */
static int collect_thread_metrics(metrics_collector_t *mc, struct wbuf *w)
{
    if (mc->pid <= 0) return -1;
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/task", mc->pid);
    DIR *d = opendir(path);
    if (!d) return -1;

    wbuf_addf(w, "{\"ts\":%.3f,\"type\":\"threads\",\"pid\":%d,\"clk_tck\":%ld,"
              "\"threads\":[", get_timestamp(), mc->pid, mc->clk_tck);
    int count = 0;
    struct dirent *ent;

    while ((ent = readdir(d)) != NULL && count < 64) {
        char *end;
        int tid = (int)strtol(ent->d_name, &end, 10);
        if (*end != '\0' || tid <= 0) continue;

        char tpath[96];
        snprintf(tpath, sizeof(tpath), "/proc/%d/task/%d/stat", mc->pid, tid);
        FILE *f = fopen(tpath, "r");
        if (!f) continue;
        char line[1024];
        char *got = fgets(line, sizeof(line), f);
        fclose(f);
        if (!got) continue;

        /* comm between parens (may contain spaces); fields after ')' */
        char *pstart = strchr(line, '(');
        char *pend = strrchr(line, ')');
        if (!pstart || !pend || pend <= pstart) continue;
        char comm[64];
        size_t clen = (size_t)(pend - pstart - 1);
        if (clen >= sizeof(comm)) clen = sizeof(comm) - 1;
        memcpy(comm, pstart + 1, clen);
        comm[clen] = '\0';

        char *p = pend + 2;
        char tstate = (*p >= 'A' && *p <= 'z') ? *p : '?';
        unsigned long utime = 0, stime = 0;
        int field = 3;
        while (*p && field <= 15) {
            while (*p == ' ') p++;
            if (*p == '\0') break;
            if (field == 14) {
                utime = strtoul(p, NULL, 10);
            } else if (field == 15) {
                stime = strtoul(p, NULL, 10);
                break;
            }
            while (*p && *p != ' ') p++;
            field++;
        }

        char esc_comm[128];
        json_escape(esc_comm, sizeof(esc_comm), comm);
        wbuf_addf(w, "%s{\"tid\":%d,\"comm\":\"%s\",\"state\":\"%c\",\"ticks\":%lu}",
                  count > 0 ? "," : "", tid, esc_comm, tstate, utime + stime);
        count++;
    }
    closedir(d);
    if (count == 0) return -1;
    wbuf_add(w, "]}");
    return w->truncated ? -1 : 0;
}

void *metrics_thread_fn(void *arg)
{
    struct agent_state *a = (struct agent_state *)arg;
    block_signals_in_thread();

    metrics_collector_t mc;
    metrics_init(&mc);
    char *frame = malloc(METRICS_FRAME_CAP);
    if (!frame) { metrics_free(&mc); return NULL; }

    while (!g_shutdown && !a->session_done) {
        /* Snapshot config + pid under the lock once per tick */
        int enabled, interval, network, disk, threads, pid = 0;
        pthread_mutex_lock(&a->state_lock);
        enabled  = a->metrics_enabled;
        interval = a->metrics_interval;
        network  = a->metrics_network;
        disk     = a->metrics_disk;
        threads  = a->metrics_threads;
        if (a->state == AGENT_PROFILING || a->state == AGENT_PAUSED)
            pid = a->pid;
        pthread_mutex_unlock(&a->state_lock);

        if (!enabled) {
            session_sleep(a, interval * 1000);
            continue;
        }

        metrics_set_pid(&mc, pid);
        mc.include_network = network;
        mc.tick++;

        struct wbuf w;
        int rc;
#define EMIT(collector)                                                   \
        do {                                                              \
            wbuf_init(&w, frame, METRICS_FRAME_CAP);                      \
            rc = collector(&mc, &w);                                      \
            if (rc == 0 && agent_send_metrics(a, w.p, w.len) < 0) goto out; \
        } while (0)

        EMIT(collect_system_metrics);
        if (mc.pid > 0) EMIT(collect_process_metrics);
        if (mc.include_network) EMIT(collect_network_metrics);
        if (disk) EMIT(collect_disk_metrics);
        if (threads && mc.pid > 0) EMIT(collect_thread_metrics);
#undef EMIT

        session_sleep(a, interval * 1000);
    }
out:
    free(frame);
    metrics_free(&mc);
    return NULL;
}
