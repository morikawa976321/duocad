<script lang="ts">
  /**
   * ビューポート上端の流れるバー(issue #112)。出すかどうかは `serverWait.svelte.ts` が決める。
   * verb の引数行(`--toolbar-overlay-h`)がビューポートの上に重なるので、その下端に置く。
   */
  let { visible }: { visible: boolean } = $props()
</script>

{#if visible}
  <div class="wait-bar" role="progressbar" aria-label="処理中"><span></span></div>
{/if}

<style>
  .wait-bar {
    position: absolute;
    top: var(--toolbar-overlay-h, 0px);
    left: 0;
    right: 0;
    height: 3px;
    overflow: hidden;
    pointer-events: none;
    background: var(--accent-soft);
    /* 開始画面(5)の上: そこから開いた文書を待つ間も見せる。トースト(20)よりは下。 */
    z-index: 11;
  }
  /* transform だけを動かす: メッシュの組み立てでメインスレッドが詰まっている間も合成側で流れ続ける。 */
  .wait-bar span {
    position: absolute;
    inset: 0;
    width: 30%;
    background: var(--accent);
    animation: wait-slide 1.1s ease-in-out infinite;
  }
  @keyframes wait-slide {
    from {
      transform: translateX(-100%);
    }
    to {
      transform: translateX(340%);
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .wait-bar span {
      width: 100%;
      animation: none;
      opacity: 0.7;
    }
  }
</style>
