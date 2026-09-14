import { useDeferredValue, useEffect, useMemo, useRef, useState } from 'react';
import { CHAR_WIDTH, FONT_SIZE, ROW_HEIGHT, layoutFlamegraph } from '../lib/flamegraph/layout';
import type { FlameNode, FlameRect } from '../lib/flamegraph/types';
import { fgDiffColor, fgModuleColor } from '../lib/flamegraph/colors';
import { pathToNode, walkBaseline, walkZoomNames } from '../lib/flamegraph/zoom';
import { useContainerWidth } from '../lib/useContainerWidth';
import { useLive } from '../store/live';
import { useUi } from '../store/ui';

interface Props {
  /** Full (unzoomed) tree for the current view. */
  tree: FlameNode | null;
  totalSamples: number;
  /** Zoom + diff apply only on the main unfiltered view. */
  allowZoom: boolean;
  onShowSource: (funcName: string) => void;
}

interface CtxMenu { x: number; y: number; funcName: string; idx: number }

const CTX_MENU_W = 200;
const CTX_MENU_H = 110;

/** The rect index of the `<g data-idx>` under an event target, if any.
 * One delegated handler on the <svg> instead of four closures per frame
 * (a wide profile renders thousands). */
function rectIndex(target: EventTarget | null): number | null {
  const el = target as Element | null;
  const g = el?.closest?.('g[data-idx]');
  if (!g) return null;
  const idx = Number(g.getAttribute('data-idx'));
  return Number.isFinite(idx) ? idx : null;
}

