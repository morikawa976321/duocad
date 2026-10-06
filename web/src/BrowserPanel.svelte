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
   * ブラウザ — **ビューポートの左上に浮かぶ、モデルの中身の一覧**(Fusion 360の
   * 「ブラウザ」に対応する場所)。
   *
   * **状態はコアが持つ**。ここは `bodies`(コアの `state.bodies` のうち `kind==="solid"`)と
   * `sketches`(コアの `state.sketches`)を描くだけで、可視性(ボディもスケッチも)も
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
    /** ユーザーに見せるボディ(常に `kind==="solid"`)。 */
    bodies: BodyMeta[]
    sketches: SketchState[]
    /** 編集中のスケッチ(無ければ null)。行に「編集中」の印を出すのに使う。 */
    activeSketchId: string | null
    /** 常設の原点ギズモ(原点・3軸・3平面)が出ているか。**文書の状態ではなく画面設定**
     * (`viewSettings.ts` がlocalStorageへ持つ)なので、ボディと違い可視性はここが持つ
     * 値をそのまま映すだけでよい。 */
    originVisible: boolean
    onToggle: (bodyId: string, visible: boolean) => void
    onShowAll: () => void
    onHideAll: () => void
    /** 行のクリックでそのスケッチに入る(再開)。**隠したスケッチも入れる**(入る側が表示へ戻す)。 */
    onEnterSketch: (sketchId: string) => void
    /** スケッチの表示/非表示(ボディの `onToggle` と同じ形。コアの `set_sketch_visible`)。 */
    onToggleSketch: (sketchId: string, visible: boolean) => void
    onShowAllSketches: () => void
    onHideAllSketches: () => void
    /** 原点ギズモの表示/非表示(7項目まとめて1つのトグル。本人指定)。 */
    onToggleOrigin: (visible: boolean) => void
    /** 行の名前の確定(コアの `rename`。ボディもスケッチも同じ口)。 */
    onRename: (target: string, name: string) => void
    /** 行の ✕(コアの `delete_items`)。**確認は挟まない** —— 1回の Ctrl+Z で戻る。 */
    onDeleteBody: (bodyId: string) => void
    onDeleteSketch: (sketchId: string) => void
    /** 断面表示(issue #6)。原点と同じく画面側の状態で、App が持つ。 */
    section: SectionState
    onSectionToggle: (on: boolean) => void
    onSectionPlane: (plane: BasePlaneName) => void
    /** 位置の欄の確定。数式のまま渡す(評価と失敗のトーストは App)。受け付けたら true。 */
    onSectionPosition: (text: string) => boolean
    onSectionInvert: (inverted: boolean) => void
  } = $props()

  const SECTION_PLANES: readonly BasePlaneName[] = ['XY', 'XZ', 'YZ']

  /** 位置の欄のラベルは平面の法線軸。 */
  function sectionAxis(plane: BasePlaneName): string {
    return plane === 'XY' ? 'Z' : plane === 'XZ' ? 'Y' : 'X'
  }

  /** ドラッグの端数は見せない(打った値はそのまま残る)。 */
  function formatPosition(value: number): string {
    return String(Number(value.toFixed(2)))
  }

  function commitPosition(event: Event): void {
    const input = event.target as HTMLInputElement
    // 同じ値に評価される式(`10/2` → 5 のまま)では Svelte が欄を書き直さないので、自分で戻す。
    // 失敗したら打った式を残す(直して打ち直せるように)。
    if (onSectionPosition(input.value)) input.value = formatPosition(section.position)
  }

  function blurOnEnter(event: KeyboardEvent): void {
    if (event.key === 'Enter') (event.target as HTMLInputElement).blur()
  }

  /** パネル全体の折りたたみ。ビューポートに重なるので、畳めないと形が隠れる。 */
  let open = $state(true)

  /** 名前を書き換えている行の ID(無ければ null)。一度に1行だけ。**下書きは別の状態に持つ** ——
   *  `{id, draft}` を1つにして `bind:value={draft}` にすると、欄を閉じた瞬間に
   *  消えかけの欄の束縛が null を読んで TypeError を投げる(実機で確認)。 */
  let renamingId = $state<string | null>(null)
  let draft = $state('')

  function startRename(id: string, current: string): void {
    draft = current
    renamingId = id
  }

  /** 確定。**空・今と同じなら送らずに閉じる**(何も変えない行をログに積まない)。それ以外の
   *  不正(前後の空白・改行)はコアが断り、エラーの札に出る。
   *
   *  **`id` で自分の行かを確かめる** —— Enter で閉じた欄の blur は DOM から外れるときに遅れて届き、
   *  その間に別の行の ✎ が押されていると、相手の行の下書きを自分の名前で確定してしまう。 */
  function commitRename(id: string, current: string): void {
    if (renamingId !== id) return
    renamingId = null
    if (draft.trim() === '' || draft === current) return
    onRename(id, draft)
  }

  /** **Esc は窓まで届かせない** —— `CommandHost` の窓の keydown が入力欄の Esc も拾って、
   *  起動中のコマンドまで中断する。 */
  function renameKey(event: KeyboardEvent, id: string, current: string): void {
    if (event.key === 'Enter') commitRename(id, current)
    else if (event.key === 'Escape') {
      event.stopPropagation()
      renamingId = null
    }
  }

  /** 開いた欄へ入力を移す(Svelte の action)。 */
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
      <!-- 区分0: 原点(issue #8)。原点・3軸・3平面の7項目を1つのトグルでまとめて出し入れする
           (本人指定「7つまとめて」) -->
      <div class="group">
        <div class="group-head">
          <span class="group-name">原点</span>
          <input
            id="origin-visible-toggle"
            type="checkbox"
            class="visible-toggle"
            checked={originVisible}
            onchange={(event) => onToggleOrigin((event.target as HTMLInputElement).checked)}
            title={originVisible ? '原点・軸・平面を隠す' : '原点・軸・平面を表示'}
          />
        </div>
      </div>

      <!-- 区分0b: 断面(issue #6)。ON のあいだだけ平面・位置・反転の操作子を出す。
           状態は App が持つ画面側のもので、文書には載らない。 -->
      <div class="group">
        <div class="group-head">
          <span class="group-name">断面</span>
          <input
            id="section-toggle"
            type="checkbox"
            class="visible-toggle"
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
                class="visible-toggle"
                checked={section.inverted}
                onchange={(event) => onSectionInvert((event.target as HTMLInputElement).checked)}
              />
              反転
            </label>
          </div>
        {/if}
      </div>

      <!-- 区分1: モデル(ボディ)。可視性の切り替えはここが持つ -->
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
            <li class="parts-row" class:hidden-row={!body.visible} data-body-id={body.body_id}>
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
                    class="visible-toggle"
                    checked={body.visible}
                    onchange={(event) =>
                      onToggle(body.body_id, (event.target as HTMLInputElement).checked)}
                  />
                  {body.name}
                </label>
                <span class="dim parts-count">{body.face_count}面</span>
                <button
                  class="row-action rename-button"
                  onclick={() => startRename(body.body_id, body.name)}
                  title="名前を変える">✎</button
                >
                <button
                  class="row-action delete-button"
                  onclick={() => onDeleteBody(body.body_id)}
                  title="このボディを消す(Ctrl+Zで戻る)">✕</button
                >
              {/if}
            </li>
          {/each}
        </ul>
      </div>

      <!-- 区分2: スケッチ。名前のクリックでそのスケッチに入る。表示/非表示のトグルは
           ボディの行と同じ `input.visible-toggle`(issue #7)。 -->
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
              <!-- **編集中のスケッチは隠せない**(トグルを無効にする)。描いている最中に
                   自分の図形が消えると、次のクリックが何を指すか分からなくなる。 -->
              <input
                type="checkbox"
                class="visible-toggle"
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
                  class="row-action rename-button"
                  onclick={() => startRename(sketch.sketch_id, sketch.name ?? sketchLabel(sketch))}
                  title="名前を変える">✎</button
                >
                <button
                  class="row-action delete-button"
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
  /* ビューポートの左上に浮かせる。`.stage` が位置の基準(App.svelte で position:relative)。
     高さは中身なりだが上限を持つ —— 27部品を並べると画面の下まで届いてしまう。 */
  .browser-panel {
    position: absolute;
    top: calc(10px + var(--toolbar-overlay-h, 0px));
    left: 10px;
    z-index: 5;
    /* 幅は `styles.css` の `:root` が持つ(設定ダイアログの位置がこれを見るため)。 */
    width: var(--browser-panel-w);
    /* 下端は、同じ列の下に浮くパネル(`OptimizePanel.svelte` の `--browser-bottom-reserve`)の手前で
       止める。上端は verb の引数行のぶん下がっているので、そのぶんも引く。 */
    max-height: calc(100% - 20px - var(--toolbar-overlay-h, 0px) - var(--browser-bottom-reserve, 0px));
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
    /* **縮ませない**。縦のflexである `.browser-body` の中では既定の `flex-shrink: 1` が効き、
       区分が中身より低く潰れる。潰れてもリストは隠れず枠の外へ溢れるので、下の区分の
       見出し(「スケッチ」)と行が重なって見える。スクロールは `.browser-body` が1つ持つ。 */
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

  /* 面数・図形数は**本文に添える情報**なので `--fs-meta`(styles.css)。
     名前と同じ列に2つの大きさが並ぶのがここでの正しい使い方。 */
  .parts-count,
  .sketch-count {
    flex: 0 0 auto;
    font-size: var(--fs-meta);
  }

  /* 行の ✎ / ✕ は**指したときだけ出す**(常に出すと名前の幅が削られ、27部品の一覧が読みにくい)。
     `visibility` で隠すので幅は常に取っておく —— 出し入れで名前が左右に揺れない。 */
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

  /* 大きさは styles.css の `.empty` が持つ(--fs-meta)。 */
  .empty {
    padding: 3px 8px;
  }
</style>
