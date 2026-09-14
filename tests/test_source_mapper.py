"""SourceMapper tests against a real -g binary compiled at test time.

Covers: symbol loading, addr2line line mapping, source annotation,
path-map remapping, and the persistent symbol cache (PERFLENS_HOME).
Skipped when gcc/addr2line/readelf aren't available.
"""

import os
import shutil
import subprocess
import time

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


# ---------------------------------------------------------------------------
# Naming frames the target's perf could not.
#
# A perf built without libelf resolves kernel frames from kallsyms but returns
# '[unknown]' for every userspace frame, however good the binary is. Measured
# on one device at 99.6% of samples. The address survives, and the server has
# the unstripped binary, so the name is recoverable here.
# ---------------------------------------------------------------------------

def _addr_of(binary, func):
    """File vaddr of a function, straight from readelf."""
    out = subprocess.run([shutil.which('readelf'), '-sW', binary],
                         capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 8 and f[3] == 'FUNC' and f[7] == func:
            return int(f[1], 16)
    raise AssertionError(f'{func} not found in {binary}')


def unknown_samples(module, addr_hex, n=3):
    """Frames shaped the way a libelf-less perf emits them."""
    return [{'comm': 'w', 'pid': 1, 'tid': 1, 'event_count': 1,
             'event_type': 'cycles',
             'frames': [{'addr': addr_hex, 'func': '[unknown]',
                         'offset': '', 'module': module}]}
            for _ in range(n)]


def test_unknown_frame_gets_named_from_the_address(fixture_binary,
                                                   perflens_home):
    """The headline case: perf gave us an address and no name."""
    mapper = make_mapper(fixture_binary, perflens_home)
    addr = _addr_of(fixture_binary, 'cpu_intensive')
    samples = unknown_samples(fixture_binary, format(addr + 4, 'x'))

    named = mapper.resolve_unknown_frames(samples)

    assert named == 3, 'expected every frame named'
    assert samples[0]['frames'][0]['func'] == 'cpu_intensive'
    mapper.close()


def test_address_outside_any_symbol_stays_unknown(fixture_binary,
                                                  perflens_home):
    """The anti-invention guarantee.

    A confidently wrong name is worse than an honest blank, so an address
    that lands in no symbol must be left alone.
    """
    mapper = make_mapper(fixture_binary, perflens_home)
    samples = unknown_samples(fixture_binary, format(0x7fffffff0000, 'x'))

    named = mapper.resolve_unknown_frames(samples)

    assert named == 0
    assert samples[0]['frames'][0]['func'] == '[unknown]'
    mapper.close()


def test_unmapped_shared_object_stays_unknown(fixture_binary, perflens_home):
    """A .so with no local file must not be resolved against --binary.

    This is the additive-only rule: resolving a libc address against the
    executable's symbol table yields a real name for the wrong function.
    """
    mapper = make_mapper(fixture_binary, perflens_home)
    addr = _addr_of(fixture_binary, 'cpu_intensive')
    samples = unknown_samples('/lib/libc-2.18.so', format(addr + 4, 'x'))

    named = mapper.resolve_unknown_frames(samples)

    assert named == 0
    assert samples[0]['frames'][0]['func'] == '[unknown]'
    mapper.close()


def test_already_named_frames_are_untouched(fixture_binary, perflens_home):
    mapper = make_mapper(fixture_binary, perflens_home)
    samples = samples_for(fixture_binary, func='main')
    assert mapper.resolve_unknown_frames(samples) == 0
    assert samples[0]['frames'][0]['func'] == 'main'
    mapper.close()


def test_module_map_points_a_device_path_at_a_local_binary(fixture_binary,
                                                           perflens_home):
    """--module-map is the escape hatch for a device path that exists
    nowhere locally, which is normal for a firmware image."""
    device_path = '/opt/fw/libthing.so'
    addr = _addr_of(fixture_binary, 'cpu_intensive')
    samples = unknown_samples(device_path, format(addr + 4, 'x'))

    mapper = make_mapper(fixture_binary, perflens_home,
                         module_map={device_path: fixture_binary})
    assert mapper.resolve_unknown_frames(samples) == 3
    assert samples[0]['frames'][0]['func'] == 'cpu_intensive'
    mapper.close()


# ---------------------------------------------------------------------------
# Shared libraries.
#
# Whether a .so frame resolves turns on the address form perf emitted, not on
# it being a library. perf prints a file-relative offset when it worked out the
# module's load base and the raw runtime address when it did not; only the
# first is usable without a base, and a base can only be recovered by voting
# with named frames — which a libelf-less perf never provides.
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def fixture_so(tmp_path_factory):
    d = tmp_path_factory.mktemp('so')
    src = str(d / 'lib.c')
    with open(src, 'w') as f:
        f.write('#include <math.h>\n'
                'double lib_hot_function(double x){return sin(x)*cos(x);}\n')
    so = str(d / 'libthing.so')
    subprocess.run(['gcc', '-g', '-O0', '-shared', '-fPIC', '-o', so, src,
                    '-lm'], check=True, capture_output=True)
    return so


def _sysrooted(tmp_path, so, device_path='/lib/libthing.so'):
    """Copy a .so where --sysroot will find it under its device path."""
    root = tmp_path / 'sysroot'
    (root / os.path.dirname(device_path.lstrip('/'))).mkdir(
        parents=True, exist_ok=True)
    shutil.copy(so, str(root) + device_path)
    return str(root)


def test_shared_library_resolves_from_a_file_relative_address(
        fixture_so, perflens_home, tmp_path):
    """The case that actually occurs in per-round collection."""
    sysroot = _sysrooted(tmp_path, fixture_so)
    addr = _addr_of(fixture_so, 'lib_hot_function')
    samples = unknown_samples('/lib/libthing.so', format(addr + 8, 'x'), n=1)

    mapper = make_mapper(None, perflens_home, sysroot=sysroot)
    assert mapper.resolve_unknown_frames(samples) == 1
    assert samples[0]['frames'][0]['func'] == 'lib_hot_function'
    mapper.close()


def test_shared_library_absolute_address_stays_unknown(fixture_so,
                                                       perflens_home,
                                                       tmp_path):
    """No load base, no answer — and guessing one would be a wrong name."""
    sysroot = _sysrooted(tmp_path, fixture_so)
    addr = _addr_of(fixture_so, 'lib_hot_function')
    samples = unknown_samples('/lib/libthing.so',
                              format(0x7f9c00000000 + addr + 8, 'x'), n=1)

    mapper = make_mapper(None, perflens_home, sysroot=sysroot)
    assert mapper.resolve_unknown_frames(samples) == 0
    assert samples[0]['frames'][0]['func'] == '[unknown]'
    mapper.close()


def test_shared_library_without_a_local_copy_stays_unknown(fixture_so,
                                                           perflens_home):
    addr = _addr_of(fixture_so, 'lib_hot_function')
    samples = unknown_samples('/lib/libthing.so', format(addr + 8, 'x'), n=1)

    mapper = make_mapper(None, perflens_home)
    assert mapper.resolve_unknown_frames(samples) == 0
    assert samples[0]['frames'][0]['func'] == '[unknown]'
    mapper.close()


# ---------------------------------------------------------------------------
# One mapper, many threads; a tool that hangs or dies
# ---------------------------------------------------------------------------

def test_concurrent_resolution_matches_single_thread(fixture_binary,
                                                     perflens_home):
    """The rebuild worker and the request threads share one mapper. Its
    addr2line protocol is "write N, read 2N lines", so two threads
    interleaving on a pipe read each other's answers -- names and lines
    silently swapped, and the pipe desynchronized for good."""
    import threading

    probe = make_mapper(fixture_binary, perflens_home)
    jobs = []
    for func in ('cpu_intensive', 'memory_churner', 'string_worker',
                 'sorting_worker'):
        _base, start, addrs = spread_over(probe, fixture_binary, func)
        jobs.append(offset_samples_for(fixture_binary, start, addrs, func))
    probe.close()

    expected = []
    for job in jobs:
        m = make_mapper(fixture_binary, perflens_home)
        expected.append(m.map_samples_to_lines(job))
        m.close()
    assert all(expected), 'fixture resolves to nothing'

    shared = make_mapper(fixture_binary, perflens_home)
    results = [None] * len(jobs)
    errors = []

    def run(i):
        try:
            for _ in range(20):
                results[i] = shared.map_samples_to_lines(jobs[i])
        except Exception as e:      # noqa: BLE001 - reported below
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(jobs))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    shared.close()
    assert not errors
    assert results == expected


