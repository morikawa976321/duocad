<script lang="ts">
  /**
   * パーツ一覧 — 表示・非表示の切り替え。
   *
   * **状態はコアが持つ**。ここは `bodies`(コアの `state.bodies`)を描くだけで、
   * 可視性そのものはローカルに持たない。チェックボックスを操作したら
   * `onToggle` 経由でコアの `set_visible` verb を呼びに行く(呼び出し元の `App.svelte` が
   * WebSocket越しに送る)。**ローカル状態は無い**(完全にステートレス)。
   *
   * 各行が持つのは表示/非表示のチェック1つだけ(2026-08-23本人裁定でpick列・
   * 「選択したものだけ表示」を削除 —— duocadは10や20の複数パーツを扱えるほどリッチな
   * 状態ではなく、現段階で考える必要のない懸念)。27部品を1つずつ押すのは実用にならない
   * ため、一括操作は全表示・全非表示の2つを持つ(指示の範囲外なので残す)。
   */
  import type { BodyMeta } from './session'

  let {
    bodies,
    onToggle,
    onShowAll,
    onHideAll,
  }: {
    bodies: BodyMeta[]
    onToggle: (bodyId: string, visible: boolean) => void
    onShowAll: () => void
    onHideAll: () => void
  } = $props()

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
  </div>
  <ul class="parts-list">
    {#each bodies as body (body.body_id)}
      <li class="parts-row" class:hidden-row={!body.visible} data-body-id={body.body_id}>
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
