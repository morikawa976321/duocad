<script lang="ts">
  /**
   * 選択1項目の情報(issue #140)。ビューポートの左下に浮かび、選択がちょうど1項目のときだけ出る
   * (2項目以上・空のときは何も出さない。本人判断)。中身の文は `selectionInfoText.ts`。
   * 置き場はブラウザの列の右: ブラウザ(z-index 5)は下端まで伸びるので、同じ列の下に置くと隠れる。
   * 札の高さを `--browser-bottom-reserve` に渡す手は、選ぶたびにブラウザの高さが変わるので取らない。
   */
  import { selectionInfoRows, selectionInfoTitle, type InfoNames } from './selectionInfoText'
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
</script>

{#if item !== null || error !== null}
  <div id="selection-info" class="floating-panel">
    {#if item !== null}
      <div class="title">{selectionInfoTitle(item, names)}</div>
      <dl>
        {#each selectionInfoRows(item) as row (row.label)}
          <dt>{row.label}</dt>
          <dd>{row.value}</dd>
        {/each}
      </dl>
    {:else}
      <div class="error">選択の情報を取れなかった: {error}</div>
    {/if}
  </div>
{/if}

<style>
  /* 指す操作を妨げない: `viewport.clickable()` は `elementFromPoint` がcanvasを返すかで判定するので、
     pointer-events を切っておけば札の下のボディの面も指せる(docs/UI.md「常設パネルを足すときは…」)。 */
  #selection-info {
    position: absolute;
    left: calc(20px + var(--browser-panel-w));
    bottom: 10px;
    z-index: 4;
    max-width: calc(100% - 30px - var(--browser-panel-w));
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
