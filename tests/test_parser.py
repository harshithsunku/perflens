"""Parser tests: header regex compat across kernel formats, perf-stat
merging, multi-round file splitting, and malformed-input tolerance."""

import pytest

from perflens.parser import (HEADER_RE, PERF_STAT_MARKER, merge_perf_stat,
                             parse_perf_script, parse_perf_stat,
                             split_perf_data)

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
