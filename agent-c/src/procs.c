/*
 * PerfLens Device Agent — process listing
 */

#include "agent.h"

/* --------------------------------------------------------------------------
 * Process listing (for list_processes command)
 * -------------------------------------------------------------------------- */

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

int do_list_processes(struct proc_entry *result, int max_results)
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

