// Pure flamegraph geometry — ported from app.js flattenTree(). No DOM.

import type { FlameNode, FlameRect } from './types';

export interface LayoutOptions {
  width: number;
  totalSamples: number;
  /** Baseline subtree walked in parallel (diff mode). */
  baseNode?: FlameNode | null;
  baseTotal?: number;
}

export interface LayoutResult {
  rects: FlameRect[];
  maxDepth: number;
  height: number;
}

export const ROW_HEIGHT = 18;
export const FONT_SIZE = 11;
export const CHAR_WIDTH = 6.5;

/**
 * Flatten a flamegraph tree into positioned rects. Children narrower than
 * 1px are pruned (keeps rect counts low on deep profiles). When a baseline
 * tree is supplied, each rect gets basePct — the same stack path's share
 * of the baseline total (0 when the path is new there).
 */
export function layoutFlamegraph(tree: FlameNode, opts: LayoutOptions): LayoutResult {
  const rects: FlameRect[] = [];
  const totalSamples = opts.totalSamples || tree.value;
  const baseTotal = opts.baseTotal ?? 0;
  const maxDepth = flatten(tree, 0, 0, opts.width, rects, totalSamples,
                           opts.baseNode ?? null, baseTotal);
  return { rects, maxDepth, height: (maxDepth + 1) * ROW_HEIGHT + 4 };
}

function flatten(node: FlameNode, depth: number, x: number, width: number,
                 rects: FlameRect[], totalSamples: number,
                 baseNode: FlameNode | null, baseTotal: number): number {
  const percent = totalSamples > 0 ? (node.value / totalSamples) * 100 : 0;
  let basePct: number | null = null;
  if (baseTotal > 0) basePct = baseNode ? (baseNode.value / baseTotal) * 100 : 0;
  rects.push({
    name: node.name, value: node.value, percent, depth, x, w: width,
    node, inlined: !!node.inlined, module: node.module || '', basePct,
  });

  let maxDepth = depth;
  let childX = x;
  if (node.children) {
    // Child index on the baseline node keeps matching O(1)
    let bmap: Map<string, FlameNode> | null = null;
    if (baseTotal > 0 && baseNode?.children) {
      bmap = new Map(baseNode.children.map((bc) => [bc.name, bc]));
    }
    for (const child of node.children) {
      const childWidth = node.value > 0 ? (child.value / node.value) * width : 0;
      if (childWidth >= 1) {
        const baseChild = bmap ? (bmap.get(child.name) ?? null) : null;
        const d = flatten(child, depth + 1, childX, childWidth, rects,
                          totalSamples, baseChild, baseTotal);
        maxDepth = Math.max(maxDepth, d);
      }
      childX += childWidth;
    }
  }
  return maxDepth;
}
