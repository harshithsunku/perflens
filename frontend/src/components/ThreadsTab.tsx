import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import type { ThreadViewResponse } from '../api/client';
import { CHAR_WIDTH, FONT_SIZE, ROW_HEIGHT, layoutFlamegraph } from '../lib/flamegraph/layout';
import type { FlameNode } from '../lib/flamegraph/types';
import { fgModuleColor, heatColor } from '../lib/flamegraph/colors';
import { useLive } from '../store/live';
import { useUi } from '../store/ui';
import { SourceLines, type SourceLine } from './SourceView';

/** Static (non-zooming) flamegraph for the thread drill-down. */
function ThreadFlamegraph({ tree, totalSamples }:
    { tree: FlameNode | null; totalSamples: number }) {
  const dark = useUi((s) => s.theme) === 'dark';
  const [hoverText, setHoverText] = useState<string | null>(null);

  const layout = useMemo(() => {
    if (!tree?.children?.length) return null;
    return layoutFlamegraph(tree, { width: 900, totalSamples });
  }, [tree, totalSamples]);

  if (!layout) return <p className="empty">No flame graph data</p>;
  const height = layout.height;

  return (
    <>
      <svg width={900} height={height} className="flamegraph-svg"
           style={{ display: 'block', margin: '0 auto' }}
           onMouseLeave={() => setHoverText(null)}>
        {layout.rects.filter((r) => r.w >= 0.5).map((r, idx) => {
          const y = height - (r.depth + 1) * ROW_HEIGHT;
          const maxChars = Math.floor((r.w - 6) / CHAR_WIDTH);
          const label = r.w > 36 && maxChars > 1
            ? (r.name.length > maxChars ? r.name.substring(0, maxChars - 1) + '…' : r.name)
            : null;
          const title = `${r.name} (${r.value} samples, ${r.percent.toFixed(1)}%)`;
          return (
            <g key={idx} onMouseMove={() => setHoverText(title)}>
              <rect x={r.x} y={y} width={Math.max(r.w - 1, 1)} height={ROW_HEIGHT - 1}
                    fill={fgModuleColor(r.name, r.module, r.inlined, dark)} rx={2} />
              {label && (
                <text x={r.x + 3} y={y + 13} fontSize={FONT_SIZE}
                      fill="var(--fg-text)" pointerEvents="none">{label}</text>
              )}
              <title>{title}</title>
            </g>
          );
        })}
      </svg>
      <div className={'fg-info-bar' + (hoverText ? ' fg-info-active' : '')}>
        {hoverText ?? 'Hover over a frame to see details'}
      </div>
    </>
  );
}