def _fake_addr2line(tmp_path, body):
    script = tmp_path / 'addr2line'
    script.write_text('#!/bin/sh\n' + body)
    script.chmod(0o755)
    return str(script)


def _cached_pairs(mapper, binary):
    return {k: v for k, v in mapper._addr2line_cache.items() if k[0] == binary}


def test_hung_addr2line_is_killed_and_nothing_is_cached(fixture_binary,
                                                        perflens_home,
                                                        tmp_path, monkeypatch):
    """A hung addr2line used to block the worker in readline() forever,
    freezing every UI update with nothing in the log. It is killed after
    the read timeout, its unanswered addresses stay unresolved rather than
    cached as '??', and a healthy tool resolves them afterwards."""
    from perflens import symcache
    from perflens.source_mapper import Addr2LinePipe

    monkeypatch.setattr(Addr2LinePipe, 'read_timeout', 0.3)
    hung = _fake_addr2line(tmp_path, 'exec sleep 30\n')
    mapper = make_mapper(fixture_binary, perflens_home, addr2line_bin=hung)
    samples = samples_for(fixture_binary, 'main', '0x0')
    t0 = time.monotonic()
    assert mapper.map_samples_to_lines(samples) == {}
    assert time.monotonic() - t0 < 5
    pipe = mapper._pipes[fixture_binary]
    assert pipe.failures == 1 and not pipe.disabled
    assert _cached_pairs(mapper, fixture_binary) == {}
    mapper.close()
    assert symcache.SymbolCache().load_addr2line(
        symcache.binary_key(fixture_binary)) == {}

    healthy = make_mapper(fixture_binary, perflens_home)
    assert healthy.map_samples_to_lines(samples)
    healthy.close()


