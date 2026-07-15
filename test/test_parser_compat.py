#!/usr/bin/env python3
"""Test HEADER_RE and parse_perf_script against all known perf script formats.

Covers kernel 2.6 through 6.x, pid/tid variants, [cpu] field, flags field,
event modifiers, comms with spaces, swapper, and agent -F normalized output.

Run:  python3 test/test_parser_compat.py
"""

import os
import sys

# Allow running from repo root or test/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))

from parser import HEADER_RE, parse_perf_script


# Each entry: (label, full_sample_text, expected_comm, expected_pid, expected_event)
# full_sample_text includes at least one stack frame so parse_perf_script works end-to-end.
TEST_CASES = [
    # --- Kernel 2.6/3.x: no [cpu], no flags ---
    (
        "Kernel 2.6/3.x (no cpu)",
        "sample_workload 12345 6543210.123456: 1000003 cycles:\n"
        "\t    7f1234567890 main+0x10 (/usr/bin/sample_workload)\n",
        "sample_workload", 12345, "cycles",
    ),

    # --- Kernel 4.x+: [cpu] present ---
    (
        "Kernel 4.x+ (with [cpu])",
        "sample_workload 12345 [002] 6543210.123456: 1000003 cycles:\n"
        "\t    7f1234567890 main+0x10 (/usr/bin/sample_workload)\n",
        "sample_workload", 12345, "cycles",
    ),

    # --- pid/tid format ---
    (
        "pid/tid format",
        "sample_workload 12345/12346 6543210.123456: 1000003 cycles:\n"
        "\t    7f1234567890 main+0x10 (/usr/bin/sample_workload)\n",
        "sample_workload", 12345, "cycles",
    ),

    # --- pid/tid + [cpu] ---
    (
        "pid/tid + [cpu]",
        "sample_workload 12345/12346 [003] 6543210.123456: 1000003 cycles:\n"
        "\t    7f1234567890 main+0x10 (/usr/bin/sample_workload)\n",
        "sample_workload", 12345, "cycles",
    ),

    # --- Comm with spaces ---
    (
        "Comm with spaces (Web Content)",
        "Web Content 54321 [001] 6543210.123456: 500000 instructions:\n"
        "\t    7fabc1230000 js::RunScript+0x42 (libxul.so)\n",
        "Web Content", 54321, "instructions",
    ),

    # --- Event modifiers: cycles:u ---
    (
        "Event modifier cycles:u",
        "myapp 1000 [000] 6543210.123456: 999999 cycles:u:\n"
        "\t    00400580 compute+0x20 (/home/user/myapp)\n",
        "myapp", 1000, "cycles",
    ),

    # --- Event modifiers: cycles:pp ---
    (
        "Event modifier cycles:pp",
        "myapp 1000 [000] 6543210.123456: 999999 cycles:pp:\n"
        "\t    00400580 compute+0x20 (/home/user/myapp)\n",
        "myapp", 1000, "cycles",
    ),

    # --- Flags field (kernel 5.x+): ".... " ---
    (
        "Flags field (.... )",
        "myapp 2000 [001] 6543210.123456: .... 1000003 cycles:\n"
        "\t    00400580 compute+0x20 (/home/user/myapp)\n",
        "myapp", 2000, "cycles",
    ),

    # --- Flags field: "d.b." ---
    (
        "Flags field (d.b.)",
        "myapp 2000 [001] 6543210.123456: d.b. 1000003 cycles:\n"
        "\t    00400580 compute+0x20 (/home/user/myapp)\n",
        "myapp", 2000, "cycles",
    ),

    # --- swapper pid 0 ---
    (
        "swapper pid 0",
        "swapper 0 [000] 6543210.123456: 1000003 cycles:\n"
        "\t    ffffffff81060b40 native_safe_halt+0x6 ([kernel.kallsyms])\n",
        "swapper", 0, "cycles",
    ),

    # --- cache-misses event ---
    (
        "cache-misses event",
        "myapp 3000 [002] 6543210.123456: 50000 cache-misses:\n"
        "\t    00400580 hot_loop+0x8 (/home/user/myapp)\n",
        "myapp", 3000, "cache-misses",
    ),

    # --- branch-misses event ---
    (
        "branch-misses event",
        "myapp 3000 [002] 6543210.123456: 10000 branch-misses:\n"
        "\t    00400580 branch_heavy+0x4 (/home/user/myapp)\n",
        "myapp", 3000, "branch-misses",
    ),

    # --- instructions event, no [cpu] (older kernel) ---
    (
        "instructions, no cpu",
        "myapp 4000 6543210.123456: 2000000 instructions:\n"
        "\t    00400580 math_loop+0xc (/home/user/myapp)\n",
        "myapp", 4000, "instructions",
    ),

    # --- Agent -F normalized output (comm,pid,time,period,event,ip,sym,dso) ---
    (
        "Agent -F normalized format",
        "sample_workload 12345 6543210.123456: 1000003 cycles:\n"
        "\t    7f1234567890 main+0x10 (/usr/bin/sample_workload)\n",
        "sample_workload", 12345, "cycles",
    ),

    # --- pid/tid + [cpu] + flags + event modifier (maximal) ---
    (
        "All optional fields present",
        "Web Content 9999/10000 [007] 6543210.123456: .... 500000 cycles:u:\n"
        "\t    7fabc1230000 js::RunScript+0x42 (libxul.so)\n",
        "Web Content", 9999, "cycles",
    ),
]


