<script lang="ts">
  /**
   * ビューキューブの下の「全体表示」ボタン。キューブは canvas に描くので、位置は `viewCubeOverlay.ts` の寸法定数と
   * verb 引数 overlay の高さ(`--toolbar-overlay-h`)から合わせる。F キーと同じ働き(App.svelte)。
   */
  import { CUBE_MARGIN_PX, CUBE_SIZE_PX } from './viewCubeOverlay'

  let { onclick }: { onclick: () => void } = $props()

  const SIZE_PX = 24
  const GAP_PX = 6
  const top = `calc(${CUBE_MARGIN_PX + CUBE_SIZE_PX + GAP_PX}px + var(--toolbar-overlay-h, 0px))`
  const right = `${CUBE_MARGIN_PX + (CUBE_SIZE_PX - SIZE_PX) / 2}px`
</script>

<button
  type="button"
  id="frame-all"
  class="frame-all"
  title="全体表示 (F)"
  aria-label="全体表示"
  style:top
  style:right
  style:width="{SIZE_PX}px"
  style:height="{SIZE_PX}px"
  {onclick}
>
  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
    <path d="M1.5 5.5v-4h4M10.5 1.5h4v4M14.5 10.5v4h-4M5.5 14.5h-4v-4" fill="none" stroke="currentColor" stroke-width="1.5" />
  </svg>
</button>

<style>
  .frame-all {
    position: absolute;
    z-index: 5;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0;
  }
</style>