function ThreadDetail({ tid, comm, onBack }:
    { tid: number; comm: string; onBack: () => void }) {
  const selectedEvent = useLive((s) => s.selectedEvent);
  const chunkCount = useLive((s) => s.chunkCount);
  const [subTab, setSubTab] = useState<'t-functions' | 't-flamegraph' | 't-source'>('t-functions');
  const [sourceFile, setSourceFile] = useState<string | null>(null);
  const [sourceLines, setSourceLines] = useState<SourceLine[] | null>(null);
  const [sourceStatus, setSourceStatus] = useState<string | null>(null);

  const { data } = useQuery<ThreadViewResponse>({
    queryKey: ['thread-view', selectedEvent, tid, chunkCount],
    queryFn: () => api.threadView(selectedEvent, tid),
  });

  const loadSource = (filePath: string) => {
    setSourceFile(filePath);
    setSourceLines(null);
    setSourceStatus('Loading source...');
    api.source(filePath, selectedEvent, tid)
      .then((d) => {
        const lines = (d.lines ?? []) as unknown as SourceLine[];
        if (lines.length > 0) {
          setSourceLines(lines);
          setSourceStatus(null);
        } else {
          setSourceStatus('No source data for this thread in ' + filePath);
        }
      })
      .catch(() => setSourceStatus('Error loading source'));
  };

  const fs = data?.function_summary;

  return (
    <div id="thread-detail">
      <div className="thread-detail-header">
        <button id="thread-back-btn" className="thread-back-btn" onClick={onBack}>
          &larr; All Threads
        </button>
        <span id="thread-detail-title" className="thread-detail-title">
          {(comm || '(unnamed)') + ' (TID ' + tid + ')'}
        </span>
      </div>
      <div className="thread-detail-tabs">
        {([['t-functions', 'Functions'], ['t-flamegraph', 'Flame Graph'],
           ['t-source', 'Source']] as const).map(([id, label]) => (
          <button key={id} className={'thread-dtab' + (subTab === id ? ' active' : '')}
                  data-thread-tab={id} onClick={() => setSubTab(id)}>
            {label}
          </button>
        ))}
      </div>
      <div id="t-functions" className={'thread-detail-panel' + (subTab === 't-functions' ? ' active' : '')}>
        <div id="thread-fn-table">
          {!data ? <p className="empty loading">Loading...</p>
            : !fs?.functions?.length ? <p className="empty">No function data</p>
            : (
              <>
                <table className="threads-table">
                  <thead>
                    <tr>
                      <th>Function</th><th>Self %</th><th>Self</th>
                      <th>Total %</th><th>Total</th><th>Module</th>
                    </tr>
                  </thead>
                  <tbody>
                    {fs.functions.map((f) => {
                      const selfPct = f.self_percent ?? f.percent ?? 0;
                      const barW = Math.max(1, Math.min(80, selfPct * 0.8));
                      return (
                        <tr key={f.name + '|' + f.module} className="thread-row" data-func={f.name}>
                          <td><code>{f.name}</code></td>
                          <td>
                            <span className="thread-cpu-bar"
                                  style={{ width: barW + 'px',
                                           background: heatColor(selfPct / 100) }}></span>
                            {selfPct.toFixed(1)}%
                          </td>
                          <td>{f.self_samples ?? f.samples}</td>
                          <td>{(f.total_percent || 0).toFixed(1)}%</td>
                          <td>{f.total_samples || 0}</td>
                          <td style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>
                            {(f.module || '').split('/').pop()}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                <p style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 8 }}>
                  {fs.total_samples.toLocaleString()} samples in this thread
                </p>
              </>
            )}
        </div>
      </div>
      <div id="t-flamegraph" className={'thread-detail-panel' + (subTab === 't-flamegraph' ? ' active' : '')}>
        <div id="thread-fg-container">
          {subTab === 't-flamegraph' && (
            <ThreadFlamegraph tree={(data?.flamegraph ?? null) as FlameNode | null}
                              totalSamples={fs?.total_samples ?? 0} />
          )}
        </div>
      </div>
      <div id="t-source" className={'thread-detail-panel' + (subTab === 't-source' ? ' active' : '')}>
        <div id="thread-source-files">
          {(data?.source_files?.length ?? 0) > 0 && (
            <div className="thread-source-file-list">
              {data!.source_files!.map((f) => (
                <button key={f.path}
                        className={'thread-source-file-btn' + (f.path === sourceFile ? ' active' : '')}
                        data-path={f.path} onClick={() => loadSource(f.path)}>
                  {f.path.split('/').pop()} ({f.total_samples})
                </button>
              ))}
            </div>
          )}
        </div>
        <div id="thread-source-view">
          {sourceStatus && <p className="empty">{sourceStatus}</p>}
          {!sourceStatus && !sourceLines && (
            <p className="empty">Click a source file to view annotated source</p>
          )}
          {sourceLines && sourceFile && (
            <SourceLines filePath={sourceFile} lines={sourceLines}
                         headerSuffix={'thread ' + (comm || tid)} />
          )}
        </div>
      </div>
    </div>
  );
}

export default function ThreadsTab({ active }: { active: boolean }) {
  const selectedEvent = useLive((s) => s.selectedEvent);
  const chunkCount = useLive((s) => s.chunkCount);
  const isReplayMode = useLive((s) => s.isReplayMode);
  const threadLiveCpu = useLive((s) => s.threadLiveCpu);
  const [detail, setDetail] = useState<{ tid: number; comm: string } | null>(null);

  const { data } = useQuery({
    queryKey: ['thread-summary', selectedEvent, chunkCount, isReplayMode],
    queryFn: () => api.threadSummary(selectedEvent),
    enabled: active && detail === null,
  });

  if (detail !== null) {
    return <ThreadDetail tid={detail.tid} comm={detail.comm}
                         onBack={() => setDetail(null)} />;
  }

  const haveLive = Object.keys(threadLiveCpu).length > 0;

  return (
    <div id="threads-overview">
      {!data?.threads?.length ? (
        <p className="empty">{data ? 'No thread data yet' : 'Waiting for data...'}</p>
      ) : (
        <>
          <table className="threads-table">
            <thead>
              <tr>
                <th>Thread</th><th>TID</th><th>Samples</th><th>CPU %</th>
                {haveLive && <th>Live CPU</th>}
                <th>Top Function</th><th>Top Functions</th>
              </tr>
            </thead>
            <tbody>
              {data.threads.map((t) => {
                const live = threadLiveCpu[t.tid];
                return (
                  <tr key={t.tid} className="thread-row" data-tid={t.tid}
                      onClick={() => setDetail({ tid: t.tid, comm: t.comm })}>
                    <td><strong>{t.comm || '(unnamed)'}</strong></td>
                    <td>{t.tid}</td>
                    <td>{t.samples.toLocaleString()}</td>
                    <td>
                      <span className="thread-cpu-bar"
                            style={{ width: Math.max(2, Math.min(100, t.percent)) + 'px' }}></span>
                      {t.percent}%
                    </td>
                    {haveLive && (
                      <td className={'live-cpu' + (live && live.pct >= 80 ? ' live-hot' : '')}
                          data-live-tid={t.tid}>
                        {live ? live.pct.toFixed(1) + '%' + (live.state ? ' ' + live.state : '') : '--'}
                      </td>
                    )}
                    <td><code>{t.top_function || '-'}</code></td>
                    <td className="thread-top-funcs">
                      {(t.top_functions ?? []).map((f, i) => (
                        <span key={f.name}>
                          {i > 0 && ', '}
                          {f.name} <span style={{ opacity: 0.5 }}>{f.percent}%</span>
                        </span>
                      ))}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 8 }}>
            {data.total_samples.toLocaleString()} total samples across {data.threads.length} threads
          </p>
        </>
      )}
    </div>
  );
}
