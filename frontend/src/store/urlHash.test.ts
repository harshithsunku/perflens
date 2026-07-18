import { describe, expect, it } from 'vitest';
import { buildHash, parseHash } from './urlHash';

describe('urlHash codec', () => {
  it('round-trips a full state', () => {
    const s = {
      tab: 'flamegraph' as const,
      event: 'instructions',
      tid: 1234,
      zoom: ['main', 'do_work()', 'x;y'],
      session: '20260101_010101_dev',
    };
    expect(parseHash(buildHash(s))).toEqual(s);
  });

  it('omits defaults', () => {
    expect(buildHash({ tab: 'functions', event: 'cycles' })).toBe('');
  });

  it('parses empty and malformed hashes safely', () => {
    expect(parseHash('')).toEqual({});
    expect(parseHash('#')).toEqual({});
    expect(parseHash('#garbage')).toEqual({});
    expect(parseHash('#tid=notanumber')).toEqual({});
  });

  it('encodes special characters in zoom names', () => {
    const h = buildHash({ zoom: ['a&b', 'c=d'] });
    expect(parseHash(h).zoom).toEqual(['a&b', 'c=d']);
  });
});
