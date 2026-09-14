// UI chrome state: active view/tab, theme, transient banners.

import { create } from 'zustand';

export type View = 'landing' | 'wizard' | 'profiling';
export type Tab = 'functions' | 'source' | 'flamegraph' | 'threads' | 'sessions';
export type Theme = 'dark' | 'light';

export const TABS: readonly Tab[] = ['functions', 'source', 'flamegraph', 'threads', 'sessions'];

export function isTab(v: unknown): v is Tab {
  return typeof v === 'string' && (TABS as readonly string[]).includes(v);
}

interface UiState {
  view: View;
  activeTab: Tab;
  theme: Theme;
  error: string | null;
  /** A sticky error stays until dismissed (transport failures, a dead
   * server); a plain one auto-hides after a few seconds. */
  errorSticky: boolean;
  helpOpen: boolean;

  showView: (v: View) => void;
  switchTab: (t: Tab) => void;
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
  showError: (msg: string, opts?: { sticky?: boolean }) => void;
  hideError: () => void;
  setHelp: (open: boolean) => void;
}

const hasDom = typeof document !== 'undefined';

function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem('perflens-theme');
    if (saved === 'light' || saved === 'dark') return saved;
  } catch { /* ignore */ }
  if (!hasDom) return 'dark';
  return (document.documentElement.getAttribute('data-theme') as Theme) || 'dark';
}

export const ERROR_AUTOHIDE_MS = 5000;
let errorTimer: ReturnType<typeof setTimeout> | undefined;

export const useUi = create<UiState>((set, get) => ({
  view: 'landing',
  activeTab: 'functions',
  theme: initialTheme(),
  error: null,
  errorSticky: false,
  helpOpen: false,

  showView: (v) => set({ view: v }),
  switchTab: (t) => set({ activeTab: t }),

  setTheme: (t) => {
    if (hasDom) document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('perflens-theme', t); } catch { /* ignore */ }
    set({ theme: t });
  },
  toggleTheme: () => get().setTheme(get().theme === 'dark' ? 'light' : 'dark'),

  showError: (msg, opts) => {
    const sticky = !!opts?.sticky;
    set({ error: msg, errorSticky: sticky });
    clearTimeout(errorTimer);
    if (!sticky) {
      errorTimer = setTimeout(() => set({ error: null, errorSticky: false }),
                              ERROR_AUTOHIDE_MS);
    }
  },
  hideError: () => {
    clearTimeout(errorTimer);
    set({ error: null, errorSticky: false });
  },

  setHelp: (open) => set({ helpOpen: open }),
}));

/** Route a failed action to the banner with the server's own message. */
export function reportError(prefix: string, err: unknown, opts?: { sticky?: boolean }): void {
  const msg = err instanceof Error ? err.message : String(err);
  useUi.getState().showError(prefix + ': ' + msg, opts);
}

/** Read a CSS custom property off the document root (theme-aware colors
 * for SVG rendering). */
export function themeColor(varName: string): string {
  if (!hasDom) return '';
  return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
}
