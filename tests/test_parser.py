"""Parser tests: header regex compat across kernel formats, perf-stat
merging, multi-round file splitting, and malformed-input tolerance."""

import pytest
from conftest import fixture_session_names

from perflens.parser import (HEADER_RE, PERF_STAT_MARKER, _normalize_event,
                             event_base, filter_samples_by_event,
                             merge_perf_stat, parse_perf_script,
                             parse_perf_stat, resolve_event, split_perf_data)

# Each entry: (label, sample_text, expected_comm, expected_pid, expected_event)
# Covers kernel 2.6 through 6.x, pid/tid variants, [cpu] field, flags field,
# event modifiers, comms with spaces, swapper, and agent -F normalized output.
COMPAT_CASES = [
    ("kernel 2.6/3.x (no cpu)",
     "sample_workload 12345 6543210.123456: 1000003 cycles:\n"
     "\t    7f1234567890 main+0x10 (/usr/bin/sample_workload)\n",
     "sample_workload", 12345, "cycles"),
    ("kernel 4.x+ (with [cpu])",
     "sample_workload 12345 [002] 6543210.123456: 1000003 cycles:\n"
     "\t    7f1234567890 main+0x10 (/usr/bin/sample_workload)\n",
     "sample_workload", 12345, "cycles"),
    ("pid/tid format",
     "sample_workload 12345/12346 6543210.123456: 1000003 cycles:\n"
     "\t    7f1234567890 main+0x10 (/usr/bin/sample_workload)\n",
     "sample_workload", 12345, "cycles"),
    ("pid/tid + [cpu]",
     "sample_workload 12345/12346 [003] 6543210.123456: 1000003 cycles:\n"
     "\t    7f1234567890 main+0x10 (/usr/bin/sample_workload)\n",
     "sample_workload", 12345, "cycles"),
    ("comm with spaces",
     "Web Content 54321 [001] 6543210.123456: 500000 instructions:\n"
     "\t    7fabc1230000 js::RunScript+0x42 (libxul.so)\n",
     "Web Content", 54321, "instructions"),
    ("event modifier :u",
     "myapp 1000 [000] 6543210.123456: 999999 cycles:u:\n"
     "\t    00400580 compute+0x20 (/home/user/myapp)\n",
     "myapp", 1000, "cycles"),
    ("event modifier :pp",
     "myapp 1000 [000] 6543210.123456: 999999 cycles:pp:\n"
     "\t    00400580 compute+0x20 (/home/user/myapp)\n",
     "myapp", 1000, "cycles"),
    ("flags field (....)",
     "myapp 2000 [001] 6543210.123456: .... 1000003 cycles:\n"
     "\t    00400580 compute+0x20 (/home/user/myapp)\n",
     "myapp", 2000, "cycles"),
    ("flags field (d.b.)",
     "myapp 2000 [001] 6543210.123456: d.b. 1000003 cycles:\n"
     "\t    00400580 compute+0x20 (/home/user/myapp)\n",
     "myapp", 2000, "cycles"),
    ("swapper pid 0",
     "swapper 0 [000] 6543210.123456: 1000003 cycles:\n"
     "\t    ffffffff81060b40 native_safe_halt+0x6 ([kernel.kallsyms])\n",
     "swapper", 0, "cycles"),
    ("cache-misses event",
     "myapp 3000 [002] 6543210.123456: 50000 cache-misses:\n"
     "\t    00400580 hot_loop+0x8 (/home/user/myapp)\n",
     "myapp", 3000, "cache-misses"),
    ("branch-misses event",
     "myapp 3000 [002] 6543210.123456: 10000 branch-misses:\n"
     "\t    00400580 branch_heavy+0x4 (/home/user/myapp)\n",
     "myapp", 3000, "branch-misses"),
    ("instructions, no cpu",
     "myapp 4000 6543210.123456: 2000000 instructions:\n"
     "\t    00400580 math_loop+0xc (/home/user/myapp)\n",
     "myapp", 4000, "instructions"),
    ("hybrid-CPU PMU event name",
     "myapp 5000 [004] 6543210.123456: 1000003 cpu_core/cycles/:\n"
     "\t    00400580 compute+0x20 (/home/user/myapp)\n",
     "myapp", 5000, "cpu_core/cycles/"),
    ("all optional fields",
     "Web Content 9999/10000 [007] 6543210.123456: .... 500000 cycles:u:\n"
     "\t    7fabc1230000 js::RunScript+0x42 (libxul.so)\n",
     "Web Content", 9999, "cycles"),
]

