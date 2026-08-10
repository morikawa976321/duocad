import { vitePreprocess } from '@sveltejs/vite-plugin-svelte'

// vitePreprocess は <script lang="ts"> の型注釈を落とすだけ(型検査はしない)。
// .ts の型検査は `npm run check`(tsc --noEmit)が担当する。
export default {
  preprocess: vitePreprocess(),
}
