import { describe, expect, it } from 'vitest';
import { BACKOFF_MAX_MS, BACKOFF_MIN_MS, nextBackoff } from './sse';

describe('reconnect backoff', () => {
  it('doubles from 3 s and caps at 30 s', () => {
    let d = BACKOFF_MIN_MS;
    const seen = [d];
    for (let i = 0; i < 6; i++) { d = nextBackoff(d); seen.push(d); }
    expect(seen).toEqual([3000, 6000, 12000, 24000, 30000, 30000, 30000]);
    expect(nextBackoff(BACKOFF_MAX_MS)).toBe(BACKOFF_MAX_MS);
  });
});
