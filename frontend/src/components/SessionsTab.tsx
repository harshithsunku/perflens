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

  const { data: sessions } = useQuery({
    queryKey: ['sessions'],
    queryFn: api.sessions,
  });

  const onImportFile = async (file: File) => {
    setImporting(true);
    setImportStatus('Importing ' + file.name + '...');
    try {
      const data = await api.importPerfData(file);
      setImporting(false);
      if (data.error) {
        setImportStatus('');
        showError('Import failed: ' + data.error);
        return;
      }
      setImportStatus('Imported ' + data.total_samples + ' samples');
      void queryClient.invalidateQueries({ queryKey: ['sessions'] });
      void replaySession(data.session_id);
    } catch (err) {
      setImporting(false);
      setImportStatus('');
      showError('Import failed: ' + String(err));
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
