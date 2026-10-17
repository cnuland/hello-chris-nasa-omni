/// <reference types="vitest/config" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Dev proxy targets are overridable via env (no secrets; plain URLs only).
const API_TARGET = process.env.API_PROXY_TARGET ?? 'http://localhost:8080';
const AGENT_TARGET = process.env.AGENT_PROXY_TARGET ?? 'http://localhost:8090';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true, rewrite: (p) => p.replace(/^\/api/, '') },
      '/agent': { target: AGENT_TARGET, changeOrigin: true, rewrite: (p) => p.replace(/^\/agent/, '') },
    },
  },
  test: {
    environment: 'jsdom',
    environmentOptions: { jsdom: { url: 'http://localhost:5173/' } },
    globals: true,
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    css: { modules: { classNameStrategy: 'non-scoped' } },
    testTimeout: 15000,
    restoreMocks: true,
  },
});
