import { defineConfig } from 'vite';

export default defineConfig({
  // Served by the FastAPI StaticFiles mount at `/client/` (the same TLS
  // termination as the backend), not at the root domain.
  base: '/client/',

  build: {
    outDir: 'dist',
    sourcemap: true,
    emptyOutDir: true,
  },

  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.ts'],
    // Vitest doesn't polyfill WebSocket by default in jsdom; many of our tests
    // use a mock WebSocket class instead of the real one anyway.
  }
});
