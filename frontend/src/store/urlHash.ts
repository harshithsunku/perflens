// URL-hash deep links: #tab=flamegraph&event=cycles&tid=123&zoom=a;b&session=<id>
// Shareable and refresh-surviving. Pure codec + store sync glue.

import type { Tab } from './ui';

export interface HashState {
  tab?: Tab;
  event?: string;
  tid?: number;
  zoom?: string[];
  session?: string;
}

export function parseHash(hash: string): HashState {
  const out: HashState = {};
  const h = hash.replace(/^#/, '');
  if (!h) return out;
  for (const kv of h.split('&')) {
    const i = kv.indexOf('=');
    if (i <= 0) continue;
    const key = kv.slice(0, i);
    const val = kv.slice(i + 1);
    if (key === 'tab') out.tab = val as Tab;
    else if (key === 'event') out.event = decodeURIComponent(val);
    else if (key === 'tid') {
      const t = parseInt(val, 10);
      if (!isNaN(t)) out.tid = t;
    } else if (key === 'zoom') out.zoom = val.split(';').map(decodeURIComponent);
    else if (key === 'session') out.session = decodeURIComponent(val);
  }
  return out;
}

export function buildHash(s: HashState): string {
  const parts: string[] = [];
  if (s.tab && s.tab !== 'functions') parts.push('tab=' + s.tab);
  if (s.event && s.event !== 'cycles') parts.push('event=' + encodeURIComponent(s.event));
  if (s.tid != null) parts.push('tid=' + s.tid);
  if (s.zoom && s.zoom.length) parts.push('zoom=' + s.zoom.map(encodeURIComponent).join(';'));
  if (s.session) parts.push('session=' + encodeURIComponent(s.session));
  return parts.length ? '#' + parts.join('&') : '';
}

/** Write the hash without adding history entries. */
export function replaceHash(s: HashState): void {
  const hash = buildHash(s);
  if (hash !== location.hash) {
    history.replaceState(null, '', hash || location.pathname + location.search);
  }
}
