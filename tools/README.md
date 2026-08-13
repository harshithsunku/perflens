# tools/

Author-side helpers. Not needed for running PerfLens itself.

| File | What it does |
|---|---|
| [`export_openapi.py`](export_openapi.py) | Dumps the FastAPI schema to `frontend/openapi.json`, which `npm --prefix frontend run typegen` turns into TypeScript types. CI diff-checks both, so run it after touching `api/models.py` or any route. |
| [`check_version.py`](check_version.py) | Asserts every file that records the version agrees with the repo-root `VERSION`, and that no hand-typed `vX.Y.Z` literal survives in `frontend/src/`. Runs in CI. |
| [`live-capture.sh`](live-capture.sh) | Stands up a real profiling session on this machine — builds `tests/matrixlab`, starts the server and agent, and collects until a sample floor is reached. No remote device needed. |
| [`encode-demo-gif.sh`](encode-demo-gif.sh) | Encodes captured frames into `docs/demo.gif` with `ffmpeg` and a 2-pass palette. |

## Docs screenshots and the demo GIF

The capture harness is `frontend/docs-shots/`, three Playwright projects
that reuse the E2E server bootstrap. No extra browser dependency.

```bash
# 1. Deterministic set — the full 12 shots from a committed fixture.
#    No perf, no agent, no device. This is what CI smoke-runs.
npm --prefix frontend run build
npm --prefix frontend run shots

# 2. Overwrite the data-heavy subset from a real profile.
tools/live-capture.sh &                      # server on :8089
PERFLENS_EXTERNAL_SERVER=1 PERFLENS_BASE_URL=http://127.0.0.1:8089 \
  npm --prefix frontend run shots:live       # 01,02,03,04,06,07,08,12 + GIF frames

# 3. Encode the GIF.
tools/encode-demo-gif.sh                     # -> docs/demo.gif
```

Step 2 exists because four things cannot come from a replay: per-thread
views read live server state, source annotation needs a locally built `-g`
binary, and a GIF of a fixed dataset would be 21 identical frames. The
function table and flame graph are re-shot live too — the fixture has no
resolvable binary, so replaying it renders the hot path as `[unknown]`.

**Always look at the output before committing it.** Sample counts, PIDs,
paths, hostnames and session ids all render into the image, and no
mechanical check covers them. Two of the bugs found this way — four
identical screenshots, and an empty flame graph — passed every assertion.

> Until 0.8.0 these were puppeteer scripts written against the pre-React
> vanilla-JS DOM. Every hook they used (`showView()`, `switchToTab()`,
> `.fn-source-link`) was deleted in the React rewrite, but `typeof` guards
> meant they kept *reporting success* while capturing the wrong page. They
> were removed rather than ported, and the CI smoke job exists so their
> replacement cannot fail the same silent way.

## Environment overrides

| Var | Default | Notes |
|---|---|---|
| `FRAMES_DIR` | `/tmp/perflens-gif-frames` | Frame staging dir for the GIF pipeline. |
| `OUT` | `docs/demo.gif` | Final GIF path. |
| `FPS` | `4` | Encoded framerate. |
| `WIDTH` | `900` | Scaled width (height auto). |
