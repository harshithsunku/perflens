"""Rendering helpers shared by the MCP tools.

Two jobs, both about context budget. First, every tool answers in markdown
by default (a ranked table reads well and costs fewer tokens than the
equivalent JSON) while still offering `response_format="json"` for
programmatic use. Second, nothing is ever returned whole: a single event's
snapshot runs from 8 KB to well over 140 KB, so results are ranked, capped,
and the reply says exactly which call fetches the next page.
"""

import json

MAX_LIMIT = 100

# Beyond this a call path is more noise than signal, so the middle is
# elided rather than the whole line being dropped.
_MAX_PATH_FRAMES = 10


def respond(data, markdown, response_format='markdown'):
    """Return either the rendered markdown or the raw structured payload."""
    if response_format == 'json':
        return json.dumps(data, indent=2, sort_keys=True)
    return markdown


def clamp(limit, default=20):
    if limit is None:
        return default
    return max(1, min(int(limit), MAX_LIMIT))


def table(headers, rows):
    """Render a markdown table. Empty rows render as a plain note."""
    if not rows:
        return '_(nothing to show)_'
    out = ['| ' + ' | '.join(headers) + ' |',
           '|' + '|'.join('---' for _ in headers) + '|']
    for row in rows:
        out.append('| ' + ' | '.join(str(c) for c in row) + ' |')
    return '\n'.join(out)


def page(items, limit, offset=0):
    """Slice `items` and describe the slice.

    Returning the follow-up offset (rather than just a truncation warning)
    is what lets an agent page through without guessing.
    """
    total = len(items)
    offset = max(0, int(offset or 0))
    limit = clamp(limit)
    window = items[offset:offset + limit]
    return window, {'total': total, 'shown': len(window), 'offset': offset,
                    'has_more': offset + len(window) < total,
                    'next_offset': offset + len(window)}


def page_note(info, tool, args=''):
    """One line telling the agent how to get the rest, when there is more."""
    if not info['has_more']:
        return ''
    joined = f'{args}, ' if args else ''
    return (f"\n\n_Showing {info['shown']} of {info['total']}. For the next "
            f"page: {tool}({joined}offset={info['next_offset']})._")


def fmt_pct(value):
    return f'{value:.1f}%'


def coverage(functions, total_samples):
    """How much of the profile the listed functions actually account for.

    Without this an agent can read a top-10 that covers 4% of samples and
    confidently name the wrong bottleneck.
    """
    if not total_samples:
        return 0.0
    covered = sum(f.get('self_samples', 0) for f in functions)
    return 100.0 * covered / total_samples


def sort_functions(functions, sort='self'):
    """Order by self (the leaf doing the work) or total (the subtree)."""
    key = 'total_samples' if sort == 'total' else 'self_samples'
    return sorted(functions, key=lambda f: f.get(key, 0), reverse=True)


def function_rows(functions):
    return [(f.get('name', '?'),
             f.get('module', '') or '-',
             f.get('self_samples', 0),
             fmt_pct(f.get('self_percent', 0.0)),
             f.get('total_samples', 0),
             fmt_pct(f.get('total_percent', 0.0)))
            for f in functions]


FUNCTION_HEADERS = ['function', 'module', 'self', 'self %', 'total', 'total %']


def fold_stacks(node, limit=10, min_percent=0.0, total=None):
    """Fold a flamegraph tree into its hottest leaf-to-root call paths.

    A flamegraph is a picture; an agent needs the same information as
    ranked text. Each node's *self* weight is its value minus its
    children's, so the ranking surfaces the frames actually burning samples
    together with how they were reached.
    """
    total = total or node.get('value', 0) or 1
    paths = []

    def walk(n, trail):
        children = n.get('children') or []
        name = n.get('name', '?')
        here = trail if name == 'root' and not trail else trail + [name]
        self_value = n.get('value', 0) - sum(c.get('value', 0) for c in children)
        if self_value > 0 and here:
            paths.append({'path': here,
                          'self_samples': self_value,
                          'self_percent': round(100.0 * self_value / total, 2)})
        for child in children:
            walk(child, here)

    walk(node, [])
    paths.sort(key=lambda p: p['self_samples'], reverse=True)
    kept = [p for p in paths if p['self_percent'] >= min_percent]
    return kept[:clamp(limit, default=10)]


def render_path(frames):
    """Render a call path root-first, eliding the middle when very deep."""
    if len(frames) > _MAX_PATH_FRAMES:
        head, tail = frames[:3], frames[-5:]
        frames = head + [f'… ({len(frames) - 8} frames)'] + tail
    return ' → '.join(frames)


def stack_lines(paths):
    if not paths:
        return '_(no stacks above the threshold)_'
    return '\n'.join(
        f"{i + 1}. **{fmt_pct(p['self_percent'])}** "
        f"({p['self_samples']} samples) — {render_path(p['path'])}"
        for i, p in enumerate(paths))


def derived_counters(perf_stat):
    """Derive the ratios that turn raw counters into a diagnosis.

    IPC and the miss rates are what distinguish "this loop is slow" from
    "this workload is starved on memory", which changes the fix entirely.
    """
    def value(*names):
        for name in names:
            raw = perf_stat.get(name)
            if isinstance(raw, dict):
                raw = raw.get('value')
            if isinstance(raw, str):
                try:
                    raw = float(raw.replace(',', ''))
                except ValueError:
                    raw = None
            if isinstance(raw, (int, float)):
                return float(raw)
        return None

    cycles = value('cycles', 'cpu-cycles')
    instructions = value('instructions')
    misses = value('cache-misses')
    refs = value('cache-references')
    branches = value('branches', 'branch-instructions')
    branch_misses = value('branch-misses')

    derived = {}
    if cycles and instructions:
        derived['ipc'] = round(instructions / cycles, 3)
    if refs and misses is not None:
        derived['cache_miss_rate_percent'] = round(100.0 * misses / refs, 2)
    if branches and branch_misses is not None:
        derived['branch_miss_rate_percent'] = round(100.0 * branch_misses / branches, 2)
    return derived


def source_context(lines, hot_indices, context):
    """Keep hot lines plus `context` lines either side, in file order.

    Returns the kept line records with a flag marking the hot ones, so the
    agent sees the surrounding code without paying for the whole file.
    """
    keep = set()
    for idx in hot_indices:
        for j in range(idx - context, idx + context + 1):
            if 0 <= j < len(lines):
                keep.add(j)
    return sorted(keep)