_IDS = [c[0] for c in COMPAT_CASES]


@pytest.mark.parametrize("label,text,comm,pid,event", COMPAT_CASES, ids=_IDS)
def test_header_regex(label, text, comm, pid, event):
    m = HEADER_RE.match(text.split('\n')[0])
    assert m is not None, f"no regex match: {label}"
    assert m.group(1).strip() == comm
    assert int(m.group(2)) == pid
    # group(3) is the optional /tid; group(4) count; group(5) event
    assert m.group(5).split(':')[0] == event.split(':')[0]


@pytest.mark.parametrize("label,text,comm,pid,event", COMPAT_CASES, ids=_IDS)
def test_parse_end_to_end(label, text, comm, pid, event):
    samples = parse_perf_script(text)
    assert len(samples) == 1
    s = samples[0]
    assert s['comm'] == comm
    assert s['pid'] == pid
    assert s['event_type'] == event
    assert len(s['frames']) == 1


def test_tid_extraction():
    samples = parse_perf_script(
        "w 100/200 6543210.1: 1 cycles:\n\t    1234 f+0x1 (m)\n")
    assert samples[0]['pid'] == 100
    assert samples[0]['tid'] == 200
    # Without /tid, tid falls back to pid
    samples = parse_perf_script(
        "w 100 6543210.1: 1 cycles:\n\t    1234 f+0x1 (m)\n")
    assert samples[0]['tid'] == 100


def test_multi_frame_stack_order():
    samples = parse_perf_script(
        "w 1 6543210.1: 1 cycles:\n"
        "\t    aaaa leaf+0x1 (m)\n"
        "\t    bbbb mid+0x2 (m)\n"
        "\t    cccc root+0x3 (m)\n")
    funcs = [f['func'] for f in samples[0]['frames']]
    assert funcs == ['leaf', 'mid', 'root']  # frames[0] is the leaf


def test_malformed_input_does_not_crash():
    garbage = "!!! not perf output\n\x00\x01\x02\nrandom words here\n" * 50
    assert parse_perf_script(garbage) == []
    assert parse_perf_script('') == []
    # Frame line with no preceding header is dropped
    assert parse_perf_script("\t    aaaa orphan+0x1 (m)\n") == []


# ---------------------------------------------------------------------------
# split_perf_data
# ---------------------------------------------------------------------------

SCRIPT_A = ("w 1 6543210.1: 1 cycles:\n\t    aaaa fa+0x1 (m)\n")
SCRIPT_B = ("w 1 6543211.1: 1 cycles:\n\t    bbbb fb+0x1 (m)\n")
STAT_1 = ("  1,000  cycles  # 1.0 GHz\n\n  1.001 seconds time elapsed\n")
STAT_2 = ("  2,000  cycles  # 1.0 GHz\n\n  1.002 seconds time elapsed\n")


def test_split_no_marker():
    script, stat = split_perf_data(SCRIPT_A)
    assert script == SCRIPT_A
    assert stat == ''


def test_split_single_marker():
    text = SCRIPT_A + PERF_STAT_MARKER + '\n' + STAT_1
    script, stat = split_perf_data(text)
    assert 'fa+0x1' in script
    assert 'cycles' in stat and 'time elapsed' in stat


def test_split_multi_round():
    """Multi-round --output files: rounds 2..N must not be lost."""
    text = (SCRIPT_A + PERF_STAT_MARKER + '\n' + STAT_1 +
            SCRIPT_B + PERF_STAT_MARKER + '\n' + STAT_2)
    script, stat = split_perf_data(text)
    samples = parse_perf_script(script)
    assert len(samples) == 2, 'second round samples were dropped'
    assert {s['frames'][0]['func'] for s in samples} == {'fa', 'fb'}
    # Both stat sections retained; counters sum
    parsed = parse_perf_stat(stat)
    assert parsed['cycles']['value'] == 3000


