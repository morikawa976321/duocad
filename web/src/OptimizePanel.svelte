<script lang="ts">
  import type { OptimizeNotice } from './optimizeNotice'
  /**
   * 形状最適化を**走らせる**パネル。
   *
   * 条件を置く4 verb はツールバーにあり、ここが受け持つのはその続き ——
   * **解像度を選ぶ / 走らせる / 進捗を見る / 中断する / 通知を読む**。
   *
   * ## 解像度の見せ方(ここが設計の肝)
   *
   * **要素サイズ[mm]を下限〜上限の範囲で自由に入れる。空欄が既定＝粗い側の端(上限)。**
   * 両端の性質が違う:
   *
   * - **粗い側の端は「正しさ」の境界** —— 割ると条件面から節点が消える。
   *   しかも**silent**(例外は出ず、もっともらしい形が返る)。上限より粗い入力はサーバが断る。
   * - **細かい側の端は「都合」の境界** —— 割っても間違いはしない。遅い、落ちる、それだけ。
   *
   * 入れた値の段(実格子へ揃えた要素サイズ・要素数・最小幅の段)は**サーバが作る**
   * (`optimize_resolution` に値を渡すと `selected` が返る) —— 要素数の数え方(余白比率とパッド)を
   * 画面が持つと必ずずれる。梯子(`steps`)は入力の候補ボタンとして出す。
   *
   * **所要時間は付かない**(計画書§6.2「所要時間は旧SIMP係数を使わない。初回は不明と
   * 表示する」)。走らせる前は1回もFEMを解いていないので実測が無く、旧SIMP係数を使った
   * 桁の見当も出さない——段の選択は要素数だけを見て行う。密度フィルタ半径(最小部材幅)の
   * 入力欄も廃止した(二値探索は密度フィルタを持たない、計画書§6.2)。
   *
   * ## 通知をここに残す理由
   *
   * 「荷重面に格子節点が付かなかった」「保持ソリッドがこの解像度では表現できていない」は
   * **結果が信用できないという意味**である。トースト(`Toast.svelte`)は時間で消える設計
   * なので、これを流すと見落とす。**次に走らせるまでパネルに残す**(トーストにも同じ文が
   * 出るが、あちらは気づかせる役、ここが読み返す役)。
   *
   * ## 状態の正は `App.svelte`
   *
   * ここは値と口を受け取るだけの presentational な部品(`SettingsDialog.svelte` と同じ規律)。
   * **`position: absolute` でフローの外**に置くので、`.viewport` の寸法＝カメラのアスペクトに
   * 触らない(styles.css の不変条件)。
   */
  import { chosenStep, shownElementSizeRange, shownMinWidthMm, stepCells } from './minWidth'
  import type {
    OptimizeConditionsReport,
    OptimizeProgress,
    OptimizeResolutionPayload,
  } from './session'

  const {
    conditions,
    resolution,
    elementSizeMm,
    minWidthMm,
    running,
    cancelling,
    progress,
    notice,
    onElementSize,
    onMinWidth,
    onRun,
    onCancel,
  }: {
    conditions: OptimizeConditionsReport | null
    resolution: OptimizeResolutionPayload | null
    /** 要素サイズの入力値[mm]。`null` = 空欄(既定 = 上限側)。 */
    elementSizeMm: number | null
    /** 選んでいる最小幅の段[mm](`min_width_steps_mm` のどれか)。`null` = 既定(いちばん細い段)。 */
    minWidthMm: number | null
    running: boolean
    cancelling: boolean
    progress: OptimizeProgress | null
    notice: OptimizeNotice | null
    onElementSize: (value: number | null) => void
    onMinWidth: (value: number | null) => void
    onRun: () => void
    onCancel: () => void
  } = $props()

  const steps = $derived(resolution?.steps ?? [])
  /** 効いている段(`chosenStep`。App と同じ判定)。入力した値への応答待ち・範囲外なら `null`。 */
  const step = $derived(chosenStep(resolution, elementSizeMm))
  const elementSizeRange = $derived(resolution === null ? null : shownElementSizeRange(resolution))
  /** 要素サイズの欄の文字列(最小幅の欄と同じく、数値から欄へは外から変わったときだけ書き戻す)。 */
  let elementSizeText = $state('')
  let elementSizeSent: number | null | undefined = undefined
  $effect.pre(() => {
    const value = elementSizeMm
    if (value === elementSizeSent) return
    elementSizeSent = value
    elementSizeText = value === null ? '' : String(value)
  })
  /** 要素サイズの欄が数として読めない(空欄は読める = 既定)。 */
  const elementSizeUnreadable = $derived(elementSizeText.trim() !== '' && !Number.isFinite(Number(elementSizeText)))
  /** 入れた値をサーバが断った理由(上限より粗い・下限より細かい)。応答がまだその値のものでなければ出さない。 */
  const elementSizeProblem = $derived(
    elementSizeMm !== null && resolution?.requested_element_size_mm === elementSizeMm
      ? (resolution.selected_error ?? null)
      : null,
  )

  function typeElementSize(raw: string): void {
    elementSizeText = raw
    const parsed = raw.trim() === '' ? null : Number(raw)
    if (parsed !== null && !Number.isFinite(parsed)) return // 読めない間は送らない(欄が赤くなり実行も止まる)
    elementSizeSent = parsed
    onElementSize(parsed)
  }
  /** 選べる最小幅の段(サーバーが配る。`optimize_run.min_width_cell_range`)。空なら この解像度では選べない。 */
  const minWidthSteps = $derived(step?.min_width_steps_mm ?? [])
  /** 効いている最小幅(`null` は既定 = いちばん細い段)。 */
  const minWidthShown = $derived(minWidthMm ?? minWidthSteps[0] ?? null)

  /**
   * パネルの実高さ(+ブラウザとの間隔10px)を `.stage` の `--browser-bottom-reserve` へ渡す。
   *
   * ブラウザ(`BrowserPanel.svelte`)は同じ列の上端に浮いており、こちらが重なり順で上にある。
   * 高さを知らせないと、伸びたこちらがブラウザの下の行を覆って押せなくする(実測: 窓の高さ860pxで
   * パネルが458pxになり、スケッチの行の中央に見出しが重なって、スケッチ名のクリックが吸われた)。
   */
  let panelEl = $state<HTMLDivElement | null>(null)
  $effect(() => {
    const el = panelEl
    const stage = el?.parentElement
    if (!el || !stage) return
    const sync = () =>
      stage.style.setProperty('--browser-bottom-reserve', `${Math.ceil(el.getBoundingClientRect().height) + 10}px`)
    sync()
    const observer = new ResizeObserver(sync)
    observer.observe(el)
    return () => {
      observer.disconnect()
      stage.style.removeProperty('--browser-bottom-reserve')
    }
  })

  const problems = $derived(conditions?.problems ?? [])
  /** 走らせられるか。**条件と目盛りの両方がそろって初めて押せる。** */
  const canRun = $derived(
    !running &&
      conditions !== null &&
      conditions.ready &&
      step !== null &&
      minWidthSteps.length > 0 &&
      !elementSizeUnreadable,
  )
