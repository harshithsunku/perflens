"""SourceMapper tests against a real -g binary compiled at test time.

Covers: symbol loading, addr2line line mapping, source annotation,
path-map remapping, and the persistent symbol cache (PERFLENS_HOME).
Skipped when gcc/addr2line/readelf aren't available.
"""

import os
import shutil
import subprocess

import pytest

from conftest import REPO

pytestmark = pytest.mark.skipif(
    not (shutil.which('gcc') and shutil.which('addr2line')
         and shutil.which('readelf')),
    reason='needs gcc + binutils')

SOURCE = os.path.join(REPO, 'tests', 'sample_workload.c')


@pytest.fixture(scope='module')
def fixture_binary(tmp_path_factory):
    """Compile the sample workload with debug info (absolute source path,
    so DWARF records an absolute filename)."""
    d = tmp_path_factory.mktemp('bin')
    binary = str(d / 'workload')
    subprocess.run(['gcc', '-g', '-O0', '-o', binary, SOURCE, '-lm'],
                   check=True, capture_output=True)
    return binary


def make_mapper(binary, home, **kw):
    from perflens.source_mapper import SourceMapper
    kw.setdefault('addr2line_bin', shutil.which('addr2line'))
    kw.setdefault('readelf_bin', shutil.which('readelf'))
    return SourceMapper(os.path.dirname(SOURCE), binary_path=binary, **kw)


def samples_for(binary, func='main', offset='0x0', n=3):
    return [{'comm': 'w', 'pid': 1, 'tid': 1, 'event_count': 1,
             'event_type': 'cycles',
             'frames': [{'addr': '0', 'func': func, 'offset': offset,
                         'module': binary}]}] * n


def test_symbols_and_line_mapping(fixture_binary, perflens_home):
    mapper = make_mapper(fixture_binary, perflens_home)
    line_data = mapper.map_samples_to_lines(samples_for(fixture_binary))
    assert line_data, 'no lines mapped'
    (fpath, lines), = line_data.items()
    assert fpath.endswith('sample_workload.c')
    assert sum(v['samples'] for v in lines.values()) == 3
    mapper.close()


def test_annotate_source(fixture_binary, perflens_home):
    mapper = make_mapper(fixture_binary, perflens_home)
    line_data = mapper.map_samples_to_lines(samples_for(fixture_binary))
    (fpath, lines), = line_data.items()
    annotated = mapper.annotate_source(SOURCE, lines)
    assert annotated, 'no annotated lines'
    hot = [ln for ln in annotated if ln['samples'] > 0]
    assert hot, 'no hot lines in annotation'
    assert all('source' in ln and 'line' in ln for ln in annotated)
    mapper.close()


def test_unknown_function_is_skipped(fixture_binary, perflens_home):
    mapper = make_mapper(fixture_binary, perflens_home)
    line_data = mapper.map_samples_to_lines(
        samples_for(fixture_binary, func='no_such_function_xyz'))
    assert line_data == {}
    mapper.close()


def test_persistent_symbol_cache(fixture_binary, perflens_home):
    """Second mapper instance must find addr2line results in
    ~/.perflens/cache/symbols.db without re-resolving."""
    from perflens import symcache

    mapper = make_mapper(fixture_binary, perflens_home)
    assert mapper.map_samples_to_lines(samples_for(fixture_binary))
    mapper.close()

    db = os.path.join(str(perflens_home), 'cache', 'symbols.db')
    assert os.path.isfile(db), 'symbols.db not created under PERFLENS_HOME'

    bkey = symcache.binary_key(fixture_binary)
    cache = symcache.SymbolCache()
    try:
        assert cache.load_symtab(bkey), 'symbol table not persisted'
        assert cache.load_addr2line(bkey), 'addr2line rows not persisted'
    finally:
        cache.close()

    # A fresh mapper with a poisoned addr2line binary still resolves,
    # proving it reads the persistent cache instead of spawning addr2line.
    mapper2 = make_mapper(fixture_binary, perflens_home,
                          addr2line_bin='/nonexistent/addr2line')
    line_data = mapper2.map_samples_to_lines(samples_for(fixture_binary))
    assert line_data and next(iter(line_data)).endswith('sample_workload.c')
    mapper2.close()


