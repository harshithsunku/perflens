"""Line-level annotation, checked against an oracle the profiler did not produce.

Why this file exists, separately from test_source_mapper.py:

Line-level source annotation is the project's differentiator, and it was
silently broken for the whole public life of 0.8.0 — every sample landed on
its function's *declaration* line instead of the line that was actually hot.
165 tests passed throughout. They passed because both committed fixtures were
captured by running the agent, so they recorded the broken output, and every
assertion was written to match what the fixture contained. A fixture generated
by the tool under test cannot falsify that tool.

So this test derives its expected values from somewhere the profiler has no
influence over: marker comments in tests/line_oracle.c. The test reads the
line numbers out of the C source, computes an address inside the hot loop from
the binary's own symbol table and DWARF, and asserts the mapper agrees. If the
symoff regression happened again, `hot_line` would come back as `decl_line`
and this fails — regardless of what any fixture says.
"""

import os
import re
import shutil
import subprocess

import pytest

from conftest import REPO

pytestmark = pytest.mark.skipif(
    not (shutil.which('gcc') and shutil.which('addr2line')
         and shutil.which('readelf') and shutil.which('objdump')),
    reason='needs gcc + binutils')

SOURCE = os.path.join(REPO, 'tests', 'line_oracle.c')


@pytest.fixture(scope='module')
def markers():
    """Line numbers named by MARKER comments, read from the C source.

    This is the oracle. It comes from the file a human wrote, not from
    anything the profiler emitted.
    """
    found = {}
    with open(SOURCE) as f:
        for lineno, text in enumerate(f, start=1):
            m = re.search(r'/\*\s*MARKER:\s*(\w+)\s*\*/', text)
            if m:
                found[m.group(1)] = lineno
    missing = {'decl_line', 'hot_line', 'second_decl', 'second_hot'} - set(found)
    assert not missing, f'line_oracle.c lost its markers: {missing}'
    assert found['hot_line'] > found['decl_line']
    return found


@pytest.fixture(scope='module')
def oracle_binary(tmp_path_factory):
    """-O1 so the loop stays recognisable but real optimisation happens."""
    out = str(tmp_path_factory.mktemp('oracle') / 'line_oracle')
    subprocess.run(['gcc', '-g', '-O1', '-o', out, SOURCE],
                   check=True, capture_output=True)
    return out


def addresses_for_line(binary, want_line):
    """Every instruction address DWARF attributes to `want_line`.

    Read out of the binary with objdump --dwarf=decodedline — the debug line
    table itself, not the profiler's interpretation of it.
    """
    r = subprocess.run(['objdump', '--dwarf=decodedline', binary],
                       capture_output=True, text=True, check=True)
    addrs = []
    for row in r.stdout.splitlines():
        parts = row.split()
        # "line_oracle.c   29   0x401136"
        if len(parts) >= 3 and parts[-1].startswith('0x'):
            try:
                lineno = int(parts[-2])
            except ValueError:
                continue
            if lineno == want_line:
                addrs.append(int(parts[-1], 16))
    return sorted(addrs)


def symbol_address(binary, name):
    r = subprocess.run(['readelf', '-sW', binary],
                       capture_output=True, text=True, check=True)
    for row in r.stdout.splitlines():
        parts = row.split()
        if len(parts) >= 8 and parts[-1] == name and parts[3] == 'FUNC':
            return int(parts[1], 16)
    raise AssertionError(f'{name} not found in {binary}')


def make_mapper(binary):
    from perflens.source_mapper import SourceMapper
    return SourceMapper(os.path.dirname(SOURCE), binary_path=binary,
                        addr2line_bin=shutil.which('addr2line'),
                        readelf_bin=shutil.which('readelf'))


def sample_at(func, offset, module):
    """One sample, shaped as the parser emits it from `perf script`."""
    return {'comm': 'oracle', 'pid': 1, 'tid': 1, 'event_count': 1,
            'event_type': 'cycles',
            'frames': [{'addr': '0', 'func': func,
                        'offset': hex(offset), 'module': module}]}


def test_marker_lines_are_where_the_source_says(markers):
    """Sanity-check the oracle itself before trusting it."""
    with open(SOURCE) as f:
        lines = f.readlines()
    assert 'oracle_hot_loop' in lines[markers['decl_line'] - 1]
    assert 'acc +=' in lines[markers['hot_line'] - 1]
    assert 'oracle_second_function' in lines[markers['second_decl'] - 1]
    assert 'acc = (acc * 31' in lines[markers['second_hot'] - 1]


def test_hot_line_resolves_to_the_loop_body_not_the_declaration(
        markers, oracle_binary, perflens_home):
    """The exact regression that shipped in 0.8.0.

    A sample taken inside the loop must map to the loop body. Reporting the
    declaration line is what the symoff bug did, and it looked plausible
    enough to survive two releases and a docs screenshot.
    """
    hot_addrs = addresses_for_line(oracle_binary, markers['hot_line'])
    assert hot_addrs, 'no code attributed to the hot line — check -O level'

    func_addr = symbol_address(oracle_binary, 'oracle_hot_loop')
    offset = hot_addrs[0] - func_addr
    assert offset > 0, 'hot line should be inside the function, not at entry'

    mapper = make_mapper(oracle_binary)
    line_data = mapper.map_samples_to_lines(
        [sample_at('oracle_hot_loop', offset, oracle_binary)] * 5)

    hit = {(os.path.basename(f), ln)
           for f, lines in line_data.items() for ln in lines}

    # This is the load-bearing assertion. Verified non-vacuous: feeding the
    # same mapper a frame with no offset — the exact shape `perf script`
    # produced before SCRIPT_FIELDS requested symoff — resolves to the
    # function prologue instead, and this line fails.
    assert (os.path.basename(SOURCE), markers['hot_line']) in hit, (
        f'expected the loop body (line {markers["hot_line"]}), got {sorted(hit)}')
    assert (os.path.basename(SOURCE), markers['decl_line']) not in hit, (
        'samples collapsed onto the declaration line')


def test_two_functions_do_not_collapse_onto_each_other(
        markers, oracle_binary, perflens_home):
    """Distinct hot lines in distinct functions must stay distinct."""
    mapper = make_mapper(oracle_binary)
    samples = []
    for func, marker in (('oracle_hot_loop', 'hot_line'),
                         ('oracle_second_function', 'second_hot')):
        addrs = addresses_for_line(oracle_binary, markers[marker])
        assert addrs, f'no code attributed to {marker}'
        samples.append(sample_at(func, addrs[0] - symbol_address(oracle_binary, func),
                                 oracle_binary))

    line_data = mapper.map_samples_to_lines(samples)
    hit = {ln for lines in line_data.values() for ln in lines}
    assert markers['hot_line'] in hit
    assert markers['second_hot'] in hit


def test_zero_offset_still_lands_in_the_right_function(
        markers, oracle_binary, perflens_home):
    """func+0x0 legitimately is the declaration/prologue — the fix must not
    have inverted the behaviour, only stopped it applying to every sample."""
    mapper = make_mapper(oracle_binary)
    line_data = mapper.map_samples_to_lines(
        [sample_at('oracle_hot_loop', 0, oracle_binary)])

    hit = {ln for lines in line_data.values() for ln in lines}
    assert hit, 'a zero-offset sample should still resolve somewhere'
    assert min(hit) <= markers['hot_line'], (
        'function entry should map at or above the loop body, not past it')
