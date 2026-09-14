import { useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { baselineFromSession, replaySession } from '../lib/replay';
import { reportError } from '../store/ui';

export default function SessionsTab() {
  const queryClient = useQueryClient();
  const fileInput = useRef<HTMLInputElement>(null);
  const [importStatus, setImportStatus] = useState('');
  const [importing, setImporting] = useState(false);

  const { data, error, isError } = useQuery({
    queryKey: ['sessions'],
    queryFn: api.sessions,
  });
  const sessions = data?.sessions;

  const onImportFile = async (file: File) => {
    setImporting(true);
    setImportStatus('Importing ' + file.name + '...');
    try {
      const result = await api.importPerfData(file);
      setImporting(false);
      setImportStatus('Imported ' + result.total_samples + ' samples');
      void queryClient.invalidateQueries({ queryKey: ['sessions'] });
      void replaySession(result.session_id);
    } catch (err) {
      setImporting(false);
      setImportStatus('');
      reportError('Import failed', err);
    }
  };

  const onDelete = async (sessionId: string) => {
    // Deleting removes the chunks from disk; nothing brings them back
    if (!window.confirm(`Delete session ${sessionId} from disk?`)) return;
    try {
      await api.deleteSession(sessionId);
      void queryClient.invalidateQueries({ queryKey: ['sessions'] });
    } catch (err) {
      reportError('Delete failed', err);
    }
  };

  return (
    <>
      <div id="import-bar">
        <input type="file" id="import-file" hidden ref={fileInput}
               onChange={(e) => {
                 const f = e.target.files?.[0];
                 e.target.value = '';
                 if (f) void onImportFile(f);
               }} />
        <button id="import-btn" className="replay-btn" disabled={importing}
                onClick={() => fileInput.current?.click()}>
          Import perf.data
        </button>
        <span id="import-status">{importStatus}</span>
      </div>
      <div id="sessions-list" data-testid="sessions-list">
        {isError ? (
          <p className="empty view-error">
            Could not list sessions: {error instanceof Error ? error.message : String(error)}
          </p>
        ) : !sessions ? (
          <p className="empty">Loading sessions...</p>
        ) : sessions.length === 0 ? (
          <p className="empty">No saved sessions.</p>
        ) : (
          <table id="sessions-table">
            <thead>
              <tr>
                <th>Session</th><th>Agent</th><th>Samples</th>
                <th>Events</th><th>Time</th><th></th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.session_id} data-live={s.live ? '1' : undefined}>
                  <td>
                    {s.session_id}
                    {s.live && (
                      <span className="session-tag session-live"
                            title="Still receiving, or the server stopped before it was finalized">
                        live
                      </span>
                    )}
                    {s.recovered && (
                      <span className="session-tag"
                            title="Metadata rebuilt from the chunks at startup">
                        recovered
                      </span>
                    )}
                  </td>
                  <td>{s.agent || '--'}</td>
                  <td>{s.total_samples}</td>
                  <td>{(s.event_types ?? []).join(', ')}</td>
                  <td>{s.timestamp || ''}</td>
                  <td>
                    <button className="replay-btn" data-session={s.session_id}
                            onClick={() => void replaySession(s.session_id)}>
                      Replay
                    </button>{' '}
                    <button className="replay-btn session-baseline-btn"
                            data-session={s.session_id}
                            title="Compare the live profile against this session"
                            onClick={() => void baselineFromSession(s.session_id)}>
                      Baseline
                    </button>{' '}
                    <button className="replay-btn session-delete-btn"
                            data-session={s.session_id}
                            title="Delete this session from disk"
                            onClick={() => void onDelete(s.session_id)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
