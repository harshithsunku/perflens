import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ERROR_AUTOHIDE_MS, isTab, reportError, useUi } from './ui';

describe('error banner', () => {
  beforeEach(() => { vi.useFakeTimers(); useUi.setState({ error: null, errorSticky: false }); });
  afterEach(() => vi.useRealTimers());

  it('auto-hides a plain error', () => {
    useUi.getState().showError('oops');
    expect(useUi.getState().error).toBe('oops');
    vi.advanceTimersByTime(ERROR_AUTOHIDE_MS + 1);
    expect(useUi.getState().error).toBeNull();
  });

  it('keeps a sticky error until dismissed', () => {
    useUi.getState().showError('server gone', { sticky: true });
    vi.advanceTimersByTime(ERROR_AUTOHIDE_MS * 10);
    expect(useUi.getState().error).toBe('server gone');
    expect(useUi.getState().errorSticky).toBe(true);
    useUi.getState().hideError();
    expect(useUi.getState().error).toBeNull();
  });

  it('a later plain error replaces a sticky one and auto-hides', () => {
    useUi.getState().showError('sticky', { sticky: true });
    useUi.getState().showError('plain');
    expect(useUi.getState().errorSticky).toBe(false);
    vi.advanceTimersByTime(ERROR_AUTOHIDE_MS + 1);
    expect(useUi.getState().error).toBeNull();
  });

  it('reportError carries the server message', () => {
    reportError('Pause failed', new Error('agent refused'));
    expect(useUi.getState().error).toBe('Pause failed: agent refused');
  });
});

describe('isTab', () => {
  it('accepts only the five tabs', () => {
    expect(isTab('flamegraph')).toBe(true);
    expect(isTab('bogus')).toBe(false);
    expect(isTab(3)).toBe(false);
  });
});
