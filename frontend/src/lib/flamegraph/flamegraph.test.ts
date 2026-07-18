import { describe, expect, it } from 'vitest';
import { layoutFlamegraph } from './layout';
import { pathToNode, walkBaseline, walkZoomNames } from './zoom';
import { fgDiffColor, fgModuleColor } from './colors';
import type { FlameNode } from './types';

const tree: FlameNode = {
  name: 'root', value: 100,
  children: [
    { name: 'main', value: 90, module: '/usr/bin/app',
      children: [
        { name: 'hot', value: 60, module: '/usr/bin/app', children: [] },
        { name: 'cold', value: 30, module: 'libc.so.6', children: [] },
      ] },
    { name: 'other', value: 10, children: [] },
  ],
};

describe('layoutFlamegraph', () => {
  it('assigns proportional widths', () => {
    const { rects } = layoutFlamegraph(tree, { width: 1000, totalSamples: 100 });
    const main = rects.find((r) => r.name === 'main')!;
    const hot = rects.find((r) => r.name === 'hot')!;
    expect(main.w).toBeCloseTo(900);
    expect(hot.w).toBeCloseTo(600);
    expect(hot.percent).toBeCloseTo(60);
    expect(hot.depth).toBe(2);
  });

  it('prunes children narrower than 1px', () => {
    const wide: FlameNode = {
      name: 'root', value: 1000,
      children: [
        { name: 'big', value: 999, children: [] },
        { name: 'tiny', value: 1, children: [] },
      ],
    };
    const { rects } = layoutFlamegraph(wide, { width: 500, totalSamples: 1000 });
    expect(rects.find((r) => r.name === 'tiny')).toBeUndefined();
    expect(rects.find((r) => r.name === 'big')).toBeDefined();
  });

  it('computes basePct against a baseline tree; new paths get 0', () => {
    const baseline: FlameNode = {
      name: 'root', value: 50,
      children: [
        { name: 'main', value: 50,
          children: [{ name: 'hot', value: 10, children: [] }] },
      ],
    };
    const { rects } = layoutFlamegraph(tree, {
      width: 1000, totalSamples: 100, baseNode: baseline, baseTotal: 50,
    });
    const hot = rects.find((r) => r.name === 'hot')!;
    expect(hot.basePct).toBeCloseTo(20); // 10/50
    const cold = rects.find((r) => r.name === 'cold')!;
    expect(cold.basePct).toBe(0); // not in baseline
  });

  it('reports height from max depth', () => {
    const { maxDepth, height } = layoutFlamegraph(tree, { width: 1000, totalSamples: 100 });
    expect(maxDepth).toBe(2);
    expect(height).toBe(3 * 18 + 4);
  });
});

describe('walkZoomNames', () => {
  it('walks a full path', () => {
    const z = walkZoomNames(tree, ['main', 'hot']);
    expect(z.node?.name).toBe('hot');
    expect(z.walkedNames).toEqual(['main', 'hot']);
  });

  it('truncates where the path breaks', () => {
    const z = walkZoomNames(tree, ['main', 'vanished', 'hot']);
    expect(z.node?.name).toBe('main');
    expect(z.walkedNames).toEqual(['main']);
  });

  it('returns null node when nothing matches', () => {
    const z = walkZoomNames(tree, ['nope']);
    expect(z.node).toBeNull();
    expect(z.walkedNames).toEqual([]);
  });
});

describe('pathToNode', () => {
  it('finds the ancestry path by identity', () => {
    const hot = tree.children![0].children![0];
    expect(pathToNode(tree, hot)).toEqual(['main', 'hot']);
  });

  it('returns null for a foreign node', () => {
    expect(pathToNode(tree, { name: 'x', value: 1 })).toBeNull();
  });
});

describe('walkBaseline', () => {
  it('follows the zoom path in the baseline', () => {
    expect(walkBaseline(tree, ['main', 'cold'])?.value).toBe(30);
    expect(walkBaseline(tree, ['main', 'gone'])).toBeNull();
  });
});

describe('colors', () => {
  it('diff color is grey near zero, red when grown, blue when shrunk', () => {
    expect(fgDiffColor(0.05, true)).toContain('220');
    expect(fgDiffColor(5, true)).toMatch(/^hsl\(4,/);
    expect(fgDiffColor(-5, true)).toMatch(/^hsl\(214,/);
  });

  it('module color buckets kernel/user/library', () => {
    expect(fgModuleColor('f', '[kernel.kallsyms]', false, true)).toMatch(/^hsl\((2\d|3\d|4[0-4]),/);
    expect(fgModuleColor('f', 'libc.so.6', false, true)).toMatch(/^hsl\((19\d|2[0-2]\d),/);
    // Unknown module → hue 0 with only the small hash-variance saturation
    expect(fgModuleColor('f', '', false, true)).toMatch(/^hsl\(0, (\d|1[0-4])%/);
  });

  it('is deterministic per name', () => {
    expect(fgModuleColor('foo', '/usr/bin/app', false, true))
      .toBe(fgModuleColor('foo', '/usr/bin/app', false, true));
  });
});
