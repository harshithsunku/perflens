import { useCallback, useEffect, useState } from 'react';
import { api } from '../api/client';
import { formatNumber } from '../lib/format';

export interface BrowseRequest {
  startPath: string;
  mode: 'file' | 'dir';
  onSelect: (path: string) => void;
}

interface Entry { name: string; path: string; is_dir: boolean; size?: number | null }

export default function BrowseModal({ request, onClose }:
    { request: BrowseRequest; onClose: () => void }) {
  const [path, setPath] = useState(request.startPath);
  const [parent, setParent] = useState<string | null>(null);
  const [entries, setEntries] = useState<Entry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(
    request.mode === 'dir' ? request.startPath : null);

  const browseTo = useCallback((p: string) => {
    setEntries(null);
    setError(null);
    setSelected(request.mode === 'dir' ? p : null);
    api.browse(p).then((data) => {
      if (data.error) { setError(data.error); return; }
      setPath(data.path);
      setParent(data.parent && data.parent !== data.path ? data.parent : null);
      setEntries(data.entries as Entry[]);
      if (request.mode === 'dir') setSelected(data.path);
    }).catch((err) => setError(String(err)));
  }, [request.mode]);

  useEffect(() => { browseTo(request.startPath); }, [browseTo, request.startPath]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const confirm = () => {
    if (selected) request.onSelect(selected);
    onClose();
  };

  return (
    <div id="browse-modal" className="modal">
      <div className="modal-content">
        <div className="modal-header">
          <h3>Browse Files</h3>
          <button className="modal-close" onClick={onClose}>&times;</button>
        </div>
        <div id="browse-path" className="browse-path">{path}</div>
        <div id="browse-entries" className="browse-entries">
          {error && <p className="empty">{error}</p>}
          {!error && !entries && <div className="wiz-spinner">Loading...</div>}
          {entries && (
            <>
              {parent && (
                <div className="browse-entry" onClick={() => browseTo(parent)}>
                  <span className="be-icon">..</span><span className="be-name">..</span>
                </div>
              )}
              {entries.map((e) => (
                <div key={e.path}
                     className={'browse-entry' + (selected === e.path ? ' selected' : '')}
                     onClick={() => {
                       if (e.is_dir) browseTo(e.path);
                       else setSelected(e.path);
                     }}
                     onDoubleClick={() => {
                       if (e.is_dir) browseTo(e.path);
                       else { request.onSelect(e.path); onClose(); }
                     }}>
                  <span className="be-icon">{e.is_dir ? '📁' : '📄'}</span>
                  <span className="be-name">{e.name}</span>
                  {!e.is_dir && e.size != null && (
                    <span className="be-size">{formatNumber(e.size)}</span>
                  )}
                </div>
              ))}
            </>
          )}
        </div>
        <div className="modal-footer">
          <button id="browse-select" className="wiz-btn wiz-btn-primary" onClick={confirm}>
            Select
          </button>
          <button id="browse-cancel" className="wiz-btn" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}
