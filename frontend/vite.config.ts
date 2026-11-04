import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const isVercel = !!(globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.VERCEL;

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://127.0.0.1:8000', '/health': 'http://127.0.0.1:8000' } },
  build: { outDir: isVercel ? 'dist' : '../backend/static', emptyOutDir: true },
});
