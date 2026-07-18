/*
 * PerfLens Device Agent — device health metrics
 */

#include "agent.h"

/* --------------------------------------------------------------------------
 * Metrics collector
 * -------------------------------------------------------------------------- */

typedef struct {
    int pid;
    int include_network;

    /* CPU delta state */
    unsigned long prev_cpu[8];   /* user,nice,sys,idle,iowait,irq,softirq,steal */
    int prev_cpu_valid;
    unsigned long prev_per_core[128][8];
    int num_cores;

    /* Process CPU delta */
    unsigned long prev_proc_ticks;
    double prev_proc_time;
    int prev_proc_valid;

    /* Warn-once flags */
    int warned_temp;
    int warned_freq;
    int warned_proc_fd;

    long page_size;
    long clk_tck;
} metrics_collector_t;

static void metrics_init(metrics_collector_t *mc)
{
    memset(mc, 0, sizeof(*mc));
    mc->page_size = sysconf(_SC_PAGESIZE);
    if (mc->page_size <= 0) mc->page_size = 4096;
    mc->clk_tck = sysconf(_SC_CLK_TCK);
    if (mc->clk_tck <= 0) mc->clk_tck = 100;
    mc->include_network = 1;
}

static void metrics_set_pid(metrics_collector_t *mc, int pid)
{
    if (pid != mc->pid) {
        mc->pid = pid;
        mc->prev_proc_valid = 0;
    }
}