def test_header_regex():
    """Test HEADER_RE directly against header lines."""
    passed = 0
    failed = 0

    for label, sample_text, exp_comm, exp_pid, exp_event in TEST_CASES:
        header_line = sample_text.split('\n')[0]
        m = HEADER_RE.match(header_line)
        if m is None:
            print("FAIL [regex]  %-40s  => no match" % label)
            print("              line: %r" % header_line)
            failed += 1
            continue

        got_comm = m.group(1).strip()
        got_pid = int(m.group(2))
        # group(3) is the optional /tid; group(4) is the count
        got_event = m.group(5).split(':')[0]  # strip modifiers for comparison

        errors = []
        if got_comm != exp_comm:
            errors.append("comm: got %r, expected %r" % (got_comm, exp_comm))
        if got_pid != exp_pid:
            errors.append("pid: got %d, expected %d" % (got_pid, exp_pid))
        if got_event != exp_event:
            errors.append("event: got %r, expected %r" % (got_event, exp_event))

        if errors:
            print("FAIL [regex]  %-40s  => %s" % (label, "; ".join(errors)))
            failed += 1
        else:
            print("PASS [regex]  %s" % label)
            passed += 1

    return passed, failed


def test_parse_perf_script():
    """Test full parse_perf_script on each sample."""
    passed = 0
    failed = 0

    for label, sample_text, exp_comm, exp_pid, exp_event in TEST_CASES:
        samples = parse_perf_script(sample_text)
        if len(samples) == 0:
            print("FAIL [parse]  %-40s  => 0 samples parsed" % label)
            failed += 1
            continue

        s = samples[0]
        errors = []
        if s['comm'] != exp_comm:
            errors.append("comm: got %r, expected %r" % (s['comm'], exp_comm))
        if s['pid'] != exp_pid:
            errors.append("pid: got %d, expected %d" % (s['pid'], exp_pid))
        if s['event_type'] != exp_event:
            errors.append("event: got %r, expected %r" % (s['event_type'], exp_event))
        if len(s['frames']) == 0:
            errors.append("no frames parsed")

        if errors:
            print("FAIL [parse]  %-40s  => %s" % (label, "; ".join(errors)))
            failed += 1
        else:
            print("PASS [parse]  %s" % label)
            passed += 1

    return passed, failed


def main():
    print("=" * 60)
    print("HEADER_RE regex tests")
    print("=" * 60)
    r_pass, r_fail = test_header_regex()

    print()
    print("=" * 60)
    print("parse_perf_script end-to-end tests")
    print("=" * 60)
    p_pass, p_fail = test_parse_perf_script()

    total_pass = r_pass + p_pass
    total_fail = r_fail + p_fail
    print()
    print("-" * 60)
    print("Total: %d passed, %d failed" % (total_pass, total_fail))

    if total_fail > 0:
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
        sys.exit(0)


if __name__ == '__main__':
    main()
