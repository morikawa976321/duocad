<script lang="ts">
  /**
   * アプリ層の組み立て — 接続・ビューポート・パネルを繋ぐ。
   * 引数を集める状態機械は `commands/CommandHost.svelte` に1つだけある。
   *
   * 選択の正はコアにある(docs/DESIGN_DECISIONS.md「層の分け方」)。ここは `state.selection` を
   * 描き直すだけで、選択の値を覚えない。責務は、ピック→コアの選択への翻訳(修飾キーで
   * 追加・除去・置き換え)と、プレビューの往復(コマンド層が決め、ここが投げて幽霊を描く)。
   */
  import { onMount, untrack } from 'svelte'
  import CommandHost from './commands/CommandHost.svelte'
  import {
    bodyRef,
    edgeRef,
    faceRef,
    refKey,
    refKind,
    refsOfKind,
    regionRef,
    sameRef,
    sketchEntityRef,
    vertexRef,
    type CommandCategory,
    type PickTarget,
    type Point2,
    type SelectableKind,
  } from './commands/command'
  import { categoryOf } from './commands/category'
  import {
    MOVE_BODY,
    SKETCH_FEATURE_COMMANDS,
    SOLID_FEATURE_COMMANDS,
    needsSketchId,
  } from './commands/features'
  import { ghostCopies, snapSketchPoint } from './entityKinds'
  import { IMPORT_STEP } from './commands/files'
  import { MEASURE } from './commands/measure'
  import type { MeasurePayload } from './session'
  import {
    OPTIMIZE_EMPTY_START_VERBS,
    OPTIMIZE_MODE_COMMANDS,
    OPTIMIZE_PRESET_ROLE,
    OPTIMIZE_ROLE_LABEL,
  } from './commands/optimize'
  import OptimizePanel from './OptimizePanel.svelte'
  import SelectionInfoPanel from './SelectionInfoPanel.svelte'
  import { optimizeNoticeFor, type OptimizeNotice } from './optimizeNotice'
  import { chosenStep, snapMinWidth } from './minWidth'
  import { buildSketchDraws, regionsStillExist } from './sketchDraws'
  import { installTestHooks } from './testHooks.svelte'
  import { bodyGhostRotation, ghostSources, resolvePicksForGhost, type GhostContext } from './ghostArgs'
  import {
    DEFAULT_SKETCH_COMMAND_ID,
    SKETCH_COMMANDS,
    SKETCH_CREATE,
    SKETCH_PENDING_COMMANDS,
    ghostEntity,
    runsOutsideSketch,
    runsWithoutSketchId,
  } from './commands/sketch'
  import ChannelBadge from './ChannelBadge.svelte'
  import FilesPanel from './FilesPanel.svelte'
  import StartScreen from './StartScreen.svelte'
  import BrowserPanel, { planeLabel } from './BrowserPanel.svelte'
  import Toast, { fail, errorLog } from './Toast.svelte'
  import {
    createSession,
    type Applied,
    type BasePlaneName,
    type BodyGeometry,
    type BodyRef,
    type ConnectionStatus,
    type EdgesPayload,
    type FemStatus,
    type OptimizeConditionsReport,
    type OptimizeDebugPayload,
    type OptimizeProgress,
    type OptimizeResolutionPayload,
    type OptimizeResultPayload,
    type OptimizeStepMeta,
    type OptimizeStepVoxels,
    type PreviewPayload,
    type SelectionRef,
    type ServerState,
    type ShapeRef,
    type SketchEntityRef,
    type VerticesPayload,
  } from './session'
  import AiPanel from './AiPanel.svelte'
  import { createAiChat } from './aiChat.svelte'
  import {
    anyDebugFlag,
    debugKindsFor,
    readDebugFlags,
    type DebugFlags,
  } from './debugFlags'
  import SettingsDialog from './SettingsDialog.svelte'
  import { createDisplaySettings } from './displaySettings.svelte'
  import { createDocumentFiles } from './documentFiles.svelte'
  import { createMeasurement } from './measurement.svelte'
  import { createSelectionInfo } from './selectionInfo.svelte'
  import { createServerWait } from './serverWait.svelte'
  import WaitBar from './WaitBar.svelte'
  import {
    createViewport,
    type BoxSelectEvent,
    type PickEvent,
    type SketchEntityHit,
    type VertexMarker,
    type Viewport,
  } from './viewport'

  /**
   * ツールバーの中身(モードごと)。領域押し出しはソリッド側・スケッチ側の両方に載る。
   * `sketch_create` は載せない: verbの行に置くと、押すと戻ってくる操作と同じ見た目で
   * モードを切り替えるボタンになるため、モードの行(`.mode-row`)に置く(`mode` のdocstring)。
   */
  const SOLID_COMMANDS = [
    ...SOLID_FEATURE_COMMANDS,
    // `file` 分類はこの1つだけ(理由は `commands/files.ts`)。
    IMPORT_STEP,
    // 測定は verb ではなく `query`(文書もログも変えない)。通常モードだけ(`commands/measure.ts`)。
    MEASURE,
  ]
  const SKETCH_MODE_COMMANDS = [...SKETCH_COMMANDS, ...SKETCH_FEATURE_COMMANDS]

  /**
   * verb名 -> そのコマンドの分類。送信経路の分岐はこれだけで決まる。
   * モードごとの一覧を全部含めること: 漏れると `runCommand` がそのモードのverbを
   * 「未知のコマンド」で断る(`SKETCH_PENDING_COMMANDS` を足し忘れると即これを踏む)。
   */
  const CATEGORY_OF: Map<string, CommandCategory> = new Map(
    [
      ...SOLID_COMMANDS,
      ...SKETCH_PENDING_COMMANDS,
      ...SKETCH_MODE_COMMANDS,
      ...OPTIMIZE_MODE_COMMANDS,
    ].map((command) => [command.id, categoryOf(command)]),
  )

  let container: HTMLDivElement
  let viewport: Viewport | null = null

  let status = $state<ConnectionStatus>('connecting')
  let model = $state<ServerState | null>(null)
  let messages = $state<string[]>([])
  let lastApplied = $state<Applied | null>(null)

  /** 送信中のコマンドの `client_op`(応答が返るまでの間だけ生きる)。 */
  let pendingOp = $state<number | null>(null)
  let opCounter = 0
  /** 送って配信待ちの `clear_selection` の `client_op`。描画に使わない。 */
  let clearingOp: number | null = null

  /** 投げたプレビューの番号(古い応答を捨てる。0は「投げていない」)。 */
  let previewId = 0
  /** 画面に幽霊が出ているか(テスト用フックが見る)。 */
  let previewing = $state(false)
  /**
   * いま構えているサーバ投機実行(`clearPreview` で null)。無関係な再配信のあと投げ直すために控える:
   * `viewport.update()` は届いた本物で幽霊を落とすが、`CommandHost` は引数が変わらない限り
   * 再発火しない(`previewedKey`)ので、投げ直さないと消えたまま戻らない。
   */
  let serverPreview: { verb: string; args: Record<string, unknown> } | null = null
  let refireTimer: ReturnType<typeof setTimeout> | null = null
  const PREVIEW_REFIRE_MS = 250

  // ---------------------------------------------------------------- モード(verbより1つ上の次元)
  /** スケッチを抜けたときの戻り先(最適化モード中にスケッチ名クリックで入ったら、抜けて最適化へ戻る)。 */
  type ResumeMode = 'idle' | 'optimize'
  /**
   * モード = 文書のどの部分系を編集対象にしているか(通常=ボディ群 / スケッチ=`sketches[i]` /
   * 最適化=`Document.optimize`)。verb = その対象に1操作を適用すること。次元が違うので
   * モードの行(`.mode-row`)が上、verbの行(`CommandHost`)が下。
   *
   * タグ付きユニオンにしたのは、スケッチと最適化モードが同時に真になる状態を書けなくするため。
   * `sketch-pending` はスケッチモードに入ったが、まだどのスケッチも開いていない状態(貼る先のクリック待ち)。
   *
   * コアには持ち込まない: `duocad/` 側にモードの概念は無く(`Document` にフラグが無い・verbは
   * 毎回 `sketch_id` を明示で受ける・MCPのツール一覧はモードによらず固定)、それが人とAIの
   * 交互操作を成立させている。モード遷移は undo/操作ログ/MCP呼び出しのどれとも一致しないので
   * verbではなく画面だけの状態。
   */
  type Mode =
    | { kind: 'idle' }
    | { kind: 'sketch-pending'; resume: ResumeMode }
    | { kind: 'sketch'; id: string; resume: ResumeMode }
    | { kind: 'optimize' }
  let mode = $state<Mode>({ kind: 'idle' })

  /**
   * 今どのスケッチを編集しているか(null なら編集していない)。`mode` からの派生。
   * `sketch-pending` では null のままなので、「基準平面を指せるのは `sketchId === null` のときだけ」
   * 等の規律(`wantsBasePlanes` / `wantsEdges` / `wantsRegions`)がそのまま貼る先待ちに効く。
   */
  const sketchId = $derived(mode.kind === 'sketch' ? mode.id : null)
  /** ポインタが指しているスケッチ平面上の点(確定前の線を見せるためだけ)。 */
  let hoverPoint = $state<Point2 | null>(null)
  /** コマンド層への参照(クリックで集めた点を渡す・失敗したら打った値を戻す)。 */
  let host = $state<{
    addPoint(u: number, v: number): boolean
    wantsPoint(): boolean
    wantsPlane(): boolean
    setBasePlane(name: BasePlaneName): boolean
    useSelectedFaceAsPlane(): boolean
    wantsPick(): PickTarget | null
    setPick(ref: SelectionRef): boolean
    commitFailed(): void
    commandSucceeded(): void
  } | null>(null)

  /**
   * この版で形状最適化を使えるか(安定版は使えない。サーバが接続のたびに送る `optimize_status` の `available`)。`null` はまだ聞いていない ——
   * 入口は出さず、ステップの取り直し(安定版では断られてエラーのトーストになる)も頼まない。聞いた直後の `onOptimizeStatus` が頼み直す。
   * **接続が切れても `null` に戻さない**: 繋ぎ直しは最初に教わった URL へ行く(`session.ts` の `knownWebSocketUrl`)ので、同じタブで別の版の
   * サーバに繋がることは無く、答えは変わらない。
   * 戻すと、繋ぎ直すたびに最適化モードの画面が一度消え、起動中のコマンドの状態と食い違う(sol の指摘で確かめた)。
   */
  let shapeOptimizationAvailable = $state<boolean | null>(null)
  function askOptimizeSteps(): void {
    if (shapeOptimizationAvailable === true) session.optimizeSteps()
  }
  /**
   * 形状最適化モードの画面を出すか。モードは対応する文書実体が無いので画面側だけの状態(バックエンドには知らせない)。
   * 使えると聞くまで(接続し直した直後を含む)は、モードを保ったまま画面と問い合わせを止める。
   */
  const optimizeMode = $derived(mode.kind === 'optimize' && shapeOptimizationAvailable === true)
  /** コマンドの行を作り直す鍵。最適化モードでも画面を止めている間は通常の行を出すので、行の顔ぶれが変わるたびに作り直す(起動中のコマンドを別の一覧へ持ち越さない)。 */
  const commandRowKey = $derived(mode.kind === 'optimize' && !optimizeMode ? 'idle' : mode.kind)
  /**
   * 直近にトーストへ出した `stale` の鍵(`role:body:index:code`)。`code` を含めないと、
   * `hidden`(出さない側)で記録された対象が後で `fingerprint`/`missing`(出す側)に変わっても
   * 「既に見た」と誤判定され二度と通知されない。消えたら忘れる(次に見た集合で作り直す)。
   */
  let seenStaleKeys = new Set<string>()

  // ---------------------------------------------------------------- D: 辺とフィーチャ
  /** 起動中のコマンドが選択から取る種別(`CommandHost` が知らせる)。 */
  let wantedKinds = $state<SelectableKind[]>([])
  /**
   * 起動中のコマンド(無起動・スケッチの既定「選ぶ」は null)。`CommandHost` の
   * `onStart` / `onEnd` と `setMode` だけが書く。描画に使わない。
   *
   * 起動中は素のクリックが選択のトグルになり(`toggleOnlyMode`)、終わると選択を全解除する
   * (`onCommandEnd`)。保持/除外ソリッド・拘束面の集合 verb は既存の割り当てが選択状態で現れ、
   * 測定は対象がちょうど2つなので、置き換えのままだと成り立たない。
   */
  let activeVerb: string | null = null
  /** 集合 verb の起動時に表示へ戻した非表示ボディの `set_visible` の応答待ち。全部戻ってから既存の割り当てを選択に並べる(`onCommandStart`)。 */
  let pendingOptimizePreset: { role: 'preserve_bodies' | 'exclude_bodies' | 'fixtures'; ops: Set<number> } | null = null
  /** 起動中のコマンドが基準平面を要求しているか(`CommandHost` が知らせる)。要求している間だけ指せる。 */
  let wantedPlane = $state(false)
  /** 起動中のコマンドが図形の直接ピックを待っている種別(`CommandHost` が知らせる)。待っている間だけ指せる。 */
  let wantedPick = $state<PickTarget | null>(null)
  /**
   * 起動中のコマンドが頂点そのものを対象にするか(`SelectionArgSpec.pickVertices`。測定)。
   * `wantedPick === 'vertex'` と同じ扱いで、対象ボディの頂点を候補に出し、辺より先に取る。
   */
  let wantedVertices = $state(false)
  /** 取り寄せた辺(body_id -> その版の折れ線)。版が変われば捨てる。 */
  let edges = $state<Record<string, EdgesPayload>>({})
  /** 取り寄せ中のもの(`${body_id}:${revision}`)。同じ要求を何度も投げないため。 */
  let edgesAsked = new Set<string>()
  /** 頂点の座標(選択に頂点が入っているBodyの分だけ取り寄せる)。 */
  let vertices = $state<Record<string, VerticesPayload>>({})
  /** 取り寄せ中のもの(辺と同じ形の印)。 */
  let verticesAsked = new Set<string>()

  // ---------------------------------------------------------------- E: ファイルと履歴
  /** ファイル操作と `duocad/models/` の一覧。応答待ち(`pendingOp`)はこちらの持ち物なので貸す。 */
  const files = createDocumentFiles({
    sendCommand: (cmd, args, op) => session.sendCommand(cmd, args, op),
    nextOp,
    hold: (op) => (pendingOp = op),
    release: releasePending,
    note,
    // トップ画面も一覧を見せているので、そちらが出ている間も取り直す。
    panelOpen: () => dataPanelOpen || showStart,
    captureThumbnail: () => viewport?.captureThumbnail() ?? null,
    connected: () => status === 'open',
  })
  /**
   * 今の文書がそのままファイルに載っているか(ヘッダの `#save-state`。サーバの写し `Session.saved`)。
   * 自動保存が書けなかったときだけ false。無題で何も変えていない文書は true。
   */
  const saved = $derived(model?.saved ?? true)
  /** 文書を開いているか(`state.document_open`)。最初の状態が届くまでは false(閉じて起動したサーバでツールバーが一瞬出るのを防ぐ)。 */
  const documentOpen = $derived(model?.document_open ?? false)
  /** トップ画面を出すか。状態が届いて、閉じていると分かったときだけ。 */
  const showStart = $derived(model !== null && !model.document_open)
  /** 直前の状態で開いていたか(閉じた瞬間だけ後片付けをする。`handleState`)。 */
  let wasDocumentOpen = false
  /**
   * 左サイドバー(ファイル一覧)を出しているか。見た目だけの状態でコアには送らない。
   * 開閉のたびに `.viewport` の寸法が変わり射影が変わる(`viewport.ts` の `applySize`)ので、
   * 画面座標を使い回す側(実機検査)は開閉を挟んだら取り直すこと。
   */
  let dataPanelOpen = $state(true)
  /** 右サイドバー(`AiPanel` だけ)を開いているか。`{#if}` で `<aside>` ごと出し分け。`.viewport` のサイズ指定は動かさず、flex の配分だけが変わる(左と同じ)。 */
  let sidebarOpen = $state(false)
  /** 設定ダイアログ(`SettingsDialog.svelte`)を出しているか。見た目だけの状態。 */
  let settingsOpen = $state(false)
  /** 形状最適化の CG を解く装置の設定(サーバの `fem_status`)。 */
  let femStatus = $state<FemStatus | null>(null)
  /** 画角・原点ギズモ・断面(どれも文書の状態ではない)。 */
  const display = createDisplaySettings({ fail })
  /**
   * この画面が出した `client_op` の控え。パラメータの打ち直しや可視性の切り替えは
   * `pendingOp` を立てない経路なので、`pendingOp` では自分の操作を照合できない。
   * 返ってきた `applied.client_op` で照合する。
   */
  const myClientOps = new Set<number>()
  /**
   * この画面のクリックで選択が変わった回数(コマンド層の「即適用」の引き金)。
   * Enterの要らないコマンド(`command.ts` の `needsEnter`)の引き金は人がこの画面で指したことでなければならない。
   * 選択集合はAIも `modify_selection` で書き換えられるので、選択の個数だけを見ると人が触っていないのに文書が変わる。
   * 数だけを渡すのは、コマンド層に「誰が変えたか」の判断を持ち込まないため。
   */
  let selfSelections = $state(0)

  // ---------------------------------------------------------------- G: アプリ内AIエージェント
  const ai = createAiChat({ send: (text) => session.aiSend(text), fail })

  const serverWait = createServerWait()

  const session = createSession({
    onState: handleState,
    onWaiting: (oldestSentAt) => serverWait.update(oldestSentAt),
    onError: (message, where, clientOp) => {
      // サムネイルは裏の要求。`pendingOp` を解くと、待っていた操作の確定より先に次の Enter が通る。
      if (where === 'save_thumbnail' || where === 'get_thumbnail') {
        files.dropThumbnailRequests()
        fail(`エラー[${where}] ${message}`)
        return
      }
      // `send` はverbではなく「繋がっていないので送れなかった」で、繋がっていないことに気づく唯一の札。
      // 内部の語を頭に付けない。
      fail(where === 'send' ? message : `エラー[${where}] ${message}`)
      // 待ちを解くのは、待っている操作そのものが失敗したときと、送れなかったときだけ。確定の直前に送った
      // プレビューなどの失敗で解くと、確定がまだサーバで走っている間に次の Enter が通る。
      if (where === 'send') pendingOp = null
      else releasePending(clientOp ?? null)
      if (where === 'preview') clearPreview()
      // 辺の取り寄せが失敗したら「頼んだ」印も消す(次に起動したときに取り直せる)
      if (where === 'get_edges' || where === 'send') edgesAsked.clear()
      if (where === 'get_vertices' || where === 'send') verticesAsked.clear()
      // 失敗してもやり直せる状態で返る: 選択も起動中のコマンドと打った値もコア側(`state.command`)にあり、
      // 失敗では消えない(OCCTのフィレットは半径に対して単調でなく、近い値で打ち直すのが実際の使い方)。
      const category = CATEGORY_OF.get(where)
      if (category === 'feature' || category === 'sketch') host?.commitFailed()
    },
    onWarning: (message) => fail(`警告 ${message}`),
    onStatus: (next) => {
      // 切断・再接続の試行は黙る(`session.ts` が「開けなかった試行」だけトーストへ回す)。
      status = next
      // (再)接続したら今のAI設定を取り直す(切れている間に別の画面が `ai_configure` した可能性がある)。
      if (next === 'open') session.aiGetStatus()
      if (next === 'open') session.femGetStatus()
      // 人が最初の依頼を打っている間に、AIへツール定義を読ませておく(遅い機種で分単位かかる)。
      if (next === 'open') session.aiPrepare()
      // `models/` の一覧も取り直す(サーバの再起動・別セッションの保存で増減しうる)。
      if (next === 'open') files.refresh()
      // 応答待ちの最中に切れたら、その `client_op` はもう返ってこない。捨てないと
      // Enterが永久に拒否される(`onerror` を伴わない正常クローズで踏む)。
      if (next !== 'open' && pendingOp !== null) {
        pendingOp = null
        clearPreview()
      }
      // 全解除の配信も同じく返ってこない(残すと以後の差分が空の選択に対して組まれ続ける)。
      if (next !== 'open') clearingOp = null
      // 切断中の要求は捨てられるが `edgesAsked` には残る。残すと再接続後も版が同じなら
      // 「もう頼んだ」と判断して二度と取りに行かず、そのBodyの辺が出ないままになる。
      if (next !== 'open') {
        edgesAsked.clear()
        verticesAsked.clear()
        files.dropThumbnailRequests()
        // 実行の押下への返事はもう来ない。走っているかは繋ぎ直したときの `optimize_status` で取り直す。
        optimizeStarting = false
      }
    },
    onPreview: handlePreview,
    onEdges: handleEdges,
    onVertices: handleVertices,
    onMeasure: handleMeasure,
    onSelectionInfo: (payload) => selectionInfo.onResult(payload),
    onFile: files.onFile,
    onAiStatus: ai.onStatus,
    onAiToolCall: ai.onToolCall,
    onAiResult: ai.onResult,
    onAiError: ai.onError,
    onFemStatus: (next) => (femStatus = next),
    onOptimizeConditions: (report) => (optimizeConditions = report),
    onOptimizeResolution: (payload) => {
      optimizeResolution = payload
      // 段は応答で遅れて届く。要素サイズが変わると最小幅の段も引き直されるので、選んでいた幅を新しい段へ寄せる。
      const step = chosenStep(payload, optimizeElementSizeMm)
      if (step !== null) optimizeMinWidthMm = snapMinWidth(step.min_width_steps_mm, optimizeMinWidthMm)
    },
    onOptimizeStatus: (running, cancelling, finishing, available) => {
      shapeOptimizationAvailable = available
      // 使えないサーバに繋ぎ直したら、最適化モードとステップの表示に居残らない(断られる口を叩き続ける)。
      // スケッチの戻り先(`resume`)は `exitSketch` が同じ印で見る。
      if (!available) {
        if (mode.kind === 'optimize') setMode({ kind: 'idle' })
        clearSearchSteps()
      }
      optimizeFinishing = running && finishing
      // 接続し直すと、待っていた形の返事は来ない。待ちを畳んで頼み直せるようにする(`$state` なので取得の `$effect` が走り直す)。
      searchStepVoxelsPending = null
      // 走り終わった・接続し直した・文書を切り替えた後は、その文書のステップを取り直す(走っている間は進捗で増える)。
      if (!running || searchSteps.length === 0) askOptimizeSteps()
      // 控えの確定はサーバが受け付けてから(押した瞬間だと、断られた押下が走っている実行の控えを上書きする)。
      if (running && optimizeStarting && optimizeStartingSettings !== null) optimizeRunSettings = optimizeStartingSettings
      optimizeStartingSettings = null
      optimizeRunning = running
      optimizeStarting = false
      // 中断中の印は走っている間だけ(畳まないと次の実行が中断表示から始まって見える)。
      optimizeCancelling = running && cancelling ? true : running ? optimizeCancelling : false
      // `running` は「いま開いている文書で走っているか」(サーバの `optimization_status`)。
      // 走らせたまま別の文書へ移ると偽になるので、その文書に進捗の行を残さない。
      if (!running) {
        optimizeProgress = null
        return
      }
      if (optimizeRunSettings !== null && optimizeRunSettings.name === files.currentName) {
        optimizeElementSizeMm = optimizeRunSettings.elementSizeMm
        optimizeMinWidthMm = optimizeRunSettings.minWidthMm
      }
      // 文書の差し替えで欄が空になっている(`resetOptimizeView`)。取り直しの `$effect` は
      // 実行中には問い合わせないので、ここで1回だけ頼む(でないと実行の終わりまで「確認している…」のまま)。
      if (optimizeConditions === null || optimizeResolution === null) {
        session.optimizeConditions()
        session.optimizeResolution(optimizeElementSizeMm)
      }
      // 走らせている文書へ戻ったら、実行中の表示が見えるモードへ入る(編集が断られる理由を画面に出す)。
      if (mode.kind === 'idle') setMode({ kind: 'optimize' })
    },
    onOptimizeProgress: (event) => {
      // 前の実行の進捗が後から届くことがある(別の接続への配信待ち)。識別子は新しいほど大きい(`optimize_steps.py`)ので、
      // 表示を変える前に捨てる(捨てないと「このステップで仕上げる」が受け付けの閉じた後に押せて見える)。
      const run = event.run ?? event.step?.run
      if (run !== undefined && (run === searchStepsStaleRun || (searchStepsRun !== null && run < searchStepsRun))) return
      optimizeProgress = event
      const step = event.step
      if (step === undefined) return
      if (step.run !== searchStepsRun) {
        // 新しい実行の最初の進捗(前の実行の一覧が遅れて戻っていても、ここで切り替える)。
        searchSteps = []
        searchStepsRun = step.run
        searchStepSelected = null
        searchStepVoxels = null
      }
      if (step.index === searchSteps.length) searchSteps = [...searchSteps, step]
      else if (step.index > searchSteps.length) session.optimizeSteps() // 取りこぼした(接続し直した直後など)
    },
    onOptimizeSteps: (steps, finishable) => {
      const run = steps[0]?.run ?? null
      // 新しい実行を始めた後に届いた、前の実行の一覧は捨てる。
      if (run !== null && (run === searchStepsStaleRun || (searchStepsRun !== null && run < searchStepsRun))) return
      if (run !== searchStepsRun) {
        searchStepSelected = null
        searchStepVoxels = null
      }
      searchSteps = steps
      searchStepsRun = run
      searchStepsFinishable = finishable
      if (searchStepSelected !== null && searchStepSelected >= steps.length) searchStepSelected = null
    },
    onOptimizeStepVoxels: (payload) => {
      searchStepVoxelsPending = null
      // 別の実行の形(遅れて届いた返事)は捨てる。
      if (payload.run !== undefined && payload.run !== searchStepsRun) return
      searchStepVoxels = payload
    },
    onOptimizeDebug: handleOptimizeDebug,
    onOptimizeResult: handleOptimizeResult,
    onOptimizeError: (message, problems, cancelled, alreadyRunning) => {
      optimizeStarting = false
      optimizeStartingSettings = null
      optimizeFinishing = false
      // 別の実行が走っているという断りは、その実行の表示(進捗の行も通知)を消さない。
      if (alreadyRunning) {
        fail(`最適化: ${message}`)
        return
      }
      optimizeProgress = null
      // 中断はエラーではないのでトーストは出さず、パネルにだけ残す。
      if (cancelled) {
        optimizeNotice = {
          kind: 'info',
          lines: ['中断した。文書は1ビットも変わっていない(結果のボディは足していない)'],
        }
        return
      }
      // デバッグ表示は消さない(失敗したときこそ塊と節点を見たい)。次の実行が `runOptimization()` の先頭で消す。
      optimizeNotice = { kind: 'bad', lines: [message, ...problems] }
      fail(`最適化: ${message}`)
    },
  })

  // ---------------------------------------------------------------- H: 形状最適化を走らせる
  // `state.optimize`(条件の置かれ方)とは別の口。ここにあるのは実行可否・解像度・進捗・結果で、
  // どれも操作ログに載らない(`duocad/catalog.py` の `category="protocol"`)。
  let optimizeConditions = $state<OptimizeConditionsReport | null>(null)
  let optimizeResolution = $state<OptimizeResolutionPayload | null>(null)
  /** 要素サイズの入力[mm]。`null` = 空欄 = 既定(上限側、いちばん粗い)。範囲の判定はサーバー(`selected_error`)。 */
  let optimizeElementSizeMm = $state<number | null>(null)
  /** 選んでいる最小幅の段[mm](`min_width_steps_mm` のどれか)。`null` = 既定(いちばん細い段)。 */
  let optimizeMinWidthMm = $state<number | null>(null)
  let optimizeRunning = $state(false)
  /** 実行を押してからサーバの返事(`optimize_status` / `optimize_error`)が届くまで。二度押しを止める。 */
  let optimizeStarting = $state(false)
  /** 走っている、または走り出すところ。文書を変える操作を画面から出さない(サーバも断る)。 */
  const optimizeBusy = $derived(optimizeRunning || optimizeStarting)
  let optimizeCancelling = $state(false)
  let optimizeProgress = $state<OptimizeProgress | null>(null)
  type OptimizeRunSettings = { name: string | null; elementSizeMm: number | null; minWidthMm: number | null }
  /** 次に走らせるまで消えない通知(トーストは時間で消える)。 */
  let optimizeNotice = $state<OptimizeNotice | null>(null)
  /** 走らせたときの設定と文書。別の文書へ移ると欄は既定へ戻る(`resetOptimizeView`)ので、走らせた文書へ戻ったときにここから戻す。 */
  let optimizeRunSettings: OptimizeRunSettings | null = null
  /** 実行を押してからサーバが受け付けるまでの控え(`onOptimizeStatus` が `optimizeRunSettings` へ確定する)。 */
  let optimizeStartingSettings: OptimizeRunSettings | null = null

  /** いま効いている段(`chosenStep`。入力した値への応答がまだなら `null` で、実行させない)。 */
  const optimizeStep = $derived(chosenStep(optimizeResolution, optimizeElementSizeMm))

  // ---------------------------------------------------------------- H3: 探索のステップを切り替えて見る・選んで仕上げる
  // 正はサーバ(`duocad/optimize_steps.py`)。一覧は走っている間は進捗の `step` で伸び、終わった後・文書を切り替えた後は
  // `optimizeSteps()` で取り直す。形(表面のセル)は選んだときだけ1つずつ取りに行く(全部を配ると数十MBになる)。
  /** いま開いている文書の直近の実行のステップ。 */
  let searchSteps = $state.raw<OptimizeStepMeta[]>([])
  /** `searchSteps` の実行の識別子と、新しい実行を始めたときに捨てた前の実行の識別子(遅れて届く返事を見分ける)。 */
  let searchStepsRun: string | null = null
  let searchStepsStaleRun: string | null = null
  /** 実行の後に仕上げられるか(探索が合格形を出して終わった実行)。 */
  let searchStepsFinishable = $state(false)
  /** 選んでいるステップ。`null` は最新を追う。 */
  let searchStepSelected = $state<number | null>(null)
  /** ステップの形をボクセルで見ているか。 */
  let searchStepsShown = $state(false)
  let searchStepVoxels = $state.raw<OptimizeStepVoxels | null>(null)
  /** 返事を待っている形の番号(待つ間は次を頼まない。返事が来たら下の `$effect` が今の選択と比べ直す)。 */
  let searchStepVoxelsPending = $state<number | null>(null)
  /** 「このステップで仕上げる」を伝えた後、結果が届くまで。 */
  let optimizeFinishing = $state(false)
  const searchStepShownIndex = $derived(
    searchSteps.length === 0 ? null : (searchStepSelected ?? searchSteps.length - 1),
  )

  $effect(() => {
    if (shapeOptimizationAvailable !== true) return
    const wanted = searchStepsShown ? searchStepShownIndex : null
    const have = searchStepVoxels !== null && searchStepVoxels.run === searchStepsRun ? searchStepVoxels.index : null
    if (wanted === null || wanted === have || searchStepVoxelsPending !== null) return
    searchStepVoxelsPending = wanted
    session.optimizeStepVoxels(wanted)
  })

  function clearSearchSteps(): void {
    searchStepsRun = null
    searchSteps = []
    searchStepsFinishable = false
    searchStepSelected = null
    searchStepVoxels = null
    searchStepVoxelsPending = null
  }

  function finishOptimization(): void {
    if (searchSteps.length === 0 || optimizeFinishing || optimizeCancelling) return
    optimizeFinishing = true
    session.finishOptimization(searchStepSelected, debugKindsFor(debugFlags))
  }

  function runOptimization(): void {
    if (optimizeBusy) return
    optimizeStarting = true
    optimizeProgress = null
    optimizeNotice = null
    // 前の実行のデバッグ表示とステップを捨ててから走らせる(残すと古い塊が重なって見える)。
    clearOptimizeDebug()
    // 前の実行の一覧・形が遅れて届いたら捨てる印(文書を開き直したときに同じ実行の一覧を取り直すのは捨てない)。
    if (searchStepsRun !== null) searchStepsStaleRun = searchStepsRun
    clearSearchSteps()
    optimizeStartingSettings = { name: files.currentName, elementSizeMm: optimizeElementSizeMm, minWidthMm: optimizeMinWidthMm }
    session.runOptimization({
      elementSizeMm: optimizeStep?.element_size_mm ?? null,
      minWidthMm: optimizeMinWidthMm,
      debug: debugKindsFor(debugFlags),
    })
  }

  /** 最小幅の段を選んだ。 */
  function handleOptimizeMinWidth(value: number | null): void {
    optimizeMinWidthMm = value
  }

  /** 要素サイズの入力が変わった。その値の段はサーバーに聞く(格子の数え方は画面が持たない)。読めない入力は `null` が来る前に欄が止める。 */
  function handleOptimizeElementSize(value: number | null): void {
    optimizeElementSizeMm = value
    session.optimizeResolution(value)
  }

  /** 文書を差し替えたとき、前の文書について出していた最適化の表示を戻す。 */
  function resetOptimizeView(): void {
    optimizeNotice = null
    optimizeProgress = null
    optimizeElementSizeMm = null
    optimizeMinWidthMm = null
    optimizeResolution = null
    optimizeConditions = null
    clearOptimizeDebug()
    clearSearchSteps()
    // 開き直した文書の直近の実行のステップを取り直す(走っていない文書では状態の配信が来ないので、ここで頼む)。
    askOptimizeSteps()
  }

  // ---------------------------------------------------------------- H2: 開発用のデバッグ表示
  // URLパラメータでだけ出る(`debugFlags.ts`)。フラグが無ければ `session.runOptimization` に
  // 空配列を渡すだけで、製品の既定の画面は変わらない。材料はいま走らせた最適化そのものから来る。
  const debugFlags: DebugFlags = readDebugFlags(
    typeof location === 'undefined' ? '' : location.search,
  )
  // `$state.raw`: 深い `$state` にすると最大15万要素の配列がプロキシで包まれ、下の `$effect` が読むたびに要素ごとの追跡を払う。
  let optimizeDebugSpace = $state.raw<OptimizeDebugPayload | null>(null)
  let optimizeDebugRho = $state.raw<OptimizeDebugPayload | null>(null)
  let optimizeDebugAnchors = $state.raw<OptimizeDebugPayload | null>(null)
  /** `?voxel=1` のときだけ入る、隠す結果ボディ。表示だけ。 */
  let optimizeDebugHiddenBodyId = $state<string | null>(null)

  function clearOptimizeDebug(): void {
    optimizeDebugSpace = null
    optimizeDebugRho = null
    optimizeDebugAnchors = null
    optimizeDebugHiddenBodyId = null
  }

  /** デバッグ配信が届いた。塊は `stage: 'derive'`、面アンカーは `stage: 'mapping'`、密度場は `stage: 'final'` で各1回。同じ `kind` は後から来たほうで置き換える。 */
  function handleOptimizeDebug(payload: OptimizeDebugPayload): void {
    if (payload.kind === 'space') optimizeDebugSpace = payload
    else if (payload.kind === 'rho') optimizeDebugRho = payload
    else if (payload.kind === 'anchors') optimizeDebugAnchors = payload
  }

  /** デバッグ表示をビューポートへ当てる。フラグが無ければ呼ばない(既定の画面には触れる経路が無い)。 */
  $effect(() => {
    const step = searchStepsShown ? searchStepVoxels : null
    const stepVoxels =
      step?.grid && step.elements
        ? {
            grid: step.grid,
            elements: step.elements,
            total: step.total ?? step.elements.length,
            shown: step.shown ?? step.elements.length,
            stride: step.stride ?? 1,
          }
        : null
    const target = viewport
    if (!anyDebugFlag(debugFlags)) {
      // フラグが無い既定の画面では、ステップの形だけを出す(見ていなければ何も出さない)。
      target?.setOptimizeDebug(stepVoxels === null ? null : { space: null, rho: stepVoxels, anchors: null, hiddenBodyId: null })
      return
    }
    // 依存を先に読み切ってから呼ぶ。`viewport?.setOptimizeDebug({...})` と直接書くと、初回に
    // `viewport` が未定義だとオプショナル連鎖が引数の評価ごと飛ばし、依存を1つも登録できず
    // 二度と再実行されない(症状は「何も出ない」で、型検査にもコンソールにも出ない)。
    const voxels = (payload: OptimizeDebugPayload | null, want: boolean) =>
      want && payload?.elements
        ? {
            grid: payload.grid,
            elements: payload.elements,
            total: payload.total ?? payload.elements.length,
            shown: payload.shown ?? payload.elements.length,
            stride: payload.stride ?? 1,
          }
        : null
    const next = {
      space: voxels(optimizeDebugSpace, debugFlags.space),
      rho: stepVoxels ?? voxels(optimizeDebugRho, debugFlags.voxel),
      anchors:
        debugFlags.anchors && optimizeDebugAnchors?.fixed && optimizeDebugAnchors.load
          ? {
              // 格子も渡す(球の半径が要素サイズで決まる。`?debug=anchors` 単独でも同じ大きさにする)。
              grid: optimizeDebugAnchors.grid,
              fixed: optimizeDebugAnchors.fixed,
              load: optimizeDebugAnchors.load,
            }
          : null,
      hiddenBodyId: optimizeDebugHiddenBodyId,
    }
    target?.setOptimizeDebug(next)
  })

  /**
   * 結果を受けた。`state` が `'done'` かそれ以外かで分かれる(`docs/contracts/ui-optimize-integration.md`「最適化の配線」)。
   * `'done'` 以外は文書が変わらないので、そのことを通知に書く。`warning` のトーストは気づかせる役で、読み返す役は通知。
   */
  function handleOptimizeResult(result: OptimizeResultPayload): void {
    optimizeProgress = null
    optimizeFinishing = false
    optimizeNotice = optimizeNoticeFor(result) // 文の組み立ては `optimizeNotice.ts`
    if (result.state === 'done') {
      note(`最適化が終わった: ${result.name}`)
      // `?voxel=1`: 平滑化メッシュへ差し替えず、ボクセルのまま止める。結果は文書に入るメッシュボディなので
      // 表示だけ隠す。隠す相手の `body_id` は結果が届いてから分かるため、数十msだけ両方が見える(過渡として受け入れ)。
      if (debugFlags.voxel && result.body_id) optimizeDebugHiddenBodyId = result.body_id
    }
  }

  /** 情報を1行、ステータスバーへ出す。エラーは `fail()` を使う(Toast.svelte のdocstring)。 */
  function note(message: string): void {
    if (!message) return
    messages = [message, ...messages].slice(0, 4)
  }
  // ---------------------------------------------------------------- 操作の番号と応答待ち
  function nextOp(): number {
    opCounter += 1
    myClientOps.add(opCounter)
    return opCounter
  }

  /**
   * 番号を取って応答待ち(`pendingOp`)にし、verb を送る(`runCommand` の sketch / feature / query)。
   * ファイル操作は `files.run`(文書を変える verb のときだけ待つ)で、ここは通さない。
   */
  function sendTracked(verb: string, args: Record<string, unknown>): void {
    const op = nextOp()
    pendingOp = op
    session.sendCommand(verb, args, op)
  }

  /** 自分が出して待っている操作の応答なら待ちを解く(解いたら true)。 */
  function releasePending(clientOp: number | null): boolean {
    if (clientOp === null || clientOp !== pendingOp) return false
    pendingOp = null
    return true
  }

  // ---------------------------------------------------------------- コアからの状態
  function handleState(next: ServerState, changed: BodyGeometry[], applied: Applied | null): void {
    const previous = model
    model = next
    files.onState(next)
    // 状態が1通届くたびに1つ増える番号(実機検査用の穴。`__duocad.stateEpoch`)。無いと
    // 「往復が終わっていない」と「もう変わらない」を外から区別できない。
    stateEpoch += 1

    // 差分更新: 版が変わったBodyだけを渡す(§2)。
    viewport?.update(changed.map((body) => ({ bodyId: body.body_id, geometry: body.geometry })))
    // 文書から消えたBodyはビューポートからも取り除く。
    viewport?.prune(next.bodies.map((body) => body.body_id))
    // サーバ投機実行のプレビュー(`showPreview`)だけの話。本物が届くと `viewport.update()` が落とすので待っている印も降ろす。
    // ローカル幽霊(`showBodyGhosts`)は `update()` の中で組み直されて出続ける(`viewportTypes.ts` の `update` の項)。
    if (changed.length > 0) {
      previewing = false
      // 自分の確定ならここは null(`commit()` が先に `onCancelPreview` を呼ぶ)。残っているのは
      // 他の操作やカメラによる再配信なので同じ引数で投げ直す。ドラッグ中は続けて来るので落ち着くまで待つ。
      if (serverPreview !== null) {
        if (refireTimer) clearTimeout(refireTimer)
        refireTimer = setTimeout(() => {
          refireTimer = null
          if (serverPreview !== null) requestPreview(serverPreview.verb, serverPreview.args, 'server')
        }, PREVIEW_REFIRE_MS)
      }
    }

    drawHighlight()
    // スケッチも「コアが持ち、ここは描き直すだけ」。直後の `lookAtSketch` が平面の枠を要るので $effect ではなく同期に描く。
    drawSketches()
    if (sketchId !== null && !sketches.some((item) => item.sketch_id === sketchId)) {
      exitSketch() // undoでスケッチごと消えた
    }
    // 閉じた(別のタブが閉じた・文書が消された場合も含む)。前の文書のモード・最適化の表示・断面・測定を持ち越さない。
    if (wasDocumentOpen && !next.document_open) {
      setMode({ kind: 'idle' })
      resetOptimizeView()
      display.closeSection()
      measure.clear()
    }
    wasDocumentOpen = next.document_open

    // 作業空間(起動中のコマンド・モード)は画面が自分で持たず配信に従う(issue #153)。外(AI か別の画面)からの変更は
    // 札で知らせる: 自分の操作の応答(`client_op` が自分のもの)でなければ外からの変更。
    const own = applied !== null && applied.client_op !== null && myClientOps.has(applied.client_op)
    if (!own && previous !== null) noteWorkspaceChange(previous, next)
    if (applied && (applied.change === 'command' || applied.change === 'mode')) {
      releasePending(applied.client_op)
      return
    }
    // 選択の変更イベントはverbの適用ではない(`change: "selection"`。session.ts の `Applied`)。
    if (applied && applied.change === 'selection') {
      // この画面が出した選択変更だけを数える(`selfSelections`)。AIが書き換えた選択は即適用の引き金にならない。
      if (applied.client_op !== null && myClientOps.has(applied.client_op)) {
        selfSelections += 1
      }
      if (applied.client_op !== null && applied.client_op === clearingOp) clearingOp = null
      releasePending(applied.client_op)
      return
    }
    if (applied && applied.change === 'visibility') {
      // 可視性もverbではない(操作ログに載らない)ので `lastApplied` を動かさない。表示切り替えは
      // `pendingOp` を立てないので、自分の操作かは `myClientOps` が持つ。
      releasePending(applied.client_op)
      // 集合 verb の起動時に表示へ戻したボディが全部戻ったら、既存の割り当てを並べる(`onCommandStart`)。
      const preset = pendingOptimizePreset
      if (preset !== null && applied.client_op !== null && preset.ops.delete(applied.client_op) && preset.ops.size === 0) {
        pendingOptimizePreset = null
        presetSelection(next.optimize[preset.role])
      }
      return
    }
    if (applied && applied.change === 'document') {
      // 開いた(`open_document`)か新規にした(`new_document`)。ファイル名を追い、一覧も取り直す。
      // 前の文書の最適化の表示を持ち越さない(段の添字は目盛りが文書ごとに違うので既定へ戻す)。
      resetOptimizeView()
      display.closeSection() // 断面はその場限り(文書が替わったら切らない状態へ)
      // 視点を合わせ直すのは open_document だけ(文書のスケールが不連続に変わるのは import_step と同じくここだけ。
      // 実測: 小さな部品→大きな板を保存して開き直すと前の視点のまま端が画面外に出た)。新規は空の文書で合わせる内容が無く、
      // 基準グリッドだけ縮める(`viewport.resetGrid` のヒステリシス許可)。
      if (applied.action === 'close') {
        note('文書を閉じた')
      } else if (applied.name !== null) {
        viewport?.frameAll()
      } else {
        viewport?.resetGrid()
      }
      if (applied.action !== 'close') {
        note(applied.name === null ? '新規の文書にした' : `開いた: ${applied.name}`)
      }
      releasePending(applied.client_op)
      lastApplied = applied
      files.refresh()
      return
    }
    if (applied) {
      // 作ったスケッチへはそのまま入る(作ってから改めて「入る」を押させない)
      if (applied.verb === 'sketch_create' && applied.sketch_id) enterSketch(applied.sketch_id)
      if (applied.verb === 'import_step') {
        // 読み込んだモデル全体が入る視点へ合わせ直す。文書のスケールが不連続に変わるのはインポートだけ
        // (実測: 対角約640mmのSTEPで端が画面外に出た)で、ふつうの編集で視点が跳ぶと妨げになるので他へ広げない。
        // ジオメトリはこの関数の先頭の `viewport?.update(changed...)` で届いている(`duocad/net/ws.py` の `_push`)。
        viewport?.frameAll()
        const count = applied.body_ids?.length ?? 0
        note(`STEPを読み込んだ(${count}部品)。そのまま面をクリックして押し出せる`)
      }
      if (releasePending(applied.client_op)) {
        // CommandHostが出した確定が成功したと確認できたので、ここで初めて無起動へ戻す(`commit()` の時点では
        // 成否が分からず、先に戻すとOCCTのフィレット失敗時の打ち直しが壊れる)。`CATEGORY_OF` に無い verb
        // (`FilesPanel` が直接送るぶん)は素通りする: 別コマンドに入力中の CommandHost を外の操作の成功で畳まない。
        const category = CATEGORY_OF.get(applied.verb)
        if (category === 'sketch' || category === 'feature' || category === 'file') {
          host?.commandSucceeded()
        }
        // 荷重の2つは確定のたびに選択を空へ戻す(`OPTIMIZE_EMPTY_START_VERBS`)。上書き代入なので、
        // 残すと次の確定が前の面の大きさ・向きまで書き換える。起動時に空にするだけでは同じverbの連続使用が漏れる。
        if (OPTIMIZE_EMPTY_START_VERBS.has(applied.verb)) presetSelection([])
      }
      lastApplied = applied
    }
  }

  function handlePreview(payload: PreviewPayload): void {
    if (payload.preview_id !== previewId) return // 追い越された応答・中断後の応答は捨てる
    viewport?.showPreview(
      payload.bodies.map((body) => ({ bodyId: body.body_id, geometry: body.geometry })),
    )
    const ghost = payload.sketch_ghost
    if (ghost !== null) {
      // 分裂は確定時にトーストで拒まれる形。入力中はトーストを出さず色だけで知らせる。
      viewport?.showSketchGhosts(ghost.sketch_id, ghost.entities, ghost.pieces === 1 ? 'normal' : 'warn')
    }
    previewing = payload.bodies.length > 0 || (ghost?.entities.length ?? 0) > 0
  }

  function handleEdges(payload: EdgesPayload): void {
    edges = { ...edges, [payload.body_id]: payload }
  }

  function handleVertices(payload: VerticesPayload): void {
    vertices = { ...vertices, [payload.body_id]: payload }
  }

  // ---------------------------------------------------------------- 測定(verbではない)
  const measure = createMeasurement({
    selection: () => selection?.items ?? [],
    modeKind: () => mode.kind,
    viewport: () => viewport,
    fail,
  })

  function handleMeasure(payload: MeasurePayload): void {
    // 結果を先に入れる(`commandSucceeded()` が呼ぶ `onCommandEnd` が結果の有無で選択を残すかを決める)。
    measure.onResult(payload)
    if (releasePending(payload.client_op)) {
      // 測定は成否を待つ入力欄が無いので、返事が来た時点で無起動へ戻してよい
      host?.commandSucceeded()
    }
  }

  // ---------------------------------------------------------------- 選択情報(issue #140。verbではない)
  const selectionInfo = createSelectionInfo({
    selection: () => selection?.items ?? [],
    // 文書を変える操作(人・AI・undo/redo)は必ずこのどちらかを動かす。
    documentVersion: () => `${model?.applied_count ?? 0}/${model?.redo_count ?? 0}`,
    connected: () => status === 'open',
    request: (requestId) => session.inspectSelection(requestId),
  })
  const selectionInfoNames = {
    body: (bodyId: string) => model?.bodies.find((body) => body.body_id === bodyId)?.name ?? bodyId,
    sketch: (id: string) => sketches.find((item) => item.sketch_id === id)?.name ?? id,
    entity: (id: string, entityId: string) => {
      const position = sketches.find((item) => item.sketch_id === id)?.entities.findIndex((e) => e.entity_id === entityId)
      return position === undefined || position < 0 ? entityId : `#${position + 1}`
    },
  }

  // ---------------------------------------------------------------- 選択(コアを叩くだけ)
  const selection = $derived(model?.selection ?? null)
  /** 形状最適化の条件(第3のモード)。正はコア(`duocad/optimize.py`)。 */
  const optimize = $derived(model?.optimize ?? null)
  /**
   * 反復のverbが引数で取る図形のID。共有選択集合から導く。点で選んでも辺で選んでも図形1つを指す。
   * コア側では引数のまま(`duocad/catalog.py` の `pattern_rectangular` の note)。
   */
  const selectedEntities = $derived([
    ...new Set(
      refsOfKind(selection?.items ?? [], 'sketch_entity')
        // 編集中のスケッチのものだけ(反復verbは `sketch_id` と `entities` を別々に送るので、混ざると
        // 他スケッチの図形IDを渡す)。`enterSketch` が他の選択を外すのが本筋で、ここは二重の守り。
        .filter((item) => item.sketch === sketchId)
        .map((item) => item.entity),
    ),
  ])

  const selectedFaces = $derived(refsOfKind(selection?.items ?? [], 'face'))
  /** 選択中のボディ(形状最適化の保持/除外を決めているあいだだけ現れる)。 */
  const selectedBodies = $derived(refsOfKind(selection?.items ?? [], 'body'))
  /**
   * `move_body` の幽霊を出す対象のボディ。コアの `selection.require_bodies` の写し(面・辺・頂点・ボディの参照から
   * `body` を逆引きし、最初に現れた順で重複を畳む)。指し方を問わず動くボディを答える: 絞ると
   * 「面で指したら幽霊が出ないのに実行すると動く」になる。
   */
  const ghostBodyIds = $derived([
    ...new Set(
      (selection?.items ?? [])
        .filter((item): item is ShapeRef | BodyRef => 'body' in item)
        .map((item) => item.body),
    ),
  ])
  /** 選択中の辺(辺を出している間だけ3Dで色が変わる)。 */
  const selectedEdges = $derived(refsOfKind(selection?.items ?? [], 'edge'))

  /** 選択中の頂点(3Dにマーカーで出る)。 */
  const selectedVertices = $derived(refsOfKind(selection?.items ?? [], 'vertex'))

  /**
   * 取り寄せた辺・頂点(`edges` / `vertices`)のうち、ボディの版(`revision`)が今と同じもの(§7)だけを返す。
   * 古い座標・添字は、実物に無い所にマーカーが浮く・別の辺を指す。
   * `requireVisible` は頂点の側だけ(隠れたボディの頂点はサーバが断る)。
   * `$effect` の中から呼ぶと `model` と手元の値の両方が依存に入る(`?.` で短絡する前に読む)。
   */
  function freshPayload<T extends { revision: number }>(
    cache: Record<string, T>,
    bodyId: string,
    options: { requireVisible?: boolean } = {},
  ): T | null {
    const body = model?.bodies.find((item) => item.body_id === bodyId)
    const known = cache[bodyId]
    if (!body || (options.requireVisible === true && !body.visible)) return null
    return known !== undefined && known.revision === body.revision ? known : null
  }

  /** 同じ鍵(`${body}:${revision}`)で2度頼まない(`edgesAsked` / `verticesAsked`)。 */
  function requestOnce(asked: Set<string>, key: string, send: () => void): void {
    if (asked.has(key)) return
    asked.add(key)
    send()
  }

  /**
   * 画面に出す頂点マーカー(参照 + 座標)。版が合っている座標しか使わない(辺と同じ §7)。
   * 座標がまだ届いていないBodyは黙って落ち、下の `$effect` が取りに行くので次の配信で出る。
   */
  const vertexMarkers = $derived.by(() => {
    const markers: VertexMarker[] = []
    for (const item of selectedVertices) {
      const known = freshPayload(vertices, item.body, { requireVisible: true })
      if (known === null) continue
      const point = known.vertices.find((candidate) => candidate.index === item.index)?.point
      if (point) markers.push({ bodyId: item.body, vertexIndex: item.index, point })
    }
    // 頂点を直接ピックさせている間だけ候補を全部出す(`vertexCandidateBody`)。選ばれた頂点しか出ない
    // 既定のままだと、回転中心に使う頂点を指せる的が無い。辺の折れ線と同じく、要求するコマンドの起動中だけ。
    if (vertexCandidateBody !== null) {
      const known = freshPayload(vertices, vertexCandidateBody, { requireVisible: true })
      if (known !== null) {
        const shown = new Set(markers.map((marker) => marker.vertexIndex))
        for (const item of known.vertices) {
          if (shown.has(item.index)) continue
          markers.push({ bodyId: vertexCandidateBody, vertexIndex: item.index, point: item.point })
        }
      }
    }
    return markers
  })

  function drawHighlight(): void {
    viewport?.highlight(selectedFaces.map((item) => ({ bodyId: item.body, faceIndex: item.index })))
    // 選択が勝つ: ボディが選ばれているあいだは保持/除外の役割色より優先(`viewport.applyFaceMaterials` が実体)。
    viewport?.highlightBodies(selectedBodies.map((item) => item.body))
    viewport?.highlightEdges(selectedEdges.map((item) => ({ bodyId: item.body, edgeIndex: item.index })))
    viewport?.showVertices(vertexMarkers)
  }

  // 頂点の取り寄せは候補のボディと1本の `$effect` で行う(`vertexCandidateBody` の下)。
  // ---------------------------------------------------------------- D: 辺を取りに行く
  /**
   * 辺を出す対象のBody。選んでいる形のBodyを優先し、無ければアクティブボディ
   * (アクティブ固定だと、27部品を読んだ文書で見ていない部品の辺が出る)。
   */
  /** 辺だけを食うコマンドの起動時に選択の最初の形状が属していたボディ(`onWantsKinds`)。降りたら null。 */
  let edgeCommandBody = $state<string | null>(null)

  const edgeTargetBody = $derived.by(() => {
    // 見えているボディだけを対象にする。AIが述語で提案した集合は隠したボディにまたがりうる、
    // そこを頼むとサーバが「隠れているものの辺は指せない」と断って見えている辺も出なくなる。
    // メッシュボディも外す(辺も頂点も持たず、サーバが「mesh body」と断る)。
    const visible = new Set(
      (model?.bodies ?? [])
        .filter((body) => body.visible && body.kind !== 'mesh')
        .map((body) => body.body_id),
    )
    const picked = (selection?.items ?? []).find(
      (item): item is ShapeRef => !('sketch' in item) && visible.has((item as ShapeRef).body),
    )
    if (picked) return picked.body
    // 辺だけを食うコマンドの起動で外した面のボディ(起動中は面を指せないので、外すと選び直す手が無い)。
    if (edgeCommandBody !== null && visible.has(edgeCommandBody)) return edgeCommandBody
    // アクティブボディへ落ちるときも同じ判定をする(隠したまま頼むと断られるだけ)。
    const active = model?.active_body_id ?? null
    return active !== null && visible.has(active) ? active : null
  })

  /**
   * 回転中心の頂点を指させている間、または頂点そのものを対象にするコマンド(測定)の起動中に、
   * 候補を出すボディ(出さないなら null)。対象は辺と同じ1つ(`edgeTargetBody`): 全ボディだと
   * 27部品の文書で数千個の的が画面を覆う。
   */
  const vertexCandidateBody = $derived(
    sketchId === null && (wantedPick === 'vertex' || wantedVertices) ? edgeTargetBody : null,
  )

  /** 選択に入った頂点のボディと、候補として出すボディの頂点の座標を取りに行く。見えているBodyだけに頼む(隠れているものはサーバが断る)。 */
  $effect(() => {
    const targets = new Set(selectedVertices.map((item) => item.body))
    if (vertexCandidateBody !== null) targets.add(vertexCandidateBody)
    for (const target of targets) {
      const body = model?.bodies.find((item) => item.body_id === target)
      if (!body || !body.visible) continue
      if (vertices[target]?.revision === body.revision) continue
      requestOnce(verticesAsked, `${target}:${body.revision}`, () => session.getVertices(target))
    }
  })

  /** 今のコマンドが辺を要求しているか(スケッチモード中は出さない)。直接ピック(回転軸)も同じ口を通す。 */
  const wantsEdges = $derived(
    sketchId === null &&
      // 面を1つ指させている間は辺を出さない。辺は面より先に拾われるので、鏡映面を狙ったクリックが
      // 辺に取られ、その辺が対象の選択へ入ってしまう。
      wantedPick !== 'face' &&
      (wantedKinds.includes('edge') || wantedPick === 'edge'),
  )

  $effect(() => {
    if (!wantsEdges || edgeTargetBody === null) {
      viewport?.showEdges(null, [])
      return
    }
    // 版が違う辺は使わない(§7)。可視性は見ない(`edgeTargetBody` が見えているボディしか返さない)。
    const known = freshPayload(edges, edgeTargetBody)
    if (known !== null) {
      viewport?.showEdges(edgeTargetBody, known.edges)
      drawHighlight()
      return
    }
    viewport?.showEdges(null, []) // 古い線を先に消してから頼む
    // `model.bodies` にボディが見つからないときも `'?'` の鍵で1回だけ頼む。
    const body = model?.bodies.find((item) => item.body_id === edgeTargetBody)
    requestOnce(edgesAsked, `${edgeTargetBody}:${body?.revision ?? '?'}`, () => session.getEdges(edgeTargetBody))
  })

  /**
   * 基準3平面を指せるのはスケッチのコマンドを起動している間だけ(辺と同じ)。
   * スケッチモード中は、奥の基準平面を掴むと線が引けなくなるので出さない。
   */
  const wantsBasePlanes = $derived(sketchId === null && wantedPlane)

  $effect(() => {
    // 先に読んでから渡す。`viewport?.showBasePlanes(wantsBasePlanes)` と書くと、初回(`onMount` 前で
    // `viewport` が null)に `?.` が短絡して `wantsBasePlanes` を読まず、依存が空のまま二度と再実行されない
    // (実測で踏んだ。起動しても平面が指せない)。
    const on = wantsBasePlanes
    viewport?.showBasePlanes(on)
  })

  /**
   * 通常モードで領域を指せるのは、領域を要求しているコマンドの起動中だけ。常時指せると、スケッチ平面と
   * 重なる面を狙うクリックが手前の領域に取られる。スケッチモード中はゲートを立てない: そこの領域ピックは
   * `setActiveSketch` 側の規律(そのスケッチの領域だけ)で動いており、触ると別スケッチの領域まで拾う。
   */
  const wantsRegions = $derived(sketchId === null && wantedKinds.includes('region'))

  $effect(() => {
    // 先に読んでから渡す(上と同じ理由)。
    const on = wantsRegions
    viewport?.pickRegions(on)
  })

  /**
   * 辺だけを食うコマンド(フィレット・面取り)の起動中か。面は指せず(`viewport.pickFaces`)、辺は連なりで選ぶ
   * (`viewport.groupEdgeChains`。OCCTはこの2つを辺1本から連なり全体へ広げてかけるので、1本ずつだと光った所より広く丸まる)。
   */
  const edgeOnlyCommand = $derived(
    sketchId === null && wantedKinds.length > 0 && wantedKinds.every((kind) => kind === 'edge'),
  )

  $effect(() => {
    const on = edgeOnlyCommand
    viewport?.pickFaces(!on)
    viewport?.groupEdgeChains(on)
  })

  /**
   * 回転中心の頂点を待っている間だけ、頂点を辺より先に取る(`viewport.preferVertices`)。
   * `move_body` は辺が常に出ているので、既定順(辺が先)だと角を狙ったクリックが必ず辺に取られる。
   */
  $effect(() => {
    const on = wantedPick === 'vertex' || wantedVertices
    viewport?.preferVertices(on)
  })

  function modifySelection(add: SelectionRef[], remove: SelectionRef[]): void {
    if (add.length === 0 && remove.length === 0) return
    session.modifySelection(add, remove, nextOp())
  }

  /**
   * 1つの参照を選択集合の編集へ翻訳する(面・辺・領域で同じ規則)。Shift / Ctrl=トグル、修飾なし=置き換え。
   * 除外専用の修飾キーは無い。置き換えは、1つだけ選んでいるものを再クリックしたら外す。
   * 置き換えのとき自分自身は remove に入れない: コアは remove を後に適用するので足したそばから外れる(`duocad/selection.py` の契約)。
   */
  function editSelection(ref: SelectionRef, mode: PickEvent['mode']): void {
    const current = currentItems()
    const already = current.some((item) => sameRef(item, ref))
    if (mode === 'toggle') {
      session.toggleSelection([ref], nextOp())
    } else if (already && current.length === 1) modifySelection([], [ref])
    else selectOnly(ref)
  }

  /**
   * ウィンドウ選択(左ドラッグ)を選択集合の編集へ翻訳する。修飾なし=囲んだものだけが選ばれている状態にする
   * (空の矩形は「全部外す」)。Shift / Ctrl=囲んだものを1つずつトグルする。
   * 1回の `modify_selection` にまとめる(参照ごとに送ると20個囲んで20往復になる)。
   * 突き合わせは `refKey` の `Set`(総当たりM×Nは、画面全体を囲む実測2835面のドラッグを大きな選択の上へ重ねたときだけ効く)。
   */
  /**
   * 当たった面を選択の参照へ翻訳する。メッシュボディだけはボディ参照へ畳む:
   * メッシュに指せる面は無い(`selection.select_face` が断る)が、ボディ単位なら選べる。
   * 編集verbは `kinds` にボディを持たないので押せないまま、「押せるのに断られる」が画面から消える。
   */
  function pickedRef(bodyId: string, faceIndex: number): SelectionRef {
    if (meshBodyIds.has(bodyId)) return bodyRef(bodyId)
    return faceRef(bodyId, faceIndex)
  }

  /**
   * 素のクリック・矩形選択をトグルとして読むか(`onPick` と `onBoxSelect` で共有)。
   * 集合 verb は既存の割り当てが選択状態で現れる集合代入で、置き換えだと他の割り当てを全部外す。
   * 測定は対象がちょうど2つなので、2つ目のクリックが1つ目を消しては揃わない。立つのは起動中(`activeVerb`)だけ。
   */
  function toggleOnlyMode(mode: PickEvent['mode']): PickEvent['mode'] {
    return commandClickToggles() ? 'toggle' : mode
  }

  /** コマンド起動中の素のクリックをトグルにするか(`activeVerb`)。 */
  function commandClickToggles(): boolean {
    return activeVerb !== null
  }

  /**
   * 保持/除外ソリッドの間はボディ粒度(`wantedKinds` に `'body'` が立つのは `SET_PRESERVE_BODIES` /
   * `SET_EXCLUDE_BODIES` だけ)。面の参照を確定すると `require_selected_bodies` が面を無視し集合が黙って空になる。
   */
  function optimizeSetFaceRef(bodyId: string, faceIndex: number): SelectionRef {
    if (wantedKinds.includes('body')) return bodyRef(bodyId)
    return pickedRef(bodyId, faceIndex)
  }

  function onBoxSelect(event: BoxSelectEvent): void {
    const mode = toggleOnlyMode(event.mode)
    // 辺は連なりごとのまとまり(連なりの辺を2本囲んでも1つ)。トグルはまとまり単位(`toggleUnits`)。
    const chainUnits = new Map<string, SelectionRef[]>()
    for (const item of event.edges) {
      const unit = edgeChainRefs(item.bodyId, item.edgeIndex)
      chainUnits.set(refKey(unit[0]), unit)
    }
    const units: SelectionRef[][] = [
      // 同じボディの面が何枚囲まれても参照は1つ(`refKey` で重複を落とす)。
      ...dedupeRefs(event.faces.map((item) => optimizeSetFaceRef(item.bodyId, item.faceIndex))).map((ref) => [ref]),
      ...chainUnits.values(),
      ...event.vertices.map((item) => [vertexRef(item.bodyId, item.vertexIndex)]),
      ...event.regions.map((item) => [regionRef(item.sketchId, item.index)]),
      ...event.sketchEntities.map((item) => [sketchEntityRef(item.sketchId, item.entityId, item.point)]),
    ]
    const picked = units.flat()
    const current = currentItems()
    const currentKeys = new Set(current.map(refKey))
    if (mode === 'toggle') {
      const { add, remove } = toggleUnits(units, currentKeys)
      modifySelection(add, remove)
      return
    }
    if (picked.length === 0) {
      if (current.length > 0) clearSelection()
      return
    }
    const pickedKeys = new Set(picked.map(refKey))
    modifySelection(
      picked,
      current.filter((item) => !pickedKeys.has(refKey(item))),
    )
  }

  /**
   * その参照だけが選ばれている状態にする(常に置き換え。再クリックで外さない)。
   * `editSelection` と違いトグルの判定を通らない: スケッチを貼る先として面を指す経路が直接呼ぶ。
   * 選択済みの面の再クリックで解除に落ちると、直後の `sketch_create` が「面が選ばれていない」で失敗する。
   */
  function selectOnly(ref: SelectionRef): void {
    const current = currentItems()
    modifySelection([ref], current.filter((item) => !sameRef(item, ref)))
  }

  /** 同じ参照が2つ以上入らないようにする(メッシュの面を畳むと同じボディ参照が並ぶ)。 */
  function dedupeRefs(refs: SelectionRef[]): SelectionRef[] {
    const seen = new Set<string>()
    return refs.filter((ref) => {
      const key = refKey(ref)
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
  }

  /** `selectOnly` の複数版 —— 選択集合を `refs` そのものに置き換える。 */
  function presetSelection(refs: SelectionRef[]): void {
    const current = currentItems()
    // `onBoxSelect` と同じ理由で `Set` 引き(保持/除外ソリッドは数百件になりうる)。
    const refKeys = new Set(refs.map(refKey))
    modifySelection(
      refs,
      current.filter((item) => !refKeys.has(refKey(item))),
    )
  }

  /**
   * コマンドが起動した(`CommandHost.start`)。形状最適化のverbだけが意味を持つ。
   * 集合代入の3つは、起動した役割の既存の割り当て(`state.optimize` の該当配列。`duocad/optimize.py` の契約で
   * そのまま `modifySelection` へ渡せる)を選択状態にして見せる。荷重の2つは空にする(`OPTIMIZE_EMPTY_START_VERBS`)。
   */
  function onCommandStart(verb: string): void {
    measure.clear() // 次の操作に入ったら、前の測定の札は用済み
    pendingOptimizePreset = null
    // 別のverbへ直接移ったら前のverbの終わりとして選択を空にする(`onCommandEnd`)。同じverbの押し直しは終わりではない。
    // `model === null` の早期returnより前に置く(起動そのものは文書が無くても起きる)。
    const switched = activeVerb !== null && activeVerb !== verb
    activeVerb = verb === DEFAULT_SKETCH_COMMAND_ID ? null : verb
    if (model === null) return
    // 荷重の2つは空から始める。「何もしない」ではなく「空を代入する」でなければ前のverbの選択が残る。
    if (OPTIMIZE_EMPTY_START_VERBS.has(verb)) {
      presetSelection([])
      return
    }
    const role = OPTIMIZE_PRESET_ROLE[verb]
    if (role === undefined) {
      // 集合代入の3つは下の `presetSelection` が置き換えるので、ここへ来るのはそれ以外だけ。
      if (switched) clearSelectionIfAny()
      return
    }
    // 非表示のボディに付いた割り当ては選択に並ばない(`state.optimize` の配列は指せるものだけ)。集合 verb は
    // 集合全体を置き換えるので、並ばなかった割り当ては確定で黙って消える。先にそのボディを表示へ戻して並べる。
    const hidden = [
      ...new Set(
        model.optimize.stale.filter((item) => item.role === role && item.code === 'hidden').map((item) => item.body),
      ),
    ]
    if (hidden.length === 0) {
      presetSelection(model.optimize[role])
      return
    }
    const ops = new Set<number>()
    for (const bodyId of hidden) {
      const op = nextOp()
      ops.add(op)
      session.setVisible(bodyId, true, op)
    }
    pendingOptimizePreset = { role, ops }
    note(`非表示だった${OPTIMIZE_ROLE_LABEL[role]}のボディ ${hidden.length} 個を表示した(割り当てを選択に並べるため)`)
  }

  function clearSelectionIfAny(): void {
    if (currentItems().length > 0) clearSelection()
  }

  // ---------------------------------------------------------------- 作業空間(起動中のコマンド・モード。issue #153)
  // 画面は起動中の verb と引数を自分で持たず、サーバへ送って配信された `state.command` に従う(`CommandHost.svelte`)。
  // 応答待ち(`pendingOp`)にはしない: 文書もビューポートも変わらない。
  function beginCommand(verb: string, args: Record<string, unknown>): void {
    session.beginCommand(verb, args, nextOp())
  }

  function setCommandArgs(args: Record<string, unknown>): void {
    session.setCommandArgs(args, nextOp())
  }

  function endCommand(): void {
    session.endCommand(nextOp())
  }

  /** 全モードのコマンド定義から表示名を引く(無ければ verb 名のまま)。 */
  function commandLabel(verb: string): string {
    const found = [...SOLID_COMMANDS, ...SKETCH_PENDING_COMMANDS, ...SKETCH_MODE_COMMANDS, ...OPTIMIZE_MODE_COMMANDS].find(
      (command) => command.id === verb,
    )
    return found?.label ?? verb
  }

  function argLabel(verb: string, name: string): string {
    const found = [...SOLID_COMMANDS, ...SKETCH_PENDING_COMMANDS, ...SKETCH_MODE_COMMANDS, ...OPTIMIZE_MODE_COMMANDS].find(
      (command) => command.id === verb,
    )
    return found?.args.find((spec) => spec.name === name)?.label ?? name
  }

  /** 外(AI か別の画面)が作業空間を変えたら1行で知らせる。自分の操作の応答では呼ばない(`handleState`)。 */
  function noteWorkspaceChange(before: ServerState, after: ServerState): void {
    const was = before.command
    const now = after.command
    if (was === null && now !== null) {
      note(`外から「${commandLabel(now.verb)}」が起動された(AI か別の画面)`)
    } else if (was !== null && now === null) {
      note(
        after.applied_count > before.applied_count
          ? `外から「${commandLabel(was.verb)}」が確定された(AI か別の画面)`
          : `外から「${commandLabel(was.verb)}」が畳まれた(AI か別の画面)`,
      )
    } else if (was !== null && now !== null) {
      if (was.verb !== now.verb) {
        note(`外から「${commandLabel(now.verb)}」に切り替えられた(AI か別の画面)`)
      } else {
        const changed = [...new Set([...Object.keys(was.args), ...Object.keys(now.args)])].filter(
          (name) => JSON.stringify(was.args[name]) !== JSON.stringify(now.args[name]),
        )
        if (changed.length > 0) {
          const shown = changed
            .map((name) => `${argLabel(now.verb, name)}=${now.args[name] === undefined ? '(空)' : JSON.stringify(now.args[name])}`)
            .join(' / ')
          note(`外から「${commandLabel(now.verb)}」の引数が変わった: ${shown}`)
        }
      }
    }
    if (JSON.stringify(before.mode) !== JSON.stringify(after.mode)) {
      const target = after.mode
      note(
        target.kind === 'sketch'
          ? `外からスケッチ ${sketches.find((item) => item.sketch_id === target.sketch_id)?.name ?? target.sketch_id} の編集に移った`
          : target.kind === 'optimize'
            ? '外から形状最適化モードに移った'
            : '外から通常モードに戻った',
      )
    }
  }

  /** 選択を全解除する。配信が届くまでの判定は `currentItems()` が空として扱う。 */
  function clearSelection(): void {
    clearingOp = nextOp()
    session.clearSelection(clearingOp)
  }

  /**
   * 選択集合の差分を組むときに読む今の選択。送った `clear_selection` の配信がまだ届いていない間は空を返す。
   * 配信前の `selection` を読むと、Esc直後に唯一選ばれていた要素をクリックしたとき `editSelection` が解除と判定し、
   * コアでは「全解除 → その要素を外す」と順に効いて選んだはずの要素が選ばれない(サーバは1接続の要求を順に処理する)。
   * 表示(ハイライト・件数)は配信どおりの `selection` を使う。
   */
  function currentItems(): SelectionRef[] {
    return clearingOp !== null ? [] : (selection?.items ?? [])
  }

  /**
   * コマンドが終わった(`CommandHost.onEnd`: Esc・中断ボタン・確定成功)。選択を全解除する。
   * 形を変えるverbの確定ではコアが先に空にしているので効くのは中断と、形を変えない確定(集合代入・測定の失敗等)。
   * 測定の成功だけは残す(読み札と3Dの線は測った選択が変わるまで残る。`measurement.svelte.ts`)。
   * `activeVerb === null` は、`setMode` が先に起動を畳んだ後に遅れて届いた通知で、選択に触らない。
   */
  function onCommandEnd(verb: string): void {
    if (activeVerb === null) return
    activeVerb = null
    if (verb === MEASURE.id && measure.current !== null) return
    clearSelectionIfAny()
  }

  /**
   * `CommandHost` の `onWants`。無起動の `command.args` は常に `[]` なので、`kinds` が空 =
   * 中断/確定成功/モード切替のどれかで降りた、とみなして集合 verb の並べ直し待ちを捨てる。
   */
  function onWantsKinds(kinds: SelectableKind[]): void {
    wantedKinds = kinds
    if (kinds.length === 0) pendingOptimizePreset = null
    // 辺だけを食うコマンドが起動したら、選択に残っている面を外す(辺は残す。面は指せなくなるので外す手が無い)。
    // `untrack`: ここは `CommandHost` の `$effect` の中から呼ばれ、選択を読むとその effect が選択の変化ごとに走り直す。
    if (kinds.length > 0 && kinds.every((kind) => kind === 'edge')) {
      untrack(() => {
        const items = currentItems()
        const first = items.find((item): item is ShapeRef => !('sketch' in item) && 'kind' in item)
        if (edgeCommandBody === null) edgeCommandBody = first?.body ?? null
        modifySelection([], refsOfKind(items, 'face'))
      })
    } else {
      edgeCommandBody = null
    }
  }

  /** 辺の参照を、連なり(`viewport.edgeChain`)の全部の参照へ広げる。 */
  function edgeChainRefs(bodyId: string, edgeIndex: number): SelectionRef[] {
    const indices = viewport?.edgeChain({ bodyId, edgeIndex }) ?? [edgeIndex]
    return indices.map((index) => edgeRef(bodyId, index))
  }

  /**
   * 参照のまとまり(連なり、または参照1つ)ごとにトグルする。全部入っていれば全部外し、そうでなければ欠けている分を足す
   * (1本ずつトグルすると、一部だけ入っていた連なりが入れ替わるだけになる)。
   */
  function toggleUnits(units: SelectionRef[][], currentKeys: Set<string>): { add: SelectionRef[]; remove: SelectionRef[] } {
    const add: SelectionRef[] = []
    const remove: SelectionRef[] = []
    for (const unit of units) {
      if (unit.every((ref) => currentKeys.has(refKey(ref)))) remove.push(...unit)
      else add.push(...unit.filter((ref) => !currentKeys.has(refKey(ref))))
    }
    return { add, remove }
  }

  /** 連なりを1つの単位として選択を編集する(`editSelection` の連なり版。トグルは `toggleUnits`)。 */
  function editEdgeChain(refs: SelectionRef[], mode: PickEvent['mode']): void {
    if (refs.length === 1) {
      editSelection(refs[0], mode)
      return
    }
    const current = currentItems()
    const currentKeys = new Set(current.map(refKey))
    const allIn = refs.every((ref) => currentKeys.has(refKey(ref)))
    if (mode === 'toggle') {
      const { add, remove } = toggleUnits([refs], currentKeys)
      modifySelection(add, remove)
      return
    }
    if (allIn && current.length === refs.length) {
      modifySelection([], refs)
      return
    }
    const keys = new Set(refs.map(refKey))
    modifySelection(refs, current.filter((item) => !keys.has(refKey(item))))
  }

  /** ピックを選択集合の編集へ翻訳する。ここが作るのは「何を足して何を引くか」だけで、結果はコアから返ってくる。 */
  function onPick(event: PickEvent): void {
    if (sketchId !== null) {
      onSketchPick(event)
      return
    }
    const current = currentItems()
    // スケッチを貼る先を待っているとき。基準平面はその場で決まり、面は「選択を置き換えたうえで `'face'` を入れる」。
    // 送るのは選択が確定して配信されてからで、その待ち受けは `CommandHost` が持つ。
    if (host?.wantsPlane()) {
      if (event.plane !== null) {
        host.setBasePlane(event.plane)
        return
      }
      if (event.hit !== null) {
        // メッシュボディには貼れない(平面が取れない。`sketch.resolve_plane` が断る)。
        // ボディ参照へ畳んでも `sketch_create` は面を要求するので、畳まずに理由を出す。
        if (meshBodyIds.has(event.hit.bodyId)) {
          note('メッシュボディにはスケッチを貼れない(平面が取れない)')
          return
        }
        host.useSelectedFaceAsPlane()
        selectOnly(faceRef(event.hit.bodyId, event.hit.faceIndex))
        return
      }
      // 何も無い所をクリックしたら、通常どおり選択解除だけして待ち続ける(下へ落とす)
    }
    // コマンドが辺・頂点を直接待っているとき(回転軸・回転中心)。下の辺・頂点の分岐より前に置く: 後だと
    // 指した辺が共有選択へ入り、コアの `require_bodies` が軸に使った部品まで動かす対象に数える。
    // 待っているものが当たらなかったクリックは素通りさせる(対象の選び直し・解除ができる)。
    // 修飾キー付きは行かない: 待っている間も対象を足す・外す手が要る(素のクリックだけを食う)。
    if (host !== null && event.mode === 'replace') {
      const wants = host.wantsPick()
      if (wants === 'edge' && event.edge !== null) {
        host.setPick(edgeRef(event.edge.bodyId, event.edge.edgeIndex))
        return
      }
      if (wants === 'vertex' && event.vertex !== null) {
        host.setPick(vertexRef(event.vertex.bodyId, event.vertex.vertexIndex))
        return
      }
      if (wants === 'face' && event.hit !== null) {
        host.setPick(faceRef(event.hit.bodyId, event.hit.faceIndex))
        return
      }
    }
    // 素クリックのトグル(`toggleOnlyMode`)は面・辺・頂点・領域のすべてに効かせる(測定は辺・頂点でも測る)。
    const pickMode: PickEvent['mode'] = toggleOnlyMode(event.mode)
    // 辺が当たったら辺。フィレット・面取り・測定の起動中だけ来る。
    if (event.edge !== null) {
      editEdgeChain(edgeChainRefs(event.edge.bodyId, event.edge.edgeIndex), pickMode)
      return
    }
    // 頂点が当たったら頂点。ここへ来るのは「AIが選んだ頂点を人がCtrl+クリックで外す」か「測る頂点を指す」のどちらか。
    if (event.vertex !== null) {
      editSelection(vertexRef(event.vertex.bodyId, event.vertex.vertexIndex), pickMode)
      return
    }
    // 領域が当たったら領域。領域を要求するコマンドの起動中(スケッチ編集中を除く)だけ来る(`wantsRegions`)。
    if (event.region !== null) {
      editSelection(regionRef(event.region.sketchId, event.region.index), pickMode)
      return
    }
    if (event.hit === null) {
      // 何もない所を素でクリック = 選択解除(修飾キー付きは空振りにする)。
      // コマンド起動中は選択を変えない(外したいものは足すのと同じクリックで外す)。
      if (commandClickToggles()) return
      if (event.mode === 'replace' && current.length > 0) clearSelection()
      else if (event.mode === 'replace') note('面を外した')
      return
    }
    // 保持/除外ソリッドの間だけボディ粒度へ畳む(`optimizeSetFaceRef`)。
    editSelection(optimizeSetFaceRef(event.hit.bodyId, event.hit.faceIndex), pickMode)
  }

  // ---------------------------------------------------------------- C: スケッチ
  const sketches = $derived(model?.sketches ?? [])
  /** 今編集しているスケッチ(ツールバーの札が読む)。モード外・まだ届いていなければ null。 */
  const activeSketch = $derived(
    sketches.find((item) => item.sketch_id === sketchId) ?? null,
  )

  function drawSketches(): void {
    viewport?.showSketches(buildSketchDraws(sketches, selection, sketchId))
  }

  /**
   * モード遷移はここ1本だけ。掃除の規則は「行き先が使えない参照を落とす」の1つ(経路が増えても書き忘れが無い):
   *
   * - スケッチの図形: 行き先のスケッチのものでなければ落とす。混ざったまま持ち越すと、Bで
   *   `move_sketch_entities` を実行したときにAの図形が動く(サーバは `entity_id` を素直に解決して黙って通る)。
   * - ボディ(`kind:'body'`): 行き先が最適化モードでなければ落とす(保持/除外の割り当てにしか使えない)。
   * - 面・辺・頂点・領域はどのモードでも残す(スケッチを抜けてそのまま押し出す等が実際の経路)。
   */
  function setMode(next: Mode): void {
    // サーバの作業空間(`state.mode`)を画面のモードに揃える(issue #153)。モードが変わると集めかけのコマンドは
    // サーバが畳む(`Session._apply_mode`)。貼る先待ち(`sketch-pending`)はサーバでは「通常モードで `sketch_create` を
    // 構えている」なので、抜けるときは Esc と同じ `end_command` を送る。既に揃っていれば送らない(自分の `sketch_create`
    // の成功・undo でスケッチが消えた・文書を閉じた、はサーバが先に動いている)。
    if (model !== null) {
      if (mode.kind === 'sketch-pending' && next.kind !== 'sketch-pending' && model.command?.verb === 'sketch_create') {
        endCommand()
      }
      const server = model.mode
      if (next.kind === 'sketch') {
        if (server.kind !== 'sketch' || server.sketch_id !== next.id) session.enterSketch(next.id, nextOp())
      } else if (next.kind === 'optimize') {
        if (server.kind !== 'optimize') session.enterOptimize(nextOp())
      } else if (server.kind === 'sketch') {
        session.leaveSketch(nextOp())
      } else if (server.kind === 'optimize') {
        session.leaveOptimize(nextOp())
      }
    }
    // 確定前の姿を持ち越さない。モードが変わると下の `{#key commandRowKey}` が `CommandHost` ごと作り直すので
    // `onCancelPreview` は来ない。ここで消さないと、`move_body` の半透明ボディや `extrude` の投機実行の幽霊が残り続ける。
    clearPreview()
    const nextSketch = next.kind === 'sketch' ? next.id : null
    const unusable = currentItems().filter((item) => {
      const kind = refKind(item)
      if (kind === 'sketch_entity') return (item as SketchEntityRef).sketch !== nextSketch
      if (kind === 'body') return next.kind !== 'optimize'
      return false
    })
    if (unusable.length > 0) modifySelection([], unusable)
    mode = next
    // `CommandHost` が作り直されるので、起動中だったコマンドは終わりの通知なしに消える。
    activeVerb = null
    hoverPoint = null
    viewport?.setActiveSketch(nextSketch)
    // どの遷移でも同期に描き直す(入る側は直後の `lookAtSketch` が平面の枠を要る。`handleState` と同じ)。
    // 抜ける側にも要る: 抜けたスケッチの点マーカーが次の状態配信まで残り、手前に浮いてクリックを奪いうる
    // (選択の掃除が空振りするとWS往復が無く配信も来ない)。
    drawSketches()
    if (next.kind === 'sketch') viewport?.lookAtSketch(next.id)
  }

  /** 今のモードから見た「スケッチを抜けたときの戻り先」。 */
  function resumeOf(current: Mode): ResumeMode {
    if (current.kind === 'optimize') return 'optimize'
    if (current.kind === 'sketch' || current.kind === 'sketch-pending') return current.resume
    return 'idle'
  }

  /** スケッチモードへ入る(まだ貼る先は決まっていない)。`sketch-pending` では `SKETCH_PENDING_COMMANDS` が起動済みで現れる(`CommandHost` の `startCommandId`)。 */
  function startSketchMode(): void {
    setMode({ kind: 'sketch-pending', resume: resumeOf(mode) })
  }

  /** そのスケッチを開く。`sketch_create` の成功(`handleState`)とブラウザパネルのスケッチ名クリックから呼ばれる。戻り先(`resume`)を持って移るので、抜ければ最適化モードへ戻る。 */
  function enterSketch(id: string): void {
    // 隠したスケッチに入るときは先に表示へ戻す(拒否しない)。編集中のスケッチは `buildSketchDraws` が
    // 可視性に関わらず描くので、サーバの往復を待たずに入ってよい。抜けた後も表示のまま。
    const target = sketches.find((item) => item.sketch_id === id)
    if (target !== undefined && !target.visible) setSketchVisible(id, true)
    setMode({ kind: 'sketch', id, resume: resumeOf(mode) })
  }

  /** ブラウザパネルのスケッチ名クリック。最適化が走っている間はモードを抜けさせない(パネルごと中断ボタンが消える)。 */
  function enterSketchFromBrowser(id: string): void {
    if (optimizeBusy) {
      fail('形状最適化の実行中はスケッチへ入れない(終わるか中断してから)')
      return
    }
    enterSketch(id)
  }

  /** スケッチモードを抜ける。戻り先は入る前のモード(通常 or 最適化)。 */
  function exitSketch(): void {
    setMode(resumeOf(mode) === 'optimize' && shapeOptimizationAvailable === true ? { kind: 'optimize' } : { kind: 'idle' })
  }

  /** 第3のモード(形状最適化)へ入る。「形状最適化」ボタンから(スケッチと同じ作法)。 */
  function enterOptimizeMode(): void {
    setMode({ kind: 'optimize' })
  }

  /** 第3のモードを抜けて通常モードへ戻る。 */
  function exitOptimizeMode(): void {
    setMode({ kind: 'idle' })
  }

  /**
   * スケッチモードのクリック。描いている最中は点、そうでなければ領域の選択。
   * 点を待つコマンドの起動中は、領域の上を押しても点になる(既存の領域の内側に線を引けないと分割できない)。
   */
  function onSketchPick(event: PickEvent): void {
    if (event.point && host?.wantsPoint()) {
      // 当たっている図形へ落としてから渡す。幽霊(`hoverPoint`)も同じ関数を通るので、吸い付いて見える位置と確定結果はずれない。
      const [u, v] = snappedPlanePoint(event.point.u, event.point.v, event.sketchEntity)
      if (host.addPoint(u, v)) return
    }
    // コマンドがスケッチの点・図形を直接待っているとき(回転中心・鏡映軸)。3Dの辺・頂点と同じ規律で
    // 共有選択への翻訳より前に置く(中心や軸に使うものが選択へ入ると、それ自体が動かす/鏡映する対象になる)。
    if (event.sketchEntity && host !== null && event.mode === 'replace') {
      const hit = event.sketchEntity
      const wants = host.wantsPick()
      if (wants === 'sketchPoint' && host.setPick(sketchEntityRef(hit.sketchId, hit.entityId, hit.point))) {
        return
      }
      // 軸に要るのは線分そのものなので、端点の近くを押しても点を落として図形へ畳む(`point` を残すとコアの `mirror_sketch_entities` が軸として読めない)。
      if (wants === 'sketchEntity' && host.setPick(sketchEntityRef(hit.sketchId, hit.entityId, null))) {
        return
      }
    }
    // 図形は領域より先(点・辺は領域の縁に乗っているので、領域が勝つと端点を掴めない)。
    const pickMode = commandClickToggles() ? 'toggle' : event.mode
    if (event.sketchEntity) {
      editSelection(
        sketchEntityRef(
          event.sketchEntity.sketchId,
          event.sketchEntity.entityId,
          event.sketchEntity.point,
        ),
        pickMode,
      )
      return
    }
    if (event.region) {
      editSelection(regionRef(event.region.sketchId, event.region.index), pickMode)
      return
    }
    if (commandClickToggles()) return
    if (event.mode === 'replace' && currentItems().length > 0) {
      clearSelection()
    }
  }
  // ---------------------------------------------------------------- コマンド層との繋ぎ
  /** 揃った引数をサーバへ送る。経路はコマンドの分類(`categoryOf`)だけで決まり、知らない名前はここで断る。 */
  function runCommand(verb: string, args: Record<string, unknown>): void {
    const category = CATEGORY_OF.get(verb)
    if (category === undefined || category === 'ui') {
      // `ui`(スケッチの「選ぶ」)は何も送らない道具なので、通常ここへは来ない。
      fail(`未知のコマンド: ${verb}`)
      return
    }
    switch (category) {
      // -------------------------------------------------------------- C: スケッチ
      case 'sketch': {
        // `sketch_create` だけはスケッチモードの外で走る(入っていることを要求すると永久に起動できない)。
        // 反復の `needsSketchId`(分類は feature だが `sketch_id` が要る)の裏返しの位置にいる。
        if (runsOutsideSketch(verb)) {
          sendTracked(verb, args)
          return
        }
        if (sketchId === null) {
          fail('スケッチモードに入っていない')
          return
        }
        viewport?.showSketchGhosts(sketchId, []) // 本物を頼んだので幽霊は消す
        // 引数名はコマンド定義とサーバの宣言で揃っているので詰め替えない。
        // `move_sketch_entities` だけは `sketch_id` を足さない(選んだ参照が持っている。`runsWithoutSketchId`)。
        sendTracked(verb, runsWithoutSketchId(verb) ? args : { ...args, sketch_id: sketchId })
        return
      }
      // -------------------------------------------------------------- D: フィーチャ
      case 'feature': {
        if (needsSketchId(verb) && sketchId === null) {
          fail('スケッチモードに入っていない(反復はスケッチの図形が対象)')
          return
        }
        if (verb === 'extrude' && !regionsStillExist(selection, sketches)) {
          // 領域はパラメータ編集で分裂・消失しうる(§7)。消えた領域を送るとサーバは添字が範囲外と言うだけ。
          fail('選んだ領域が今のスケッチに無い(図形が変わって分裂・消失した可能性)。選び直す')
          return
        }
        sendTracked(verb, needsSketchId(verb) ? { ...args, sketch_id: sketchId } : args)
        return
      }
      // -------------------------------------------------------------- F: ファイル
      case 'file': {
        // `FilesPanel` と同じ口(`files.run`)へ流す(二重化すると応答待ちの解き方に差が生まれる)。
        files.run(verb, args)
        return
      }
      // -------------------------------------------------------------- 測定(読み取り専用)
      case 'query': {
        // verb と同じ口で送る。返事は状態配信ではなく `{type:'measure'}` で、`handleMeasure` が `client_op` を突き合わせる。
        sendTracked(verb, args)
        return
      }
      default: {
        // 分類を1つ足すとここで型が落ちる(送り方を書かずに分類だけ増やせない)。
        const unhandled: never = category
        fail(`送り方が決まっていない分類: ${String(unhandled)}`)
      }
    }
  }
  /** 幽霊の解決に要る手元の状態(`ghostArgs.ts`)。辺・頂点は版を照合した後の値だけ渡す(`freshPayload`)。 */
  function ghostContext(): GhostContext {
    return {
      sketches,
      editingId: sketchId,
      selection,
      edgesOf: (bodyId) => freshPayload(edges, bodyId),
      verticesOf: (bodyId) => freshPayload(vertices, bodyId),
    }
  }

  /**
   * カーソルの生の (u,v) を、当たっているスケッチの図形か1mmグリッドへ落とす(スナップ)。
   * クリック(`onSketchPick` の `addPoint`)と幽霊(`hoverPoint`)は必ずこの関数を通すこと。段の規則は
   * `entityKinds.snapSketchPoint` の1か所(viewport のカーソル強調も通る)。1mm への丸めは点を待っているときだけ。
   */
  function snappedPlanePoint(u: number, v: number, hit: SketchEntityHit | null): Point2 {
    return snapSketchPoint(
      u,
      v,
      hit,
      (entityId) =>
        sketches
          .find((item) => item.sketch_id === hit?.sketchId)
          ?.entities.find((item) => item.entity_id === entityId),
      viewport?.viewResolution(),
      host?.wantsPoint() === true,
    )
  }

  /**
   * ブラウザだけで描く確定前の姿(`preview: 'local'`)の3経路: これから打つ図形(`ghostEntity`)、
   * 既にある図形の変換後(`ghostCopies`)、既にあるボディの変換後(`move_body`。スケッチの外で走る唯一の経路)。
   * どれもサーバへ何も投げない(Escで中断しても文書が変わらないことを構造で成立させる)。
   */
  function showLocalGhost(verb: string, args: Record<string, unknown>): void {
    const view = viewport
    if (view === null) return
    if (verb === MOVE_BODY.id) {
      // コアの `selection.require_bodies` と同じ導き方(ずれると幽霊と結果が別のボディになる)。空配列でも必ず呼ぶ(古い幽霊を残さない)。
      if (args.mode === 'rotate') {
        const rotate = bodyGhostRotation(args)
        view.showBodyGhosts(
          rotate === null
            ? []
            : ghostBodyIds.map((bodyId) => ({ bodyId, offset: [0, 0, 0] as [number, number, number], rotate })),
        )
        return
      }
      const offset = [args.dx, args.dy, args.dz]
      if (!offset.every((value) => typeof value === 'number' && Number.isFinite(value))) {
        view.showBodyGhosts([])
        return
      }
      view.showBodyGhosts(
        ghostBodyIds.map((bodyId) => ({ bodyId, offset: offset as [number, number, number] })),
      )
      return
    }
    if (sketchId === null) return
    const single = ghostEntity(verb, args)
    if (single !== null) {
      view.showSketchGhosts(sketchId, [single])
      return
    }
    // 空配列でも呼ぶ(対象が消えた・値が使えないときに古い幽霊を残さない)。
    view.showSketchGhosts(sketchId, ghostCopies(verb, args, ghostSources(ghostContext(), verb, args)))
  }

  /** 確定前の姿。ローカル方式はサーバへ投げない(形はブラウザ側で分かっており、投機実行にすると中断しても文書に一度は入る)。 */
  function requestPreview(
    verb: string,
    args: Record<string, unknown>,
    mode: 'server' | 'local',
  ): void {
    if (mode === 'local') {
      // 参照を数値へ解決してから渡す(`resolvePicksForGhost`)。`showLocalGhost` は解決済みの数値しか読まない層。
      showLocalGhost(verb, resolvePicksForGhost(ghostContext(), verb, args))
      return
    }
    if (refireTimer) clearTimeout(refireTimer)
    refireTimer = null
    serverPreview = { verb, args }
    previewId += 1
    session.preview(verb, args, previewId)
  }

  function clearPreview(): void {
    previewId += 1 // 投げてある応答を無効にする(戻ってきても番号が合わない)
    previewing = false
    serverPreview = null
    if (refireTimer) clearTimeout(refireTimer)
    refireTimer = null
    viewport?.clearPreview()
    // 空配列なら宛先のスケッチによらず消える。サーバの幽霊は選んだ領域のスケッチに出るので、
    // 開いているスケッチ(`sketchId`)が無いときも消す。
    viewport?.showSketchGhosts(sketchId ?? '', [])
  }

  function onWindowKeydown(event: KeyboardEvent): void {
    // Esc は測定の札を消す(コマンドの中断は `CommandHost` が同じキーで別に見る)。
    if (event.key === 'Escape') {
      measure.clear()
      return
    }
    if (!(event.ctrlKey || event.metaKey)) return
    const key = event.key.toLowerCase()
    if (key !== 'z' && key !== 'y' && key !== 's') return
    // 編集中のフィールドではブラウザ標準のテキスト取り消しに任せ、文書のundo/redoは発火しない
    // (`CommandHost.onKeydown` と同じ判定)。BUTTONとチェックボックスは除外しない: フォーカスが乗っているだけなら
    // Ctrl+Zは文書のundoでよい(既存の実機検査が固定している)。戻るのは直前のverbで、可視性は操作ログに載らない(`duocad/selection.py` の `set_visible`)。
    const target = event.target as HTMLElement | null
    const isTextEditingField =
      target?.tagName === 'TEXTAREA' ||
      (target?.tagName === 'INPUT' && !['checkbox', 'radio'].includes((target as HTMLInputElement).type))
    // Ctrl+S は今の文書を手で書き直す(`files.save`)。保存は自動で済んでいる(issue #5)ので普段は何も変わらないが、
    // 自動保存が書けなかったときの押し直しと、ブラウザの「ページを保存」を止めるために残す。
    // `preventDefault()` はフォーカスがどこにあっても必ず通す(止めないとページ保存ダイアログが開き、保存が起きたか見えなくなる)。
    // ここだけ `isTextEditingField` で抜けない: Ctrl+S にテキスト編集としての意味は無く、譲る相手がページ保存ダイアログしか
    // いないので、見送ると何も起きない死んだキーになる。
    if (key === 's') {
      event.preventDefault()
      files.save()
      return
    }
    if (!documentOpen) return
    if (isTextEditingField) return
    // 実行中は送らない(サーバも `DocumentBusy` で断る。Undo した条件のまま結果が足される)。
    if (optimizeBusy) {
      event.preventDefault()
      fail('形状最適化の実行中は取り消し・やり直しできない(終わるか中断してから)')
      return
    }
    if (key === 'z' && !event.shiftKey) {
      event.preventDefault()
      session.undo()
    } else if (key === 'y' || (key === 'z' && event.shiftKey)) {
      event.preventDefault()
      session.redo()
    }
  }

  // ---------------------------------------------------------------- 可視性(パーツ一覧から)
  // コアの `set_visible` verb を呼ぶだけ(アプリ層に可視性の状態を持たせない)。
  const partBodies = $derived(model?.bodies ?? [])
  /**
   * 面を持たないボディ(三角形メッシュ)のID。`duocad.document.MESH_KIND` と同じ綴り。
   * ピックの畳み込み(`pickedRef`)に要る: viewport はメッシュも1枚の面(添字1)として描くので、素直に翻訳すると
   * コアの `select_face` が断り、ドラッグの一括選択はまとめて1回で送るのでメッシュが1つ入るだけで選択が丸ごと落ちる
   * (`docs/contracts/ui-optimize-integration.md`「編集 verb を効かせない」)。
   */
  const meshBodyIds = $derived(
    new Set(partBodies.filter((body) => body.kind === 'mesh').map((body) => body.body_id)),
  )

  function setVisible(bodyId: string, visible: boolean): void {
    session.setVisible(bodyId, visible, nextOp())
  }

  function showAllParts(): void {
    for (const body of partBodies) if (!body.visible) setVisible(body.body_id, true)
  }

  function hideAllParts(): void {
    for (const body of partBodies) if (body.visible) setVisible(body.body_id, false)
  }

  /** スケッチの表示/非表示(ボディの `setVisible` と同じ経路。コアの `set_sketch_visible`)。 */
  function setSketchVisible(id: string, visible: boolean): void {
    session.setSketchVisible(id, visible, nextOp())
  }

  function showAllSketches(): void {
    for (const item of sketches) if (!item.visible) setSketchVisible(item.sketch_id, true)
  }

  /** ブラウザの行の名前(コアの `rename`。ボディもスケッチも同じ口)。 */
  function renameItem(target: string, name: string): void {
    sendTracked('rename', { target, name })
  }

  /** ブラウザの行の ✕(コアの `delete_items`)。選択は使わない。 */
  function deleteBody(id: string): void {
    sendTracked('delete_items', { bodies: [id] })
  }

  /** 編集中のスケッチを消すときは先に抜ける(消えた後の配信でも抜けるが、待たずに描くのをやめる)。 */
  function deleteSketch(id: string): void {
    if (id === sketchId) exitSketch()
    sendTracked('delete_items', { sketches: [id] })
  }

  /** 編集中のスケッチは残す(行のトグルが無効なのと同じ線。`BrowserPanel.svelte`)。 */
  function hideAllSketches(): void {
    for (const item of sketches) {
      if (item.visible && item.sketch_id !== sketchId) setSketchVisible(item.sketch_id, false)
    }
  }

  onMount(() => {
    viewport = createViewport(container)
    display.attach(viewport)
    viewport.onPick(onPick)
    viewport.onBoxSelect(onBoxSelect)
    viewport.onPlaneMove((point, hit) => {
      hoverPoint = point === null ? null : snappedPlanePoint(point.u, point.v, hit)
    })
    // 可変解像度: ズームで見え方が変わったらサーバへ知らせ、ソリッド・辺・領域をその粗さで測り直させる。
    // スケッチの折れ線はビューポート側が自分で折り直す。連打の抑制も向こうが持っている。
    viewport.onResolutionChange(sendViewResolution)
    session.start()
    return () => {
      session.dispose()
      serverWait.dispose()
      viewport?.dispose()
      viewport = null
    }
  })

  /**
   * 今の見え方をサーバへ知らせる(可変解像度)。繋がっていないときは黙って捨てる:
   * `session.send` の「操作を送れなかった」札は人が起こした操作向けで、ホイールを回しただけで出るのは誤報。
   */
  function sendViewResolution(resolution: {
    worldPerPixel: number
    sizeHint: number
    focus?: Record<string, number[] | null> | null
  }): void {
    if (status !== 'open') return
    session.setViewResolution(resolution.worldPerPixel, resolution.sizeHint, resolution.focus ?? null)
  }

  // 繋がった(繋ぎ直した)ら今の見え方をもう一度知らせる。サーバ側の目標精度はセッションの持ち物で、
  // サーバが立ち上がり直すと既定の粗さへ戻るため。
  $effect(() => {
    if (status !== 'open') return
    const resolution = viewport?.viewResolution()
    if (resolution && resolution.worldPerPixel > 0) sendViewResolution(resolution)
  })

  // 選択が変わったら描き直す
  $effect(() => {
    void selection
    drawHighlight()
    // スケッチはここで描き直さない(`handleState` で描き直し済み。ここにも置くと全スケッチのジオメトリを2回作り直す)。
  })

  /**
   * 形状最適化(第3のモード)の見た目を描き直す。モードを抜けているあいだは描かない。
   * `viewport.ts` の `FaceRef` はコアの `ShapeRef` と形が違う(`{bodyId, faceIndex}`)のでここで詰め替える。
   */
  $effect(() => {
    if (!optimizeMode || optimize === null) {
      viewport?.setOptimizeState(null)
      return
    }
    viewport?.setOptimizeState({
      preserveBodies: optimize.preserve_bodies.map((item) => item.body),
      excludeBodies: optimize.exclude_bodies.map((item) => item.body),
      fixtures: optimize.fixtures.map((item) => ({ bodyId: item.body, faceIndex: item.index })),
      // 面ごとの how を `(body, index)` で突き合わせる。`loads`(いま指せる面)と `load_specs`(文書が持つ how)は
      // 長さが違いうる(荷重面のボディを隠すと前者からだけ消える)。突き合わない面は描かない。
      loads: optimize.loads.flatMap((item) => {
        const spec = optimize.load_specs.find(
          (one) => one.body === item.body && one.index === item.index,
        )
        return spec === undefined
          ? []
          : [{
              bodyId: item.body,
              faceIndex: item.index,
              magnitude: spec.magnitude,
              direction: spec.direction,
              vector: spec.vector,
            }]
      }),
    })
  })

  /**
   * 最適化モードにいる間、条件の点検と解像度の目盛りを取り直す。引き金はモードの出入りと `state.optimize` の変化で、
   * どちらも軽い(点検は文書を舐めるだけ、解像度は条件面のbboxを数枚測るだけ)。走っている最中は聞き直さない。
   */
  $effect(() => {
    void optimize
    if (!optimizeMode || status !== 'open' || optimizeBusy || shapeOptimizationAvailable !== true) return
    session.optimizeConditions()
    session.optimizeResolution(untrack(() => optimizeElementSizeMm))
  })

  /**
   * `state.optimize.stale`(いま使えない割り当て)をトーストで知らせる。`code` で分岐する(`duocad/optimize.py` の契約):
   * `"hidden"` は部品を隠すたびに出るので出さない側、`"fingerprint"`/`"missing"` は出す側。モードに関係なく見る
   * (割り当てを壊すのは他のverb)。鍵に `code` を含める理由は `seenStaleKeys`。
   */
  $effect(() => {
    if (shapeOptimizationAvailable !== true) return // 形状最適化を出さない版では、その条件の警告も出さない
    const stale = optimize?.stale ?? []
    const next = new Set<string>()
    for (const item of stale) {
      const key = `${item.role}:${item.body}:${item.index ?? ''}:${item.code}`
      next.add(key)
      if (item.code !== 'hidden' && !seenStaleKeys.has(key)) fail(item.reason)
    }
    seenStaleKeys = next
  })

  /** 状態配信を受けた回数(`handleState` が増やす。実機検査が往復の完了を知る)。 */
  let stateEpoch = $state(0)

  // 自動テスト(scripts/smoke*.mjs)がブラウザの中を見るための穴 `window.__duocad`(中身は testHooks.svelte.ts)。
  installTestHooks({
    viewport: () => viewport,
    status: () => status,
    model: () => model,
    partBodies: () => partBodies,
    selectedFaces: () => selectedFaces,
    previewing: () => previewing,
    showLocalGhost,
    lastApplied: () => lastApplied,
    messages: () => messages,
    errorLog,
    fov: () => display.fov,
    applyFov: display.applyFov,
    stateEpoch: () => stateEpoch,
    optimizeMode: () => optimizeMode,
    modeKind: () => mode.kind,
    wantedKinds: () => wantedKinds,
    optimizeRunning: () => optimizeRunning,
    optimizeCancelling: () => optimizeCancelling,
    optimizeConditions: () => optimizeConditions,
    optimizeResolution: () => optimizeResolution,
    optimizeElementSizeMm: () => optimizeElementSizeMm,
    optimizeProgress: () => optimizeProgress,
    optimizeNotice: () => optimizeNotice,
    debugFlags,
    optimizeDebugSpace: () => optimizeDebugSpace,
    optimizeDebugRho: () => optimizeDebugRho,
    optimizeDebugAnchors: () => optimizeDebugAnchors,
    sketches: () => sketches,
    sketchId: () => sketchId,
    hoverPoint: () => hoverPoint,
    wantsBasePlanes: () => wantsBasePlanes,
    wantsRegions: () => wantsRegions,
    edges: () => edges,
    edgeTargetBody: () => edgeTargetBody,
    wantsEdges: () => wantsEdges,
    wantedPick: () => wantedPick,
    vertexCandidateBody: () => vertexCandidateBody,
    selectedEntities: () => selectedEntities,
    vertexMarkers: () => vertexMarkers,
    measurement: () => measure.current,
    wantedVertices: () => wantedVertices,
    lastFile: () => files.lastFile,
    documentList: () => files.list,
    currentDocumentName: () => files.currentName,
  })
</script>

<svelte:window onkeydown={onWindowKeydown} />

<div class="app">
  <header>
    <!-- 開発版の札 / 安定版の更新ボタン(`ChannelBadge.svelte`)。窓を取り違えたら最初に目に入る左端に置く。 -->
    <ChannelBadge />
    <!-- 左サイドバー(ファイル一覧)の開閉。畳むとパネルが画面から消えるので、外に手を出す場所が要る。 -->
    <button
      id="data-panel-toggle"
      class="tag"
      class:on={dataPanelOpen}
      onclick={() => (dataPanelOpen = !dataPanelOpen)}
      title="ファイル一覧の表示/非表示"
      aria-pressed={dataPanelOpen}
    >
      ファイル
    </button>
    <!-- 現在開いているファイル名。名前の無い文書は「(無題)」。書けているかは隣の `#save-state` が言う。 -->
    {#if documentOpen}
      <span id="current-file" class="dim">{files.currentName ?? '(無題)'}</span>
    {/if}
    <!-- 自動保存が書けなかったときだけ出る札(`state.saved`。issue #5)。「保存済み」は黙っていてよく、人の手が要る「未保存」だけを出す(Ctrl+S で書き直せる)。 -->
    {#if documentOpen && !saved}
      <span id="save-state" class="tag on" title="自動保存が書けなかった(Ctrl+Sで書き直す)">未保存</span>
    {/if}
    <!-- 常設の指標は出さない。接続状態は操作したのに送れなかった瞬間にトーストで言う(`session.ts` の `send`)。
         部品数・操作数は人の判断に使わない。プレビュー中の印は、幽霊自身が示す見え方を目指す(`previewing` は実機検査が見る)。 -->
    <!-- 設定ダイアログの開閉トグル。一度決めたらほとんど変更しない設定はここへまとめる(作業中に切り替えるものはその場のパネル)。
         ダイアログは非モーダル(`show()`)で開いている間もこのボタンが押せるので、そのまま閉じる口にする。
         idは `settings-open` のまま(実機検査が名前で掴んでいる)。 -->
    <button
      id="settings-open"
      class="tag"
      onclick={() => {
        settingsOpen = !settingsOpen
        // GPU が使えず CPU へ戻ったことは配られないので、開くたびに取り直す。
        if (settingsOpen) session.femGetStatus()
      }}
      title={settingsOpen ? '設定を閉じる' : '設定を開く'}
    >
      設定
    </button>
    <!-- 右サイドバー(AI)の開閉。ヘッダーの右端(`margin-left:auto` で押し出すので、間に何を足しても右端に残る)。 -->
    <button
      id="sidebar-toggle"
      class="tag end"
      class:on={sidebarOpen}
      onclick={() => (sidebarOpen = !sidebarOpen)}
      title="AIパネルの表示/非表示"
      aria-pressed={sidebarOpen}
    >
      AI
    </button>
  </header>

  <!-- コマンドのツールバー。モードが変わったらコマンド層を作り直す(集めかけの引数を持ち越さない)。
       閉じている(トップ画面)間はツールバーごと出さない(灰色の道具が並ぶと「開いているが使えない」に見える)。 -->
  {#if documentOpen}
    <section class="toolbar">
      <!-- モードの行(`.mode-row`)。入口・札・「抜ける」がここに揃い、verbは下の `CommandHost` にだけ並ぶ。 -->
      {#if mode.kind === 'idle'}
        <div class="mode-row">
          <!-- 行の見出し。下の `CommandHost` の「コマンド」と同じ `.label` の列に揃える(揃わないと、モードとverbが「同じ形のボタンの帯」2本に見える)。 -->
          <span class="label">モード</span>
          <!-- スケッチの入口。idはコマンドボタンと同じ `sketch-create` のまま(実機検査が叩く口)。
               両方が同時にDOMへ出ることはない(この行は通常モードだけ、コマンドボタンは貼る先待ちのときだけ)。 -->
          <button
            id="sketch-create"
            class="tag"
            onclick={startSketchMode}
            title="スケッチモードに入る(このあと貼る先の基準平面か面をクリックする)"
          >
            スケッチ
          </button>
          <!-- 形状最適化(第3のモード)への入口。viewport上に「これをクリックすれば入れる」実物が無いのでボタンを置く(UI指針1の裏返し)。 -->
          {#if shapeOptimizationAvailable === true}
            <button
              id="optimize-enter"
              class="tag"
              onclick={enterOptimizeMode}
              title="形状最適化モードに入る(保持/除外ソリッド・拘束面・荷重面を決める)"
            >
              形状最適化
            </button>
          {/if}
        </div>
      {/if}
      <!-- スケッチモードだが、まだどのスケッチも開いていない(貼る先のクリック待ち)。下の `CommandHost` には `sketch_create` が起動済みで出ている。 -->
      {#if mode.kind === 'sketch-pending'}
        <div class="mode-row">
          <span class="label">スケッチ</span>
          <span id="sketch-mode" class="tag on">貼る先をクリック(基準平面 XY/XZ/YZ か、ソリッドの面)</span>
          <button id="sketch-exit" onclick={exitSketch}>抜ける</button>
        </div>
      {/if}
      <!-- スケッチモードの札とモード操作。idは `SketchPanel.svelte` にあった頃と同じ(実機検査が叩く口)。 -->
      {#if mode.kind === 'sketch' && activeSketch}
        <div class="mode-row">
          <span class="label">スケッチ</span>
          <span id="sketch-mode" class="tag on">
            編集中 {planeLabel(activeSketch.plane, '面に貼ったスケッチ')}
            ・図形 {activeSketch.entities.length} ・領域 {activeSketch.regions.length}
          </span>
          <button id="sketch-look" onclick={() => sketchId && viewport?.lookAtSketch(sketchId)}>
            正面から見る
          </button>
          <button id="sketch-exit" onclick={exitSketch}>抜ける</button>
        </div>
      {/if}
      {#if optimizeMode}
        <div class="mode-row">
          <span class="label">形状最適化</span>
          <span id="optimize-mode" class="tag on">
            保持 {optimize?.preserve_bodies.length ?? 0} ・除外 {optimize?.exclude_bodies.length ?? 0}
            ・拘束 {optimize?.fixtures.length ?? 0} ・荷重 {optimize?.loads.length ?? 0}
          </span>
          <!-- 走っている間は抜けさせない(抜けるとパネルごと中断ボタンが消え、止める手段がゼロになる)。 -->
          <button id="optimize-exit" disabled={optimizeBusy} onclick={exitOptimizeMode}>
            抜ける
          </button>
        </div>
      {/if}
      <!-- verbの行。鍵はモードのタグそのもの(スケッチAからBへ移るときは作り直さない。`commandRowKey`)。 -->
      {#key commandRowKey}
        <CommandHost
          bind:this={host}
          {selection}
          busy={pendingOp !== null || optimizeBusy}
          {selfSelections}
          commands={mode.kind === 'sketch'
            ? SKETCH_MODE_COMMANDS
            : mode.kind === 'sketch-pending'
              ? SKETCH_PENDING_COMMANDS
              : optimizeMode
                ? OPTIMIZE_MODE_COMMANDS
                : SOLID_COMMANDS}
          defaultCommandId={mode.kind === 'sketch' ? DEFAULT_SKETCH_COMMAND_ID : undefined}
          startCommandId={mode.kind === 'sketch-pending' ? SKETCH_CREATE.id : undefined}
          hover={hoverPoint}
          entities={selectedEntities}
          shared={model?.command ?? null}
          onBegin={beginCommand}
          onSetArgs={setCommandArgs}
          onEndShared={endCommand}
          onRun={runCommand}
          onPreview={requestPreview}
          onCancelPreview={clearPreview}
          onNote={note}
          onStart={onCommandStart}
          onEnd={onCommandEnd}
          onWants={onWantsKinds}
          onWantsPlane={(wants) => (wantedPlane = wants)}
          onWantsPoint={(wants) => viewport?.setSketchCursorWanted(wants)}
          onWantsPick={(target) => (wantedPick = target)}
          onWantsVertices={(wants) => (wantedVertices = wants)}
        />
      {/key}
    </section>
  {/if}

  <!-- 右サイドバーを閉じている間は `--sidebar-w` を0にする(トーストが右端へ寄る。`Toast.svelte`)。 -->
  <div class="main" style:--sidebar-w={sidebarOpen ? null : '0px'}>
    <!-- 左サイドバー: 保存済みファイルの一覧。開閉で変わるのは flex の配分だけで、`.viewport` 自身のサイズ指定は動かない(styles.css の不変条件)。 -->
    {#if dataPanelOpen}
      <aside class="data-panel">
        <FilesPanel
          documents={files.list}
          currentName={files.currentName}
          {documentOpen}
          onRename={files.rename}
          onOpen={files.open}
          onNew={files.create}
          onClose={files.close}
          onCopy={files.copy}
          onDelete={files.remove}
          onExportStep={files.exportStep}
          onExportStl={files.exportStl}
          thumbnailUrl={files.thumbnailUrl}
          onNeedThumbnail={files.wantThumbnail}
        />
      </aside>
    {/if}

    <!-- ビューポートと、その上に浮かぶブラウザの入れ物。`.stage` が浮かせる側の位置の基準で、大きさを決めるのは
         `.viewport` だけ(ブラウザは position:absolute でフローの外なので、開閉してもカメラのアスペクトに触らない)。 -->
    <div class="stage">
      <!-- canvas はこの div の中で three.js が作る。Svelte はここの中身を触らない -->
      <div class="viewport" bind:this={container}></div>
      <WaitBar visible={serverWait.visible} />

      <!-- 最初の状態が届く前も覆う(出さないとグリッドが一瞬見えてから消える)。 -->
      {#if !documentOpen}
        <StartScreen
          waiting={!showStart}
          documents={files.list}
          filesPanelOpen={dataPanelOpen}
          onOpen={files.open}
          onNew={files.create}
          onShowFiles={() => (dataPanelOpen = true)}
          thumbnailUrl={files.thumbnailUrl}
          onNeedThumbnail={files.wantThumbnail}
        />
      {/if}

      <!-- 測定の読み札(issue #10)。結果があるときだけ浮く(docs/UI.md「常設の指標を置かない」)。 -->
      {#if measure.text !== ''}
        <div id="measure-result" class="measure-readout">{measure.text}</div>
      {/if}

      {#if documentOpen}
        <SelectionInfoPanel
          item={selectionInfo.item}
          error={selectionInfo.error}
          names={selectionInfoNames}
          besideOptimizePanel={optimizeMode}
        />
        <BrowserPanel
          bodies={partBodies}
          {sketches}
          activeSketchId={sketchId}
          originVisible={display.originVisible}
          section={display.section}
          onToggle={setVisible}
          onShowAll={showAllParts}
          onHideAll={hideAllParts}
          onEnterSketch={enterSketchFromBrowser}
          onToggleSketch={setSketchVisible}
          onShowAllSketches={showAllSketches}
          onHideAllSketches={hideAllSketches}
          onToggleOrigin={display.applyOriginVisible}
          onRename={renameItem}
          onDeleteBody={deleteBody}
          onDeleteSketch={deleteSketch}
          onSectionToggle={display.toggleSection}
          onSectionPlane={display.setSectionPlane}
          onSectionPosition={display.setSectionPosition}
          onSectionInvert={display.setSectionInverted}
        />
      {/if}

      <!-- 形状最適化を走らせるパネル。最適化モードのあいだだけ出す。`.stage` 内の position:absolute なので `.viewport` の寸法には触らない(styles.css の不変条件)。 -->
      {#if optimizeMode}
        <OptimizePanel
          conditions={optimizeConditions}
          resolution={optimizeResolution}
          elementSizeMm={optimizeElementSizeMm}
          minWidthMm={optimizeMinWidthMm}
          running={optimizeBusy}
          cancelling={optimizeCancelling}
          progress={optimizeProgress}
          notice={optimizeNotice}
          onElementSize={handleOptimizeElementSize}
          onMinWidth={handleOptimizeMinWidth}
          onRun={runOptimization}
          onCancel={() => session.cancelOptimization()}
          {searchSteps}
          stepIndex={searchStepShownIndex}
          stepFollowsLatest={searchStepSelected === null}
          stepsShown={searchStepsShown}
          stepVoxels={searchStepVoxels}
          finishable={searchStepsFinishable}
          finishing={optimizeFinishing}
          onSelectStep={(index) => (searchStepSelected = index)}
          onShowSteps={(shown) => (searchStepsShown = shown)}
          onFinish={finishOptimization}
        />
      {/if}

      <!-- 設定ダイアログ。`.stage` の中に置くのはブラウザの右に出すため(左右のサイドバーを開閉してもブラウザとの隣接が保たれる)。
           画面右上の `position: fixed` だとAIサイドバーを閉じたときビューキューブの70%を覆った(実測)。大きさは持たない。 -->
      <SettingsDialog
        open={settingsOpen}
        fov={display.fov}
        onFov={display.applyFov}
        aiStatus={ai.status}
        onSetApiKey={(key) => session.aiSetApiKey(key)}
        {femStatus}
        onFemDevice={(device) => session.femSetDevice(device)}
        showGpu={shapeOptimizationAvailable === true}
        onClose={() => (settingsOpen = false)}
      />
    </div>

    <!-- 右サイドバー。AiPanel だけ(完全にAI専用)。`{#if}` で `<aside>` ごと出し分け、`.viewport` のサイズ指定は動かさず flex の配分だけが変わる。 -->
    {#if sidebarOpen}
      <aside class="sidebar">
        <AiPanel
          status={ai.status}
          transcript={ai.transcript}
          onSend={ai.send}
          onCancel={() => session.aiCancel()}
          onConfigure={(providerKind, baseUrl) => {
            session.aiConfigure(providerKind, baseUrl)
            // 設定を変えると会話が作り直されるので、新しい相手にも読ませておく。
            session.aiPrepare()
          }}
          onSetThinking={(thinking) => session.aiSetThinking(thinking)}
        />
      </aside>
    {/if}

    <!-- エラーの札。大きさは持たない(position:absolute)ので `.viewport` の寸法には触らない。出す側は `fail()` の1本だけ。 -->
    <Toast />
  </div>
</div>