# ---------------------------------------------------------------------------
# merge_perf_stat
# ---------------------------------------------------------------------------

def test_merge_perf_stat_sums_counters():
    old = {'cycles': {'value': 100, 'comment': ''},
           'instructions': {'value': 50, 'comment': ''}}
    new = {'cycles': {'value': 25, 'comment': 'x'}}
    merged = merge_perf_stat(old, new)
    assert merged['cycles']['value'] == 125
    assert merged['instructions']['value'] == 50
    # Inputs are not mutated
    assert old['cycles']['value'] == 100


def test_merge_perf_stat_empty_old():
    new = {'cycles': {'value': 7, 'comment': ''}}
    assert merge_perf_stat({}, new) == new
    assert merge_perf_stat({}, new) is not new  # copy, not alias


def test_merge_perf_stat_new_keys_added():
    old = {'cycles': {'value': 1, 'comment': ''}}
    merged = merge_perf_stat(old, {'branches': {'value': 2, 'comment': ''}})
    assert merged['branches']['value'] == 2
    assert merged['cycles']['value'] == 1


# ---------------------------------------------------------------------------
# Hybrid-CPU event names
#
# A P/E-core machine never reports a bare 'cycles'. It reports
# 'cpu_core/cycles/' and 'cpu_atom/cycles/', and the same event arrives
# spelled with a trailing modifier ('cpu_atom/cycles/P') when it comes from
# an imported perf.data rather than a live agent round. Asking for 'cycles'
# used to match nothing at all on that hardware.
# ---------------------------------------------------------------------------

HYBRID_EVENTS = ['cpu_core/cycles/', 'cpu_atom/cycles/',
                 'cpu_core/instructions/', 'cpu_atom/instructions/']


@pytest.mark.parametrize('raw,expected', [
    ('cycles', 'cycles'),
    ('cycles:P', 'cycles'),
    ('cycles:ppp', 'cycles'),
    ('cpu_atom/cycles/', 'cpu_atom/cycles/'),
    ('cpu_atom/cycles/P', 'cpu_atom/cycles/'),
    ('cpu_core/branch-misses/P', 'cpu_core/branch-misses/'),
])
def test_normalize_event_drops_modifier_not_pmu(raw, expected):
    """The PMU prefix identifies real hardware and must survive; the
    precise-ip modifier is noise that differs by capture path."""
    assert _normalize_event(raw) == expected


@pytest.mark.parametrize('raw,expected', [
    ('cycles', 'cycles'),
    ('cycles:P', 'cycles'),
    ('cpu_atom/cycles/', 'cycles'),
    ('cpu_core/cycles/P', 'cycles'),
    ('cpu_atom/branch-instructions/', 'branch-instructions'),
])
def test_event_base_strips_pmu_and_modifier(raw, expected):
    assert event_base(raw) == expected


def test_resolve_event_prefers_exact_match():
    assert resolve_event('cpu_atom/cycles/', HYBRID_EVENTS) == \
        ['cpu_atom/cycles/']


def test_resolve_event_expands_base_name_across_pmus():
    """'cycles' means both PMUs' cycles, not nothing."""
    assert resolve_event('cycles', HYBRID_EVENTS) == \
        ['cpu_core/cycles/', 'cpu_atom/cycles/']


def test_resolve_event_unknown_returns_empty():
    assert resolve_event('branch-misses', HYBRID_EVENTS) == []


def test_resolve_event_plain_hardware_is_unaffected():
    plain = ['cycles', 'instructions']
    assert resolve_event('cycles', plain) == ['cycles']