def ip_samples_for(binary, base, addrs, func):
    """Frames as `perf script` prints them WITHOUT the symoff field: a bare
    symbol name and the raw runtime ip, no `+0x<offset>`."""
    return [{'comm': 'w', 'pid': 1, 'tid': 1, 'event_count': 1,
             'event_type': 'cycles',
             'frames': [{'addr': format(base + a, 'x'), 'func': func,
                         'offset': '', 'module': binary}]}
            for a in addrs]


def offset_samples_for(binary, start, addrs, func):
    """The same frames as `perf script` prints them WITH symoff."""
    return [{'comm': 'w', 'pid': 1, 'tid': 1, 'event_count': 1,
             'event_type': 'cycles',
             'frames': [{'addr': '0', 'func': func,
                         'offset': hex(a - start), 'module': binary}]}
            for a in addrs]


def spread_over(mapper, binary, func):
    """A page-aligned load base and several file addresses inside `func`."""
    from perflens.source_mapper import PAGE_SIZE
    start = mapper._load_symbols(binary)[func]
    end = mapper._symbol_spans(binary)[start]
    span = min(end - start, PAGE_SIZE)
    step = max(span // 8, 1)
    addrs = [start + i * step for i in range(8) if i * step < span]
    return 0x7F0000000000, start, addrs


def test_ip_recovery_matches_the_symoff_path(fixture_binary, perflens_home):
    """Frames with no `symoff` must resolve exactly like frames that have it.

    Agents built before symoff was requested -- and every session already on
    disk -- carry only the raw ip. Falling back to the symbol address put
    every sample in a function on its declaration line; the load base is
    recovered from the ip instead, and must agree with the exact path.
    """
    probe = make_mapper(fixture_binary, perflens_home)
    base, start, addrs = spread_over(probe, fixture_binary, 'cpu_intensive')
    probe.close()
    assert len(addrs) >= 4, 'need enough frames to pin down a load base'

    by_ip = make_mapper(fixture_binary, perflens_home)
    ip_lines = by_ip.map_samples_to_lines(
        ip_samples_for(fixture_binary, base, addrs, 'cpu_intensive'))
    assert by_ip._load_base.get(fixture_binary) == base
    by_ip.close()

    by_offset = make_mapper(fixture_binary, perflens_home)
    off_lines = by_offset.map_samples_to_lines(
        offset_samples_for(fixture_binary, start, addrs, 'cpu_intensive'))
    by_offset.close()

    assert ip_lines == off_lines, 'ip recovery disagrees with symoff'
    (_fpath, lines), = ip_lines.items()
    assert len(lines) > 1, 'all samples collapsed onto one line'


def test_ip_recovery_rejects_addresses_outside_the_symbol(fixture_binary,
                                                          perflens_home):
    """--binary makes every frame claim that binary, so a kernel or libc ip
    can arrive attributed to it. Subtracting the base would then yield an
    address belonging to no symbol -- and for a kernel ip, one too wide for
    SQLite, which used to abort the whole request with OverflowError.

    Such a frame falls back to the symbol address, exactly as before ip
    recovery existed. What must never happen is a bogus address reaching the
    resolver or the cache.
    """
    mapper = make_mapper(fixture_binary, perflens_home)
    base, start, addrs = spread_over(mapper, fixture_binary, 'cpu_intensive')
    end = mapper._symbol_spans(fixture_binary)[start]

    samples = ip_samples_for(fixture_binary, base, addrs, 'cpu_intensive')
    samples += [{'comm': 'w', 'pid': 1, 'tid': 1, 'event_count': 1,
                 'event_type': 'cycles',
                 'frames': [{'addr': 'ffffffff81234567',
                             'func': 'cpu_intensive',
                             'offset': '', 'module': fixture_binary}]}]

    line_data = mapper.map_samples_to_lines(samples)
    assert line_data, 'the well-formed frames should still resolve'

    kernel_ip = 0xffffffff81234567
    assert mapper._vaddr_from_ip(samples[-1]['frames'][0],
                                 fixture_binary, 'cpu_intensive') is None
    for cached_binary, vaddr in mapper._addr2line_cache:
        if cached_binary == fixture_binary:
            assert vaddr < end, 'an address outside the symbol was resolved'
            assert vaddr != kernel_ip - base, 'raw kernel ip reached the cache'
    mapper.close()


def test_missing_symoff_without_a_derivable_base_still_maps_the_file(
        fixture_binary, perflens_home):
    """With too few frames to pin a base down, fall back to the symbol
    address: the line is the function's first one, but the file is right."""
    mapper = make_mapper(fixture_binary, perflens_home)
    line_data = mapper.map_samples_to_lines(
        samples_for(fixture_binary, func='cpu_intensive', offset='', n=1))
    assert mapper._load_base.get(fixture_binary) is None
    assert line_data, 'fallback lost the file entirely'
    mapper.close()


def test_path_map_remaps_compile_prefix(fixture_binary, perflens_home,
                                        tmp_path):
    """A path_map entry rewrites DWARF compile-time paths to local ones."""
    local_dir = tmp_path / 'local-src'
    local_dir.mkdir()
    shutil.copy(SOURCE, local_dir / 'sample_workload.c')

    compile_dir = os.path.dirname(SOURCE)
    mapper = make_mapper(fixture_binary, perflens_home,
                         path_map={compile_dir: str(local_dir)})
    line_data = mapper.map_samples_to_lines(samples_for(fixture_binary))
    (fpath, lines), = line_data.items()
    annotated = mapper.annotate_source(fpath, lines)
    assert annotated, 'annotation through path_map failed'
    mapper.close()


# ---------------------------------------------------------------------------
# Module attribution
#
# --binary names the unstripped build of the profiled executable. Applying it
# to every frame regardless of module asks addr2line for addresses that are
# not in that file — and on a real capture most frames are libc/libm/kernel,
# not the executable. In the committed ARM fixture that is 62k of 124k frames.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('module', [
    '/usr/lib/aarch64-linux-gnu/libc.so.6',
    '/usr/lib/x86_64-linux-gnu/libm.so.6',
    '/lib/ld-linux-x86-64.so.2',
    'libfoo.so',
    '[kernel.kallsyms]',
    '[unknown]',
    '[vdso]',
])
def test_shared_and_kernel_frames_are_not_attributed_to_binary(
        fixture_binary, perflens_home, module):
    mapper = make_mapper(fixture_binary, perflens_home)
    chosen = mapper._binary_for_frame({'func': 'x', 'module': module})
    assert chosen != fixture_binary, (
        f'{module} frames must not resolve against --binary')


@pytest.mark.parametrize('module', [
    '/opt/app/matrixlab',       # the running executable, renamed vs --binary
    '/home/kali/perflens-test/sample_workload',
    'workload',
    '',                         # perf gave us nothing better
])
def test_executable_frames_still_use_binary(fixture_binary, perflens_home,
                                            module):
    """Must not become a basename comparison: the documented cross-compile
    workflow points --binary at a separately-named unstripped build."""
    mapper = make_mapper(fixture_binary, perflens_home)
    chosen = mapper._binary_for_frame({'func': 'x', 'module': module})
    assert chosen == fixture_binary


def test_line_mapping_unaffected_for_main_binary_frames(fixture_binary,
                                                        perflens_home):
    """The narrowing must not cost the case --binary exists for."""
    mapper = make_mapper(fixture_binary, perflens_home)
    line_data = mapper.map_samples_to_lines(samples_for(fixture_binary))
    assert line_data, 'main-binary frames should still resolve to source lines'
