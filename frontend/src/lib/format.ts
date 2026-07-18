// Formatting helpers ported from the vanilla UI (app.js).

export function formatNumber(n: number | null | undefined): string {
  if (n === undefined || n === null) return '--';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  if (typeof n === 'number' && !Number.isInteger(n)) return n.toFixed(2);
  return String(n);
}

export function formatStatValue(key: string, value: number): string {
  if (key === 'ipc') return value.toFixed(2);
  if (key === 'branch_miss_rate') return value.toFixed(1) + '%';
  if (typeof value === 'number' && !Number.isInteger(value)) return value.toFixed(1);
  return formatNumber(value);
}

export function formatKB(kb: number): string {
  if (kb >= 1048576) return (kb / 1048576).toFixed(1) + 'GB';
  if (kb >= 1024) return (kb / 1024).toFixed(0) + 'MB';
  return kb + 'KB';
}

export function formatBytes(b: number): string {
  if (b >= 1073741824) return (b / 1073741824).toFixed(1) + 'GB';
  if (b >= 1048576) return (b / 1048576).toFixed(1) + 'MB';
  if (b >= 1024) return (b / 1024).toFixed(1) + 'KB';
  return b + 'B';
}

export function formatRate(bps: number): string {
  if (bps >= 1048576) return (bps / 1048576).toFixed(1) + ' MB/s';
  if (bps >= 1024) return (bps / 1024).toFixed(1) + ' KB/s';
  return bps + ' B/s';
}

export function formatUptime(sec: number | null | undefined): string {
  if (sec == null) return '--';
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return d + 'd ' + h + 'h';
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm';
}

export function formatCount(n: number | null | undefined): string {
  if (n == null) return '--';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'G';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

export function fmtClock(t: number): string {
  const d = new Date(t * 1000);
  const pad = (n: number) => ('0' + n).slice(-2);
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

/** Simple string hash — must match the Python _hash_code (SVG export). */
export function hashCode(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) - hash) + str.charCodeAt(i);
    hash |= 0;
  }
  return Math.abs(hash);
}

export function debounce<A extends unknown[]>(
  fn: (...args: A) => void, delay: number,
): (...args: A) => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  return (...args: A) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}
