import { describe, expect, it } from 'vitest';
import { formatBytes, formatCount, formatKB, formatNumber, formatRate,
         formatStatValue, formatUptime, hashCode } from './format';

describe('format helpers', () => {
  it('formatNumber scales K/M/B', () => {
    expect(formatNumber(null)).toBe('--');
    expect(formatNumber(999)).toBe('999');
    expect(formatNumber(1500)).toBe('1.5K');
    expect(formatNumber(2_500_000)).toBe('2.5M');
    expect(formatNumber(3_000_000_000)).toBe('3.0B');
    expect(formatNumber(1.234)).toBe('1.23');
  });

  it('formatStatValue special-cases ipc and branch_miss_rate', () => {
    expect(formatStatValue('ipc', 1.5)).toBe('1.50');
    expect(formatStatValue('branch_miss_rate', 2.34)).toBe('2.3%');
    expect(formatStatValue('cycles', 1_000_000)).toBe('1.0M');
  });

  it('formatKB / formatBytes / formatRate', () => {
    expect(formatKB(512)).toBe('512KB');
    expect(formatKB(2048)).toBe('2MB');
    expect(formatKB(2 * 1048576)).toBe('2.0GB');
    expect(formatBytes(500)).toBe('500B');
    expect(formatBytes(1536)).toBe('1.5KB');
    expect(formatRate(2048)).toBe('2.0 KB/s');
  });

  it('formatUptime / formatCount', () => {
    expect(formatUptime(90061)).toBe('1d 1h');
    expect(formatUptime(3700)).toBe('1h 1m');
    expect(formatUptime(120)).toBe('2m');
    expect(formatCount(1500)).toBe('1.5K');
  });

  it('hashCode matches the Python _hash_code for SVG export parity', () => {
    // Values must stay in sync with perflens.export._hash_code
    expect(hashCode('main')).toBe(Math.abs((() => {
      let h = 0;
      for (const c of 'main') { h = ((h << 5) - h) + c.charCodeAt(0); h |= 0; }
      return h;
    })()));
    expect(hashCode('a')).toBe(97);
  });
});
