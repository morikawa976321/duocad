<script lang="ts">
  /**
   * 干渉チェックの結果のパネル(issue #186)。ビューポート右に浮かぶ。隙間の欄と再検査、組の一覧(見出しは干渉・隙間不足・未確定・接触の4つ。
   * 接触だけ畳む)。行を選ぶとビューポートがその組を強調する(`interference.svelte.ts`)。持つのは開閉の見た目だけ。
   */
  import { summaryLines } from './interferenceGroups'
  import type { Interference } from './interference.svelte'
  import { isImeKey } from './keys'

  let { interference }: { interference: Interference } = $props()

  const result = $derived(interference.result)
  const lines = $derived(result === null ? [] : summaryLines(result))

  function onKey(event: KeyboardEvent): void {
    if (event.key !== 'Enter' || isImeKey(event)) return
    event.preventDefault()
    interference.run()
  }
</script>

{#if interference.open}
  <aside id="interference-panel" class="floating-panel">
    <header>
      <span class="title">干渉チェック</span>
      <button type="button" id="interference-close" class="close" title="閉じる" onclick={() => interference.close()}>×</button>
    </header>
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
      {#each lines as line (line)}
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
                <span class="names">{row.title}</span>
                <span class="detail">{row.detail}</span>
                {#each row.marks as mark (mark)}<span class="mark">{mark}</span>{/each}
              </button>
            {/each}
          </details>
        {/each}
      </div>
    {/if}
  </aside>
{/if}

<style>
  /* `.stage` が位置の基準。ビューキューブの下に置く(右上)。ビューポートの寸法には触れない(styles.css の不変条件)。 */
  #interference-panel {
    position: absolute;
    top: calc(150px + var(--toolbar-overlay-h, 0px));
    right: 10px;
    z-index: 5;
    width: 300px;
    max-height: calc(100% - 170px - var(--toolbar-overlay-h, 0px));
    overflow: auto;
    font-variant-numeric: tabular-nums;
  }

  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
  }

  .title {
    color: var(--accent);
  }

  .close {
    padding: 0 6px;
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