def test_addr2line_dying_mid_batch_leaves_the_rest_for_a_retry(
        fixture_binary, perflens_home, tmp_path):
    """Answers one address per life, then exits. Each call resolves one
    more; the unanswered ones are never remembered as '??'."""
    flaky = _fake_addr2line(tmp_path,
                            'read a\necho main\necho /src/w.c:7\nexit 0\n')
    mapper = make_mapper(fixture_binary, perflens_home, addr2line_bin=flaky)
    samples = [samples_for(fixture_binary, 'main', hex(o))[0]
               for o in (0x0, 0x4, 0x8)]
    got = mapper.map_samples_to_lines(samples)
    assert sum(sum(d['samples'] for d in lines.values())
               for lines in got.values()) == 1
    assert len(_cached_pairs(mapper, fixture_binary)) == 1
    assert mapper._pipes[fixture_binary].failures == 1
    mapper.map_samples_to_lines(samples)
    got = mapper.map_samples_to_lines(samples)
    assert len(_cached_pairs(mapper, fixture_binary)) == 3
    assert all(v == ('/src/w.c', 7)
               for v in _cached_pairs(mapper, fixture_binary).values())
    assert got == {'/src/w.c': {7: {'samples': 3}}}
    mapper.close()


def test_pipe_is_given_up_after_repeated_failures(fixture_binary,
                                                   perflens_home, tmp_path):
    """Three deaths in a row disable the pipe with one log line; later
    lookups answer '??' at once instead of respawning a broken tool per
    chunk, and nothing about it is persisted."""
    from perflens import symcache
    dead = _fake_addr2line(tmp_path, 'exit 1\n')
    mapper = make_mapper(fixture_binary, perflens_home, addr2line_bin=dead)
    samples = samples_for(fixture_binary, 'main', '0x0')
    for _ in range(3):
        assert mapper.map_samples_to_lines(samples) == {}
    pipe = mapper._pipes[fixture_binary]
    assert pipe.disabled and pipe.failures == 3
    assert mapper._get_pipe(fixture_binary) is None
    assert mapper.map_samples_to_lines(samples) == {}
    assert list(_cached_pairs(mapper, fixture_binary).values()) == [('??', 0)]
    mapper.close()
    assert symcache.SymbolCache().load_addr2line(
        symcache.binary_key(fixture_binary)) == {}


def test_close_stops_the_mapper_spawning_tools(fixture_binary, perflens_home):
    mapper = make_mapper(fixture_binary, perflens_home)
    assert mapper.map_samples_to_lines(samples_for(fixture_binary))
    assert mapper._pipes
    mapper.close()
    assert not mapper._pipes
    assert mapper._get_pipe(fixture_binary) is None


def test_replay_resolution_does_not_count_toward_the_live_tally(
        fixture_binary, perflens_home):
    mapper = make_mapper(fixture_binary, perflens_home)
    frames = [{'addr': '0', 'func': '[unknown]', 'offset': '',
               'module': fixture_binary}]
    samples = [{'comm': 'w', 'pid': 1, 'tid': 1, 'event_count': 1,
                'event_type': 'cycles', 'frames': list(frames)}]
    mapper.resolve_unknown_frames(samples, count=False)
    assert mapper.symbolization_stats()['userspace_frames'] == 0
    mapper.resolve_unknown_frames(samples)
    assert mapper.symbolization_stats()['userspace_frames'] == 1
    mapper.close()
