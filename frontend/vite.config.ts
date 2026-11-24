import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { resolve } from 'node:path';

const backend = process.env.YUKIBOT_BACKEND_URL ?? 'http://127.0.0.1:3180';

export default defineConfig({
  plugins: [svelte()],
  base: '/mod-web/frontend/',
  server: {
    proxy: {
      '/api': backend,
      '/auth': backend,
      '^/mod-web(?!/frontend(?:/|$))': backend,
    },
  },
  build: {
    target: 'es2022',
    rollupOptions: {
      input: {
        index: resolve(import.meta.dirname, 'index.html'),
        map: resolve(import.meta.dirname, 'src/map/client.ts'),
      },
      output: {
        entryFileNames: (chunk) => (chunk.name === 'map' ? 'map.js' : 'assets/[name]-[hash].js'),
      },
    },
  },
});
