// UI chrome state: active view/tab, theme, transient banners.

import { create } from 'zustand';

export type View = 'landing' | 'wizard' | 'profiling';
export type Tab = 'functions' | 'source' | 'flamegraph' | 'threads' | 'sessions';
export type Theme = 'dark' | 'light';

interface UiState {
  view: View;
  activeTab: Tab;
  theme: Theme;
  error: string | null;
  helpOpen: boolean;

  showView: (v: View) => void;
  switchTab: (t: Tab) => void;
  setTheme: (t: Theme) => void;
  toggleTheme: () => void;
  showError: (msg: string) => void;
  hideError: () => void;
  setHelp: (open: boolean) => void;
}

function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem('perflens-theme');
    if (saved === 'light' || saved === 'dark') return saved;
  } catch { /* ignore */ }
  return (document.documentElement.getAttribute('data-theme') as Theme) || 'dark';
}

let errorTimer: ReturnType<typeof setTimeout> | undefined;

export const useUi = create<UiState>((set, get) => ({
  view: 'landing',
  activeTab: 'functions',
  theme: initialTheme(),
  error: null,
  helpOpen: false,

  showView: (v) => set({ view: v }),
  switchTab: (t) => set({ activeTab: t }),

  setTheme: (t) => {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('perflens-theme', t); } catch { /* ignore */ }
    set({ theme: t });
  },
  toggleTheme: () => get().setTheme(get().theme === 'dark' ? 'light' : 'dark'),

  showError: (msg) => {
    set({ error: msg });
    clearTimeout(errorTimer);
    errorTimer = setTimeout(() => set({ error: null }), 5000);
  },
  hideError: () => {
    clearTimeout(errorTimer);
    set({ error: null });
  },

  setHelp: (open) => set({ helpOpen: open }),
}));

/** Read a CSS custom property off the document root (theme-aware colors
 * for SVG rendering). */
export function themeColor(varName: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
}
