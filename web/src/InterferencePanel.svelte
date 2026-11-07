<script lang="ts">
  /**
   * 干渉チェックの結果のパネル(issue #186)。ビューポート右に浮かぶ。隙間の欄と再検査、組の一覧(見出しは干渉・隙間不足・未確定・接触の4つ。
   * 接触だけ畳む)。行を選ぶとビューポートがその組を強調し、その行に「寄せる」「選ぶ」が出る(`interference.svelte.ts`)。
   * 持つのは開閉と畳みの見た目だけ。畳んでも結果は残る(下の面を指すために退かす手。✕ は結果も捨てる)。
   */
  import type { Interference } from './interference.svelte'
  import { MARK_HINTS } from './interferenceGroups'
  import { isImeKey } from './keys'

  let { interference }: { interference: Interference } = $props()

  const result = $derived(interference.result)
  let folded = $state(false)

  function onKey(event: KeyboardEvent): void {
    if (event.key !== 'Enter' || isImeKey(event)) return
    event.preventDefault()
    if (interference.running) return
    interference.run()
  }
</script>

{#if interference.open}
  <aside id="interference-panel" class="floating-panel" class:folded>
    <header>
      <button
        type="button"
        id="interference-fold"
        class="caret"
        title={folded ? '開く' : '畳む'}
        aria-expanded={!folded}
        onclick={() => (folded = !folded)}
      >
        {folded ? '▸' : '▾'}
      </button>
      <span class="title">干渉チェック</span>
      {#if folded && interference.running}<span class="dim">検査中…</span>{/if}
      <button type="button" id="interference-close" class="close" title="閉じる(結果も消える)" onclick={() => interference.close()}>×</button>
    </header>
    {#if !folded}
      <div class="clearance">
        <label for="interference-clearance">隙間</label>
        <input
          id="interference-clearance"
          type="text"
          inputmode="decimal"
          autocomplete="off"
          spellcheck="false"
          bind:value={interference.clearanceText}
          onkeydown={onKey}
        />
        <span class="unit">mm</span>
        <button type="button" id="interference-run" disabled={interference.running} onclick={() => interference.run()}>
          {interference.running ? '検査中…' : '再検査'}
        </button>
      </div>
      {#if interference.notice !== ''}
        <div class="notice" id="interference-notice">{interference.notice}</div>
      {/if}
      {#if result !== null}
        {#each interference.summary as line (line)}
          <div class="summary">{line}</div>
        {/each}
        <div class="groups">
          {#each interference.groups as group (group.key)}
            <details open={group.open} class="group {group.key}" data-group={group.key}>
              <summary>{group.heading} <span class="count">{group.rows.length}</span></summary>
              {#each group.rows as row (row.key)}
                <button
                  type="button"
                  class="row"
                  class:picked={interference.selectedKey === row.key}
                  data-pair={row.key}
                  onclick={() => interference.select(row.key)}
                >
                  <span class="names" title={row.title}>{row.title}</span>
                  <span class="detail">{row.detail}</span>
                  {#each row.marks as mark (mark)}<span class="mark" title={MARK_HINTS[mark]}>{mark}</span>{/each}
                </button>
                {#if interference.selectedKey === row.key}
                  <div class="row-actions">
                    <button type="button" class="interference-frame" onclick={() => interference.frameRow(row.key)}>寄せる</button>
                    <button type="button" class="interference-select" onclick={() => interference.selectRow(row.key)}>この2つを選ぶ</button>
                  </div>
                {/if}
              {/each}
            </details>
          {/each}
        </div>
      {/if}
    {/if}
  </aside>
{/if}

<style>
  /* `.stage` が位置の基準。ビューキューブの下に置く(右上)。ビューポートの寸法には触れない(styles.css の不変条件)。 */
  #interference-panel {
    position: absolute;
    top: calc(190px + var(--toolbar-overlay-h, 0px));
    right: 10px;
    z-index: 5;
    width: 300px;
    max-height: calc(100% - 210px - var(--toolbar-overlay-h, 0px));
    overflow: auto;
    font-variant-numeric: tabular-nums;
  }

  #interference-panel.folded {
    width: auto;
  }

  header {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 6px;
  }

  .folded header {
    margin-bottom: 0;
  }

  .title {
    color: var(--accent);
  }

  .dim {
    color: var(--dim);
  }

  .caret {
    padding: 0 4px;
  }

  .close {
    margin-left: auto;
    padding: 0 6px;
  }

  /* 見た目の寸法は変えず、押せる範囲だけを上下左右に広げる。 */
  .caret,
  .close {
    position: relative;
  }

  .caret::after,
  .close::after {
    content: '';
    position: absolute;
    inset: -5px -4px;
  }

  .row-actions {
    display: flex;
    gap: 6px;
    padding: 2px 6px 4px 8px;
    border-left: 2px solid var(--accent);
  }

  .clearance {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 6px;
  }

  .clearance input {
    width: 4.5em;
  }

  .unit {
    color: var(--dim);
  }

  .notice {
    margin-bottom: 6px;
    color: var(--bad);
  }

  .summary {
    color: var(--dim);
    margin-bottom: 2px;
  }

  .groups {
    margin-top: 6px;
  }

  summary {
    cursor: pointer;
    padding: 2px 0;
  }

  .overlap > summary {
    color: #ff6b5e;
  }

  .short > summary,
  .unknown > summary {
    color: #ffb347;
  }

  .count {
    color: var(--dim);
    margin-left: 4px;
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 2px 8px;
    width: 100%;
    text-align: left;
    padding: 3px 6px;
    background: transparent;
    border: 0;
    border-left: 2px solid transparent;
  }

  .row:hover {
    background: color-mix(in oklab, var(--text) 8%, transparent);
  }

  .row.picked {
    border-left-color: var(--accent);
    background: color-mix(in oklab, var(--accent) 14%, transparent);
  }

  .names {
    flex: 1 1 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .detail {
    color: var(--dim);
  }

  .mark {
    padding: 0 4px;
    border: 1px solid var(--line);
    border-radius: 3px;
    font-size: 0.85em;
    color: var(--dim);
  }
</style>
