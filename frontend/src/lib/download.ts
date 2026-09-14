// Export downloads. `window.open(url)` on a 400/404 put the raw error
// JSON in a new tab; fetching first lets the error reach the banner, and
// a blob download also sidesteps popup blockers.

import { useUi } from '../store/ui';

function filenameFrom(disposition: string | null, fallback: string): string {
  const m = disposition && /filename="?([^";]+)"?/.exec(disposition);
  return m ? m[1] : fallback;
}

export async function downloadExport(url: string, fallbackName: string): Promise<void> {
  try {
    const r = await fetch(url);
    if (!r.ok) {
      let message = `HTTP ${r.status}`;
      if (r.headers.get('content-type')?.includes('json')) {
        const body = await r.json().catch(() => null) as
          { error?: { message?: string } } | null;
        if (body?.error?.message) message = body.error.message;
      }
      useUi.getState().showError('Export failed: ' + message);
      return;
    }
    const blob = await r.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filenameFrom(r.headers.get('content-disposition'), fallbackName);
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10_000);
  } catch (err) {
    useUi.getState().showError('Export failed: '
      + (err instanceof Error ? err.message : String(err)));
  }
}
