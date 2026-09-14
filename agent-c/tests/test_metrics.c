/*
 * Unit tests for the metrics parsers. Includes metrics.c directly so the
 * static parsers are reachable; the inputs are what real kernels print.
 * Build and run with `make check` in agent-c/.
 */

#include "../src/metrics.c"

/* metrics.c calls into main.c for its frames; none of that runs here. */
int agent_send_frame(struct agent_state *a, const void *p, size_t n, uint8_t f)
{ (void)a; (void)p; (void)n; (void)f; return 0; }
int agent_send_metrics(struct agent_state *a, const char *j, size_t n)
{ (void)a; (void)j; (void)n; return 0; }
int agent_send_response(struct agent_state *a, const char *j)
{ (void)a; (void)j; return 0; }
void start_metrics_thread(struct agent_state *a) { (void)a; }

static int failures = 0;
#define CHECK(cond) do { if (!(cond)) { failures++; \
    fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } } while (0)

static void test_proc_stat_with_an_offline_core(void)
{
    /* cpu1 offline: no row. Later cores must still be counted. */
    const char *text =
        "cpu  100 0 50 800 10 0 5 0 0 0\n"
        "cpu0 50 0 25 400 5 0 2 0 0 0\n"
        "cpu2 50 0 25 400 5 0 3 0 0 0\n"
        "cpu3 0 0 0 800 0 0 0 0 0 0\n"
        "intr 12345 1 2 3\n"
        "ctxt 67890\n"
        "btime 1700000000\n"
        "procs_running 2\n"
        "procs_blocked 1\n";
    unsigned long per_core[8][8];
    struct stat_totals t;
    parse_proc_stat(text, &t, per_core, 8);
    CHECK(t.num_cores == 4);
    CHECK(t.cpu[0] == 100 && t.cpu[3] == 800);
    CHECK(per_core[0][0] == 50);
    CHECK(per_core[1][0] == 0 && per_core[1][3] == 0);   /* the hole */
    CHECK(per_core[2][6] == 3);
    CHECK(per_core[3][3] == 800);
    CHECK(t.ctxt == 67890 && t.intr == 12345);
    CHECK(t.procs_running == 2 && t.procs_blocked == 1);

    /* Cores past the caller's capacity are counted, not written */
    unsigned long small[2][8];
    parse_proc_stat(text, &t, small, 2);
    CHECK(t.num_cores == 4);
    CHECK(small[0][0] == 50);
}

static void test_cpu_pct(void)
{
    unsigned long prev[8] = {100, 0, 50, 800, 10, 0, 5, 0};
    unsigned long curr[8] = {200, 0, 100, 850, 10, 0, 5, 0};
    /* delta total 200, delta idle 50 -> 75% busy */
    double pct = calc_cpu_pct(prev, curr);
    CHECK(pct > 74.9 && pct < 75.1);
    /* A counter reset (reboot, container restart) reads 0, not garbage */
    unsigned long reset[8] = {1, 0, 1, 1, 0, 0, 0, 0};
    CHECK(calc_cpu_pct(prev, reset) == 0.0);
}

static void test_meminfo_modern(void)
{
    const char *text =
        "MemTotal:        995964 kB\n"
        "MemFree:         120000 kB\n"
        "MemAvailable:    600000 kB\n"
        "Buffers:          30000 kB\n"
        "Cached:          400000 kB\n"
        "SwapCached:           0 kB\n"
        "SwapTotal:       262140 kB\n"
        "SwapFree:        262000 kB\n";
    struct meminfo m;
    parse_meminfo(text, &m);
    CHECK(m.total == 995964);
    CHECK(m.have_available && m.available == 600000);
    CHECK(m.buffers == 30000 && m.cached == 400000);
    CHECK(m.swap_total == 262140 && m.swap_free == 262000);
}

static void test_meminfo_pre_3_14_has_no_memavailable(void)
{
    /* Kernel 3.10: no MemAvailable. Page cache is reclaimable, so used
     * memory is not simply total - free -- that counted the cache as used
     * and read 88% on a box with 400 MB of cache. */
    const char *text =
        "MemTotal:        995964 kB\n"
        "MemFree:         120000 kB\n"
        "Buffers:          30000 kB\n"
        "Cached:          400000 kB\n"
        "SwapTotal:            0 kB\n"
        "SwapFree:             0 kB\n";
    struct meminfo m;
    parse_meminfo(text, &m);
    CHECK(!m.have_available);
    CHECK(m.available == 120000 + 30000 + 400000);
}

static void test_thermal_zone_choice(void)
{
    const char *const board_first[] = { "pmic-thermal", "battery", "cpu-thermal", "gpu" };
    CHECK(choose_thermal_zone(board_first, 4) == 2);
    const char *const x86[] = { "acpitz", "x86_pkg_temp" };
    CHECK(choose_thermal_zone(x86, 2) == 1);
    const char *const soc[] = { "soc_thermal" };
    CHECK(choose_thermal_zone(soc, 1) == 0);
    const char *const none[] = { "battery", "pmic" };
    CHECK(choose_thermal_zone(none, 2) == -1);   /* -> hottest of all */
    CHECK(choose_thermal_zone(none, 0) == -1);
}

int main(void)
{
    test_proc_stat_with_an_offline_core();
    test_cpu_pct();
    test_meminfo_modern();
    test_meminfo_pre_3_14_has_no_memavailable();
    test_thermal_zone_choice();
    if (failures) {
        fprintf(stderr, "%d check(s) failed\n", failures);
        return 1;
    }
    printf("test_metrics: all checks passed\n");
    return 0;
}