static double get_timestamp(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

static double calc_cpu_pct(const unsigned long *prev, const unsigned long *curr)
{
    unsigned long p_idle = prev[3] + prev[4];
    unsigned long c_idle = curr[3] + curr[4];
    unsigned long p_total = 0, c_total = 0;
    int i;
    for (i = 0; i < 8; i++) { p_total += prev[i]; c_total += curr[i]; }
    unsigned long d_total = c_total - p_total;
    unsigned long d_idle = c_idle - p_idle;
    if (d_total == 0) return 0.0;
    return 100.0 * (double)(d_total - d_idle) / (double)d_total;
}

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

static int collect_system_metrics(metrics_collector_t *mc, char *buf, size_t bufsz)
{
    double ts = get_timestamp();
    FILE *f;
    char line[512];
    unsigned long curr_cpu[8] = {0};
    unsigned long curr_per_core[128][8];
    int num_cores = 0;
    unsigned long ctxt = 0, interrupts = 0;
    int procs_running = 0, procs_blocked = 0;
    int has_overall = 0;

    memset(curr_per_core, 0, sizeof(curr_per_core));

    f = fopen("/proc/stat", "r");
    if (!f) return -1;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "cpu ", 4) == 0) {
            parse_cpu_fields(line, curr_cpu);
        } else if (strncmp(line, "cpu", 3) == 0 && line[3] >= '0' && line[3] <= '9') {
            int cid = atoi(line + 3);
            if (cid >= 0 && cid < 128) {
                parse_cpu_fields(line, curr_per_core[cid]);
                if (cid + 1 > num_cores) num_cores = cid + 1;
            }
        } else if (strncmp(line, "ctxt ", 5) == 0) {
            ctxt = strtoul(line + 5, NULL, 10);
        } else if (strncmp(line, "intr ", 5) == 0) {
            interrupts = strtoul(line + 5, NULL, 10);
        } else if (strncmp(line, "procs_running ", 14) == 0) {
            procs_running = atoi(line + 14);
        } else if (strncmp(line, "procs_blocked ", 14) == 0) {
            procs_blocked = atoi(line + 14);
        }
    }
    fclose(f);
    mc->num_cores = num_cores;

    /* CPU overall % */
    double cpu_overall = -1.0;
    if (mc->prev_cpu_valid) {
        cpu_overall = calc_cpu_pct(mc->prev_cpu, curr_cpu);
        has_overall = 1;
    }
    memcpy(mc->prev_cpu, curr_cpu, sizeof(curr_cpu));
    mc->prev_cpu_valid = 1;

    /* Per-core % */
    char core_str[2048] = "";
    int coff = 0;
    int i;
    for (i = 0; i < num_cores && i < 128; i++) {
        double pct = 0.0;
        if (mc->prev_cpu_valid) {
            /* prev_per_core was set on previous call */
            unsigned long z[8] = {0};
            if (memcmp(mc->prev_per_core[i], z, sizeof(z)) != 0)
                pct = calc_cpu_pct(mc->prev_per_core[i], curr_per_core[i]);
        }
        if (coff > 0) coff += snprintf(core_str + coff, sizeof(core_str) - coff, ",");
        coff += snprintf(core_str + coff, sizeof(core_str) - coff, "%.1f", pct);
    }
    memcpy(mc->prev_per_core, curr_per_core, sizeof(curr_per_core));

    /* CPU frequency */
    char freq_str[1024] = "";
    int foff = 0;
    int has_freq = 0;
    for (i = 0; i < num_cores; i++) {
        char fpath[128];
        snprintf(fpath, sizeof(fpath),
                 "/sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq", i);
        long val = read_int_file(fpath);
        if (val < 0) break;
        has_freq = 1;
        if (foff > 0) foff += snprintf(freq_str + foff, sizeof(freq_str) - foff, ",");
        foff += snprintf(freq_str + foff, sizeof(freq_str) - foff, "%ld", val / 1000);
    }
    if (!has_freq && !mc->warned_freq) {
        mc->warned_freq = 1;
        fprintf(stderr, "[perflens-agent] WARNING: Metrics: cpufreq not available "
                "(will not warn again)\n");
    }

    /* Memory */
    unsigned long mem_total = 0, mem_avail = 0, mem_free = 0;
    unsigned long buffers = 0, cached = 0, swap_total = 0, swap_free = 0;
    f = fopen("/proc/meminfo", "r");
    if (f) {
        while (fgets(line, sizeof(line), f)) {
            unsigned long v;
            if (sscanf(line, "MemTotal: %lu kB", &v) == 1) mem_total = v;
            else if (sscanf(line, "MemAvailable: %lu kB", &v) == 1) mem_avail = v;
            else if (sscanf(line, "MemFree: %lu kB", &v) == 1) mem_free = v;
            else if (sscanf(line, "Buffers: %lu kB", &v) == 1) buffers = v;
            else if (sscanf(line, "Cached: %lu kB", &v) == 1) cached = v;
            else if (sscanf(line, "SwapTotal: %lu kB", &v) == 1) swap_total = v;
            else if (sscanf(line, "SwapFree: %lu kB", &v) == 1) swap_free = v;
        }
        fclose(f);
    }
    if (mem_avail == 0 && mem_free > 0) mem_avail = mem_free;
    unsigned long mem_used = mem_total - mem_avail;
    double mem_pct = mem_total > 0 ? 100.0 * mem_used / mem_total : 0.0;

    /* Load average */
    double load_1m = 0, load_5m = 0, load_15m = 0;
    f = fopen("/proc/loadavg", "r");
    if (f) { if (fscanf(f, "%lf %lf %lf", &load_1m, &load_5m, &load_15m) < 1) {;} fclose(f); }

    /* Temperature */
    long temp_raw = read_int_file("/sys/class/thermal/thermal_zone0/temp");
    int has_temp = (temp_raw >= 0);
    int temp_c = has_temp ? (int)(temp_raw / 1000) : 0;
    if (!has_temp && !mc->warned_temp) {
        mc->warned_temp = 1;
        fprintf(stderr, "[perflens-agent] WARNING: Metrics: thermal_zone0 not found "
                "(will not warn again)\n");
    }

    /* Uptime */
    double uptime = 0;
    f = fopen("/proc/uptime", "r");
    if (f) { if (fscanf(f, "%lf", &uptime) < 1) {;} fclose(f); }

    /* Build optional sections */
    char temp_str[32] = "";
    if (has_temp) snprintf(temp_str, sizeof(temp_str), "\"temp_c\":%d,", temp_c);

    char freq_section[1100] = "";
    if (has_freq)
        snprintf(freq_section, sizeof(freq_section),
                 "\"freq_mhz\":[%s],", freq_str);

    char cpu_pct_str[32];
    if (has_overall)
        snprintf(cpu_pct_str, sizeof(cpu_pct_str), "%.1f", cpu_overall);
    else
        snprintf(cpu_pct_str, sizeof(cpu_pct_str), "null");

    int n = snprintf(buf, bufsz,
        "{\"ts\":%.3f,\"type\":\"system\","
        "\"cpu\":{\"overall_pct\":%s,\"per_core\":[%s],%s\"num_cores\":%d},"
        "\"mem\":{\"total_kb\":%lu,\"used_kb\":%lu,\"available_kb\":%lu,"
        "\"buffers_kb\":%lu,\"cached_kb\":%lu,"
        "\"swap_total_kb\":%lu,\"swap_used_kb\":%lu,\"used_pct\":%.1f},"
        "\"load\":{\"avg_1m\":%.2f,\"avg_5m\":%.2f,\"avg_15m\":%.2f},"
        "%s"
        "\"uptime_sec\":%lu,"
        "\"context_switches\":%lu,\"interrupts\":%lu,"
        "\"procs_running\":%d,\"procs_blocked\":%d}",
        ts, cpu_pct_str, core_str, freq_section, num_cores,
        mem_total, mem_used, mem_avail, buffers, cached,
        swap_total, swap_total - swap_free, mem_pct,
        load_1m, load_5m, load_15m,
        temp_str,
        (unsigned long)uptime,
        ctxt, interrupts,
        procs_running, procs_blocked);
    return n;
}

