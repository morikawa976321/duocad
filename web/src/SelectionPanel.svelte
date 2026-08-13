<script lang="ts">
  /**
   * 選択パネル — **共有される選択集合を人間側から見える形にする**(タスクB・2026-08-13)。
   *
   * ここが「交互操作が人間側から見える唯一の場所」である(実装計画書§8b B):
   *
   * - AIが `propose_selection` で書いた集合は **`origin === "proposed"`** で届き、
   *   確定した選択と**違う色**で出る。承認(`commit_selection`)のボタンもここにある。
   * - 人はここから1件ずつ**外せる**(`modify_selection` の `remove`)。
   *   3Dで指せるのは面だけなので、**辺・頂点・領域を過不足なく直せる場所はここしかない**
   *   (`viewport.ts` の `highlight` のdocstring参照)。
   *
   * **状態は1つも持たない。** 表示しているのはコアの `state.selection` そのもので、
   * 操作はすべてコアのverbへ出ていく(アプリ層に選択状態を置かない。§2)。
   */
  import { KIND_LABELS, describeSelection, refKey, refKind } from './commands/command'
  import type { BodyMeta, SelectionRef, SelectionState, ShapeRef } from './session'

  let {
    selection,
    bodies,
    onRemove,
    onClear,
    onCommit,
  }: {
    selection: SelectionState | null
    bodies: BodyMeta[]
    onRemove: (ref: SelectionRef) => void
    onClear: () => void
    onCommit: () => void
  } = $props()

  const items = $derived(selection?.items ?? [])
  const proposed = $derived(selection?.origin === 'proposed')
  const summary = $derived(describeSelection(items))

  function bodyName(bodyId: string): string {
    return bodies.find((body) => body.body_id === bodyId)?.name ?? bodyId
  }

  /** 1件を「どこの何か」に読み下す(領域はスケッチの中の番号)。 */
  function describeItem(ref: SelectionRef): string {
    const kind = refKind(ref)
    if (kind === 'region') {
      const region = ref as { sketch: string; index: number }
      return `${KIND_LABELS.region} ${region.index} / ${region.sketch}`
    }
    const shape = ref as ShapeRef
    return `${KIND_LABELS[kind]} ${shape.index} / ${bodyName(shape.body)}`
  }
</script>

<section class="selection-panel" class:proposed>
  <div class="selection-head">
    <strong>選択</strong>
    <span id="selection-summary" class="value" data-count={items.length} data-origin={selection?.origin ?? 'user'}>
      {summary}
    </span>
    {#if proposed}
      <span class="badge">AIの提案</span>
    {/if}
    <span class="spacer"></span>
    {#if proposed}
      <button id="selection-commit" onclick={onCommit}>承認する</button>
    {/if}
    <button id="selection-clear" onclick={onClear} disabled={items.length === 0}>全解除</button>
  </div>

  {#if proposed}
    <p class="hint">
      AIが提案した選択です。承認するまでコマンドの引数に使えません(コアが止めます)。
      1件外す・足すと、それ自体が承認になります。
    </p>
  {/if}

  {#if items.length === 0}
    <p class="hint dim">面をクリックして選ぶ(Shift+クリックで追加 / Ctrl+クリックで除去)</p>
  {:else}
    <ul class="selection-list">
      {#each items as item (refKey(item))}
        <li class="selection-row" data-ref={refKey(item)}>
          <span class="selection-name">{describeItem(item)}</span>
          <button
            class="selection-remove"
            onclick={() => onRemove(item)}
            aria-label={`${describeItem(item)} を選択から外す`}
          >
            外す
          </button>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  /* **高さを固定する。** 中身(選択の件数)は1クリックごとに変わるので、高さを内容任せに
     すると、面を選ぶたびに下のパネルが伸縮して**ビューポートがリサイズされ、カメラの
     アスペクトと射影が変わる** —— 「さっき見えていた面が、選んだ瞬間に少しずれる」。
     2026-08-13の実機検査で、1回目のクリックの後に2回目のクリックが隣の部品へ当たる形で
     実際に踏んだ(件数表示のぶんだけ画面が動いていた)。 */
  .selection-panel {
    border-top: 1px solid var(--line);
    background: var(--panel);
    padding: 6px 10px;
    height: 132px;
    flex: 0 0 auto;
    overflow-y: auto;
  }

  /* AIの提案は**枠ごと**色が変わる(3Dのハイライト色と揃える。viewport.ts の PROPOSED_COLOR) */
  .selection-panel.proposed {
    box-shadow: inset 3px 0 0 #36d1ff;
  }

  .selection-head {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .spacer {
    flex: 1 1 auto;
  }

  .badge {
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 8px;
    background: #36d1ff;
    color: #08121a;
  }

  .hint {
    margin: 4px 0 0;
    font-size: 11px;
  }

  .selection-list {
    list-style: none;
    margin: 4px 0 0;
    padding: 0;
  }

  .selection-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 2px 0;
  }

  .selection-name {
    flex: 1 1 auto;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .selection-remove {
    flex: 0 0 auto;
    font-size: 11px;
    padding: 1px 6px;
  }

  .dim {
    color: var(--dim);
  }
</style>
