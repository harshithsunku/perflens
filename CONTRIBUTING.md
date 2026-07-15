# Contributing to PerfLens

Thanks for taking the time. PerfLens is small on purpose, so a short list covers most of what you need to know.

## Ground rules

- **Stdlib only.** No `pip install` on the server, no `npm install` on the UI. If a dependency feels needed, open an issue first so we can talk about it.
- **No framework.** The UI is plain HTML + vanilla JS + CSS. The server is plain `ThreadingHTTPServer`. The agent is plain `perf` + sockets.
- **Defensive parsing.** `perf script` output drifts across kernel versions. Add tests under `test/` if your change touches `parser.py`.
- **No proprietary names** in code, docs, comments, or commit messages. The repo is meant to stay generic.

## Development setup

```bash
git clone https://github.com/harshithsunku/perflens.git
cd perflens

# Run the server (stdlib only — no virtualenv needed)
python3 server/perflens_server.py --source-dir test --binary test/sample_workload

# In another shell, build the test workload and start the agent against it
cd test && make
(cd ../agent-c && make)
../agent-c/perflens-agent --server 127.0.0.1 --pid $(pgrep sample_workload)
```

Then browse `http://localhost:8080`.

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

Releases are tag-driven: pushing `v<x.y.z>` triggers `.github/workflows/build.yml`, which builds server tarballs for Linux x86_64, macOS arm64, and Windows x86_64, plus static C agent binaries for five architectures, then attaches them to a GitHub Release.

## License

By contributing you agree your work is offered under the project's [MIT license](LICENSE).
