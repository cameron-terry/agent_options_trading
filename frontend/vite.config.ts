/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Dev-only: proxies API calls to the FastAPI backend (`python -m
    // options_agent.ui`, defaults to :8000) so `npm run dev` can exercise
    // real endpoints. The built SPA is same-origin in production and needs
    // no proxy.
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  test: {
    // jsdom gives component tests a DOM; the SSE-driven flow in App.tsx needs
    // a real browser (EventSource) and is covered by Playwright, not here.
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    // Test files live next to source under src/**; vite build never imports
    // them, so they stay out of the production bundle.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    css: false,
  },
})
