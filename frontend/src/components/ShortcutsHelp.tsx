// Keyboard-shortcut help overlay, toggled with `?`.

import { SHORTCUTS } from '../lib/shortcuts';
import { useUi } from '../store/ui';

export default function ShortcutsHelp() {
  const open = useUi((s) => s.helpOpen);
  const setHelp = useUi((s) => s.setHelp);
  if (!open) return null;

  return (
    <div className="help-overlay" data-testid="shortcuts-help"
         role="dialog" aria-modal="true" aria-label="Keyboard shortcuts"
         onClick={() => setHelp(false)}>
      <div className="help-card" onClick={(e) => e.stopPropagation()}>
        <div className="help-title">
          Keyboard shortcuts
          <button className="help-close" aria-label="Close help"
                  onClick={() => setHelp(false)}>&times;</button>
        </div>
        <table className="help-table">
          <tbody>
            {SHORTCUTS.map((s) => (
              <tr key={s.keys}>
                <td className="help-keys"><kbd>{s.keys}</kbd></td>
                <td>{s.action}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="help-hint">
          Shortcuts are inactive while typing in a text field.
        </div>
      </div>
    </div>
  );
}
