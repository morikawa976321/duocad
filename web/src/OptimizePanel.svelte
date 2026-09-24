<script module lang="ts">
  /**
   * パネルに残す通知1件(次に走らせるまで消えない)。**型だけをモジュールへ置く** ——
   * runesモードのインスタンススクリプトは `export` を持てない(`$props` が入口)。
   */
  export interface OptimizeNotice {
    /** `bad` は「結果が信用できない/走らせられない」、`info` は済んだことの報告。 */
    kind: 'bad' | 'info'
    lines: string[]
  }
</script>

<script lang="ts">
  /**
   * 形状最適化を**走らせる**パネル。
   *
   * 条件を置く4 verb はツールバーにあり、ここが受け持つのはその続き ——
   * **解像度を選ぶ / 走らせる / 進捗を見る / 中断する / 通知を読む**。
   *
   * ## 解像度の見せ方(ここが設計の肝)
   *
   * **「範囲内で自由に選ぶ」ではなく、「既定＝粗い側の端。細かくする方向にだけ動かす」。**
   * 両端の性質が違うからである:
   *
   * - **粗い側の端は「正しさ」の境界** —— 割ると条件面から節点が消える。
   *   しかも**silent**(例外は出ず、もっともらしい形が返る)。
   * - **細かい側の端は「都合」の境界** —— 割っても間違いはしない。遅い、落ちる、それだけ。
   *
   * したがってスライダーの左端(添字0)が既定であり、**そこより粗い側へ動かす操作を
   * 用意しない**。目盛りの実体は `optimize_run.resolution_limits()["steps"]` で、
   * **段はサーバが作る** —— 要素数の数え方(余白比率とパッド)を画面が持つと必ずずれる。
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
  import { judgeMinWidth, minWidthProblem, roundUpNotice, shownCeilingMm, shownGuaranteedMm, shownStepMm } from './minWidth'
  import type {
    OptimizeConditionsReport,
    OptimizeProgress,
    OptimizeResolutionPayload,
  } from './session'

  const {
    conditions,
    resolution,
    stepIndex,
    minWidthMm,
    running,
    cancelling,
    progress,
    notice,
    onStep,
    onMinWidth,
    onRun,
    onCancel,
  }: {
    conditions: OptimizeConditionsReport | null
    resolution: OptimizeResolutionPayload | null
    stepIndex: number
    /** 最小幅の入力値[mm]。`null` = 空欄(サーバーの既定 = 要素サイズの2倍と1mmの大きい方を使う)。
     *  正の値そのもの(下限割れの判定はここではしない)。 */
    minWidthMm: number | null
    running: boolean
    cancelling: boolean
    progress: OptimizeProgress | null
    notice: OptimizeNotice | null
    onStep: (index: number) => void
    onMinWidth: (value: number | null) => void
    onRun: () => void
    onCancel: () => void
  } = $props()

  const steps = $derived(resolution?.steps ?? [])
  /** 実際に効いている段の位置。**目盛りが縮んだときのために毎回丸め直す。** */
  const clamped = $derived(Math.min(stepIndex, Math.max(steps.length - 1, 0)))
  const step = $derived(steps[clamped] ?? null)
  /** 入力した最小幅の判定(下限・上限はサーバーが段ごとに配る。判定と表示は `minWidth.ts`)。 */
  const minWidthVerdict = $derived(judgeMinWidth(minWidthMm, step))
  const minWidthInvalid = $derived(minWidthVerdict.kind === 'below' || minWidthVerdict.kind === 'above')
  const minWidthProblemText = $derived(step === null ? null : minWidthProblem(minWidthVerdict, step))
  const minWidthGuaranteedText = $derived(shownGuaranteedMm(minWidthVerdict))
  const minWidthRoundUpText = $derived(roundUpNotice(minWidthVerdict, minWidthMm))
  const minWidthLowerStep = $derived(minWidthVerdict.kind === 'ok' ? minWidthVerdict.lowerStepMm : null)
  const minWidthCells = $derived(minWidthVerdict.kind === 'ok' ? minWidthVerdict.cells : null)
  const minWidthUpperStep = $derived(minWidthVerdict.kind === 'ok' ? minWidthVerdict.stepMm : null)
  /**
   * 最小幅の欄に**打っている文字列そのもの**。数値(`minWidthMm`)から欄へは書き戻さない —— 書き戻すと
   * 「1.」→「1.0」と打つ途中で欄が「1」に化け、続けて打った「5」で「15」になる(Blink の
   * `type=number` は打ちかけの「1.」を空文字として返す)。欄を書き換えるのは、値が外から変わった
   * とき(段のボタン・文書の切り替え)だけ。
   */
  let minWidthText = $state('')
  /** 欄から最後に送った値。`minWidthMm` がこれと違えば外から変わったので欄を書き直す。 */
  let minWidthSent: number | null | undefined = undefined
  $effect.pre(() => {
    const value = minWidthMm
    if (value === minWidthSent) return
    minWidthSent = value
    minWidthText = value === null ? '' : String(value)
  })
  /** 欄の文字列が数として読めない(空欄は読める = 既定)。 */
  const minWidthUnreadable = $derived(minWidthText.trim() !== '' && !Number.isFinite(Number(minWidthText)))

  function typeMinWidth(raw: string): void {
    minWidthText = raw
    const parsed = raw.trim() === '' ? null : Number(raw)
    const value = parsed !== null && Number.isFinite(parsed) ? parsed : null
    minWidthSent = value
    onMinWidth(value)
  }

  /** 段のボタンに並べる先頭の数(全段はサーバーが配る。残りは入力欄に打つか、切り上げ先の表示から辿る)。 */
  const STEP_BUTTONS = 4
  const problems = $derived(conditions?.problems ?? [])
  /** 走らせられるか。**条件と目盛りの両方がそろって初めて押せる。** */
  const canRun = $derived(
    !running && conditions !== null && conditions.ready && step !== null && !minWidthInvalid && !minWidthUnreadable,
  )
