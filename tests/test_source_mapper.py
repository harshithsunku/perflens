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
