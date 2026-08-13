import { describe, expect, it } from 'vitest';

// The version rendered by the docs drawer is injected from the repo-root
// VERSION file (vite.config.ts). Before 0.8.0 it was a hand-typed string that
// drifted a whole minor ahead of the package it shipped in; this guards the
// injection itself, and tools/check_version.py guards agreement across files.
describe('__PERFLENS_VERSION__', () => {
  it('is injected at build time', () => {
    expect(typeof __PERFLENS_VERSION__).toBe('string');
  });

  it('is a bare semver with no leading v and no whitespace', () => {
    expect(__PERFLENS_VERSION__).toMatch(/^\d+\.\d+\.\d+$/);
  });
});
