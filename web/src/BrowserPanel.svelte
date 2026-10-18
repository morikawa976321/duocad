<script module lang="ts">
  import type { SketchState } from './session'

  /** スケッチの平面の表示名。面に貼ったものの文言だけ場所で違う(ブラウザは「面」、ツールバーは「面に貼ったスケッチ」)。 */
  export function planeLabel(plane: SketchState['plane'], faceLabel = '面'): string {
    if (plane.kind === 'base') return plane.name
    if (plane.kind === 'offset') return `オフセット ${plane.distance}mm`
    return faceLabel
  }
</script>

<script lang="ts">
  /**
   * ブラウザ — ビューポート左上に浮かぶ、モデルの中身の一覧(Fusion 360の「ブラウザ」に対応)。
   * 状態はコアが持つ。`bodies`(`state.bodies` のうち `kind==="solid"`)と `sketches`(`state.sketches`)を描くだけで、
   * 可視性もローカルに持たない。持つのは折りたたみの開閉だけ(見た目)。
   * 今は「モデル」と「スケッチ」の2区分をフラットに並べるだけ(入れ子のツリー化と「コンストラクション」は将来。
   * 見出しを先に立てておかないと、後からツリーにするとき一覧の作り直しになる)。
   * `position: absolute` で `.stage` の上に浮くので、開閉してもカメラのアスペクトは変わらない
   * (`Toast.svelte` と同じ。`styles.css` の「ビューポートのサイズを変えるのは .viewport だけ」)。
   */
  import { isImeKey } from './keys'
  import { renameTarget } from './renameDraft'
  import type { BasePlaneName, BodyMeta } from './session'
  import type { SectionState } from './viewport'

  let {
    bodies,
    sketches,
    activeSketchId,
    originVisible,
    section,
    onToggle,
    onShowAll,
    onHideAll,
    onEnterSketch,
    onToggleSketch,
    onShowAllSketches,
    onHideAllSketches,
    onToggleOrigin,
    onRename,
    onDeleteBody,
    onDeleteSketch,
    onSectionToggle,
    onSectionPlane,
    onSectionPosition,
    onSectionInvert,
  }: {
    bodies: BodyMeta[]
    sketches: SketchState[]
    /** 編集中のスケッチ(無ければ null)。 */
    activeSketchId: string | null
    /** 常設の原点ギズモが出ているか。文書の状態ではなく画面設定(`viewSettings.ts` が localStorage へ持つ)。 */
    originVisible: boolean
    onToggle: (bodyId: string, visible: boolean) => void
    onShowAll: () => void
    onHideAll: () => void
    /** 隠したスケッチも入れる(入る側が表示へ戻す)。 */
    onEnterSketch: (sketchId: string) => void
    onToggleSketch: (sketchId: string, visible: boolean) => void
    onShowAllSketches: () => void
    onHideAllSketches: () => void
    onToggleOrigin: (visible: boolean) => void
    onRename: (target: string, name: string) => void
    /** 確認は挟まない: 1回の Ctrl+Z で戻る。 */
    onDeleteBody: (bodyId: string) => void
    onDeleteSketch: (sketchId: string) => void
    /** 断面表示(issue #6)。原点と同じく画面側の状態で、App が持つ。 */
    section: SectionState
    onSectionToggle: (on: boolean) => void
    onSectionPlane: (plane: BasePlaneName) => void
    /** 数式のまま渡す(評価と失敗のトーストは App)。受け付けたら true。欄は結果によらず状態に戻す(`commitPosition`)。 */
    onSectionPosition: (text: string) => boolean
    onSectionInvert: (inverted: boolean) => void
  } = $props()

  const SECTION_PLANES: readonly BasePlaneName[] = ['XY', 'XZ', 'YZ']

  function sectionAxis(plane: BasePlaneName): string {
    return plane === 'XY' ? 'Z' : plane === 'XZ' ? 'Y' : 'X'
  }

  function formatPosition(value: number): string {
    return String(Number(value.toFixed(2)))
  }

  function commitPosition(event: Event): void {
    const input = event.target as HTMLInputElement
    onSectionPosition(input.value)
    // 欄を状態に戻す。同じ値に評価される式(`10/2` → 5 のまま)では Svelte が欄を書き直さないのでここで戻す。
    // 読めない式(`abc`・`1/0`)は位置が変わらない(理由はトーストが言う)ので、不正な文字を欄に残さず直前の位置に戻す。
    input.value = formatPosition(section.position)
  }

  function blurOnEnter(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !isImeKey(event)) (event.target as HTMLInputElement).blur()
  }

  let open = $state(true)

  /** 名前を書き換えている行の ID。下書きは別の状態に持つ: `{id, draft}` を1つにして `bind:value={draft}` にすると、
   *  欄を閉じた瞬間に消えかけの欄の束縛が null を読んで TypeError を投げる(実機で確認)。 */
  let renamingId = $state<string | null>(null)
  let draft = $state('')

  function startRename(id: string, current: string): void {
    draft = current
    renamingId = id
  }

  /** 空・今と同じなら送らずに閉じる(前後の空白は落として送る。`renameTarget`)。`id` で自分の行かを確かめる: Enter で閉じた欄の blur は DOM から外れるときに遅れて届き、
   *  その間に別の行の ✎ が押されていると、相手の行の下書きを自分の名前で確定してしまう。 */
  function commitRename(id: string, current: string): void {
    if (renamingId !== id) return
    renamingId = null
    const next = renameTarget(draft, current)
    if (next !== null) onRename(id, next)
  }

  /** Esc は窓まで届かせない: `CommandHost` の窓の keydown が入力欄の Esc も拾って、起動中のコマンドまで中断する。 */
  function renameKey(event: KeyboardEvent, id: string, current: string): void {
    // IME の変換中の Enter(候補の確定)・Esc(変換の取り消し)は名前の確定・取り消しではない。
    if (isImeKey(event)) return
    if (event.key === 'Enter') commitRename(id, current)
    else if (event.key === 'Escape') {
      event.stopPropagation()
      renamingId = null
    }
  }

  function focusSelect(input: HTMLInputElement): void {
    input.focus()
    input.select()
  }

  function sketchLabel(sketch: SketchState): string {
    return sketch.name ?? `スケッチ(${planeLabel(sketch.plane)})`
  }

  const visibleCount = $derived(bodies.filter((body) => body.visible).length)
  const visibleSketchCount = $derived(sketches.filter((sketch) => sketch.visible).length)

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
      <div class="group">
        <div class="group-head">
          <span class="group-name">原点</span>
          <input
            id="origin-visible-toggle"
            type="checkbox"
            checked={originVisible}
            onchange={(event) => onToggleOrigin((event.target as HTMLInputElement).checked)}
            title={originVisible ? '原点・軸・平面を隠す' : '原点・軸・平面を表示'}
          />
        </div>
      </div>

      <div class="group">
        <div class="group-head">
          <span class="group-name">断面</span>
          <input
            id="section-toggle"
            type="checkbox"
            checked={section.on}
            onchange={(event) => onSectionToggle((event.target as HTMLInputElement).checked)}
            title={section.on ? '断面をやめる' : '断面で切る'}
          />
        </div>
        {#if section.on}
          <div class="section-controls">
            <div class="section-planes">
              {#each SECTION_PLANES as plane (plane)}
                <button
                  id={`section-plane-${plane}`}
                  class="tag"
                  class:on={section.plane === plane}
                  onclick={() => onSectionPlane(plane)}
                  title={`${plane} 平面で切る`}
                >
                  {plane}
                </button>
              {/each}
            </div>
            <label class="section-row">
              <span class="dim">{sectionAxis(section.plane)}</span>
              <input
                id="section-position"
                type="text"
                value={formatPosition(section.position)}
                onchange={commitPosition}
                onkeydown={blurOnEnter}
                title="平面の位置[mm]。数式も打てる"
              />
              <span class="dim">mm</span>
            </label>
            <label class="section-row section-invert">
              <input
                id="section-invert"
                type="checkbox"
                checked={section.inverted}
                onchange={(event) => onSectionInvert((event.target as HTMLInputElement).checked)}
              />
              反転
            </label>
          </div>
        {/if}
      </div>

      <div class="group">
        <div class="group-head">
          <span class="group-name">モデル</span>
          <span class="meta dim">{visibleCount}/{bodies.length} 表示</span>
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
            <li class="parts-row" class:hidden-row={!body.visible}>
              {#if renamingId === body.body_id}
                <input
                  class="rename-input"
                  type="text"
                  bind:value={draft}
                  use:focusSelect
                  onkeydown={(event) => renameKey(event, body.body_id, body.name)}
                  onblur={() => commitRename(body.body_id, body.name)}
                />
              {:else}
                <label class="parts-name">
                  <input
                    type="checkbox"
                    checked={body.visible}
                    onchange={(event) =>
                      onToggle(body.body_id, (event.target as HTMLInputElement).checked)}
                  />
                  {body.name}
                </label>
                <span class="dim parts-count">{body.face_count}面</span>
                <button
                  class="row-action"
                  onclick={() => startRename(body.body_id, body.name)}
                  title="名前を変える">✎</button
                >
                <button
                  class="row-action"
                  onclick={() => onDeleteBody(body.body_id)}
                  title="このボディを消す(Ctrl+Zで戻る)">✕</button
                >
              {/if}
            </li>
          {/each}
        </ul>
      </div>

      <div class="group">
        <div class="group-head">
          <span class="group-name">スケッチ</span>
          <span class="meta dim">{visibleSketchCount}/{sketches.length} 表示</span>
        </div>
        <div class="group-bulk">
          <button id="sketches-show-all" onclick={onShowAllSketches}>全表示</button>
          <button id="sketches-hide-all" onclick={onHideAllSketches}>全非表示</button>
        </div>
        <ul class="sketch-list" id="browser-sketches">
          {#if sketches.length === 0}
            <li class="empty dim">スケッチが無い</li>
          {/if}
          {#each sketches as sketch (sketch.sketch_id)}
            <li
              class="sketch-row"
              class:active={sketch.sketch_id === activeSketchId}
              class:row-active={sketch.sketch_id === activeSketchId}
              class:hidden-row={!sketch.visible}
              data-sketch-id={sketch.sketch_id}
            >
              <!-- 編集中のスケッチは隠せない(トグルを無効にする): 描いている最中に自分の図形が消えると、次のクリックが何を指すか分からなくなる。 -->
              <input
                type="checkbox"
                checked={sketch.visible}
                disabled={sketch.sketch_id === activeSketchId}
                onchange={(event) =>
                  onToggleSketch(sketch.sketch_id, (event.target as HTMLInputElement).checked)}
                title={sketch.sketch_id === activeSketchId
                  ? '編集中のスケッチは隠せない'
                  : sketch.visible
                    ? 'このスケッチを隠す'
                    : 'このスケッチを表示'}
              />
              {#if renamingId === sketch.sketch_id}
                <input
                  class="rename-input"
                  type="text"
                  bind:value={draft}
                  use:focusSelect
                  onkeydown={(event) => renameKey(event, sketch.sketch_id, sketchLabel(sketch))}
                  onblur={() => commitRename(sketch.sketch_id, sketchLabel(sketch))}
                />
              {:else}
                <button
                  class="sketch-name row-button"
                  onclick={() => onEnterSketch(sketch.sketch_id)}
                  title={`${planeLabel(sketch.plane)} — ` +
                    (sketch.visible
                      ? 'クリックでこのスケッチに入る'
                      : 'クリックで表示に戻してこのスケッチに入る')}
                >
                  {sketchLabel(sketch)}
                </button>
                {#if sketch.sketch_id === activeSketchId}
                  <span class="tag on">編集中</span>
                {:else}
                  <span class="dim sketch-count">図形{sketch.entities.length}</span>
                {/if}
                <button
                  class="row-action"
                  onclick={() => startRename(sketch.sketch_id, sketch.name ?? sketchLabel(sketch))}
                  title="名前を変える">✎</button
                >
                <button
                  class="row-action"
                  onclick={() => onDeleteSketch(sketch.sketch_id)}
                  title="このスケッチを図形ごと消す(押し出したボディは残る。Ctrl+Zで戻る)">✕</button
                >
              {/if}
            </li>
          {/each}
        </ul>
      </div>
    </div>
  {/if}
</aside>

<style>
  /* `.stage` が位置の基準(App.svelte で position:relative)。 */
  .browser-panel {
    position: absolute;
    top: calc(10px + var(--toolbar-overlay-h, 0px));
    left: 10px;
    z-index: 5;
    /* 幅は `styles.css` の `:root` が持つ(設定ダイアログの位置がこれを見る)。 */
    width: var(--browser-panel-w);
    /* 下端は同じ列の下に浮くパネル(`OptimizePanel.svelte` の `--browser-bottom-reserve`)の手前で止める。
       上端は verb の引数行のぶん下がっているので、そのぶんも引く。 */
    max-height: calc(100% - 20px - var(--toolbar-overlay-h, 0px) - var(--browser-bottom-reserve, 0px));
    display: flex;
    flex-direction: column;
    min-height: 0;
    background: color-mix(in oklab, var(--panel) 88%, transparent);
    border: 1px solid var(--line);
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
    /* 縮ませない: 縦のflexである `.browser-body` の中では既定の `flex-shrink: 1` が効いて区分が中身より低く潰れ、
       リストが枠の外へ溢れて下の区分と重なる。スクロールは `.browser-body` が1つ持つ。 */
    flex: 0 0 auto;
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
  }

  .section-controls {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 4px 8px;
  }

  .section-planes {
    display: flex;
    gap: 4px;
  }

  .section-planes button {
    flex: 1 1 auto;
  }

  .section-row {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .section-invert {
    cursor: pointer;
  }

  .section-row input[type='text'] {
    flex: 1 1 auto;
    min-width: 0;
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

  .parts-row.hidden-row,
  .sketch-row.hidden-row {
    color: var(--dim);
  }

  .sketch-row.hidden-row .sketch-name {
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
    font-size: var(--fs-meta);
  }

  .row-action {
    flex: 0 0 auto;
    padding: 0 4px;
    visibility: hidden;
  }

  .parts-row:hover .row-action,
  .sketch-row:hover .row-action,
  .row-action:focus-visible {
    visibility: visible;
  }

  .rename-input {
    flex: 1 1 auto;
    min-width: 0;
  }

  .empty {
    padding: 3px 8px;
  }
</style>
