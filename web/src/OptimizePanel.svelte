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
   * 形状最適化を**走らせる**パネル(2026-09-05 `20260904-1705`)。
   *
   * 条件を置く4 verb はツールバーにあり、ここが受け持つのはその続き ——
   * **最小部材幅を決める / 解像度を選ぶ / 走らせる / 進捗を見る / 中断する / 通知を読む**。
   *
   * ## 解像度の見せ方(2026-09-04本人了承。ここが設計の肝)
   *
   * **「範囲内で自由に選ぶ」ではなく、「既定＝粗い側の端。細かくする方向にだけ動かす」。**
   * 両端の性質が違うからである:
   *
   * - **粗い側の端は「正しさ」の境界** —— 割ると条件面から節点が消え、最小部材幅が
   *   表現できない。しかも**silent**(例外は出ず、もっともらしい形が返る)。
   * - **細かい側の端は「都合」の境界** —— 割っても間違いはしない。遅い、落ちる、それだけ。
   *
   * したがってスライダーの左端(添字0)が既定であり、**そこより粗い側へ動かす操作を
   * 用意しない**。目盛りの実体は `optimize_run.resolution_limits()["steps"]` で、
   * **段はサーバが作る** —— 要素数の数え方(余白比率とパッド)を画面が持つと必ずずれる。
   *
   * **時間は入力にしない。** 各段に予想時間が**帰結として**付いて回るだけで、
   * 「何分で終わらせたい」から解像度を逆算する口は置かない —— 逆算すると、利用者が
   * 知らないうちに最小部材幅と条件面の要求を割る(しかも割ったことは画面から見えない)。
   *
   * ## 通知をここに残す理由
   *
   * 「荷重面に格子節点が付かなかった」「保持ソリッドがこの解像度では表現できていない」は
   * **結果が信用できないという意味**である。トースト(`Toast.svelte`)は8秒で消える設計
   * なので、これを流すと見落とす。**次に走らせるまでパネルに残す**(トーストにも同じ文が
   * 出るが、あちらは気づかせる役、ここが読み返す役)。
   *
   * ## 状態の正は `App.svelte`
   *
   * ここは値と口を受け取るだけの presentational な部品(`SettingsDialog.svelte` と同じ規律)。
   * **`position: absolute` でフローの外**に置くので、`.viewport` の寸法＝カメラのアスペクトに
   * 触らない(styles.css の不変条件)。
   */
  import type {
    OptimizeConditionsReport,
    OptimizeProgress,
    OptimizeResolutionPayload,
  } from './session'

  const {
    conditions,
    resolution,
    rminText,
    stepIndex,
    running,
    cancelling,
    progress,
    notice,
    onRmin,
    onStep,
    onRun,
    onCancel,
  }: {
    conditions: OptimizeConditionsReport | null
    resolution: OptimizeResolutionPayload | null
    /** 入力欄の生の文字列(数にできない途中の打鍵を握り潰さないため文字列で持つ)。 */
    rminText: string
    stepIndex: number
    running: boolean
    cancelling: boolean
    progress: OptimizeProgress | null
    notice: OptimizeNotice | null
    onRmin: (text: string) => void
    onStep: (index: number) => void
    onRun: () => void
    onCancel: () => void
  } = $props()

  const steps = $derived(resolution?.steps ?? [])
  /** 実際に効いている段の位置。**目盛りが縮んだときのために毎回丸め直す**
   *  (最小部材幅を打ち替えると段数が変わりうる)。 */
  const clamped = $derived(Math.min(stepIndex, Math.max(steps.length - 1, 0)))
  const step = $derived(steps[clamped] ?? null)
  const problems = $derived(conditions?.problems ?? [])
  /** 走らせられるか。**条件と目盛りの両方がそろって初めて押せる。** */
  const canRun = $derived(
    !running && conditions !== null && conditions.ready && step !== null,
  )

  /**
   * 秒を人が読む長さへ。**桁の見当**しか意味が無いので、分は整数、秒も10秒刻みで丸める
   * (「約1分23秒」は持っていない精度を主張してしまう)。
   */
  function human(seconds: number): string {
    if (!Number.isFinite(seconds) || seconds <= 0) return '—'
    if (seconds < 90) return `${Math.max(Math.round(seconds / 10) * 10, 10)}秒`
    return `${Math.round(seconds / 60)}分`
  }
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

  <label>
    最小部材幅
    <input
      id="optimize-rmin"
      type="number"
      min="0"
      step="0.1"
      value={rminText}
      disabled={running}
      placeholder="mm"
      oninput={(event) => onRmin(event.currentTarget.value)}
    />
    <span class="unit">mm</span>
  </label>

  <!-- **目盛りは細かくする方向にだけ動く**(左端＝既定＝いちばん粗くて安い)。
       段が1つも無い＝「間違わない解像度」が作れない状態なので、スライダーごと出さずに
       理由を出す。 -->
  {#if resolution?.error}
    <p id="optimize-resolution-note" class="hint bad">{resolution.error}</p>
  {:else if steps.length === 0}
    <p id="optimize-resolution-note" class="hint bad">
      この部品と条件面では、間違わない解像度が総要素数の上限({resolution?.max_elements ?? '—'})に
      収まらない。条件面をもっと大きい面に取り直すか、最小部材幅を大きくすること
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
      {step ? step.element_count.toLocaleString() : '—'} 要素 ・ 予想
      <strong>{step ? human(step.seconds_typical) : '—'}</strong>(最大
      {step ? human(step.seconds_worst) : '—'})
      <br />
      予想時間は<strong>2〜3倍の幅で読む値</strong>であって予測ではない(機械の熱状態だけで1.7倍振れる)。
    </p>
    <p class="hint dim">
      左端が既定 —— 最小部材幅と条件面を満たす、いちばん粗くて安い設定である。
      <strong>右へ動かすほど得られるもの</strong>: STLの表面が滑らかになり、応力(＝安全率)と
      体積の見積もりが確かになる。<strong>払うもの</strong>: 時間とメモリ。
      <strong>左へは動かない</strong> —— 粗くすると条件面から節点が消え、最小部材幅が
      表現できなくなる(しかも黙って起きる)。
    </p>
  {/if}

  <div class="row">
    <button id="optimize-run" disabled={!canRun} onclick={onRun}>実行</button>
    <button id="optimize-cancel" disabled={!running || cancelling} onclick={onCancel}>
      {cancelling ? '中断中…' : '中断'}
    </button>
  </div>

  <!-- **進捗**。反復ごと・候補材料量ごとに届く(`20260904-1704` の契約①)。
       走っていないときは行ごと消す —— 前回の最後の1件を残すと、走っているように見える。 -->
  {#if running}
    <p id="optimize-progress" class="hint">
      {progress === null ? '準備中…' : `[${progress.phase}] ${progress.message}`}
      {#if progress && progress.iteration !== undefined}
        (反復 {progress.iteration} / 評価 {progress.evaluation})
      {:else if progress && progress.evaluation > 0}
        (評価 {progress.evaluation})
      {/if}
      {#if cancelling}
        <br />中断を伝えた。次に進捗が出た時点で降りる(塊を起こす段の中では止まらない)。
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
     左のボディの面が1つも指せなくなった**(2026-09-05実測。`smoke-optimize-mode.mjs` が
 検出)。条件を置くのはこのモードの仕事そのものなので、モデルを覆う位置には置けない。

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
  #optimize-rmin,
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
</style>
