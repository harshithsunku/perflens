// Flamegraph color functions — ported from app.js fgModuleColor /
// fgDiffColor / heatColor. Pure (theme passed in), so vitest-testable.

import { hashCode } from '../format';

/** Module-based color: kernel=warm, user binary=green, libraries=blue,
 * unknown=grey. Inlined frames get lower saturation. */
export function fgModuleColor(name: string, module: string,
                              inlined: boolean, dark: boolean): string {
  let lightBase = dark ? 42 : 50;
  let lightVar = hashCode(name + 'y') % 12;
  let satBase = inlined ? 40 : 70;
  const satVar = hashCode(name + 'x') % 15;
  let hue: number;

  if (!module || module === '[unknown]') {
    hue = 0;
    satBase = 0;
    lightBase = dark ? 35 : 60;
    lightVar = hashCode(name + 'y') % 8;
  } else if (module === '[kernel.kallsyms]' || module.indexOf('/vmlinux') >= 0 ||
             module.indexOf('[kernel') >= 0) {
    hue = 20 + (hashCode(name) % 25);
  } else if (module.indexOf('.so') >= 0 || module.indexOf('ld-linux') >= 0 ||
             module.indexOf('libc') >= 0 || module.indexOf('libm') >= 0 ||
             module.indexOf('libpthread') >= 0 || module.indexOf('libstdc++') >= 0) {
    hue = 190 + (hashCode(name) % 40);
  } else {
    hue = 80 + (hashCode(name) % 40);
  }

  return `hsl(${hue}, ${Math.min(satBase + satVar, 100)}%, ${lightBase + lightVar}%)`;
}

/** Diverging diff color: red = grew vs baseline, blue = shrank, grey =
 * unchanged. 8pp change = full intensity. */
export function fgDiffColor(deltaPP: number, dark: boolean): string {
  if (Math.abs(deltaPP) < 0.1) return dark ? 'hsl(220, 6%, 30%)' : 'hsl(220, 8%, 82%)';
  const mag = Math.min(Math.abs(deltaPP) / 8, 1);
  const hue = deltaPP > 0 ? 4 : 214;
  const sat = 55 + Math.round(mag * 35);
  const light = dark ? 50 - Math.round(mag * 16) : 82 - Math.round(mag * 30);
  return `hsl(${hue}, ${sat}%, ${light}%)`;
}

/** 0 = green, 1 = red (thread table bars). */
export function heatColor(ratio: number): string {
  if (ratio < 0.33) return '#3fb950';
  if (ratio < 0.66) return '#d29922';
  return '#f85149';
}
