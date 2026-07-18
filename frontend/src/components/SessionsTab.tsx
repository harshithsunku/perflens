import { useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { baselineFromSession, replaySession } from '../lib/replay';
import { useUi } from '../store/ui';

export default function SessionsTab() {
  const queryClient = useQueryClient();
  const showError = useUi((s) => s.showError);
  const fileInput = useRef<HTMLInputElement>(null);
  const [importStatus, setImportStatus] = useState('');
  const [importing, setImporting] = useState(false);

  const { data } = useQuery({
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
      showError('Import failed: '
        + (err instanceof Error ? err.message : String(err)));
    }
  };

  const onDelete = async (sessionId: string) => {
    try {
      await api.deleteSession(sessionId);
      void queryClient.invalidateQueries({ queryKey: ['sessions'] });
    } catch (err) {
      showError('Delete failed: '
        + (err instanceof Error ? err.message : String(err)));
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
        {!sessions ? (
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
                <tr key={s.session_id}>
                  <td>{s.session_id}</td>
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
