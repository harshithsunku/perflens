# tools/

Author-side helpers. Not needed for running PerfLens itself.

| File | What it does |
|---|---|
| [`export_openapi.py`](export_openapi.py) | Dumps the FastAPI schema to `frontend/openapi.json`, which `npm --prefix frontend run typegen` turns into TypeScript types. CI diff-checks both, so run it after touching `api/models.py` or any route. |
| [`check_version.py`](check_version.py) | Asserts every file that records the version agrees with the repo-root `VERSION`, and that no hand-typed `vX.Y.Z` literal survives in `frontend/src/`. Runs in CI. |
| [`encode-demo-gif.sh`](encode-demo-gif.sh) | Encodes captured frames into `docs/demo.gif` with `ffmpeg` and a 2-pass palette. |

## Docs screenshots and the demo GIF

The capture harness lives in `frontend/docs-shots/` and runs on Playwright,
reusing the E2E server bootstrap. See that directory's notes for the full
flow. `encode-demo-gif.sh` is the last step of the GIF pipeline and is
driven from there.

> Until 0.8.0 these were puppeteer scripts written against the pre-React
> vanilla-JS DOM. Every hook they used (`showView()`, `switchToTab()`,
> `.fn-source-link`) was deleted in the React rewrite, but `typeof` guards
> meant they kept *reporting success* while capturing the wrong page. They
> were removed rather than ported.

## Environment overrides

| Var | Default | Notes |
|---|---|---|
| `FRAMES_DIR` | `/tmp/perflens-gif-frames` | Frame staging dir for the GIF pipeline. |
| `OUT` | `docs/demo.gif` | Final GIF path. |
| `FPS` | `4` | Encoded framerate. |
| `WIDTH` | `900` | Scaled width (height auto). |
