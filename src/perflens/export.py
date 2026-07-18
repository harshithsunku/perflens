"""Export renderers: collapsed-stack text and standalone SVG flame graphs."""


def export_collapsed(samples):
    """Export samples in Brendan Gregg collapsed stack format.

    Each line: semicolon-separated stack (bottom to top) followed by space
    and sample count. Compatible with flamegraph.pl, speedscope, Perfetto.
    """
    stacks = {}
    for sample in samples:
        if not sample['frames']:
            continue
        # Build stack bottom-to-top (reversed frames, since frames[0] is leaf)
        funcs = [f['func'] for f in reversed(sample['frames'])]
        key = ';'.join(funcs)
        stacks[key] = stacks.get(key, 0) + 1

    lines = []
    for stack, count in sorted(stacks.items()):
        lines.append(f'{stack} {count}')
    return '\n'.join(lines) + '\n' if lines else ''


def render_flamegraph_svg(fg_root, total_samples, event_type):
    """Render flamegraph tree as standalone SVG with embedded styles."""
    width = 1200
    row_height = 18
    font_size = 11
    margin_top = 50  # space for title

    # Flatten tree
    rects = []
    _flatten_for_svg(fg_root, 0, 0, width, rects, total_samples)
    max_depth = max((r['depth'] for r in rects), default=0)
    height = margin_top + (max_depth + 1) * row_height + 4

    # Build SVG
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"'
        f' viewBox="0 0 {width} {height}" font-family="monospace">',
        '<style>',
        '  rect:hover { stroke: #fff; stroke-width: 1; }',
        '  text { pointer-events: none; fill: #fff; }',
        '  .title { font-size: 16px; fill: #333; font-weight: bold; }',
        '  .subtitle { font-size: 12px; fill: #666; }',
        '</style>',
        f'<rect width="{width}" height="{height}" fill="#f8f8f0"/>',
        f'<text x="10" y="20" class="title">PerfLens Flamegraph — {_svg_escape(event_type)}</text>',
        f'<text x="10" y="38" class="subtitle">{total_samples} samples</text>',
    ]

    for r in rects:
        inlined = r.get('inlined', False)
        hue = 30 + (_hash_code(r['name']) % 30)
        sat = 50 + (_hash_code(r['name'] + 'x') % 15) if inlined \
            else 80 + (_hash_code(r['name'] + 'x') % 20)
        light = 45 + (_hash_code(r['name'] + 'y') % 15)
        color = f'hsl({hue}, {sat}%, {light}%)'
        y = height - (r['depth'] + 1) * row_height
        rw = max(r['w'] - 1, 1)

        inlined_tag = ' (inlined)' if inlined else ''
        pct = f"{r['percent']:.1f}"
        title = f"{_svg_escape(r['name'])}{inlined_tag} ({r['value']} samples, {pct}%)"
        stroke = ' stroke-dasharray="3 2" stroke="rgba(0,0,0,0.3)" stroke-width="1"' \
            if inlined else ''
        lines.append('<g>')
        lines.append(f'  <rect x="{r["x"]:.1f}" y="{y}" width="{rw:.1f}"'
                     f' height="{row_height - 1}" fill="{color}" rx="1"{stroke}>'
                     f'<title>{title}</title></rect>')
        if r['w'] > 40:
            max_chars = int(r['w'] / 7)
            label = r['name'][:max_chars] + '..' if len(r['name']) > max_chars else r['name']
            lines.append(f'  <text x="{r["x"] + 3:.1f}" y="{y + 13}"'
                         f' font-size="{font_size}">{_svg_escape(label)}</text>')
        lines.append('</g>')

    lines.append('</svg>')
    return '\n'.join(lines)


def _flatten_for_svg(node, depth, x, width, rects, total_samples):
    """Flatten flamegraph tree into list of rects for SVG export."""
    pct = (node['value'] / total_samples * 100) if total_samples > 0 else 0
    entry = {
        'name': node['name'], 'value': node['value'], 'percent': pct,
        'depth': depth, 'x': x, 'w': width,
    }
    if node.get('inlined'):
        entry['inlined'] = True
    rects.append(entry)
    child_x = x
    for child in (node.get('children') or []):
        cw = (child['value'] / node['value']) * width if node['value'] > 0 else 0
        if cw >= 1:
            _flatten_for_svg(child, depth + 1, child_x, cw, rects, total_samples)
        child_x += cw


def _hash_code(s):
    """Simple string hash matching the JS hashCode function."""
    h = 0
    for c in s:
        h = ((h << 5) - h) + ord(c)
        h &= 0xFFFFFFFF
    return h


def _svg_escape(s):
    """Escape text for SVG/XML."""
    return (s.replace('&', '&amp;').replace('<', '&lt;')
             .replace('>', '&gt;').replace('"', '&quot;'))