</script>

<div id="optimize-panel" class="floating-panel" bind:this={panelEl}>
  <div class="head"><strong>形状最適化を走らせる</strong></div>

  <!-- **条件の点検はいちばん上**。走らせられない理由があるなら、入力欄を触る前に読ませる。
       文はサーバが作る(`conditions_report` の `problems`)—— **この版は拘束面1枚・
       荷重面1枚だけ**という制限もここに文として現れる。 -->
  {#if conditions === null}
    <p id="optimize-conditions" class="hint dim">条件を確認している…</p>
  {:else if conditions.ready}
    <p id="optimize-conditions" class="hint ok dim">
      条件はそろっている(保持 {conditions.preserve_bodies.length} ・除外
      {conditions.exclude_bodies.length} ・拘束 {conditions.fixtures.length} ・荷重
      {conditions.loads.length})
    </p>
  {:else}
    <ul id="optimize-conditions" class="problems">
      {#each problems as problem}
        <li>{problem}</li>
      {/each}
    </ul>
  {/if}

  <!-- **要素サイズは下限〜上限の自由入力。空欄が既定(上限＝いちばん粗い)。**
       段が1つも無い＝「間違わない解像度」が作れない状態なので、入力欄ごと出さずに理由を出す。 -->
  {#if resolution === null}
    <p id="optimize-resolution-note" class="hint dim">解像度を確認している…</p>
  {:else if resolution.error}
    <p id="optimize-resolution-note" class="hint bad">{resolution.error}</p>
  {:else if steps.length === 0}
    <p id="optimize-resolution-note" class="hint bad">
      この部品と条件面では、間違わない解像度が設計空間の要素数の上限({resolution.max_design_elements?.toLocaleString() ?? '—'})に
      収まらない(要素サイズの上限は条件面からの要求と {resolution.max_element_size_cap_mm ?? '—'}mm の細かい方)。
      条件面が小さいなら大きい面に取り直し、部品が大きいなら設計空間を小さくすること
    </p>
  {:else}
    <label class="field-row">
      要素サイズ[mm]
      <!-- **`type="number"` にしない**(最小幅の欄と同じ理由。打ちかけの「1.」が空文字になる)。 -->
      <input
        id="optimize-resolution"
        type="text"
        inputmode="decimal"
        autocomplete="off"
        placeholder={`既定 ${steps[0].element_size_mm.toFixed(2)}`}
        class:invalid={elementSizeUnreadable || elementSizeProblem !== null}
        value={elementSizeText}
        disabled={running}
        oninput={(event) => typeElementSize(event.currentTarget.value)}
      />
    </label>
    <p id="optimize-resolution-range" class="hint dim">
      {elementSizeRange?.lower ?? '—'}〜{elementSizeRange?.upper ?? '—'}mm。空欄なら既定(上限側、いちばん粗くて安い)。候補:
      {#each steps as candidate}
        <button
          type="button"
          class="step"
          disabled={running}
          onclick={() => {
            elementSizeSent = candidate.element_size_mm
            elementSizeText = String(candidate.element_size_mm)
            onElementSize(candidate.element_size_mm)
          }}>{candidate.element_size_mm.toFixed(2)}</button
        >
      {/each}
    </p>
    {#if elementSizeUnreadable}
      <p id="optimize-resolution-note" class="hint bad">要素サイズ「{elementSizeText}」が数として読めない</p>
    {:else if elementSizeProblem !== null}
      <p id="optimize-resolution-note" class="hint bad">{elementSizeProblem}</p>
    {/if}
    <p id="optimize-estimate" class="hint dim">
      {#if step === null && !elementSizeUnreadable && elementSizeProblem === null}
        この値の格子を確認している…
      {:else}
        実際の要素 {step ? step.element_size_mm.toFixed(2) : '—'}mm(格子の刻みに揃えた値) ・ 設計空間 約
        {step ? step.design_elements_estimate.toLocaleString() : '—'} 要素(見積もり。上限
        {resolution.max_design_elements?.toLocaleString() ?? '—'})。
      {/if}
      所要時間は事前に見積もらない(実測が無い段階では出さない。走らせると進捗に出る)。
    </p>
    <p class="hint dim">
      <strong>細かくするほど得られるもの</strong>: STLの表面が滑らかになり、応力(＝安全率)と
      体積の見積もりが確かになる。<strong>払うもの</strong>: 時間とメモリ。
      <strong>上限より粗くはできない</strong> —— 条件面から節点が消える(しかも黙って起きる)か、完成形の安全率が測れなくなる。
    </p>
    <label class="field-row">
      最小幅[mm]
      <!-- **段から選ぶだけ**(数値は打たせない)。段は 2mm 以上で最小の m から、格子の最短の辺の 1/4 まで
           (`optimize_run.min_width_cell_range`。根拠の無い暫定値)。 -->
      <select
        id="optimize-min-width"
        disabled={running || minWidthSteps.length === 0}
        value={minWidthShown === null ? '' : String(minWidthShown)}
        onchange={(event) => onMinWidth(Number(event.currentTarget.value))}
      >
        {#each minWidthSteps as value, index}
          <option value={String(value)}
            >{shownMinWidthMm(value)}(m={stepCells(step!, value)}){index === 0 ? ' 既定' : ''}</option
          >
        {/each}
      </select>
    </label>
    {#if step !== null && minWidthSteps.length === 0}
      <p id="optimize-min-width-note" class="hint bad">
        この要素サイズでは選べる最小幅が無い(2mm 以上の段が、格子の最短の辺の 1/4 に収まらない)。要素サイズを下げること
      </p>
    {:else}
      <p id="optimize-min-width-note" class="hint dim">
        探索が守る立方の幅(要素サイズ × m。全セルがこの幅の立方に属するという規則で、部材の太さの保証ではない。
        完成形のつなぎ目の幅は実行後に診断する)
      </p>
    {/if}
  {/if}

  <div class="row">
    <button id="optimize-run" disabled={!canRun} onclick={onRun}>実行</button>
    <button id="optimize-cancel" disabled={!running || cancelling} onclick={onCancel}>
      {cancelling ? '中断中…' : '中断'}
    </button>
  </div>

  <!-- **進捗**。局面(prepare/derive/search/mesh/finalize/insert)ごとに届く
       (`20260904-1704` の契約①)。探索の間は評価(FEM 1回)ごとに1件届く。
       走っていないときは行ごと消す —— 前回の最後の1件を残すと、走っているように見える。 -->
  {#if running}
    <p id="optimize-progress" class="hint">
      {progress === null ? '準備中…' : `[${progress.phase}] ${progress.message}`}
      {#if cancelling}
        <br />中断を伝えた。次に進捗が出た時点で降りる(探索中は走っている評価が終わった時点。
        塊を起こす段の中では止まらないことがある)。
      {/if}
    </p>
  {/if}

  <!-- **消えない通知**(次に走らせるまで残る)。結果が信用できないという意味の文を
       トーストで流さないための場所。 -->
  {#if notice !== null}
    <ul id="optimize-notice" class={notice.kind === 'bad' ? 'problems' : 'done dim'}>
      {#each notice.lines as line}
        <li>{line}</li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  /* **ビューポート左のブラウザ(`BrowserPanel.svelte`)と同じ列の下端**に出す。

     **新しい帯を画面に取らない**のが位置の決め手である —— `viewport.project()` は
     `clickable(x, y)` で「浮いているパネルに覆われていないか」を見ており(実機検査が
     面をクリックできるかどうかもこれで決まる)、**ブラウザの右に320pxの帯を足した版では
     左のボディの面が1つも指せなくなった**(`smoke-optimize-mode.mjs` が検出)。条件を
     置くのはこのモードの仕事そのものなので、モデルを覆う位置には置けない。

     `SettingsDialog` がブラウザの右へ出るのは、開いている時間が短い一時的なダイアログ
     だからである。こちらはモードのあいだ出しっぱなしなので同じ扱いにできない。

     `position: absolute` はフローの外 —— `.viewport` の寸法＝カメラのアスペクトには
     触らない(styles.css の不変条件)。重なり順はブラウザ(5)より上、設定(10)より下。 */
  #optimize-panel {
    position: absolute;
    left: 10px;
    bottom: 10px;
    z-index: 9;
    width: var(--browser-panel-w);
    max-height: 62%;
    overflow-y: auto;
  }

  .head {
    margin-bottom: 6px;
  }

  /* 並べ方は styles.css の `.field-row`。ここは行の間隔だけ。 */
  label {
    margin-top: 6px;
  }

  /* 幅・面は styles.css の `input` が持つ。ここは残り幅いっぱいに伸ばすだけ。 */
  #optimize-resolution {
    flex: 1 1 auto;
    width: auto;
    min-width: 0;
  }

  .row {
    display: flex;
    gap: 6px;
    margin-top: 8px;
  }

  .hint {
    margin: 6px 0 0;
  }

  /* **走らせられない理由・結果が信用できない理由**。トーストと違って消えない。 */
  .problems,
  .done {
    margin: 6px 0 0;
    padding-left: 1.1em;
    font-size: var(--fs-meta);
  }

  .problems {
    color: var(--bad);
  }

  .bad {
    color: var(--bad);
  }

  button.step {
    padding: 0 6px;
    margin-right: 2px;
  }

  #optimize-resolution.invalid {
    border-color: var(--bad);
  }
</style>
