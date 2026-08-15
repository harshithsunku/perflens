"""Export renderers: collapsed stacks and standalone SVG flame graphs.

Both are pure functions over parsed samples and flamegraph trees — no I/O, no
subprocess — but they are what leaves the tool and gets opened in flamegraph.pl,
speedscope and Perfetto, so a malformed line or a broken XML entity shows up in
someone else's viewer rather than here.
"""

import xml.etree.ElementTree as ET

import pytest

from conftest import FIXTURE, load_fixture_chunks
from perflens.export import (
    _hash_code,
    _svg_escape,
    export_collapsed,
    render_flamegraph_svg,
)


def sample(*funcs):
    """A sample whose frames run leaf-first, as the parser produces them."""
    return {'frames': [{'func': f, 'module': 'a.out'} for f in funcs]}


def tree(name, value, children=(), **extra):
    return {'name': name, 'value': value, 'children': list(children), **extra}


# ---------------------------------------------------------------------------
# export_collapsed
# ---------------------------------------------------------------------------

def test_collapsed_empty_input():
    assert export_collapsed([]) == ''


def test_collapsed_skips_samples_with_no_frames():
    assert export_collapsed([{'frames': []}]) == ''


def test_collapsed_writes_stacks_bottom_to_top():
    """frames[0] is the leaf, so the emitted stack must be reversed."""
    out = export_collapsed([sample('leaf', 'middle', 'root')])
    assert out == 'root;middle;leaf 1\n'


def test_collapsed_counts_identical_stacks():
    out = export_collapsed([sample('a', 'b'), sample('a', 'b'), sample('c')])
    assert out == 'b;a 2\nc 1\n'


def test_collapsed_is_sorted_and_newline_terminated():
    out = export_collapsed([sample('z'), sample('a'), sample('m')])
    assert out == 'a 1\nm 1\nz 1\n'
    assert out.endswith('\n')


def test_collapsed_on_real_capture():
    chunks = load_fixture_chunks(FIXTURE)
    samples = [s for chunk in chunks for s in chunk]
    out = export_collapsed(samples)
    lines = out.rstrip('\n').split('\n')
    assert lines
    for line in lines:
        stack, _, count = line.rpartition(' ')
        assert stack, f'line has no stack: {line!r}'
        assert count.isdigit(), f'line does not end in a count: {line!r}'
    # Counts must account for every sample that had frames.
    total = sum(int(line.rpartition(' ')[2]) for line in lines)
    assert total == sum(1 for s in samples if s['frames'])


# ---------------------------------------------------------------------------
# render_flamegraph_svg
# ---------------------------------------------------------------------------

def test_svg_is_well_formed_xml():
    root = tree('root', 10, [tree('main', 8, [tree('work', 5)])])
    svg = render_flamegraph_svg(root, 10, 'cycles')
    parsed = ET.fromstring(svg)
    assert parsed.tag.endswith('svg')


def test_svg_zero_samples_does_not_divide_by_zero():
    """An event can be present with no samples yet — mid first chunk."""
    svg = render_flamegraph_svg(tree('root', 0), 0, 'cycles')
    ET.fromstring(svg)
    assert '0 samples' in svg


def test_svg_reports_event_and_sample_count():
    svg = render_flamegraph_svg(tree('root', 4), 4, 'instructions')
    assert 'instructions' in svg
    assert '4 samples' in svg


def test_svg_drops_subpixel_children():
    """Children narrower than a pixel are not emitted, so a wide tree cannot
    produce a rect per leaf."""
    root = tree('root', 10000,
                [tree('big', 9999), tree('sliver', 1)])
    svg = render_flamegraph_svg(root, 10000, 'cycles')
    assert 'sliver' not in svg
    assert 'big' in svg


def test_svg_marks_inlined_frames():
    root = tree('root', 10, [tree('inl', 10, inlined=True)])
    svg = render_flamegraph_svg(root, 10, 'cycles')
    assert 'inlined' in svg
    assert 'stroke-dasharray' in svg


@pytest.mark.parametrize('name', [
    'std::vector<int>&',
    'operator<<',
    'foo(char const*, bool&)',
    'Ns::T<A&B>::run',
])
def test_svg_escapes_cpp_symbols(name):
    """Templated C++ names carry <, > and & — the characters that break XML."""
    root = tree('root', 100, [tree(name, 100)])
    svg = render_flamegraph_svg(root, 100, 'cycles')
    ET.fromstring(svg)          # raises if a raw < or & leaked through
    assert '<title>' in svg


def test_svg_truncated_label_stays_well_formed():
    """Labels are truncated to fit the rect, and the cut can land anywhere.

    This is safe only because truncation happens on the raw name and escaping
    comes after; escaping first and cutting second would slice `&amp;` in half
    and produce invalid XML. The loop walks the special characters across the
    cut point (171 chars at full width) to hold that ordering in place.
    """
    max_chars = int(1200 / 7)
    for offset in range(-4, 5):
        name = 'a' * (max_chars + offset) + '&<>"' + 'b' * 40
        assert len(name) > max_chars, 'test must actually trigger truncation'
        root = tree('root', 100, [tree(name, 100)])
        svg = render_flamegraph_svg(root, 100, 'cycles')
        ET.fromstring(svg)
        assert '..' in svg, 'label was not truncated'


def test_svg_on_real_capture():
    from perflens.aggregator import EventAccumulator
    chunks = load_fixture_chunks(FIXTURE)
    acc = EventAccumulator('cycles')
    for chunk in chunks:
        acc.add_samples(chunk)
    snap = acc.snapshot()
    svg = render_flamegraph_svg(snap['flamegraph'], acc.total_samples, 'cycles')
    ET.fromstring(svg)
    assert 'PerfLens Flamegraph' in svg


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def test_hash_code_is_stable_and_bounded():
    """Mirrors the JS hashCode so exported SVGs colour like the live UI."""
    assert _hash_code('main') == _hash_code('main')
    assert _hash_code('') == 0
    for s in ('a', 'main', 'a' * 500, 'ünïcødé'):
        assert 0 <= _hash_code(s) <= 0xFFFFFFFF


def test_svg_escape_handles_ampersand_first():
    """Escaping & after < would double-escape the entity it just produced."""
    assert _svg_escape('a&b') == 'a&amp;b'
    assert _svg_escape('<a>') == '&lt;a&gt;'
    assert _svg_escape('"q"') == '&quot;q&quot;'
    assert _svg_escape('&lt;') == '&amp;lt;'
