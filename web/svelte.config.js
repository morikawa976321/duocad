import { vitePreprocess } from '@sveltejs/vite-plugin-svelte'

// vitePreprocess は型注釈を落とすだけ(型検査は `npm run check`)。
export default {
  preprocess: vitePreprocess(),
}