</script>

<div id="optimize-panel">
  <div class="head"><strong>形状最適化を走らせる</strong></div>

  <!-- **条件の点検はいちばん上**。走らせられない理由があるなら、入力欄を触る前に読ませる。
       文はサーバが作る(`conditions_report` の `problems`)—— **この版は拘束面1枚・
       荷重面1枚だけ**という制限もここに文として現れる。 -->
  {#if conditions === null}
    <p id="optimize-conditions" class="hint dim">条件を確認している…</p>
  {:else if conditions.ready}
    <p id="optimize-conditions" class="hint ok">
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

  <!-- **目盛りは細かくする方向にだけ動く**(左端＝既定＝いちばん粗くて安い)。
       段が1つも無い＝「間違わない解像度」が作れない状態なので、スライダーごと出さずに
       理由を出す。 -->
  {#if resolution === null}
    <p id="optimize-resolution-note" class="hint dim">解像度を確認している…</p>
  {:else if resolution.error}
    <p id="optimize-resolution-note" class="hint bad">{resolution.error}</p>
  {:else if steps.length === 0}
    <p id="optimize-resolution-note" class="hint bad">
      この部品と条件面では、間違わない解像度が総要素数の上限({resolution.max_elements ?? '—'})に
      収まらない。条件面をもっと大きい面に取り直すこと
    </p>
  {:else}
    <label>
      解像度
      <input
        id="optimize-resolution"
        type="range"
        min="0"
        max={steps.length - 1}
        step="1"
        value={clamped}
        disabled={running}
        oninput={(event) => onStep(Number.parseInt(event.currentTarget.value, 10))}
      />
      <span class="unit">{clamped === 0 ? '既定(粗い)' : `+${clamped}`}</span>
    </label>
    <p id="optimize-estimate" class="hint dim">
      要素 {step ? step.element_size_mm.toFixed(2) : '—'}mm ・ 格子
      {step ? step.element_count.toLocaleString() : '—'} 要素。
      所要時間は事前に見積もらない(実測が無い段階では出さない。走らせると進捗に出る)。
    </p>
    <p class="hint dim">
      左端が既定 —— 条件面を満たす、いちばん粗くて安い設定である。
      <strong>右へ動かすほど得られるもの</strong>: STLの表面が滑らかになり、応力(＝安全率)と
      体積の見積もりが確かになる。<strong>払うもの</strong>: 時間とメモリ。
      <strong>左へは動かない</strong> —— 粗くすると条件面から節点が消える(しかも黙って起きる)。
    </p>
    <label>
      最小幅[mm]
      <!-- **`type="number"` にしない**(上の `minWidthText`)。数字の鍵盤は `inputmode` で出す。 -->
      <input
        id="optimize-min-width"
        type="text"
        inputmode="decimal"
        autocomplete="off"
        placeholder="既定"
        list="optimize-min-width-list"
        class:invalid={minWidthInvalid || minWidthUnreadable}
        value={minWidthText}
        disabled={running}
        oninput={(event) => typeMinWidth(event.currentTarget.value)}
      />
    </label>
    <p id="optimize-min-width-default-hint" class="hint dim">
      空欄なら既定(要素サイズの2倍と1mmの大きい方)を使う
    </p>
    <datalist id="optimize-min-width-list">
      {#each step?.min_width_steps_mm ?? [] as value, index}
        <option {value} label="m={index + 2}"></option>
      {/each}
    </datalist>
    <!-- ボタンは丸めない値を入れる(`minWidth.ts` の `shownStepMm`)。 -->
    {#if step !== null && step.min_width_steps_mm.length > 0}
      <p id="optimize-min-width-steps" class="hint dim">
        保証できる幅は段になる(要素サイズ × m):
        {#each step.min_width_steps_mm.slice(0, STEP_BUTTONS) as value, index}
          <button
            type="button"
            class="step"
            disabled={running}
            onclick={() => onMinWidth(value)}>{shownStepMm(value)}mm(m={index + 2})</button
          >
        {/each}
        {#if step.min_width_steps_mm.length > STEP_BUTTONS}…{/if}
        上限 {shownCeilingMm(step)}mm
      </p>
    {/if}
    {#if minWidthUnreadable}
      <p id="optimize-min-width-note" class="hint bad">最小幅「{minWidthText}」が数として読めない</p>
    {:else if minWidthProblemText !== null}
      <p id="optimize-min-width-note" class="hint bad">
        {minWidthProblemText}。{minWidthVerdict.kind === 'below'
          ? '値を上げるか、解像度を細かくすること'
          : '値を下げるか、解像度を粗くすること'}
      </p>
    {:else if minWidthRoundUpText !== null}
      <p id="optimize-min-width-note" class="hint bad">
        {minWidthRoundUpText}。
        {#if minWidthLowerStep !== null}
          <button
            type="button"
            class="step"
            disabled={running}
            onclick={() => onMinWidth(minWidthLowerStep)}
            >{shownStepMm(minWidthLowerStep)}mm(m={(minWidthCells ?? 0) - 1})に下げる</button
          >
        {/if}
        {#if minWidthUpperStep !== null}
          <button
            type="button"
            class="step"
            disabled={running}
            onclick={() => onMinWidth(minWidthUpperStep)}
            >{shownStepMm(minWidthUpperStep)}mm(m={minWidthCells})にそろえる</button
          >
        {/if}
      </p>
    {:else if minWidthGuaranteedText !== null}
      <p id="optimize-min-width-note" class="hint dim">
        実際に保証される幅: {minWidthGuaranteedText}mm(探索する形の幅で、書き出す形は平滑化で変わる)
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
    <ul id="optimize-notice" class={notice.kind === 'bad' ? 'problems' : 'done'}>
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
    padding: 8px 10px;
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--line);
  }

  .head {
    margin-bottom: 6px;
  }

  label {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 6px;
    margin-top: 6px;
    color: var(--dim);
    white-space: nowrap;
  }

  /* 幅・面は styles.css の `input` が持つ。ここは残り幅いっぱいに伸ばすだけ。 */
  #optimize-resolution {
    flex: 1 1 auto;
    width: auto;
    min-width: 0;
  }

  #optimize-resolution {
    padding: 0;
    accent-color: var(--accent);
  }

  .unit {
    flex: 0 0 auto;
    width: 6em;
    text-align: right;
    color: var(--text);
  }

  .row {
    display: flex;
    gap: 6px;
    margin-top: 8px;
  }

  .hint {
    margin: 6px 0 0;
  }

  .ok {
    color: var(--dim);
  }

  /* **走らせられない理由・結果が信用できない理由**。トーストと違って消えない。 */
  .problems,
  .done {
    margin: 6px 0 0;
    padding-left: 1.1em;
    font-size: var(--fs-meta);
  }

  .problems {
    color: oklch(72% 0.14 30);
  }

  .done {
    color: var(--dim);
  }

  .bad {
    color: oklch(72% 0.14 30);
  }

  button.step {
    padding: 0 6px;
    margin-right: 2px;
  }

  #optimize-min-width.invalid {
    border-color: oklch(72% 0.14 30);
  }
</style>
