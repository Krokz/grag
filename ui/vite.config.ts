import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  // absolute asset URLs: the SPA fallback serves index.html from arbitrary
  // paths, so relative './assets/...' would resolve against the wrong base
  // and come back as HTML (silently unstyled page).
  base: '/',
  build: {
    outDir: '../src/grag/api/static',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1500,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8471',
    },
  },
});
