import { describe, expect, it } from 'vitest';
import { defaultEvent, eventBase, pickEvent, resolveEvent, versionKey } from './events';

describe('eventBase', () => {
  it('strips PMU prefixes and modifiers', () => {
    expect(eventBase('cycles')).toBe('cycles');
    expect(eventBase('cycles:u')).toBe('cycles');
    expect(eventBase('cpu_core/cycles/')).toBe('cycles');
    expect(eventBase('cpu_atom/branch-instructions/P')).toBe('branch-instructions');
  });
});

describe('resolveEvent', () => {
  const hybrid = ['cpu_atom/branch-instructions/', 'cpu_atom/cycles/',
                  'cpu_core/branch-instructions/', 'cpu_core/cycles/'];
  it('takes an exact hit first', () => {
    expect(resolveEvent('cpu_atom/cycles/', hybrid)).toBe('cpu_atom/cycles/');
  });
  it('prefers the performance cores for a base name', () => {
    expect(resolveEvent('cycles', hybrid)).toBe('cpu_core/cycles/');
  });
  it('returns null when nothing matches', () => {
    expect(resolveEvent('instructions', hybrid)).toBeNull();
    expect(resolveEvent('cycles', [])).toBeNull();
  });
});

describe('defaultEvent / pickEvent', () => {
  it('is cycles, then the software clocks, then whatever is first', () => {
    expect(defaultEvent(['instructions', 'cycles'])).toBe('cycles');
    expect(defaultEvent(['page-faults', 'cpu-clock'])).toBe('cpu-clock');
    expect(defaultEvent(['context-switches', 'task-clock'])).toBe('task-clock');
    expect(defaultEvent(['page-faults'])).toBe('page-faults');
    expect(defaultEvent([])).toBeNull();
  });
  it('keeps a current event that is still available, else defaults', () => {
    expect(pickEvent('instructions', ['cycles', 'instructions'])).toBe('instructions');
    expect(pickEvent('cycles', ['cpu_atom/cycles/', 'cpu_core/cycles/'])).toBe('cpu_core/cycles/');
    expect(pickEvent('cache-misses', ['cycles', 'instructions'])).toBe('cycles');
    expect(pickEvent('cycles', [])).toBe('cycles');
  });
});

describe('versionKey', () => {
  it('orders by generation first, then chunk_count', () => {
    expect(versionKey({ generation: 1, chunk_count: 5 }))
      .toBeLessThan(versionKey({ generation: 2, chunk_count: 0 }));
    expect(versionKey({ generation: 1, chunk_count: 5 }))
      .toBeGreaterThan(versionKey({ generation: 1, chunk_count: 4 }));
    // Pre-0.12.0 servers send no generation: it reads as 1
    expect(versionKey({ chunk_count: 3 })).toBe(versionKey({ generation: 1, chunk_count: 3 }));
  });
});
