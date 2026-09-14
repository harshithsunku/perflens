/*
 * PerfLens Device Agent — process listing
 */

#include "agent.h"

/* --------------------------------------------------------------------------
 * Process listing (for list_processes command)
 *
 * Two snapshots of every process's CPU ticks half a second apart, sorted by
 * the delta. Only the top entries get their cmdline read: the old version
 * opened /proc/<pid>/comm and cmdline for every one of up to 4096
 * processes before sorting, and comm is in the stat line anyway. CPU% is
 * per core (100 = one busy core, as `top` shows it), the same convention
 * the process metrics use -- they used to disagree for the same pid.
 * -------------------------------------------------------------------------- */

struct proc_snap {
    int pid;
    unsigned long ticks;
    char comm[64];
};

/* comm (from between the parens) and utime+stime (fields 14 and 15) from
 * one /proc/<pid>/stat line. */
static int read_proc_stat(int pid, unsigned long *ticks, char *comm, size_t clen)
{
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/stat", pid);
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;

    char line[1024];
    ssize_t n = read(fd, line, sizeof(line) - 1);
    close(fd);
    if (n <= 0) return -1;
    line[n] = '\0';

    /* comm may contain spaces or parens: it ends at the last ')' */
    char *open_p = strchr(line, '(');
    char *close_p = strrchr(line, ')');
    if (!open_p || !close_p || close_p < open_p) return -1;
    if (comm) {
        size_t len = (size_t)(close_p - open_p - 1);
        if (len >= clen) len = clen - 1;
        memcpy(comm, open_p + 1, len);
        comm[len] = '\0';
    }

    char *p = close_p + 1;
    int field = 3;
    unsigned long utime = 0, stime = 0;
    while (*p) {
        while (*p == ' ') p++;
        if (!*p) break;
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

static void read_cmdline(int pid, char *out, size_t cap)
{
    char path[64];
    snprintf(path, sizeof(path), "/proc/%d/cmdline", pid);
    out[0] = '\0';
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return;
    ssize_t n = read(fd, out, cap - 1);
    close(fd);
    if (n < 0) n = 0;
    out[n] = '\0';
    for (ssize_t j = 0; j < n; j++)
        if (out[j] == '\0') out[j] = ' ';
    while (n > 0 && out[n - 1] == ' ')
        out[--n] = '\0';
}

static int cmp_proc_cpu(const void *a, const void *b)
{
    const struct proc_entry *pa = (const struct proc_entry *)a;
    const struct proc_entry *pb = (const struct proc_entry *)b;
    if (pb->cpu > pa->cpu) return 1;
    if (pb->cpu < pa->cpu) return -1;
    return pa->pid - pb->pid;
}

static double monotonic_seconds(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

int do_list_processes(struct proc_entry *result, int max_results)
{
    long clk_tck = sysconf(_SC_CLK_TCK);
    if (clk_tck <= 0) clk_tck = 100;

    /* First snapshot: every pid, growable -- a host with more processes
     * than a fixed cap used to lose whichever came last in readdir order,
     * which could be the one the operator was looking for. */
    size_t cap = 1024, count = 0;
    struct proc_snap *snap = malloc(sizeof(*snap) * cap);
    if (!snap) return 0;

    DIR *d = opendir("/proc");
    if (!d) { free(snap); return 0; }

    double t0 = monotonic_seconds();
    struct dirent *ent;
    while ((ent = readdir(d)) != NULL) {
        char *end;
        int pid = (int)strtol(ent->d_name, &end, 10);
        if (*end != '\0' || pid <= 0) continue;
        if (count == cap) {
            struct proc_snap *grown = realloc(snap, sizeof(*snap) * cap * 2);
            if (!grown) break;
            snap = grown;
            cap *= 2;
        }
        if (read_proc_stat(pid, &snap[count].ticks, snap[count].comm,
                           sizeof(snap[count].comm)) == 0) {
            snap[count].pid = pid;
            count++;
        }
    }
    closedir(d);

    usleep(500000);
    double elapsed = monotonic_seconds() - t0;
    if (elapsed <= 0) elapsed = 0.5;

    /* Second snapshot, into entries; sort; cmdline for the top ones only */
    struct proc_entry *all = malloc(sizeof(*all) * (count ? count : 1));
    if (!all) { free(snap); return 0; }

    size_t n = 0;
    for (size_t i = 0; i < count; i++) {
        unsigned long ticks2;
        if (read_proc_stat(snap[i].pid, &ticks2, NULL, 0) < 0) continue;
        unsigned long delta = ticks2 >= snap[i].ticks ? ticks2 - snap[i].ticks : 0;
        struct proc_entry *e = &all[n++];
        e->pid = snap[i].pid;
        e->cpu = 100.0 * (double)delta / (elapsed * (double)clk_tck);
        snprintf(e->comm, sizeof(e->comm), "%s",
                 snap[i].comm[0] ? snap[i].comm : "?");
        e->cmdline[0] = '\0';
    }
    free(snap);

    qsort(all, n, sizeof(*all), cmp_proc_cpu);

    int ret = (int)(n < (size_t)max_results ? n : (size_t)max_results);
    for (int i = 0; i < ret; i++)
        read_cmdline(all[i].pid, all[i].cmdline, sizeof(all[i].cmdline));
    memcpy(result, all, sizeof(*all) * (size_t)ret);
    free(all);
    return ret;
}