static int collect_process_metrics(metrics_collector_t *mc, char *buf, size_t bufsz)
{
    if (mc->pid <= 0) return -1;
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/stat", mc->pid);
    FILE *f = fopen(path, "r");
    if (!f) return -1;

    char raw[2048];
    size_t nr = fread(raw, 1, sizeof(raw) - 1, f);
    fclose(f);
    raw[nr] = '\0';

    /* Find last ')' to handle comm with spaces/parens */
    char *pend = strrchr(raw, ')');
    if (!pend) return -1;
    char *pstart = strchr(raw, '(');
    char comm[256] = "";
    if (pstart && pend > pstart) {
        size_t clen = pend - pstart - 1;
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

    /* CPU % delta */
    double now = get_timestamp();
    unsigned long total_ticks = utime + stime;
    char cpu_str[32];
    if (mc->prev_proc_valid) {
        double dt = now - mc->prev_proc_time;
        if (dt > 0) {
            double pct = 100.0 * (double)(total_ticks - mc->prev_proc_ticks) /
                         (dt * mc->clk_tck);
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
    snprintf(path, sizeof(path), "/proc/%d/status", mc->pid);
    f = fopen(path, "r");
    if (f) {
        char sline[256];
        while (fgets(sline, sizeof(sline), f)) {
            if (strncmp(sline, "voluntary_ctxt_switches:", 24) == 0)
                vol_csw = strtol(sline + 24, NULL, 10);
            else if (strncmp(sline, "nonvoluntary_ctxt_switches:", 27) == 0)
                invol_csw = strtol(sline + 27, NULL, 10);
        }
        fclose(f);
    }

    /* FD count */
    int fds = 0;
    snprintf(path, sizeof(path), "/proc/%d/fd", mc->pid);
    DIR *d = opendir(path);
    if (d) {
        struct dirent *ent;
        while ((ent = readdir(d)) != NULL) {
            if (ent->d_name[0] != '.') fds++;
        }
        closedir(d);
    } else if (!mc->warned_proc_fd) {
        mc->warned_proc_fd = 1;
        fprintf(stderr, "[perflens-agent] WARNING: Metrics: cannot read /proc/%d/fd "
                "(will not warn again)\n", mc->pid);
    }

    /* OOM score */
    snprintf(path, sizeof(path), "/proc/%d/oom_score", mc->pid);
    long oom = read_int_file(path);
    if (oom < 0) oom = 0;

    /* Escape comm for JSON */
    char esc_comm[512];
    json_escape(esc_comm, sizeof(esc_comm), comm);

    double ts = get_timestamp();
    int n = snprintf(buf, bufsz,
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
    return n;
}

static int collect_network_metrics(metrics_collector_t *mc, char *buf, size_t bufsz)
{
    FILE *f = fopen("/proc/net/dev", "r");
    if (!f) return -1;

    char line[512];
    int off = 0;
    int count = 0;

    off += snprintf(buf + off, bufsz - off,
                    "{\"ts\":%.3f,\"type\":\"network\",\"interfaces\":{",
                    get_timestamp());

    /* Skip 2 header lines */
    if (fgets(line, sizeof(line), f) == NULL) { fclose(f); return -1; }
    if (fgets(line, sizeof(line), f) == NULL) { fclose(f); return -1; }

    while (fgets(line, sizeof(line), f)) {
        char iface[32];
        unsigned long rx_bytes, rx_packets, rx_errs, rx_drops;
        unsigned long tx_bytes, tx_packets, tx_errs;
        /* Parse: iface: rx_bytes rx_packets rx_errs rx_drop ... tx_bytes tx_packets tx_errs */
        char *colon = strchr(line, ':');
        if (!colon) continue;
        /* Extract interface name */
        char *s = line;
        while (*s == ' ') s++;
        size_t ilen = colon - s;
        if (ilen >= sizeof(iface)) ilen = sizeof(iface) - 1;
        memcpy(iface, s, ilen);
        iface[ilen] = '\0';
        if (strcmp(iface, "lo") == 0) continue;

        char *p = colon + 1;
        /* Fields: rx_bytes rx_packets rx_errs rx_drop rx_fifo rx_frame rx_compressed rx_multicast
                   tx_bytes tx_packets tx_errs */
        unsigned long fields[16] = {0};
        int fi;
        for (fi = 0; fi < 16 && *p; fi++) {
            while (*p == ' ') p++;
            fields[fi] = strtoul(p, &p, 10);
        }
        rx_bytes = fields[0]; rx_packets = fields[1];
        rx_errs = fields[2]; rx_drops = fields[3];
        tx_bytes = fields[8]; tx_packets = fields[9]; tx_errs = fields[10];

        /* Build the entry separately and bounds-check before appending:
         * hosts with many interfaces (container veth pairs) can exceed
         * the buffer, and off > bufsz would underflow bufsz - off. */
        char entry[512];
        int elen = snprintf(entry, sizeof(entry),
            "%s\"%s\":{\"rx_bytes\":%lu,\"rx_packets\":%lu,\"rx_errors\":%lu,"
            "\"rx_drops\":%lu,\"tx_bytes\":%lu,\"tx_packets\":%lu,\"tx_errors\":%lu}",
            count > 0 ? "," : "", iface,
            rx_bytes, rx_packets, rx_errs, rx_drops,
            tx_bytes, tx_packets, tx_errs);
        if (elen < 0 || (size_t)elen >= sizeof(entry))
            continue;
        if ((size_t)off + (size_t)elen + 3 > bufsz)
            break;  /* buffer full — keep the interfaces we have */
        memcpy(buf + off, entry, (size_t)elen);
        off += elen;
        count++;
    }
    fclose(f);
    if (count == 0) return -1;

    memcpy(buf + off, "}}", 3);
    off += 2;
    return off;
}

/* Disk I/O (opt-in via configure_metrics {"disk": true}): cumulative
 * counters from /proc/diskstats plus per-process /proc/<pid>/io. The
 * server/UI computes rates from consecutive snapshots, like network. */
static int collect_disk_metrics(metrics_collector_t *mc, char *buf, size_t bufsz)
{
    FILE *f = fopen("/proc/diskstats", "r");
    if (!f) return -1;

    char line[512];
    int off = snprintf(buf, bufsz,
                       "{\"ts\":%.3f,\"type\":\"disk\",\"devices\":{",
                       get_timestamp());
    int count = 0;
    char included[8][64];  /* whole-disk names already emitted */

    while (fgets(line, sizeof(line), f) && count < 8) {
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

        char entry[256];
        int elen = snprintf(entry, sizeof(entry),
            "%s\"%s\":{\"reads\":%lu,\"read_bytes\":%llu,"
            "\"writes\":%lu,\"write_bytes\":%llu,"
            "\"read_ms\":%lu,\"write_ms\":%lu}",
            count > 0 ? "," : "", name,
            rd_ios, (unsigned long long)rd_sectors * 512,
            wr_ios, (unsigned long long)wr_sectors * 512,
            rd_ms, wr_ms);
        if (elen < 0 || (size_t)elen >= sizeof(entry))
            continue;
        if ((size_t)off + (size_t)elen + 256 > bufsz)
            break;
        memcpy(buf + off, entry, (size_t)elen);
        off += elen;
        snprintf(included[count], sizeof(included[count]), "%s", name);
        count++;
    }
    fclose(f);
    if (count == 0) return -1;

    off += snprintf(buf + off, bufsz - off, "}");

    /* Per-process I/O — readable only for same-uid processes (or root) */
    if (mc->pid > 0) {
        char path[64];
        snprintf(path, sizeof(path), "/proc/%d/io", mc->pid);
        FILE *pf = fopen(path, "r");
        if (pf) {
            unsigned long long rb = 0, wb = 0, syscr = 0, syscw = 0;
            while (fgets(line, sizeof(line), pf)) {
                if (sscanf(line, "read_bytes: %llu", &rb) == 1) continue;
                if (sscanf(line, "write_bytes: %llu", &wb) == 1) continue;
                if (sscanf(line, "syscr: %llu", &syscr) == 1) continue;
                if (sscanf(line, "syscw: %llu", &syscw) == 1) continue;
            }
            fclose(pf);
            char entry[192];
            int elen = snprintf(entry, sizeof(entry),
                ",\"proc\":{\"read_bytes\":%llu,\"write_bytes\":%llu,"
                "\"syscr\":%llu,\"syscw\":%llu}",
                rb, wb, syscr, syscw);
            if (elen > 0 && (size_t)elen < sizeof(entry) &&
                (size_t)off + (size_t)elen + 2 <= bufsz) {
                memcpy(buf + off, entry, (size_t)elen);
                off += elen;
            }
        }
    }

    memcpy(buf + off, "}", 2);
    off += 1;
    return off;
}

/* Per-thread stats (opt-in via configure_metrics {"threads": true}):
 * tid, comm, state, and cumulative CPU ticks for every thread of the
 * profiled process. The UI computes per-thread CPU%% from consecutive
 * snapshots using the included clk_tck. Capped at 64 threads. */
static int collect_thread_metrics(metrics_collector_t *mc, char *buf, size_t bufsz)
{
    if (mc->pid <= 0) return -1;
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/task", mc->pid);
    DIR *d = opendir(path);
    if (!d) return -1;

    int off = snprintf(buf, bufsz,
        "{\"ts\":%.3f,\"type\":\"threads\",\"pid\":%d,\"clk_tck\":%ld,"
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

        char entry[224];
        int elen = snprintf(entry, sizeof(entry),
            "%s{\"tid\":%d,\"comm\":\"%s\",\"state\":\"%c\",\"ticks\":%lu}",
            count > 0 ? "," : "", tid, esc_comm, tstate, utime + stime);
        if (elen < 0 || (size_t)elen >= sizeof(entry)) continue;
        if ((size_t)off + (size_t)elen + 4 > bufsz) break;
        memcpy(buf + off, entry, (size_t)elen);
        off += elen;
        count++;
    }
    closedir(d);
    if (count == 0) return -1;

    memcpy(buf + off, "]}", 3);
    off += 2;
    return off;
}

void *metrics_thread_fn(void *arg)
{
    struct agent_state *a = (struct agent_state *)arg;
    block_signals_in_thread();

    metrics_collector_t mc;
    metrics_init(&mc);
    char buf[8192];

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
            /* Sleep in short intervals so we notice shutdown quickly */
            int wait_ms = interval * 1000;
            struct timespec tick = {0, 200000000L}; /* 200ms */
            while (wait_ms > 0 && !g_shutdown && !a->session_done) {
                nanosleep(&tick, NULL);
                wait_ms -= 200;
            }
            continue;
        }

        metrics_set_pid(&mc, pid);
        mc.include_network = network;

        /* System metrics */
        int len = collect_system_metrics(&mc, buf, sizeof(buf));
        if (len > 0) {
            if (agent_send_metrics(a, buf, len) < 0) break;
        }

        /* Process metrics */
        if (mc.pid > 0) {
            len = collect_process_metrics(&mc, buf, sizeof(buf));
            if (len > 0) {
                if (agent_send_metrics(a, buf, len) < 0) break;
            }
        }

        /* Network metrics */
        if (mc.include_network) {
            len = collect_network_metrics(&mc, buf, sizeof(buf));
            if (len > 0) {
                if (agent_send_metrics(a, buf, len) < 0) break;
            }
        }

        /* Disk I/O metrics (opt-in) */
        if (disk) {
            len = collect_disk_metrics(&mc, buf, sizeof(buf));
            if (len > 0) {
                if (agent_send_metrics(a, buf, len) < 0) break;
            }
        }

        /* Per-thread metrics (opt-in, needs a profiled pid) */
        if (threads && mc.pid > 0) {
            len = collect_thread_metrics(&mc, buf, sizeof(buf));
            if (len > 0) {
                if (agent_send_metrics(a, buf, len) < 0) break;
            }
        }

        /* Sleep in short intervals so we notice shutdown quickly */
        int wait_ms = interval * 1000;
        struct timespec tick = {0, 200000000L}; /* 200ms */
        while (wait_ms > 0 && !g_shutdown && !a->session_done) {
            nanosleep(&tick, NULL);
            wait_ms -= 200;
        }
    }
    return NULL;
}

