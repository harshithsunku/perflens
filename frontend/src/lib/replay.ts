// Session replay + baseline-from-session actions, shared by the Sessions
// tab, URL-hash deep links, and the import flow.

import { api } from '../api/client';
import type { PerEventEntry } from '../api/client';
import { useLive } from '../store/live';
import { useUi } from '../store/ui';

export async function replaySession(sessionId: string, keepTab = false): Promise<void> {
  try {
    const data = await api.session(sessionId);
    useLive.getState().loadReplay(sessionId, data);
    useUi.getState().showView('profiling');
    if (!keepTab) useUi.getState().switchTab('functions');
  } catch (err) {
    useUi.getState().showError('Replay error: ' + errMsg(err));
  }
}

export async function baselineFromSession(sessionId: string): Promise<void> {
  try {
    const data = await api.session(sessionId);
    useLive.getState().setBaseline(
      data.per_event as Record<string, PerEventEntry>, sessionId);
    useUi.getState().switchTab('functions');
  } catch (err) {
    useUi.getState().showError('Baseline error: ' + errMsg(err));
  }
}

function errMsg(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
