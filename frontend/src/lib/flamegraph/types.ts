/** Flamegraph tree node as served by the API. */
export interface FlameNode {
  name: string;
  value: number;
  children?: FlameNode[];
  inlined?: boolean;
  module?: string;
}

/** One laid-out rectangle (row-major, bottom-up depth). */
export interface FlameRect {
  name: string;
  value: number;
  percent: number;
  depth: number;
  x: number;
  w: number;
  node: FlameNode;
  inlined: boolean;
  module: string;
  /** Baseline share of the same stack path (diff mode); null = diff off. */
  basePct: number | null;
}
