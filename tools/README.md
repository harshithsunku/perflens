# tools/

Author-side helpers for regenerating the docs site assets. Not needed for
running PerfLens itself.

| File | What it does |
|---|---|
| [`capture-screenshots.js`](capture-screenshots.js) | Captures the 7 PNG screenshots in `docs/screenshots/` by driving a live PerfLens server with puppeteer. |
| [`capture-demo-gif.js`](capture-demo-gif.js) | Captures 32 frames for the live-demo GIF (function table → flame graph → source view). |
| [`encode-demo-gif.sh`](encode-demo-gif.sh) | Encodes those frames into `docs/demo.gif` with `ffmpeg` and a 2-pass palette. |

## Prereqs

```bash
# From the repo root
npm install              # pulls puppeteer (devDependency only)
apt-get install ffmpeg   # only needed for encode-demo-gif.sh
```

## Regenerate everything

Open four terminals (or run the first three in the background).

```bash
# 1) CPU-busy test program
./tests/sample_workload

# 2) PerfLens server with --binary so source mapping works
python3 server/perflens_server.py \
    --http-port 8089 --port 9899 \
    --binary tests/sample_workload --source-dir tests

# 3) agent in --server mode against the workload PID
PID=$(pgrep -f sample_workload | tail -1)
agent-c/perflens-agent --server 127.0.0.1 --port 9899 \
    --pid "$PID" --duration 4 --frequency 199

# 4) Tell the agent to start collecting
curl -X POST http://localhost:8089/api/agent/command \
     -H "Content-Type: application/json" \
     -d '{"cmd":"start","args":{"pid":'"$PID"',"frequency":199,"duration":4},"timeout":30}'
```

Wait until `curl http://localhost:8089/api/status` reports a few thousand
samples, then:

```bash
node tools/capture-screenshots.js          # -> docs/screenshots/*.png
node tools/capture-demo-gif.js             # -> /tmp/perflens-gif-frames/*.png
tools/encode-demo-gif.sh                   # -> docs/demo.gif
```

## Environment overrides

| Var | Default | Notes |
|---|---|---|
| `PERFLENS_URL` | `http://localhost:8089` | Where the server is reachable. |
| `OUT_DIR` | `docs/screenshots` | Where `capture-screenshots.js` writes PNGs. |
| `FRAMES_DIR` | `/tmp/perflens-gif-frames` | Frame staging dir for the GIF pipeline. |
| `OUT` | `docs/demo.gif` | Final GIF path. |
| `FPS` | `4` | Encoded framerate. |
| `WIDTH` | `900` | Scaled width (height auto). |
