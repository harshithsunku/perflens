# Contributing to PerfLens

Thanks for taking the time. PerfLens is small on purpose, so a short list covers most of what you need to know.

## Ground rules

- **Small, deliberate dependency set.** The server depends on fastapi,
  uvicorn, orjson, and zstandard — everything installs user-space via
  `uvx`/`pipx`/`pip`. New dependencies need an issue first. The UI stays
  plain HTML + vanilla JS + CSS (no bundler, no framework), and the agent
  stays a zero-dependency static C binary.
- **Defensive parsing.** `perf script` output drifts across kernel versions. Add tests under `tests/` if your change touches `parser.py`.
- **No proprietary names** in code, docs, comments, or commit messages. The repo is meant to stay generic.

## Development setup

```bash
git clone https://github.com/harshithsunku/perflens.git
cd perflens

# Install in a virtualenv with test deps, then run the server
uv venv .venv && uv pip install -p .venv/bin/python -e '.[dev]'
.venv/bin/perflens serve --source-dir tests --binary tests/sample_workload

# In another shell, build the test workload and start the agent against it
cd tests && make
(cd ../agent-c && make)
../agent-c/perflens-agent --server 127.0.0.1 --pid $(pgrep sample_workload)
```

Then browse `http://localhost:8080`.

## Tests

```bash
make -C agent-c                      # the protocol tests drive the real binary
.venv/bin/python -m pytest tests/    # full suite
npm ci && npm run e2e                # puppeteer browser E2E (self-contained)
```

For the C agent:

```bash
cd agent-c
make                              # native
make CROSS=aarch64-linux-gnu-     # cross-compile
```

## Reporting issues

Please include:

- Output of `uname -a` on the **target** device.
- `perf --version` on the target.
- The first 20 lines of the agent's stderr (capability probing tells us a lot).
- What you expected vs. what you saw.

For UI bugs, a screenshot and the browser console log help.

## Pull requests

1. Open an issue first for non-trivial changes. Cheap to discuss, expensive to redo.
2. One concern per PR. A bug fix doesn't need surrounding refactor.
3. Test end-to-end before pushing: server + agent + browser UI against a real Linux target. Type checks alone aren't enough for a perf tool.
4. Keep commit messages tight. Subject ≤ 70 chars, body wraps at 72, focus on the *why*.
5. Don't add `--no-verify`, `--no-gpg-sign`, or skip CI. If a hook fails, fix the underlying issue.

## Regenerating docs assets

Screenshots and the demo GIF on the docs site (`docs/screenshots/`, `docs/demo.gif`) are author-generated via puppeteer + ffmpeg. The scripts live in [`tools/`](tools/). See [`tools/README.md`](tools/README.md) for the full flow; the short version:

```bash
npm install
# (start server + agent + workload — see tools/README.md)
node tools/capture-screenshots.js
node tools/capture-demo-gif.js && tools/encode-demo-gif.sh
```

## Releasing

Releases are tag-driven: pushing `v<x.y.z>` triggers `.github/workflows/build.yml`, which builds the Python wheel + sdist, static C agent binaries for five architectures, and static addr2line/readelf tools bundles, then attaches them to a GitHub Release.

## License

By contributing you agree your work is offered under the project's [MIT license](LICENSE).
