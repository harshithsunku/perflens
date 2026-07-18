// Global keyboard shortcuts. Inert while typing in form fields; tab
// shortcuts apply only in the profiling view. `?` works everywhere.

import { useUi, type Tab } from '../store/ui';

const TAB_KEYS: Record<string, Tab> = {
  '1': 'functions',
  '2': 'source',
  '3': 'flamegraph',
  '4': 'threads',
  '5': 'sessions',
};

function isTypingTarget(el: EventTarget | null): boolean {
  const t = el as HTMLElement | null;
  if (!t) return false;
  return t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
    t.tagName === 'SELECT' || t.isContentEditable;
}

/** Install the global keydown handler. Returns the uninstaller. */
export function installShortcuts(): () => void {
  const onKey = (e: KeyboardEvent) => {
    if (isTypingTarget(e.target)) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const ui = useUi.getState();

    if (e.key === '?') {
      ui.setHelp(!ui.helpOpen);
      e.preventDefault();
      return;
    }
    if (e.key === 'Escape' && ui.helpOpen) {
      ui.setHelp(false);
      return;
    }
    if (ui.helpOpen || ui.view !== 'profiling') return;

    const tab = TAB_KEYS[e.key];
    if (tab) {
      ui.switchTab(tab);
      e.preventDefault();
      return;
    }
    if (e.key === '/') {
      // Jump to the flamegraph search box
      ui.switchTab('flamegraph');
      e.preventDefault();
      requestAnimationFrame(() =>
        document.getElementById('fg-search')?.focus());
      return;
    }
    if (e.key === 't') ui.toggleTheme();
  };
  document.addEventListener('keydown', onKey);
  return () => document.removeEventListener('keydown', onKey);
}

/** The shortcut list shown in the help overlay (single source of truth). */
export const SHORTCUTS: { keys: string; action: string }[] = [
  { keys: '1 – 5', action: 'Switch tab (Functions / Source / Flame Graph / Threads / Sessions)' },
  { keys: '/', action: 'Search the flame graph' },
  { keys: 'Ctrl+F', action: 'Focus search on the Flame Graph tab' },
  { keys: 't', action: 'Toggle light / dark theme' },
  { keys: '?', action: 'Show or hide this help' },
  { keys: 'Esc', action: 'Close dialogs and popovers' },
];
