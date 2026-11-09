<script lang="ts">
  /**
   * 選択1項目の情報(issue #140)。ビューポートの左下に浮かび、選択がちょうど1項目のときだけ出る
   * (2項目以上・空のときは何も出さない。本人判断)。中身の文は `selectionInfoText.ts`。
   */
  import { selectionInfoErrorText, selectionInfoRows, selectionInfoTitle, type InfoNames } from './selectionInfoText'
  import type { MeasureItem } from './session'

  let {
    item,
    error,
    names,
  }: {
    item: MeasureItem | null
    error: string | null
    names: InfoNames
  } = $props()

  /**
   * 札はブラウザと同じ列の下端に置くので、実高さ(+間隔10px)を `.stage` の `--selection-info-reserve` へ渡し、
   * ブラウザの下端をその手前で止める。渡さないと、下端まで伸びたブラウザ(z-index 5)の下に札が隠れる。
   */
  let panelEl = $state<HTMLDivElement | null>(null)
  $effect(() => {
    const el = panelEl
    const stage = el?.parentElement
    if (!el || !stage) return
    const sync = () =>
      stage.style.setProperty('--selection-info-reserve', `${Math.ceil(el.getBoundingClientRect().height) + 10}px`)
    sync()
    const observer = new ResizeObserver(sync)
    observer.observe(el)
    return () => {
      observer.disconnect()
      stage.style.removeProperty('--selection-info-reserve')
    }
  })
</script>

{#if item !== null || error !== null}
  <div id="selection-info" class="floating-panel" bind:this={panelEl}>
    {#if item !== null}
      <div class="title">{selectionInfoTitle(item, names)}</div>
      <dl>
        {#each selectionInfoRows(item) as row (row.label)}
          <dt>{row.label}</dt>
          <dd>{row.value}</dd>
        {/each}
      </dl>
    {:else}
      <div class="error">選択の情報を取れなかった: {selectionInfoErrorText(error ?? '')}</div>
    {/if}
  </div>
{/if}

<style>
  /* 指す操作を妨げない: `viewport.clickable()` は `elementFromPoint` がcanvasを返すかで判定するので、
     pointer-events を切っておけば札の下のボディの面も指せる(docs/UI.md「常設パネルを足すときは…」)。 */
  /* 最適化パネル(`OptimizePanel.svelte`)も同じ列の下端に浮くので、出ているときはその上に積む。 */
  #selection-info {
    position: absolute;
    left: 10px;
    bottom: calc(10px + var(--browser-bottom-reserve, 0px));
    z-index: 4;
    max-width: calc(100% - 20px);
    pointer-events: none;
    font-variant-numeric: tabular-nums;
  }

  .title {
    margin-bottom: 4px;
    color: var(--accent);
    white-space: nowrap;
  }

  dl {
    display: grid;
    grid-template-columns: auto auto;
    column-gap: 10px;
    row-gap: 1px;
    margin: 0;
  }

  dt {
    color: var(--dim);
  }

  dd {
    margin: 0;
    white-space: nowrap;
  }

  .error {
    color: var(--bad);
  }
</style>
