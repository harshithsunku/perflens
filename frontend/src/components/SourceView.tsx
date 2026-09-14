import { useEffect, useRef, useState } from 'react';
import { api } from '../api/client';
import { useLive } from '../store/live';

export interface SourceLine {
  line: number;
  samples: number;
  percent: number;
  source: string;
}

interface SourceFileRef {
  path: string;
  found: boolean;
  total_samples: number;
  functions?: string[];
}

function heatClass(percent: number): string {
  let heat = 0;
  if (percent > 0) heat = 1;
  if (percent > 2) heat = 2;
  if (percent > 5) heat = 3;
  if (percent > 15) heat = 4;
  if (percent > 30) heat = 5;
  return 'heat-' + heat;
}

/** Shared annotated-source renderer (main Source tab + thread drill-down). */
export function SourceLines({ filePath, lines, headerSuffix }:
    { filePath: string; lines: SourceLine[]; headerSuffix: string }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const scrolledFor = useRef<string | null>(null);
  const pendingHighlight = useLive((s) => s.pendingHighlight);

  const totalSamples = lines.reduce((sum, l) => sum + l.samples, 0);
  let hottestLine = 0;
  let maxSamples = 0;
  for (const l of lines) {
    if (l.samples > maxSamples) { maxSamples = l.samples; hottestLine = l.line; }
  }
  const maxLine = Math.max(2000, hottestLine + 100);
  const displayLines = lines.length > maxLine ? lines.slice(0, maxLine) : lines;
  const truncated = lines.length > displayLines.length;

  // Scroll to the hottest line when a file opens (or flash the highlighted
  // function). Not on every refresh: the live view re-fetches per chunk,
  // and the scroll position used to jump back to the hottest line each
  // time, out from under whatever the operator was reading.
  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    if (scrolledFor.current === filePath && !pendingHighlight) return;
    const raf = requestAnimationFrame(() => {
      scrolledFor.current = filePath;
      if (pendingHighlight) {
        useLive.setState({ pendingHighlight: null });
        for (const sl of root.querySelectorAll('.source-line')) {
          const code = sl.querySelector('.line-code');
          if (code?.textContent?.includes(pendingHighlight)) {
            sl.classList.add('source-flash');
            sl.scrollIntoView({ behavior: 'smooth', block: 'center' });
            sl.addEventListener('animationend',
              () => sl.classList.remove('source-flash'), { once: true });
            return;
          }
        }
      }
      if (hottestLine > 0 && hottestLine <= maxLine) {
        root.querySelector(`[data-line="${hottestLine}"]`)
          ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    });
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filePath, lines, pendingHighlight]);

  return (
    <>
      <div className="source-header">{filePath} ({totalSamples} samples, {headerSuffix})</div>
      <div className="source-scroll" ref={scrollRef}>
        {displayLines.map((l) => (
          <div key={l.line} className={'source-line ' + heatClass(l.percent)}
               id={'source-line-' + l.line} data-line={l.line}>
            <span className="line-no">{l.line}</span>
            <span className="line-samples">
              {l.samples > 0 ? `${l.samples} (${l.percent.toFixed(1)}%)` : ''}
            </span>
            <span className="line-code">{l.source}</span>
          </div>
        ))}
      </div>
      {truncated && (
        <div className="source-truncated">
          Showing first {displayLines.length} of {lines.length} lines.
          Hottest line: {hottestLine}
        </div>
      )}
    </>
  );
}

export default function SourceView() {
  const selectedEvent = useLive((s) => s.selectedEvent);
  const currentSourceFile = useLive((s) => s.currentSourceFile);
  const chunkCount = useLive((s) => s.chunkCount);
  const generation = useLive((s) => s.generation);
  const isReplayMode = useLive((s) => s.isReplayMode);
  const entry = useLive((s) => s.perEvent[s.selectedEvent]);
  // A stable reference while the file's embedded lines do not change
  // (replay embeds annotated source in the snapshot).
  const embeddedLines = useLive((s) => {
    const src = s.perEvent[s.selectedEvent]?.source as
      Record<string, SourceLine[]> | null | undefined;
    return s.currentSourceFile ? src?.[s.currentSourceFile] : undefined;
  });

  const [lines, setLines] = useState<SourceLine[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const loadedFile = useRef<string | null>(null);

  const sourceFiles = (entry?.source_files ?? []) as SourceFileRef[];

  // Keyed on the file, the event and the chunk: a new chunk re-fetches
  // the annotation while the previous lines stay on screen (no flash, no
  // scroll jump); a file switch clears them. The cancelled flag keeps a
  // slow response for file A from painting under file B's header.
  useEffect(() => {
    setLoadError(null);
    if (!currentSourceFile) {
      setLines(null);
      loadedFile.current = null;
      return;
    }
    if (embeddedLines) {
      setLines(embeddedLines);
      loadedFile.current = currentSourceFile;
      return;
    }
    if (isReplayMode) {
      setLines(null);
      setLoadError('No annotated source for ' + currentSourceFile + ' in this session');
      return;
    }
    const fileChanged = loadedFile.current !== currentSourceFile;
    if (fileChanged) {
      setLines(null);
      setLoading(true);
    }
    let cancelled = false;
    api.source(currentSourceFile, selectedEvent)
      .then((data) => {
        if (cancelled) return;
        setLoading(false);
        const got = (data.lines ?? []) as unknown as SourceLine[];
        if (got.length > 0) {
          setLines(got);
          loadedFile.current = currentSourceFile;
        } else {
          setLines(null);
          setLoadError('No source data for ' + currentSourceFile);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setLoading(false);
        if (fileChanged) setLines(null);
        setLoadError('Could not load source: '
          + (err instanceof Error ? err.message : String(err)));
      });
    return () => { cancelled = true; };
  }, [currentSourceFile, selectedEvent, chunkCount, generation, isReplayMode, embeddedLines]);

  return (
    <>
      <div id="source-file-picker">
        {sourceFiles.length > 1 && sourceFiles.map((f) => (
          <button key={f.path}
                  className={'file-picker-btn' + (f.path === currentSourceFile ? ' active' : '')}
                  data-path={f.path}
                  onClick={() => useLive.setState({ currentSourceFile: f.path })}>
            {f.path.split('/').pop()} ({f.total_samples}){f.found ? '' : ' (not found)'}
          </button>
        ))}
      </div>
      <div id="source-view">
        {!currentSourceFile && (
          <p className="empty">Click a function in the table to view source code.</p>
        )}
        {loading && !lines && <p className="empty loading">Loading source...</p>}
        {loadError && !lines && <p className="empty">{loadError}</p>}
        {lines && lines.length > 0 && currentSourceFile && (
          <SourceLines filePath={currentSourceFile} lines={lines}
                       headerSuffix={selectedEvent} />
        )}
      </div>
    </>
  );
}
