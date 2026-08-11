<script lang="ts">
  /**
   * パーツ一覧 — 表示・非表示の切り替え(M3.5・2026-08-11)。
   *
   * **状態はコアが持つ**。ここは `bodies`(コアの `state.bodies`)を描くだけで、
   * 可視性そのものはローカルに持たない。チェックボックスを操作したら
   * `onToggle` 経由でコアの `set_visible` verb を呼びに行く(呼び出し元の `App.svelte` が
   * WebSocket越しに送る)。ここが持つローカル状態は**「選択したものだけ表示」ボタンのための
   * 一時的な下書き**(`picked`)だけで、これはドメインの状態ではない
   * (undo/永続化の対象ではない、ページを閉じれば消えてよい選び途中の下書き)。
   *
   * 27部品を1つずつ押すのは実用にならないため、一括操作を3つ持つ:
   * 全表示・全非表示・「選択したものだけ表示」(chosenで選んだ行だけ残して他を隠す)。
   */
  import type { BodyMeta } from './session'

  let {
    bodies,
    onToggle,
    onShowAll,
    onHideAll,
    onShowOnly,
  }: {
    bodies: BodyMeta[]
    onToggle: (bodyId: string, visible: boolean) => void
    onShowAll: () => void
    onHideAll: () => void
    onShowOnly: (bodyIds: string[]) => void
  } = $props()

  // 「選択したものだけ表示」の下書き(ドメイン状態ではない。上のdocstring参照)。
  let picked = $state<Set<string>>(new Set())

  function togglePicked(bodyId: string): void {
    const next = new Set(picked)
    if (next.has(bodyId)) next.delete(bodyId)
    else next.add(bodyId)
    picked = next
  }

  function showOnlyPicked(): void {
    onShowOnly([...picked])
  }

  const visibleCount = $derived(bodies.filter((body) => body.visible).length)
</script>

<aside class="parts-panel">
  <div class="parts-header">
    <strong>パーツ</strong>
    <span class="dim">{visibleCount}/{bodies.length} 表示</span>
  </div>
  <div class="parts-bulk">
    <button id="parts-show-all" onclick={onShowAll}>全表示</button>
    <button id="parts-hide-all" onclick={onHideAll}>全非表示</button>
    <button id="parts-show-only-picked" onclick={showOnlyPicked} disabled={picked.size === 0}>
      選択したものだけ表示
    </button>
  </div>
  <ul class="parts-list">
    {#each bodies as body (body.body_id)}
      <li class="parts-row" class:hidden-row={!body.visible} data-body-id={body.body_id}>
        <input
          type="checkbox"
          class="pick"
          checked={picked.has(body.body_id)}
          onchange={() => togglePicked(body.body_id)}
          aria-label={`${body.name} を一括表示の対象にする`}
        />
        <label class="parts-name">
          <input
            type="checkbox"
            class="visible-toggle"
            checked={body.visible}
            onchange={(event) => onToggle(body.body_id, (event.target as HTMLInputElement).checked)}
          />
          {body.name}
        </label>
        <span class="dim parts-count">{body.face_count}面</span>
      </li>
    {/each}
  </ul>
</aside>

<style>
  .parts-panel {
    width: 220px;
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
    border-right: 1px solid var(--line);
    background: var(--panel);
  }

  .parts-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 10px;
    border-bottom: 1px solid var(--line);
  }

  .parts-bulk {
    display: flex;
    gap: 4px;
    padding: 6px 8px;
    border-bottom: 1px solid var(--line);
    flex-wrap: wrap;
  }

  .parts-bulk button {
    flex: 1 1 auto;
    padding: 3px 4px;
    font-size: 11px;
  }

  .parts-list {
    list-style: none;
    margin: 0;
    padding: 0;
    overflow-y: auto;
    flex: 1 1 auto;
    min-height: 0;
  }

  .parts-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 3px 8px;
    border-bottom: 1px solid var(--line);
  }

  .parts-row.hidden-row {
    color: var(--dim);
  }

  .parts-name {
    display: flex;
    align-items: center;
    gap: 6px;
    flex: 1 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    cursor: pointer;
  }

  .parts-count {
    flex: 0 0 auto;
    font-size: 11px;
  }

  .dim {
    color: var(--dim);
  }
</style>
