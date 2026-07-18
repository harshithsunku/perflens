// Playwright webServer command: materialize the device-captured fixture
// session into a temp PERFLENS_HOME, then run `perflens serve` on the
// E2E ports. Playwright kills the whole process group on teardown.

import { execFileSync, spawn } from 'node:child_process';
import { createGunzip } from 'node:zlib';
import { createReadStream, createWriteStream, cpSync, existsSync, mkdirSync,
         mkdtempSync, readdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { pipeline } from 'node:stream/promises';
import { fileURLToPath } from 'node:url';

const HTTP_PORT = process.env.PERFLENS_E2E_HTTP_PORT || '18477';
const TCP_PORT = process.env.PERFLENS_E2E_TCP_PORT || '19477';
const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const FIXTURE = 'session-x86-baseline';

function pickPython() {
  if (process.env.PERFLENS_PYTHON) return process.env.PERFLENS_PYTHON;
  const venv = join(REPO, '.venv', 'bin', 'python');
  if (existsSync(venv)) return venv;
  return 'python3';
}

async function materializeFixture(sessionsDir) {
  const src = join(REPO, 'tests', 'fixtures', FIXTURE);
  const dest = join(sessionsDir, FIXTURE);
  mkdirSync(dest, { recursive: true });
  let i = 0;
  for (const fname of readdirSync(src).sort()) {
    if (fname.startsWith('chunk_') && fname.endsWith('.txt.gz')) {
      const out = join(dest, `chunk_${String(i).padStart(5, '0')}.txt`);
      await pipeline(createReadStream(join(src, fname)), createGunzip(),
                     createWriteStream(out));
      i++;
    }
  }
  writeFileSync(join(dest, 'metadata.json'), JSON.stringify({
    version: '0.5.0', session_id: FIXTURE, agent: 'fixture',
    timestamp: '2026-07-15T00:00:00', total_samples: 0,
    chunks: i, event_types: [], perf_stat: {},
  }));
  if (existsSync(join(src, 'metrics.json'))) {
    cpSync(join(src, 'metrics.json'), join(dest, 'metrics.json'));
  }
}

const python = pickPython();
execFileSync(python, ['--version'], { stdio: 'ignore' });

const home = mkdtempSync(join(tmpdir(), 'perflens-e2e-'));
const sessionsDir = join(home, 'sessions');
mkdirSync(sessionsDir, { recursive: true });
await materializeFixture(sessionsDir);

console.log(`[e2e] PERFLENS_HOME=${home} http=${HTTP_PORT} tcp=${TCP_PORT}`);
const child = spawn(python, [
  '-m', 'perflens.cli', 'serve',
  '--http-port', HTTP_PORT, '--port', TCP_PORT,
  '--source-dir', REPO,
], {
  stdio: 'inherit',
  env: {
    ...process.env,
    PERFLENS_HOME: home,
    PYTHONPATH: join(REPO, 'src'),
  },
});
child.on('exit', (code) => process.exit(code ?? 0));
for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => child.kill(sig));
}