export default function FlameGraph({ tree, totalSamples, allowZoom, onShowSource }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const infoBarRef = useRef<HTMLDivElement>(null);
  const clickTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const width = useContainerWidth(containerRef, 32);
  const [search, setSearch] = useState('');
  // Typing does not re-lay-out thousands of frames on every keystroke;
  // the match pass follows the deferred value. The input keeps spaces
  // (trimming on change made a space impossible to type).
  const deferredSearch = useDeferredValue(search);
  const term = deferredSearch.trim();
  const [ctxMenu, setCtxMenu] = useState<CtxMenu | null>(null);

  const dark = useUi((s) => s.theme) === 'dark';
  const zoomNames = useLive((s) => s.zoomNames);
  const setZoomNames = useLive((s) => s.setZoomNames);
  const selectedEvent = useLive((s) => s.selectedEvent);
  const baseline = useLive((s) => s.baseline);
  const diffEnabled = useLive((s) => s.diffEnabled);
  const selectedTid = useLive((s) => s.selectedTid);
  const timeWindow = useLive((s) => s.timeWindow);

  // A pending single-click must not fire after unmount
  useEffect(() => () => clearTimeout(clickTimer.current), []);

  // Resolve zoom by name path (node refs go stale on every refresh)
  const { renderRoot, walkedNodes } = useMemo(() => {
    if (!tree) return { renderRoot: null, walkedNodes: [] as FlameNode[] };
    if (!allowZoom || !zoomNames.length) return { renderRoot: tree, walkedNodes: [] };
    const z = walkZoomNames(tree, zoomNames);
    return { renderRoot: z.node ?? tree, walkedNodes: z.walkedNodes };
  }, [tree, zoomNames, allowZoom]);

  const diffActive = allowZoom && diffEnabled && !!baseline &&
    selectedTid === null && !timeWindow && !!baseline.perEvent[selectedEvent];

  const layout = useMemo(() => {
    if (!renderRoot || width < 10) return null;
    let baseNode: FlameNode | null = null;
    let baseTotal = 0;
    if (diffActive && baseline) {
      const b = baseline.perEvent[selectedEvent];
      if (b?.flamegraph) {
        baseTotal = b.function_summary
          ? b.function_summary.total_samples : (b.flamegraph as FlameNode).value;
        baseNode = walkBaseline(b.flamegraph as FlameNode,
                                allowZoom ? zoomNames : []);
      }
    }
    const total = allowZoom && zoomNames.length ? renderRoot.value : totalSamples;
    return layoutFlamegraph(renderRoot, {
      width, totalSamples: total, baseNode, baseTotal,
    });
  }, [renderRoot, width, totalSamples, diffActive, baseline, selectedEvent,
      zoomNames, allowZoom]);

  // E2E hook: expose current layout + zoom path
  useEffect(() => {
    (window as unknown as Record<string, unknown>).__perflens = {
      get rects() { return layout?.rects ?? []; },
      get zoomNames() { return useLive.getState().zoomNames; },
    };
  }, [layout]);

  const searchRe = useMemo(() => {
    if (!term) return null;
    try { return new RegExp(term, 'i'); } catch { return undefined; }
  }, [term]);

  const searchStats = useMemo(() => {
    if (!searchRe || !layout) return null;
    let count = 0;
    let samples = 0;
    for (const r of layout.rects) {
      if (searchRe.test(r.name)) { count++; samples += r.value; }
    }
    const rootValue = layout.rects.length ? layout.rects[0].value : 0;
    const pct = rootValue > 0 ? ((samples / rootValue) * 100).toFixed(1) : '0.0';
    return `${count} / ${layout.rects.length} frames (${pct}%)`;
  }, [searchRe, layout]);

  // Colors and labels per rect, computed once per layout rather than in
  // every render's JSX
  const drawn = useMemo(() => {
    if (!layout) return [];
    const height = layout.height;
    return layout.rects.map((r) => {
      const maxChars = Math.floor((r.w - 6) / CHAR_WIDTH);
      return {
        r,
        y: height - (r.depth + 1) * ROW_HEIGHT,
        color: (r.basePct !== null && r.basePct !== undefined)
          ? fgDiffColor(r.percent - r.basePct, dark)
          : fgModuleColor(r.name, r.module, r.inlined, dark),
        label: r.w > 36 && maxChars > 1
          ? (r.name.length > maxChars ? r.name.substring(0, maxChars - 1) + '…' : r.name)
          : null,
      };
    });
  }, [layout, dark]);

  const zoomToRect = (rect: FlameRect) => {
    if (!tree || !renderRoot) return;
    const rel = pathToNode(renderRoot, rect.node);
    if (rel === null) return;
    setZoomNames([...(allowZoom ? zoomNames : []), ...rel]);
  };

  const resetZoom = () => {
    setZoomNames([]);
    useLive.getState().selectTid(null);
  };

  const hover = (rect: FlameRect | null) => {
    const bar = infoBarRef.current;
    if (!bar) return;
    if (!rect) {
      bar.textContent = 'Hover over a frame to see details';
      bar.classList.remove('fg-info-active');
      return;
    }
    const inlinedTag = rect.inlined ? ' [inlined]' : '';
    let diffTag = '';
    if (rect.basePct !== null && rect.basePct !== undefined) {
      const dd = rect.percent - rect.basePct;
      diffTag = `  Δ ${dd >= 0 ? '+' : ''}${dd.toFixed(2)}pp vs baseline ` +
        `(was ${rect.basePct.toFixed(2)}%)`;
    }
    bar.textContent = `${rect.name}${inlinedTag}  (${rect.value} samples, ` +
      `${rect.percent.toFixed(2)}%)${diffTag}${rect.module ? '  — ' + rect.module : ''}`;
    bar.classList.add('fg-info-active');
  };

  // Context menu global dismiss
  useEffect(() => {
    if (!ctxMenu) return;
    const close = () => setCtxMenu(null);
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close(); };
    document.addEventListener('click', close);
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', close, true);
    return () => {
      document.removeEventListener('click', close);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', close, true);
    };
  }, [ctxMenu]);

  // Ctrl+F focuses flamegraph search while this tab is mounted
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
        e.preventDefault();
        document.getElementById('fg-search')?.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  const height = layout?.height ?? 0;
  const zoomed = allowZoom && zoomNames.length > 0 && walkedNodes.length > 0;
  const crumbs = zoomed ? walkedNodes : [];
  const rectAt = (target: EventTarget | null) => {
    const idx = rectIndex(target);
    return idx === null || !layout ? null : { idx, rect: layout.rects[idx] };
  };

  return (
    <>
      <div className="flamegraph-search">
        <input type="text" id="fg-search" placeholder="Search functions..."
               aria-label="Search functions (regular expression)"
               value={search} onChange={(e) => setSearch(e.target.value)} />
        <span id="fg-search-matches">
          {searchRe === undefined ? 'invalid regex' : (searchStats ?? '')}
        </span>
        <button id="fg-search-clear" className={search ? '' : 'hidden'}
                aria-label="Clear search" onClick={() => setSearch('')}>&#x2715;</button>
      </div>
      <div id="flamegraph-container" ref={containerRef} data-testid="flamegraph">
        {(!tree || !tree.children || tree.children.length === 0) ? (
          <p className="empty">No flame graph data yet.</p>
        ) : layout && (
          <>
            {zoomed && (
              <div className="flamegraph-controls">
                <button id="flamegraph-reset" className="fg-reset-btn" onClick={resetZoom}>
                  Reset Zoom
                </button>
                <div className="fg-breadcrumb">
                  root &rsaquo;{' '}
                  {crumbs.map((n, i) => (
                    <span key={i}>
                      {i > 0 && ' › '}
                      <span
                        className={'fg-crumb' + (i === crumbs.length - 1 ? ' fg-crumb-current' : '')}
                        data-crumb-idx={i < crumbs.length - 1 ? i : undefined}
                        role={i < crumbs.length - 1 ? 'button' : undefined}
                        tabIndex={i < crumbs.length - 1 ? 0 : undefined}
                        onClick={i < crumbs.length - 1
                          ? () => setZoomNames(zoomNames.slice(0, i + 1))
                          : undefined}
                        onKeyDown={i < crumbs.length - 1
                          ? (e) => { if (e.key === 'Enter') setZoomNames(zoomNames.slice(0, i + 1)); }
                          : undefined}>
                        {n.name}
                      </span>
                    </span>
                  ))}
                </div>
              </div>
            )}
            <svg width={width} height={height} xmlns="http://www.w3.org/2000/svg"
                 onMouseLeave={() => hover(null)}
                 onMouseMove={(e) => hover(rectAt(e.target)?.rect ?? null)}
                 onClick={(e) => {
                   const hit = rectAt(e.target);
                   if (!hit) return;
                   clearTimeout(clickTimer.current);
                   clickTimer.current = setTimeout(() => {
                     const r = hit.rect;
                     if (r.name !== 'root' && r.node.children?.length) zoomToRect(r);
                   }, 250);
                 }}
                 onDoubleClick={(e) => {
                   const hit = rectAt(e.target);
                   if (!hit) return;
                   clearTimeout(clickTimer.current);
                   if (hit.rect.name !== 'root') {
                     useLive.setState({ pendingHighlight: hit.rect.name });
                     onShowSource(hit.rect.name);
                   }
                 }}
                 onContextMenu={(e) => {
                   const hit = rectAt(e.target);
                   if (!hit) return;
                   e.preventDefault();
                   setCtxMenu({ x: e.clientX, y: e.clientY,
                                funcName: hit.rect.name, idx: hit.idx });
                 }}>
              {drawn.map(({ r, y, color, label }, idx) => {
                const cls = searchRe
                  ? (searchRe.test(r.name) ? 'fg-match' : 'fg-dim')
                  : undefined;
                return (
                  <g key={idx} data-idx={idx} className={cls}
                     data-inlined={r.inlined ? '1' : undefined}
                     style={{ cursor: 'pointer' }}>
                    <rect x={r.x} y={y} width={Math.max(r.w - 1, 1)}
                          height={ROW_HEIGHT - 1} fill={color} rx={2} />
                    {label && (
                      <text x={r.x + 3} y={y + 13} fontSize={FONT_SIZE}
                            fill="var(--fg-text)" pointerEvents="none">
                        {label}
                      </text>
                    )}
                  </g>
                );
              })}
            </svg>
            <div className="fg-info-bar" id="fg-info-bar" ref={infoBarRef}>
              Hover over a frame to see details
            </div>
          </>
        )}
      </div>
      {ctxMenu && layout && (
        <div id="fg-context-menu" className="fg-context-menu visible" role="menu"
             style={{
               // Clamped to the viewport: a right-click near an edge used
               // to open the menu half off screen
               left: Math.max(0, Math.min(ctxMenu.x, window.innerWidth - CTX_MENU_W)),
               top: Math.max(0, Math.min(ctxMenu.y, window.innerHeight - CTX_MENU_H)),
             }}
             onClick={(e) => e.stopPropagation()}>
          <div className="fg-ctx-item" data-action="source" role="menuitem" tabIndex={0}
               onClick={() => {
                 setCtxMenu(null);
                 if (ctxMenu.funcName !== 'root') {
                   useLive.setState({ pendingHighlight: ctxMenu.funcName });
                   onShowSource(ctxMenu.funcName);
                 }
               }}>
            View source
          </div>
          <div className="fg-ctx-item" data-action="zoom" role="menuitem" tabIndex={0}
               onClick={() => {
                 setCtxMenu(null);
                 const rect = layout.rects[ctxMenu.idx];
                 if (rect?.node.children?.length) zoomToRect(rect);
               }}>
            Zoom in
          </div>
          <div className="fg-ctx-item" data-action="copy" role="menuitem" tabIndex={0}
               onClick={() => {
                 setCtxMenu(null);
                 navigator.clipboard.writeText(ctxMenu.funcName).catch(() => {
                   useUi.getState().showError('Clipboard access was refused by the browser');
                 });
               }}>
            Copy function name
          </div>
        </div>
      )}
    </>
  );
}
