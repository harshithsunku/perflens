/// <reference types="vitest/config" />
import { readFileSync } from 'node:fs';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// The repo-root VERSION file is the single source of truth: the agent bakes it
// in via agent-c/Makefile, the Python package reads it, and the UI shows it in
// the docs drawer. Injecting it here means the UI can never drift out of sync
// the way a hand-typed string does. tools/check_version.py enforces the rest.
const VERSION = readFileSync(new URL('../VERSION', import.meta.url), 'utf8').trim();

// Build output lands in the Python package: the server serves src/perflens/ui
// via importlib.resources and the wheel ships it via the hatch artifacts glob.
export default defineConfig({
  define: {
    __PERFLENS_VERSION__: JSON.stringify(VERSION),
  },
  plugins: [react(), tailwindcss()],
  build: {
    outDir: '../src/perflens/ui',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // Dev: `perflens serve` on 8080 owns the API; Vite owns HMR.
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
