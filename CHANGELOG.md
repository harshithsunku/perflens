# Changelog

All notable changes to PerfLens are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html); pre-1.0
releases may break APIs between minor versions when needed.

## [0.5.0] — 2026-05-17

**The "enterprise + docs" release.** Per-thread analysis lands as a full
tab. Cross-compilation toolchains and sysroots are first-class. The web
UI scales to large codebases and the Linux server binary is now fully
portable across glibc versions. A polished GitHub Pages documentation
site ships alongside the code.

### Added

- **Per-thread analysis tab** — a dedicated Threads view shows each tid
  with sample counts, CPU share, and top functions. Flame graphs,
  function tables, and source annotations can be filtered to a single
  thread.
- **Cross-compilation support** — `--toolchain-prefix` derives both
  `addr2line` and `readelf` from a single prefix. `--sysroot` resolves
  shared-library module paths and source files under a target tree,
  similar to `perf --symfs`. Settings can also be changed at runtime via
  the wizard's *Toolchain & Cross-compilation* section.
- **Multi-threaded test workload** — `test/matrixlab` is a new
  multi-threaded program for end-to-end testing of the per-thread flow.
- **Documentation site** — a hand-crafted static site under
  [`docs/`](docs/) deployed via GitHub Pages at
  https://harshithsunku.github.io/perflens/. Long-scroll landing,
  architecture deep-dive, full CLI/HTTP reference, dark+light themes,
  copy-buttons, scroll-spy TOC, live demo GIF.
- **CONTRIBUTING.md + CODE_OF_CONDUCT.md** — short, project-specific
  guides for the public repo.
- **`tools/` directory** — author-side puppeteer + ffmpeg scripts that
  regenerate the docs screenshots and demo GIF. Gives the puppeteer
  devDependency a visible purpose in-tree.
- **Interactive tabbed in-app docs** — the in-UI Docs panel was rebuilt
  with tabs (Getting Started, Configuration, UI Guide, Troubleshooting,
  Architecture) and now reflects every enterprise feature.

### Changed

- **Server hot paths optimized** — sample ring buffer is now a `deque`,
  flame graph and function table rebuilds run in a background worker,
  and a vaddr cache reduces `addr2line` calls. Noticeably snappier on
  large codebases and long sessions.
- **Wizard re-ordered** — *Process* before *Perf* so the pid-required
  error can't fire on the wrong step. The wizard also shows the agent's
  probed perf capabilities up front and pre-indexes the binary so the
  Source tab is ready when the first samples land.
- **Flame graph + function table scale further** — table virtualization
  was tuned for codebases with thousands of functions; flame graph
  rendering layout was rewritten for legibility at depth.
- **Thread source files load on demand** — clicking a thread in the
  Threads tab fetches its source-file list, instead of eagerly loading
  every thread's files at render time.
- **Linux server binary is portable** — the PyInstaller-frozen server
  for Linux x86_64 is now built inside a `manylinux_2_28` container, so
  it runs on any glibc ≥ 2.28 distro (RHEL 8, Ubuntu 18.04+, Debian 10+,
  etc.). A separate `linux-x86_64-legacy` build covers glibc ≥ 2.35
  hosts that want the more recent Python.
- **README quick-start examples** use a `<ver>` placeholder rather than
  a hardcoded version that drifts every release.

### Fixed

- **Threads tab missed some tids** — `perf script -F` field list now
  includes `tid` so the parser sees every thread, not just main.
- **C agent** — fixed the interactive command protocol, the
  `list_processes` sort order, and an unsafe signal handler path.
- **Source mapping on large codebases** — the source mapper now handles
  binaries with very long DIE chains and unusual compile-unit layouts
  without falling over.

### Infrastructure

- **CI builds the Linux server in `manylinux_2_28`** (with the
  `EXTERNALLY-MANAGED` marker handling for PEP 668), and adds an
  ubuntu-22.04 legacy build for older Python use cases.
- `RUNPATH` is stripped before PyInstaller, so the frozen binary plays
  nicely with static-linking experiments downstream.
- A build status, latest-release, stars, and docs-site badge now live
  at the top of the README.
- `node_modules/` is untracked and gitignored; `package.json` +
  `package-lock.json` remain in-tree so the docs regen pipeline is
  reproducible.

### Documentation

- Live demo GIF (`docs/demo.gif`) showing the function table updating
  in real time, then flame graph, then source view.
- Open Graph + Twitter card meta tags on all docs pages so link unfurls
  render correctly on Slack, Twitter/X, and Discord.

---

## [0.4.0] — 2026-04-13

First public-quality release with cross-compilation hooks, the wizard
flow, two-mode agent (`--server` / `--listen`), and the initial saved
sessions / replay UI. See [GitHub release notes](https://github.com/harshithsunku/perflens/releases/tag/v0.4.0)
for the original asset list.

## [0.2.1] — 2026-04-12

CI hardening, C agent build matrix expansion.

## [0.2.0] — 2026-04-11

Second iteration: C agent, capability probing, multi-arch tarballs.

## [0.1.0] — 2026-04-10

Initial public preview.
