<script lang="ts">
  /**
   * ブラウザ — **ビューポートの左上に浮かぶ、モデルの中身の一覧**(Fusion 360の
   * 「ブラウザ」に対応する場所)。旧 `PartsPanel.svelte` を置き換える。
   *
   * **状態はコアが持つ**。ここは `bodies`(コアの `state.bodies` のうち `kind==="solid"`)と
   * `sketches`(コアの `state.sketches`)を描くだけで、可視性もスケッチも
   * ローカルには持たない。持っているのは**折りたたみの開閉だけ**で、これは見た目であって
   * モデルの状態ではない。
   *
   * **今は「モデル」と「スケッチ」の2区分をフラットに並べるだけ**。入れ子のツリー化と
   * 「コンストラクション」の追加は将来の発展であり、今はその2区分の器だけを用意してある
   * (区分の見出しを先に立てておかないと、後からツリーにするときに一覧の作り直しになる)。
   *
   * **ビューポートの寸法には触らない**。`position: absolute` で `.stage` の上に浮くので
   * フローから外れており、開閉してもカメラのアスペクトは変わらない(`Toast.svelte` と同じ
   * 作り。`styles.css` の「ビューポートのサイズを変えるのは .viewport だけ」を守るため)。
   */
  import type { BodyMeta, SketchState } from './session'

  let {
    bodies,
    sketches,
    activeSketchId,
    onToggle,
    onShowAll,
    onHideAll,
    onEnterSketch,
  }: {
    /** ユーザーに見せるボディ(参照ボディを除いた `kind==="solid"` だけ)。 */
    bodies: BodyMeta[]
    sketches: SketchState[]
    /** 編集中のスケッチ(無ければ null)。行に「編集中」の印を出すのに使う。 */
    activeSketchId: string | null
    onToggle: (bodyId: string, visible: boolean) => void
    onShowAll: () => void
    onHideAll: () => void
    /** 行のクリックでそのスケッチに入る(再開)。 */
    onEnterSketch: (sketchId: string) => void
  } = $props()

  /** パネル全体の折りたたみ。ビューポートに重なるので、畳めないと形が隠れる。 */
  let open = $state(true)

  const visibleCount = $derived(bodies.filter((body) => body.visible).length)

  function planeLabel(sketch: SketchState): string {
    return sketch.plane.kind === 'base' ? sketch.plane.name : '面'
  }
</script>

<aside class="browser-panel" class:collapsed={!open}>
  <div class="browser-header">
    <button
      id="browser-collapse"
      class="caret"
      onclick={() => (open = !open)}
      title={open ? '畳む' : '開く'}
      aria-expanded={open}
    >
      {open ? '▾' : '▸'}
    </button>
    <strong>ブラウザ</strong>
  </div>

  {#if open}
    <div class="browser-body">
      <!-- 区分1: モデル(ボディ)。可視性の切り替えはここが持つ -->
      <div class="group">
        <div class="group-head">
          <span class="group-name">モデル</span>
          <span class="dim">{visibleCount}/{bodies.length} 表示</span>
        </div>
        <div class="group-bulk">
          <button id="parts-show-all" onclick={onShowAll}>全表示</button>
          <button id="parts-hide-all" onclick={onHideAll}>全非表示</button>
        </div>
        <ul class="parts-list">
          {#if bodies.length === 0}
            <li class="empty dim">ボディが無い</li>
          {/if}
          {#each bodies as body (body.body_id)}
            <li class="parts-row" class:hidden-row={!body.visible} data-body-id={body.body_id}>
              <label class="parts-name">
                <input
                  type="checkbox"
                  class="visible-toggle"
                  checked={body.visible}
                  onchange={(event) =>
                    onToggle(body.body_id, (event.target as HTMLInputElement).checked)}
                />
                {body.name}
              </label>
              <span class="dim parts-count">{body.face_count}面</span>
            </li>
          {/each}
        </ul>
      </div>

      <!-- 区分2: スケッチ。名前のクリックでそのスケッチに入る -->
      <div class="group">
        <div class="group-head">
          <span class="group-name">スケッチ</span>
          <span class="dim">{sketches.length}</span>
        </div>
        <ul class="sketch-list" id="browser-sketches">
          {#if sketches.length === 0}
            <li class="empty dim">スケッチが無い</li>
          {/if}
          {#each sketches as sketch (sketch.sketch_id)}
            <li
              class="sketch-row"
              class:active={sketch.sketch_id === activeSketchId}
              data-sketch-id={sketch.sketch_id}
            >
              <button
                class="sketch-name"
                onclick={() => onEnterSketch(sketch.sketch_id)}
                title="クリックでこのスケッチに入る"
              >
                スケッチ({planeLabel(sketch)})
              </button>
              {#if sketch.sketch_id === activeSketchId}
                <span class="tag on">編集中</span>
              {:else}
                <span class="dim sketch-count">図形{sketch.entities.length}</span>
              {/if}
            </li>
          {/each}
        </ul>
      </div>
    </div>
  {/if}
</aside>

<style>
  /* ビューポートの左上に浮かせる。`.stage` が位置の基準(App.svelte で position:relative)。
     高さは中身なりだが上限を持つ —— 27部品を並べると画面の下まで届いてしまう。 */
  .browser-panel {
    position: absolute;
    top: 10px;
    left: 10px;
    z-index: 5;
    width: 232px;
    max-height: calc(100% - 20px);
    display: flex;
    flex-direction: column;
    min-height: 0;
    background: color-mix(in oklab, var(--panel) 88%, transparent);
    border: 1px solid var(--line);
    /* 浮いているものが背景と地続きに見えないように、影で1枚持ち上げる */
    box-shadow: 0 6px 18px oklch(0% 0 0 / 0.45);
  }

  .browser-panel.collapsed {
    width: auto;
  }

  .browser-header {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 8px;
    border-bottom: 1px solid var(--line);
    flex: 0 0 auto;
  }

  .browser-panel.collapsed .browser-header {
    border-bottom: none;
  }

  .caret {
    padding: 0 4px;
    border: none;
    background: none;
    color: var(--dim);
    line-height: 1;
  }

  .caret:not(:disabled):hover {
    color: var(--accent);
    border: none;
  }

  .browser-body {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
  }

  .group {
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  .group + .group {
    border-top: 1px solid var(--line);
  }

  .group-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 6px;
    padding: 4px 8px;
    background: var(--line-soft);
  }

  .group-name {
    color: var(--text);
  }

  .group-bulk {
    display: flex;
    gap: 4px;
    padding: 4px 8px;
  }

  .group-bulk button {
    flex: 1 1 auto;
    padding: 2px 4px;
    font-size: 11px;
  }

  .parts-list,
  .sketch-list {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .parts-row,
  .sketch-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 2px 8px;
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

  .parts-count,
  .sketch-count {
    flex: 0 0 auto;
    font-size: 11px;
  }

  .sketch-row.active {
    background: var(--accent-soft);
  }

  .sketch-name {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    text-align: left;
    padding: 2px 4px;
    font-size: 12px;
    background: none;
    border: 1px solid transparent;
  }

  .empty {
    padding: 3px 8px;
    font-size: 11px;
  }

  .dim {
    color: var(--dim);
  }
</style>