def test_filter_samples_by_event_falls_back_to_base_name():
    text = (
        "matrixlab 100 [002] 1.0: 1000 cpu_core/cycles/:\n"
        "\t    7f0000000010 hot+0x4 (/opt/m)\n"
        "matrixlab 100 [015] 1.1: 1000 cpu_atom/cycles/:\n"
        "\t    7f0000000010 hot+0x4 (/opt/m)\n"
        "matrixlab 100 [002] 1.2: 1000 cpu_core/instructions/:\n"
        "\t    7f0000000010 hot+0x4 (/opt/m)\n"
    )
    samples = parse_perf_script(text)
    assert len(samples) == 3
    # The default event name still selects both PMUs' cycles...
    assert len(filter_samples_by_event(samples, 'cycles')) == 2
    # ...while an exact name stays exact.
    assert len(filter_samples_by_event(samples, 'cpu_atom/cycles/')) == 1
    assert len(filter_samples_by_event(samples, 'instructions')) == 1


def test_import_and_live_spellings_agree():
    """Same event, two capture paths, one event_type."""
    live = parse_perf_script(
        "m 1 [0] 1.0: 10 cpu_atom/cycles/:\n\t 7f10 f+0x0 (/o/m)\n")
    imported = parse_perf_script(
        "m 1 [0] 1.0: 10 cpu_atom/cycles/P:\n\t 7f10 f+0x0 (/o/m)\n")
    assert live[0]['event_type'] == imported[0]['event_type']


def test_frame_strings_are_shared_across_samples():
    """Repeated symbols must not allocate a fresh string per frame — that
    is what made a long session cost ~3.3 KB per sample."""
    text = ''.join(
        f"matrixlab 100 [002] {i}.0: 1000 cycles:\n"
        "\t    7f0000000010 hot_loop+0x4 (/opt/matrixlab)\n"
        for i in range(50)
    )
    samples = parse_perf_script(text)
    assert len(samples) == 50
    funcs = {id(s['frames'][0]['func']) for s in samples}
    modules = {id(s['frames'][0]['module']) for s in samples}
    comms = {id(s['comm']) for s in samples}
    assert len(funcs) == 1
    assert len(modules) == 1
    assert len(comms) == 1


# ---------------------------------------------------------------------------
# perf stat digit grouping: the device's locale, not ours
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('line,name,value', [
    ('     9,310,933,573      cycles:u    #  3.155 GHz', 'cycles', 9310933573),
    ('     9.310.933.573      cycles      #  3.155 GHz', 'cycles', 9310933573),
    ("     9'310'933'573      cycles", 'cycles', 9310933573),
    ('     9 310 933 573      cycles', 'cycles', 9310933573),
    ('     9\u202f310\u202f933\u202f573      cycles', 'cycles', 9310933573),
    ('               12      page-faults', 'page-faults', 12),
    ('          2,950.76 msec task-clock  #  0.983 CPUs utilized', 'task-clock', 2950.76),
    ('          2.950,76 msec task-clock  #  0.983 CPUs utilized', 'task-clock', 2950.76),
    ('             12.50 msec task-clock', 'task-clock', 12.5),
    ('          2 950,76 msec task-clock', 'task-clock', 2950.76),
], ids=['c-locale', 'de_DE', 'de_CH', 'fr_FR-space', 'fr_FR-nnbsp',
        'small-int', 'msec-c', 'msec-de', 'msec-plain', 'msec-fr'])
def test_perf_stat_accepts_every_thousands_separator(line, name, value):
    """perf stat prints big numbers with the locale's grouping, and only ','
    used to be stripped: a de_DE device's 9.310.933.573 became 9.31."""
    from perflens.parser import parse_perf_stat
    stats = parse_perf_stat(line)
    assert stats[name]['value'] == value


def test_perf_stat_skips_what_it_cannot_read():
    """Runs on the receive thread: a malformed line must never raise."""
    from perflens.parser import parse_perf_stat
    text = (
        "         1,234,567      cycles\n"
        "         1,23,45        instructions\n"     # not grouped by threes
        "         garbage        cache-misses\n"
        "       1.2.3 seconds time elapsed\n"          # the float() that used to raise
        "              2.00 msec task-clock\n"
    )
    stats = parse_perf_stat(text)
    assert stats['cycles']['value'] == 1234567
    assert stats['task-clock']['value'] == 2.0
    assert 'instructions' not in stats
    assert 'time_elapsed' not in stats


