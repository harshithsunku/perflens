// Zoom-by-name-path: node references go stale on every data refresh; a
// path of ancestor names from the root survives re-fetches — and unlike a
// global name search, cannot land on a different stack. Ported from
// app.js applyZoomFromNames / pathToNode.

import type { FlameNode } from './types';

export interface ZoomResult {
  /** The deepest node the path reached (null when nothing matched). */
  node: FlameNode | null;
  /** The names actually walked (truncated where the path broke). */
  walkedNames: string[];
  /** Nodes along the walked path, root child first (excludes tree root). */
  walkedNodes: FlameNode[];
}

/** Walk `names` down from `tree`, truncating where a name no longer
 * matches (a function can disappear between rounds). */
export function walkZoomNames(tree: FlameNode, names: string[]): ZoomResult {
  let node: FlameNode = tree;
  const walked: FlameNode[] = [];
  for (const name of names) {
    const next = (node.children ?? []).find((k) => k.name === name);
    if (!next) break;
    node = next;
    walked.push(node);
  }
  return {
    node: walked.length ? node : null,
    walkedNames: walked.map((n) => n.name),
    walkedNodes: walked,
  };
}

/** Name path from `root` down to `target` (identity match), or null. */
export function pathToNode(root: FlameNode, target: FlameNode): string[] | null {
  if (root === target) return [];
  for (const kid of root.children ?? []) {
    if (kid === target) return [kid.name];
    const sub = pathToNode(kid, target);
    if (sub) {
      sub.unshift(kid.name);
      return sub;
    }
  }
  return null;
}

/** Walk the baseline tree along the zoom path (diff mode); null when the
 * zoomed subtree does not exist in the baseline (it is all "new"). */
export function walkBaseline(baseTree: FlameNode, names: string[]): FlameNode | null {
  let node: FlameNode | null = baseTree;
  for (const name of names) {
    if (!node) return null;
    node = (node.children ?? []).find((k) => k.name === name) ?? null;
  }
  return node;
}
