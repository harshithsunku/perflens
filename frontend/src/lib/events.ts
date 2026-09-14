// Event-name helpers shared by the stores and views.
//
// A hybrid P/E-core CPU never reports a bare `cycles`: it reports
// `cpu_core/cycles/` and `cpu_atom/cycles/`. The server resolves a base
// name onto one of those when it is unambiguous and refuses (400
// ambiguous_event) when it is not, so the UI has to pick a concrete name
// before it asks.

/** `cpu_core/cycles/` -> `cycles`; `cycles:u` -> `cycles`. */
export function eventBase(event: string): string {
  let name = event;
  const colon = name.indexOf(':');
  if (colon >= 0) name = name.slice(0, colon);
  const slash = name.indexOf('/');
  if (slash >= 0) {
    name = name.slice(slash + 1);
    if (name.endsWith('/')) name = name.slice(0, -1);
    const mod = name.lastIndexOf('/');
    if (mod >= 0) name = name.slice(0, mod);
  }
  return name;
}

/**
 * Resolve `wanted` onto one of `available`: an exact hit, else the
 * same base name on the performance cores, else any PMU with that base,
 * else null.
 */
export function resolveEvent(wanted: string, available: string[]): string | null {
  if (available.includes(wanted)) return wanted;
  const base = eventBase(wanted);
  const matches = available.filter((e) => eventBase(e) === base);
  if (!matches.length) return null;
  return matches.find((e) => e.startsWith('cpu_core/')) ?? matches[0];
}

/** The event to show when nothing specific was asked for: time first
 * (cycles, then the software clocks a PMU-less target samples on). */
export function defaultEvent(available: string[]): string | null {
  if (!available.length) return null;
  for (const base of ['cycles', 'cpu-clock', 'task-clock']) {
    const hit = resolveEvent(base, available);
    if (hit) return hit;
  }
  return available[0];
}

/**
 * Pick the event to select when the available set changes: keep the
 * current one when it is (or resolves to) one of them, else the default.
 */
export function pickEvent(current: string, available: string[]): string {
  if (!available.length) return current;
  return resolveEvent(current, available) ?? defaultEvent(available) ?? current;
}

/**
 * Order the version stamps the server sends: a reset bumps `generation`
 * and restarts `chunk_count` at 0, so the pair orders lexicographically.
 * Both are far below 2^31, so one number carries both.
 */
export function versionKey(v: { generation?: number | null; chunk_count?: number | null }): number {
  return (v.generation ?? 1) * 4294967296 + (v.chunk_count ?? 0);
}
