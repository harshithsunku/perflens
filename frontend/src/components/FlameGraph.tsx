import { useEffect, useMemo, useRef, useState } from 'react';
import { CHAR_WIDTH, FONT_SIZE, ROW_HEIGHT, layoutFlamegraph } from '../lib/flamegraph/layout';
import type { FlameNode, FlameRect } from '../lib/flamegraph/types';
import { fgDiffColor, fgModuleColor } from '../lib/flamegraph/colors';
import { pathToNode, walkBaseline, walkZoomNames } from '../lib/flamegraph/zoom';
import { debounce } from '../lib/format';
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

export default function FlameGraph({ tree, totalSamples, allowZoom, onShowSource }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const infoBarRef = useRef<HTMLDivElement>(null);
  const clickTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const [width, setWidth] = useState(0);
  const [search, setSearch] = useState('');
  const [ctxMenu, setCtxMenu] = useState<CtxMenu | null>(null);

  const dark = useUi((s) => s.theme) === 'dark';
  const zoomNames = useLive((s) => s.zoomNames);
  const setZoomNames = useLive((s) => s.setZoomNames);
  const selectedEvent = useLive((s) => s.selectedEvent);
  const baseline = useLive((s) => s.baseline);
  const diffEnabled = useLive((s) => s.diffEnabled);
  const selectedTid = useLive((s) => s.selectedTid);
  const timeWindow = useLive((s) => s.timeWindow);

  // Track the container width (also handles the hidden→visible tab flip:
  // ResizeObserver fires when the container gets its real size)
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const measure = () => setWidth(el.clientWidth - 32);
    measure();
    const ro = new ResizeObserver(debounce(measure, 150));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

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
    if (!search) return null;
    try { return new RegExp(search, 'i'); } catch { return undefined; }
  }, [search]);

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

  return (
    <>
      <div className="flamegraph-search">
        <input type="text" id="fg-search" placeholder="Search functions..."
               value={search} onChange={(e) => setSearch(e.target.value.trim())} />
        <span id="fg-search-matches">
          {searchRe === undefined ? 'invalid regex' : (searchStats ?? '')}
        </span>
        <button id="fg-search-clear" className={search ? '' : 'hidden'}
                onClick={() => setSearch('')}>&#x2715;</button>
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
                        onClick={i < crumbs.length - 1
                          ? () => setZoomNames(zoomNames.slice(0, i + 1))
                          : undefined}>
                        {n.name}
                      </span>
                    </span>
                  ))}
                </div>
              </div>
            )}
            <svg width={width} height={height} xmlns="http://www.w3.org/2000/svg"
                 onMouseLeave={() => hover(null)}>
              {layout.rects.map((r, idx) => {
                const color = (r.basePct !== null && r.basePct !== undefined)
                  ? fgDiffColor(r.percent - r.basePct, dark)
                  : fgModuleColor(r.name, r.module, r.inlined, dark);
                const y = height - (r.depth + 1) * ROW_HEIGHT;
                const maxChars = Math.floor((r.w - 6) / CHAR_WIDTH);
                const label = r.w > 36 && maxChars > 1
                  ? (r.name.length > maxChars ? r.name.substring(0, maxChars - 1) + '…' : r.name)
                  : null;
                const cls = searchRe
                  ? (searchRe.test(r.name) ? 'fg-match' : 'fg-dim')
                  : undefined;
                return (
                  <g key={idx} data-idx={idx} className={cls}
                     data-inlined={r.inlined ? '1' : undefined}
                     style={{ cursor: 'pointer' }}
                     onMouseMove={() => hover(r)}
                     onClick={() => {
                       clearTimeout(clickTimer.current);
                       clickTimer.current = setTimeout(() => {
                         if (r.name !== 'root' && r.node.children?.length) zoomToRect(r);
                       }, 250);
                     }}
                     onDoubleClick={() => {
                       clearTimeout(clickTimer.current);
                       if (r.name !== 'root') {
                         useLive.setState({ pendingHighlight: r.name });
                         onShowSource(r.name);
                       }
                     }}
                     onContextMenu={(e) => {
                       e.preventDefault();
                       setCtxMenu({ x: e.clientX, y: e.clientY, funcName: r.name, idx });
                     }}>
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
        <div id="fg-context-menu" className="fg-context-menu visible"
             style={{ left: ctxMenu.x, top: ctxMenu.y }}
             onClick={(e) => e.stopPropagation()}>
          <div className="fg-ctx-item" data-action="source"
               onClick={() => {
                 setCtxMenu(null);
                 if (ctxMenu.funcName !== 'root') {
                   useLive.setState({ pendingHighlight: ctxMenu.funcName });
                   onShowSource(ctxMenu.funcName);
                 }
               }}>
            View source
          </div>
          <div className="fg-ctx-item" data-action="zoom"
               onClick={() => {
                 setCtxMenu(null);
                 const rect = layout.rects[ctxMenu.idx];
                 if (rect?.node.children?.length) zoomToRect(rect);
               }}>
            Zoom in
          </div>
          <div className="fg-ctx-item" data-action="copy"
               onClick={() => {
                 setCtxMenu(null);
                 navigator.clipboard.writeText(ctxMenu.funcName).catch(() => {});
               }}>
            Copy function name
          </div>
        </div>
      )}
    </>
  );
}
