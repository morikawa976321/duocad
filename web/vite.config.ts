import { svelte } from '@sveltejs/vite-plugin-svelte'
import { defineConfig } from 'vite'

// SvelteKit は使わない(Svelte + Vite だけ。docs/DESIGN_DECISIONS.md「技術選定の根拠」)。
export default defineConfig({
  plugins: [svelte()],
  base: './',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    sourcemap: true,
  },
  server: { port: 5173, strictPort: true },
})
