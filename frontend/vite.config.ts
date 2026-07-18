/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

// Build output lands in the Python package: the server serves src/perflens/ui
// via importlib.resources and the wheel ships it via the hatch artifacts glob.
export default defineConfig({
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