# ---------------------------------------------------------------------------
# Fast path, long lines, hybrid counters
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('name', fixture_session_names())
def test_fixture_sample_counts_match_their_metadata(name):
    """The frame fast path (a tab-led line is a frame) must parse the real
    captures to the sample count the device reported when they were
    saved."""
    import json
    import os

    from conftest import REPO, load_fixture_chunks
    with open(os.path.join(REPO, 'tests', 'fixtures', name, 'metadata.json')) as f:
        expected = json.load(f)['total_samples']
    assert sum(len(c) for c in load_fixture_chunks(name)) == expected


def test_overlong_line_is_skipped_not_matched():
    import time
    junk = 'comm 1 ' + 'x' * 40000 + ' 1.0: 1 cycles:\n'
    good = ("sample_workload 12345 6543210.123456: 1000003 cycles:\n"
            "\t    7f1234567890 main+0x10 (/usr/bin/sample_workload)\n")
    t0 = time.monotonic()
    samples = parse_perf_script(junk + good)
    assert time.monotonic() - t0 < 2
    assert len(samples) == 1 and samples[0]['frames'][0]['func'] == 'main'


def test_tab_led_line_that_is_not_a_frame_still_reaches_the_header_regex():
    """A header never starts with a tab, but the fast path must fall through
    rather than drop a line it could not parse as a frame."""
    text = ("\tsample_workload 1 6543210.1: 1 cycles:\n"
            "\t    7f1234567890 main+0x10 (/usr/bin/x)\n")
    samples = parse_perf_script(text)
    assert len(samples) == 1 and samples[0]['comm'] == 'sample_workload'


def test_derived_stats_sum_pmu_qualified_counters():
    """A hybrid CPU reports cpu_core/cycles/ and cpu_atom/cycles/, never a
    bare cycles; IPC and the miss rates used to be blank there."""
    text = ("     1,000      cpu_core/cycles/\n"
            "       500      cpu_atom/cycles/\n"
            "     3,000      cpu_core/instructions/\n"
            "       750      cpu_atom/instructions/\n"
            "       200      cpu_core/cache-references/\n"
            "        50      cpu_core/cache-misses/\n")
    stats = parse_perf_stat(text)
    assert stats['ipc']['value'] == 2.5
    assert stats['cache_miss_rate']['value'] == 25.0


def test_perf_stat_accepts_ungrouped_numbers():
    """The agent runs perf under LC_ALL=C since 0.12.0, and the C locale
    groups nothing: a 12-digit cycles count arrives as plain digits. The
    grouping-tolerant regex matched neither branch for it and every
    counter but the two-digit page-faults was dropped -- found on the
    first hardware run after the change, not by any fixture (all of which
    were captured under a grouping locale)."""
    text = ("\n Performance counter stats for process id '1587':\n\n"
            "      784760762882      cpu_atom/cycles/                          \n"
            "     <not counted>      cpu_core/cycles/                    (0.00%)\n"
            "     1895864972475      cpu_atom/instructions/                    \n"
            "                33      page-faults                               \n"
            "           2950.76 msec task-clock            #    0.983 CPUs utilized\n"
            "          2950.760 msec cpu-clock\n"
            "       180.009563356 seconds time elapsed\n")
    stats = parse_perf_stat(text)
    assert stats['cpu_atom/cycles/']['value'] == 784760762882
    assert stats['cpu_atom/instructions/']['value'] == 1895864972475
    assert 'cpu_core/cycles/' not in stats
    assert stats['page-faults']['value'] == 33
    assert stats['task-clock']['value'] == 2950.76
    assert stats['cpu-clock']['value'] == 2950.76
    assert stats['time_elapsed']['value'] == 180.009563356
    assert stats['ipc']['value'] == 2.42


def test_branch_miss_rate_from_branch_instructions():
    """perf spells the counter `branch-instructions` when asked for it by
    that name (the fixtures do); only `branches` used to be recognised."""
    stats = parse_perf_stat("     4,000      branch-instructions\n"
                            "        80      branch-misses\n")
    assert stats['branch_miss_rate']['value'] == 2.0
