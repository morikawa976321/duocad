import { svelte } from '@sveltejs/vite-plugin-svelte'
import { defineConfig } from 'vite'

// SvelteKit は使わない(Svelte + Vite だけ)。ルーター・SSR・アダプタ・フォームはどれも不要で、
// 2026年に観測される破壊的変更はほぼ全て SvelteKit 側に集中しているため
// (docs/DESIGN_DECISIONS.md「技術選定の根拠」)。ここを増やさないことが長期保守の主な予防になっている。
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
