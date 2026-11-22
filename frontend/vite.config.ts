import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

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
  build: { target: 'es2022' },
});
