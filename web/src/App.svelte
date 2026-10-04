<script lang="ts">
  /**
   * アプリ層の組み立て — 接続・ビューポート・パネルを繋ぐだけの場所。
   *
   * **「面を選ぶ → 距離を打つ → Enter」の状態機械も、引数を集める仕組みも
   * `commands/CommandHost.svelte` に1つだけあり**、verbはそこへ載せるだけになる
   * (コマンドが増えても、ここは変わらない)。
   *
   * **選択の正はコアにある。** ブラウザ側に持たせると、実装計画書§3.5が定める
   * 「選択集合もコア側(永続・双方向)」を満たせない —— ここは `state.selection` を
   * 描き直すだけで、**選択の値を1つも覚えていない**。可視性と同じ形である。
   *
   * ここに残っている責務は2つだけ:
   *
   * 1. **ピック → コアの選択への翻訳**(修飾キーで追加・除去・置き換えを決める)
   * 2. **プレビューの往復**(コマンド層が「何を試すか」を決め、ここが投げて幽霊を描く)
   */
  import { onMount } from 'svelte'
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
  import type { MeasurePayload, OptimizeResolutionStep } from './session'
  import {
    OPTIMIZE_EMPTY_START_VERBS,
    OPTIMIZE_MODE_COMMANDS,
    OPTIMIZE_PRESET_ROLE,
    OPTIMIZE_ROLE_LABEL,
  } from './commands/optimize'
  import OptimizePanel from './OptimizePanel.svelte'
  import { optimizeNoticeFor, type OptimizeNotice } from './optimizeNotice'
  import { isOutOfRange, judgeMinWidth, minWidthProblem, stepAt } from './minWidth'
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
  import FilesPanel from './FilesPanel.svelte'
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
    type OptimizeConditionsReport,
    type OptimizeDebugPayload,
    type OptimizeProgress,
    type OptimizeResolutionPayload,
    type OptimizeResultPayload,
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
  import {
    createViewport,
    type BoxSelectEvent,
    type PickEvent,
    type SketchEntityHit,
    type VertexMarker,
    type Viewport,
  } from './viewport'

  /**
   * ツールバーの中身(モードごと)。**状態機械は1つのまま**で、変わるのは一覧と既定だけ。
   * ソリッド側の一覧には2つ、スケッチ側には反復の2つが載る ——
   * 領域押し出しは**両方に居る**(描いた直後に押し出すのと、モードを抜けてから
   * 領域を選んで押し出すのは、どちらも実際に使う)。
   *
   * **`sketch_create` はもうここに載らない**。あれはverbの行に置くと
   * 「押すと戻ってくる操作」(押し出し・フィレット)と同じ見た目で**モードを切り替える**
   * ボタンになる。いまはモードの行(`.mode-row`)に上げてあり、押すと `sketch-pending` へ
   * 入って `SKETCH_PENDING_COMMANDS` が起動済みで現れる(下の `mode` のdocstring)。
   */
  const SOLID_COMMANDS = [
    ...SOLID_FEATURE_COMMANDS,
    // **`file` 分類はこの1つだけ**(理由は `commands/files.ts` のモジュールdocstring)。
    // 書き出し(`export_step`/`export_stl`)と
    // 保存/開く/新規は左サイドバーに残るので、`CommandDef` を持たずここにも載らない。
    // `IMPORT_SVG` はスケッチモードのコマンドへ移り、`SKETCH_COMMANDS`(下)側に載っている。
    IMPORT_STEP,
    // **測定は verb ではない**(`query`。文書もログも変えない)が、verb→target の枠に
    // そのまま乗るのでここに並ぶ(`commands/measure.ts`)。通常モードだけ。
    MEASURE,
  ]
  const SKETCH_MODE_COMMANDS = [...SKETCH_COMMANDS, ...SKETCH_FEATURE_COMMANDS]

  /**
   * verb名 -> そのコマンドの分類。**送信経路の分岐はこれだけで決まる。**
   *
   * 知らない名前は `undefined` になり、`runCommand` がその場で断る。
   * **モードごとの一覧を全部含める** —— 1つ忘れると `runCommand` がそのモードのverbを
   * 「未知のコマンド」で断ってしまう(`sketch_create` は `SOLID_COMMANDS` に無いので、
   * `SKETCH_PENDING_COMMANDS` を足し忘れると即これを踏む)。
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
  /** 送って配信待ちの `clear_selection` の `client_op`(`currentItems()`)。描画に使わない。 */
  let clearingOp: number | null = null

  /** 投げたプレビューの番号(古い応答を捨てるため。0は「投げていない」)。 */
  let previewId = 0
  /** 画面に幽霊が出ているか(テスト用フックが見る)。 */
  let previewing = $state(false)

  // ---------------------------------------------------------------- モード(verbより1つ上の次元)
  /**
   * スケッチを抜けたときの戻り先。**「一時停止からの復帰」を型で持つ** ——
   * 最適化モードの最中に(ブラウザのスケッチ名クリックで)スケッチへ入ったら、抜けたときに
   * 最適化モードへ戻る。
   */
  type ResumeMode = 'idle' | 'optimize'
  /**
   * **モードはverbより1つ上の次元**。
   *
   * モード = **文書のどの部分系を編集対象にしているか**(通常=ボディ群 / スケッチ=`sketches[i]` /
   * 最適化=`Document.optimize`)であり、verb = **その対象に1操作を適用すること**。次元が
   * 違うので、同じツールバーの行に並べない —— モードの行(`.mode-row`)が上、verbの行
   * (`CommandHost`)が下。
   *
   * **不正な状態が表現できない**のが型にした理由である —— `BrowserPanel` のスケッチ名
   * クリックが「スケッチと最適化モードが同時に真」になる穴を実際に開けたことがあり(QA指摘)、
   * タグ付きユニオンならその状態が最初から書けない。
   *
   * `sketch-pending` は**スケッチモードに入ったが、まだどのスケッチも開いていない**状態
   * (貼る先のクリック待ち)。これは新設した状態ではなく、**通常モードで `sketch_create` を
   * 起動している間**として今までも存在していたものを、モードの側へ持ち上げただけである
   * —— だから手数は変わらない(「スケッチ」を押す → 貼る先をクリックの2手)。
   *
   * **コアには持ち込まない。** `duocad/` 側にモードの概念は1つも無く(`Document` にフラグが
   * 無い・verbは毎回 `sketch_id` を明示で受ける・MCPのツール一覧はモードによらず固定)、
   * それが人とAIの交互操作を成立させている(人がスケッチAを見ている間にAIがBを触れる)。
   * モード遷移は「1回のundo / 操作ログ1行 / MCP1呼び出し」のどれとも一致しないので
   * verbではない —— ここは画面だけの状態である。
   */
  type Mode =
    | { kind: 'idle' }
    | { kind: 'sketch-pending'; resume: ResumeMode }
    | { kind: 'sketch'; id: string; resume: ResumeMode }
    | { kind: 'optimize' }
  let mode = $state<Mode>({ kind: 'idle' })

  /**
   * 今どのスケッチを編集しているか(null なら編集していない)。**図形も領域もコアが持つ**。
   *
   * **`mode` からの派生**にしてあるので、これを読む側(この画面と `viewport.ts` で計100箇所
   * 以上)は今までと1文字も変わらない。`sketch-pending` では **null のまま**なのが要点で、
   * 「基準平面を指せるのは `sketchId === null` のときだけ」等の既存の規律
   * (`wantsBasePlanes` / `wantsEdges` / `wantsRegions`)がそのまま貼る先待ちに効く。
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
    restoreLastInputs(): void
    commandSucceeded(): void
  } | null>(null)

  /**
   * 第3のモード(形状最適化)に入っているか。**対応する文書実体が無い**ので、スケッチと違い
   * 画面側だけの状態でよい(バックエンドには一切知らせない)。**状態機械は増やさない** ——
   * 変わるのはツールバーの一覧(`OPTIMIZE_MODE_COMMANDS`)と、それに応じた描画だけ。
   *
   * **スケッチとの排他は `Mode` が持つ** —— タグ付きユニオンでは `sketch` と `optimize` が
   * 同じ1つの値の別の枝なので、「同時に真」が書けない。要求の保持(スケッチから戻ってくる)は
   * `ResumeMode` が担う。
   */
  const optimizeMode = $derived(mode.kind === 'optimize')
  /**
   * 直近にトーストへ出した `stale` の鍵(`role:body:index:code`)。**`code` を含める** ——
   * 含めないと、`hidden`(出さない側)として一度記録された
   * 対象が後で `fingerprint`/`missing`(出す側)に変わっても、role/body/indexだけが一致する
   * ために「既に見た」と誤判定され、二度と通知されない。**消えたら忘れる**
   * (次に見た集合で作り直す)ので、直ってからまた壊れれば同じ組でも再度出す。
   */
  let seenStaleKeys = new Set<string>()

  // ---------------------------------------------------------------- D: 辺とフィーチャ
  /** 起動中のコマンドが選択から取る種別(`CommandHost` が知らせる)。 */
  let wantedKinds = $state<SelectableKind[]>([])
  /**
   * 起動中のコマンド(無起動・スケッチの既定「選ぶ」は null)。`CommandHost` の
   * `onStart` / `onEnd` と `setMode` だけが書く。普通の変数でよい(描画に使わない)。
   *
   * **起動中は素のクリックが選択のトグルになり(`toggleOnlyMode`)、終わると選択を全解除する
   * (`onCommandEnd`)。** 起動していない間は従来どおり(素のクリックは置き換え)なので、
   * 先に選んでからverbを起動する手・AIが選んだ選択をそのまま使う手は変わらない。
   *
   * 保持/除外ソリッド・拘束面の集合 verb は起動した時点で既存の割り当てが選択状態で現れ、
   * 測定は対象が「ちょうど2つ」なので、置き換えのままだと成り立たない(どちらも起動中のトグルで満たされる)。
   */
  let activeVerb: string | null = null
  /**
   * 集合 verb の起動時に表示へ戻した非表示ボディの `set_visible` の応答待ち。**全部戻ってから**
   * 既存の割り当てを選択に並べる(`onCommandStart`)。描画には使わないので普通の変数でよい。
   */
  let pendingOptimizePreset: { role: 'preserve_bodies' | 'exclude_bodies' | 'fixtures'; ops: Set<number> } | null = null
  /**
   * 起動中のコマンドが**基準平面を要求している**か(`CommandHost` が知らせる)。
   * 辺と同じ規律で、要求している間だけビューポートで指せるようにする。
   */
  let wantedPlane = $state(false)
  /**
   * 起動中のコマンドが**図形の直接ピックを待っている**種別(`CommandHost` が知らせる)。
   * 辺・基準平面と同じ規律で、待っている間だけ**その種別を指せるようにする**。
   */
  let wantedPick = $state<PickTarget | null>(null)
  /**
   * 起動中のコマンドが**頂点そのものを対象にする**か(`SelectionArgSpec.pickVertices`。測定。
   * `CommandHost` が知らせる)。回転中心のピック(`wantedPick === 'vertex'`)と同じ扱いで、
   * 対象ボディの頂点を候補として出し、辺より先に取る。
   */
  let wantedVertices = $state(false)
  /** 取り寄せた辺(body_id -> その版の折れ線)。**版が変われば捨てる**。 */
  let edges = $state<Record<string, EdgesPayload>>({})
  /** 取り寄せ中のもの(`${body_id}:${revision}`)。同じ要求を何度も投げないため。 */
  let edgesAsked = new Set<string>()
  /** 頂点の座標(**選択に頂点が入っているBodyの分だけ**取り寄せる)。 */
  let vertices = $state<Record<string, VerticesPayload>>({})
  /** 取り寄せ中のもの(辺と同じ形の印)。 */
  let verticesAsked = new Set<string>()

  // ---------------------------------------------------------------- E: ファイルと履歴
  /** ファイル操作と `duocad/models/` の一覧。応答待ち(`pendingOp`)はこちらの持ち物なので貸す。 */
  const files = createDocumentFiles({
    runVerb: (verb, args, op) => session.runVerb(verb, args, op),
    nextOp,
    hold: (op) => (pendingOp = op),
    release: releasePending,
    note,
    panelOpen: () => dataPanelOpen,
  })
  /**
   * 今の文書がそのままファイルに載っているか(ヘッダの `#save-state`)。**サーバの写し**
   * (`Session.saved`)。自動保存が書けなかったときだけ false になる —— 無題で何も変えていない
   * 文書は true(書くものが無い)で、最初の変更で `untitled-<n>.json` が自動で付く。
   */
  const saved = $derived(model?.saved ?? true)
  /**
   * 左サイドバー(ファイル一覧)を出しているか。**見た目だけの状態**で、コアには送らない
   * (モデルの状態ではないので操作ログにも残さない)。
   *
   * 畳むとビューポートが横に広がる —— `.viewport` は ResizeObserver で自分の寸法を
   * 見張っており(`viewport.ts` の `applySize`)、カメラのアスペクトも追随する。
   * **開閉のたびに射影が変わる**ので、画面の座標を覚えて使い回す側(実機検査)は
   * 開閉を挟んだら取り直すこと。
   */
  let dataPanelOpen = $state(true)
  /**
   * 右サイドバー(`AiPanel` だけ = **完全にAI専用**)を開いているか。
   *
   * 左サイドバー(`dataPanelOpen`)と**同じ形**にしてある —— `{#if}` で `<aside>` ごと
   * 出し分け、開閉ボタンはヘッダーの逆端(`#sidebar-toggle`)。`.viewport` のサイズ指定は
   * 動かさず、flex の配分だけが変わる(左と同じ不変条件)。
   */
  let sidebarOpen = $state(true)
  /**
   * 設定ダイアログ(`SettingsDialog.svelte`)を出しているか。**既定は閉じている。**
   * 見た目だけの状態で、コアにも操作ログにも行かない。
   */
  let settingsOpen = $state(false)
  /** 画角・原点ギズモ・断面(どれも文書の状態ではない)。 */
  const display = createDisplaySettings({ fail })
  /**
   * この画面が出した `client_op` の控え。
   *
   * **`pendingOp` では足りない** —— パラメータの打ち直しや可視性の切り替えは
   * 「応答待ち」を立てない経路なので、`pendingOp` と突き合わせると
   * **自分でやったのに印が付かない**(パラメータ編集は中心的な操作の1つ)。
   * 出した番号を全部覚えておいて、返ってきた `applied.client_op` で照合する。
   */
  const myClientOps = new Set<number>()
  /**
   * **この画面のクリックで選択が変わった回数**(コマンド層の「即適用」の引き金)。
   *
   * Enterの要らないコマンド(`command.ts` の `needsEnter`)は、引数が揃った瞬間に適用する。
   * その引き金は**人がこの画面で指したこと**でなければならない —— 選択集合はAIも
   * `modify_selection` で書き換えられるので、「選択が1つになった」だけを見て発火させると、
   * **人が何も触っていないのに文書が変わる**。
   *
   * 見分けは、返ってきた `client_op` が
   * `myClientOps` にあるかどうかでできる: あれば、それはこの画面が出した選択変更である。
   * **数を渡すだけ**にして
   * あるのは、コマンド層に「誰が変えたか」の判断を持ち込まないため(向こうは
   * 「起動してから増えたか」しか見ない)。
   */
  let selfSelections = $state(0)

  // ---------------------------------------------------------------- G: アプリ内AIエージェント
  const ai = createAiChat({ send: (text) => session.aiSend(text), fail })

  const session = createSession({
    onState: handleState,
    onError: (message, where) => {
      // `where` はどのverbで失敗したかを言う印だが、**`send` はverbではない**
      // (「繋がっていないので送れなかった」。2026-08-27にヘッダの常設ステータスを外して以来、
      // 繋がっていないことに気づく唯一の札になった)。内部の語を頭に付けると、
      // その1枚だけが人間の言葉でなくなるので付けない。
      fail(where === 'send' ? message : `エラー[${where}] ${message}`)
      pendingOp = null
      if (where === 'preview') clearPreview()
      // 辺の取り寄せが失敗したら「頼んだ」印も消す(次に起動したときに取り直せる)
      if (where === 'get_edges' || where === 'send') edgesAsked.clear()
      if (where === 'get_vertices' || where === 'send') verticesAsked.clear()
      // **失敗しても、やり直せる状態にして返す**。OCCTのフィレットは半径に対して
      // 単調でなく(§7: r=10.0 失敗 / r=10.5 成功)、すぐ近くの値で打ち直すのが実際の
      // 使い方になる。選択はコア側にあるので失敗では消えない —— ここで戻すのは
      // 打った値だけでよく、24本選び直しにはならない。
      if (CATEGORY_OF.get(where) === 'feature') host?.restoreLastInputs()
    },
    onWarning: (message) => fail(`警告 ${message}`),
    onStatus: (next) => {
      // 切断・再接続の試行は黙る(`session.ts` が「開けなかった試行」だけトーストへ回す)。
      // キープアライブで切れた直後は自動で繋ぎ直すだけで、戻ってきたら開くことが多い。
      status = next
      // (再)接続したら今のAI設定を取り直す(サーバは再起動していなくても、
      // 前回の接続が切れている間に別の画面が `ai_configure` した可能性がある)。
      if (next === 'open') session.aiGetStatus()
      // 人が最初の依頼を打っている間に、AIへツール定義を読ませておく(遅い機種で分単位かかる)。
      if (next === 'open') session.aiPrepare()
      // (再)接続したら `models/` の一覧も取り直す(サーバの再起動・別セッションの
      // 保存で増減しうる。`FilesPanel.svelte` が空のまま固まらないようにする)。
      if (next === 'open') files.refresh()
      // 応答待ちの最中に接続が切れたら、その `client_op` はもう返ってこない。
      // 捨てないと再接続後も「直前の操作の応答待ち」が残り続け、Enterが永久に拒否される
      // (`onerror` を伴わない正常クローズで踏む)。
      if (next !== 'open' && pendingOp !== null) {
        pendingOp = null
        clearPreview()
      }
      // 全解除の配信も同じく返ってこない(残すと以後の差分が空の選択に対して組まれ続ける)。
      if (next !== 'open') clearingOp = null
      // **投げ損ねた辺の要求を覚えたままにしない**。接続が切れて
      // いる間の要求は送られずに捨てられるが、`edgesAsked` には残る —— 再接続しても
      // 版が同じなら「もう頼んだ」と判断して二度と取りに行かず、**そのBodyの辺が
      // 出ないまま**フィレットが使えなくなる(`pendingOp` を捨てるのと同じ形の穴)。
      if (next !== 'open') {
        edgesAsked.clear()
        verticesAsked.clear()
        // 実行の押下への返事(`optimize_status` / `optimize_error`)はもう来ない。走っているかは
        // 繋ぎ直したときにサーバが最初に送る `optimize_status` で取り直す。
        optimizeStarting = false
      }
    },
    onPreview: handlePreview,
    onEdges: handleEdges,
    onVertices: handleVertices,
    onMeasure: handleMeasure,
    onFile: files.onFile,
    onAiStatus: ai.onStatus,
    onAiToolCall: ai.onToolCall,
    onAiResult: ai.onResult,
    onAiError: ai.onError,
    onOptimizeConditions: (report) => (optimizeConditions = report),
    onOptimizeResolution: (payload) => (optimizeResolution = payload),
    onOptimizeStatus: (running, cancelling) => {
      optimizeRunning = running
      optimizeStarting = false
      // **中断中の印は「走っている間」だけ**。走り終わったら畳む —— 畳まないと、
      // 次に実行を押した瞬間に中断表示から始まって見える。
      optimizeCancelling = running && cancelling ? true : running ? optimizeCancelling : false
    },
    onOptimizeProgress: (event) => (optimizeProgress = event),
    onOptimizeDebug: handleOptimizeDebug,
    onOptimizeResult: handleOptimizeResult,
    onOptimizeError: (message, problems, cancelled, alreadyRunning) => {
      optimizeStarting = false
      // **別の実行が走っているという断りは、その実行の表示を消さない**(進捗の行も通知も
      // その実行のもの)。トーストで知らせるだけにする。
      if (alreadyRunning) {
        fail(`最適化: ${message}`)
        return
      }
      optimizeProgress = null
      // **中断はエラーではない**(人がそう言ったのだから、赤いトーストで驚かせない)。
      // パネルには残す —— 「押したのに何も増えていない」の理由が画面から読める必要がある。
      if (cancelled) {
        optimizeNotice = {
          kind: 'info',
          lines: ['中断した。文書は1ビットも変わっていない(結果のボディは足していない)'],
        }
        return
      }
      // **デバッグ表示は消さない**(2026-09-05 QA指摘への回答)。失敗したときこそ
      // 「塊が悪いのか最適化が悪いのか」を見たいので、`stage:'derive'` で届いた塊と
      // 節点は残す。古くなる心配は無い —— 次の実行が `runOptimization()` の先頭で消す。
      optimizeNotice = { kind: 'bad', lines: [message, ...problems] }
      fail(`最適化: ${message}`)
    },
  })

  // ---------------------------------------------------------------- H: 形状最適化を走らせる
  //
  // **配りもの(`state.optimize`)とは別の口**である —— あちらは「条件がどう置かれているか」、
  // ここにあるのは「走らせられるか / どの解像度で / いま何反復目か / 結果はどうだったか」で、
  // どれも操作ログに載らない(`duocad/catalog.py` の `category="protocol"`)。
  let optimizeConditions = $state<OptimizeConditionsReport | null>(null)
  let optimizeResolution = $state<OptimizeResolutionPayload | null>(null)
  /** 解像度の目盛りの位置。**0が既定(いちばん粗い)**で、増やすほど細かい。 */
  let optimizeStepIndex = $state(0)
  let optimizeMinWidthMm = $state<number | null>(null)
  /** 直前に「下限割れ」だったか(トーストの立ち上がり検知用、描画には使わない普通の変数でよい)。
   *  下限割れが続いているあいだ(1文字打つごと等)にトーストを連打しないための状態。 */
  let optimizeMinWidthWasInvalid = false
  let optimizeRunning = $state(false)
  /** 実行を押してからサーバの返事(`optimize_status` / `optimize_error`)が届くまで。**二度押しを止める**
   *  —— 返事が戻るまで実行ボタンが押せたままだと、2回目が「既に走っている」の断りになる。 */
  let optimizeStarting = $state(false)
  /** 走っている、または走り出すところ。**文書を変える操作を画面から出さない**(サーバも断る)。 */
  const optimizeBusy = $derived(optimizeRunning || optimizeStarting)
  let optimizeCancelling = $state(false)
  let optimizeProgress = $state<OptimizeProgress | null>(null)
  /** **次に走らせるまで消えない通知**(トーストは時間で消えるので、結果の信用度はこちら)。 */
  let optimizeNotice = $state<OptimizeNotice | null>(null)

  /** いま選ばれている段。目盛りが縮んだときのために毎回丸め直す(`stepAt`。OptimizePanel と同じ丸め)。 */
  const optimizeStep = $derived(stepAt(optimizeResolution?.steps ?? [], optimizeStepIndex))

  function runOptimization(): void {
    if (optimizeBusy) return
    optimizeStarting = true
    optimizeProgress = null
    optimizeNotice = null
    // **前の実行のデバッグ表示を捨ててから走らせる。** 残すと、新しい条件を解いている
    // あいだ古い塊が重なって見え、切り分けの道具が切り分けを妨げる。
    clearOptimizeDebug()
    session.runOptimization({
      elementSizeMm: optimizeStep?.element_size_mm ?? null,
      minWidthMm: optimizeMinWidthMm,
      debug: debugKindsFor(debugFlags),
    })
  }

  /**
   * 最小幅の判定を見て、**範囲外へ新しく入った瞬間だけ**トーストで知らせる(タスク仕様「下回ったら...
   * トーストで値の変更を促す」「解像度を変えると下限も変わる」)。割れたまま1文字ずつ打ち直しても
   * 連打しない —— 赤い入力欄と実行不能ボタンが「いま無効」を常時示しているので、トーストの役目は
   * 「気づかせる」の1回だけでよい。`optimizeMinWidthWasInvalid` は入力欄と段の変更の**2経路で共有する
   * 1つの印**(立ち上がりの判定)で、読み書きはここに閉じる。
   */
  function noteMinWidthValidity(
    value: number | null,
    step: OptimizeResolutionStep | null,
    message: (problem: string) => string,
  ): void {
    const verdict = judgeMinWidth(value, step)
    const invalidNow = isOutOfRange(verdict)
    if (invalidNow && !optimizeMinWidthWasInvalid) fail(message(minWidthProblem(verdict, step!) ?? ''))
    optimizeMinWidthWasInvalid = invalidNow
  }

  /** 最小幅の入力欄が変わった。 */
  function handleOptimizeMinWidth(value: number | null): void {
    optimizeMinWidthMm = value
    noteMinWidthValidity(value, optimizeStep, (problem) => `最小幅 ${value}mm: ${problem}。値を見直すこと`)
  }

  /** 解像度の段を変える。既に入っている最小幅が新しい下限・上限を外れたら、黙って実行可能なまま残さない。 */
  function handleOptimizeStep(index: number): void {
    optimizeStepIndex = index
    noteMinWidthValidity(
      optimizeMinWidthMm,
      stepAt(optimizeResolution?.steps ?? [], index),
      (problem) =>
        `解像度を変えたので最小幅の範囲が変わった。いまの入力 ${optimizeMinWidthMm}mm: ${problem}。値を見直すこと`,
    )
  }

  /** 文書を差し替えたとき、前の文書について出していた最適化の表示を戻す。 */
  function resetOptimizeView(): void {
    optimizeNotice = null
    optimizeProgress = null
    optimizeStepIndex = 0
    optimizeMinWidthMm = null
    optimizeMinWidthWasInvalid = false
    optimizeResolution = null
    optimizeConditions = null
    clearOptimizeDebug()
  }

  // ---------------------------------------------------------------- H2: 開発用のデバッグ表示
  //
  // **URLパラメータでだけ出る**(`debugFlags.ts`)。フラグが1つも立っていなければ、
  // ここは `session.runOptimization` に空配列を渡すだけで、サーバも画面も何もしない ——
  // **製品の既定の画面は1ピクセルも変わらない。**
  //
  // probe との違いは「デモ題材に固定されていない」こと(このタスクの完了条件2)。
  // 材料は**いま走らせた最適化そのもの**から来るので、利用者が置いた任意の条件・
  // 任意のボディで出る。
  const debugFlags: DebugFlags = readDebugFlags(
    typeof location === 'undefined' ? '' : location.search,
  )
  // **`$state.raw`**(丸ごと差し替えるだけ)。深い `$state` にすると最大15万要素の配列が
  // プロキシで包まれ、下の `$effect` が読むたびに要素ごとの追跡を払う。
  let optimizeDebugSpace = $state.raw<OptimizeDebugPayload | null>(null)
  let optimizeDebugRho = $state.raw<OptimizeDebugPayload | null>(null)
  let optimizeDebugAnchors = $state.raw<OptimizeDebugPayload | null>(null)
  /** `?voxel=1` のときだけ入る、隠す結果ボディ。**表示だけ**(文書には触れない)。 */
  let optimizeDebugHiddenBodyId = $state<string | null>(null)

  function clearOptimizeDebug(): void {
    optimizeDebugSpace = null
    optimizeDebugRho = null
    optimizeDebugAnchors = null
    optimizeDebugHiddenBodyId = null
  }

  /**
   * デバッグ配信が届いた。塊は `stage: 'derive'` で1回、面アンカーは `stage: 'mapping'`(探索が
   * 使う確定表)で1回、密度場は `stage: 'final'` で1回届く。同じ `kind` が来たら後から来たほうで
   * 置き換える。
   */
  function handleOptimizeDebug(payload: OptimizeDebugPayload): void {
    if (payload.kind === 'space') optimizeDebugSpace = payload
    else if (payload.kind === 'rho') optimizeDebugRho = payload
    else if (payload.kind === 'anchors') optimizeDebugAnchors = payload
  }

  /**
   * デバッグ表示をビューポートへ当てる。**フラグが1つも立っていなければ呼ばない** ——
   * `setOptimizeDebug(null)` すら送らないので、既定の画面には触れる経路が無い。
   */
  $effect(() => {
    if (!anyDebugFlag(debugFlags)) return
    // **依存を先に読み切ってから呼ぶ。** `viewport?.setOptimizeDebug({...})` と直接書くと、
    // 初回に `viewport` がまだ未定義(`onMount` で入る)のとき**オプショナル連鎖が
    // 引数の評価ごと飛ばす**ので、この効果は依存を1つも登録できず**二度と再実行されない**
    // —— 症状は「フラグを付けても何も出ない」で、型検査にもコンソールにも出ない。
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
      rho: voxels(optimizeDebugRho, debugFlags.voxel),
      anchors:
        debugFlags.anchors && optimizeDebugAnchors?.fixed && optimizeDebugAnchors.load
          ? {
              // **格子も渡す**(球の半径が要素サイズから決まる。`?debug=anchors` 単独でも
              // 同じ大きさになるように。2026-09-05 QA指摘)。
              grid: optimizeDebugAnchors.grid,
              fixed: optimizeDebugAnchors.fixed,
              load: optimizeDebugAnchors.load,
            }
          : null,
      hiddenBodyId: optimizeDebugHiddenBodyId,
    }
    viewport?.setOptimizeDebug(next)
  })

  /**
   * 結果を受けた。**`state` で分かれる**(`'done'` と、それ以外の6値。`20260904-1704`
   * の契約①、段階5で `binary_search.SearchResult.status` の3値ぶん増え、境界条件の確定で
   * `boundary_mapping_failed` が増えた)。
   *
   * **`"done"` 以外は文書が1ビットも変わっていない**ので、そのことを通知に書く ——
   * 「走ったのに何も増えていない」を、利用者が画面から読めるようにするため。
   * `warning` はトーストにも出る(`onWarning` 経由)が、あちらは気づかせる役で、
   * **読み返す役はここ**である。
   */
  function handleOptimizeResult(result: OptimizeResultPayload): void {
    optimizeProgress = null
    optimizeNotice = optimizeNoticeFor(result) // 文の組み立ては `optimizeNotice.ts`
    if (result.state === 'done') {
      note(`最適化が終わった: ${result.name}`)
      // **`?voxel=1`: 平滑化メッシュへ差し替えず、ボクセルのまま止める。**
      // **数十msだけ両方が見える**(密度場はメッシュ化より前に届き、隠す相手の `body_id` が
      // 分かるのは結果が届いてからなので、その間だけ重なる。2026-09-05 QA指摘)。
      // 消すには「まだ存在しないボディを先に隠す」口が要るので、過渡として受け入れている。
      // probe は最初からボクセルしか描いていなかったので「差し替えない」で済んだが、
      // 本体の結果は**文書に入るメッシュボディ**なので、代わりに**表示だけ隠す**。
      // 文書は1ビットも変えない(隠すのはビューポートのローカルな見え方だけ)。
      if (debugFlags.voxel && result.body_id) optimizeDebugHiddenBodyId = result.body_id
    }
  }

  /**
   * **情報**を1行、ステータスバーへ出す(状態の説明・成功の報告)。
   *
   * エラーはここへ来ない —— `fail()` を使うこと。分けているのは表示先が違うからではなく、
   * **「操作が拒まれた」に人間が気づけるかどうかが違う**からである(Toast.svelte のdocstring)。
   */
  function note(message: string): void {
    if (!message) return
    messages = [message, ...messages].slice(0, 4)
  }
  // ---------------------------------------------------------------- 操作の番号と応答待ち
  function nextOp(): number {
    opCounter += 1
    myClientOps.add(opCounter) // この番号で返ってきた操作は「自分」(上のdocstring)
    return opCounter
  }

  /**
   * 番号を取って応答待ち(`pendingOp`)にし、verb を送る(`runCommand` の sketch / feature / query)。
   * ファイル操作は `files.run`(文書を変える verb のときだけ待つ)で、ここは通さない。
   */
  function sendTracked(verb: string, args: Record<string, unknown>): void {
    const op = nextOp()
    pendingOp = op
    session.runVerb(verb, args, op)
  }

  /** 自分が出して待っている操作の応答なら待ちを解く(解いたら true)。 */
  function releasePending(clientOp: number | null): boolean {
    if (clientOp === null || clientOp !== pendingOp) return false
    pendingOp = null
    return true
  }

  // ---------------------------------------------------------------- コアからの状態
  function handleState(next: ServerState, changed: BodyGeometry[], applied: Applied | null): void {
    model = next
    files.onState(next)
    // **状態が1通届くたびに1つ増える番号**(実機検査のための穴。`__duocad.stateEpoch`)。
    // これが無いと「サーバの往復がまだ終わっていない」と「もう何も変わらない」を
    // 外から区別できず、**要求が出ていく前の画面を『落ち着いた』と読む**検査になる
    // (2026-08-30に実際にそう読んで、入りと切りが同じ値に見えた)。
    stateEpoch += 1

    // 差分更新: 版が変わったBodyだけをビューポートへ渡す。27部品のうち1つを押し出しても
    // 描き直すのはその1つ(サーバ側の差分送信をここで無駄にしない。§2)。
    viewport?.update(changed.map((body) => ({ bodyId: body.body_id, geometry: body.geometry })))
    // 文書から消えたBody(サーバの契約: 知っているのに state.bodies に無いID)があれば
    // ビューポートからも取り除く。
    viewport?.prune(next.bodies.map((body) => body.body_id))
    // **これはサーバ投機実行のプレビュー(`showPreview`)だけの話**である。本物が届くと
    // `viewport.update()` がそれを落とすので、待っている印もここで降ろす。
    // **ローカル幽霊(`showBodyGhosts`)はここに含めない** —— あちらは同じ `update()` の中で
    // 届いた形から組み直されて出続ける(`viewportTypes.ts` の `update` の項)。
    if (changed.length > 0) previewing = false

    drawHighlight()
    // スケッチも「コアが持ち、ここは描き直すだけ」(可視性・選択と同じ形)。
    // **$effect ではなくここで同期に描く** —— 直後の `lookAtSketch` が平面の枠を要るので、
    // 描画が1手遅れると「入った瞬間に正面を向く」が効かない。
    drawSketches()
    if (sketchId !== null && !sketches.some((item) => item.sketch_id === sketchId)) {
      exitSketch() // undoでスケッチごと消えた
    }
    // **消えた図形のIDを持ち続ける心配は無くなった**(2026-08-30) —— 反復の対象は
    // 共有選択集合から導くようになり、選択の掃除はコア側の責務になった。

    // **選択の変更イベントはverbの適用ではない**(`change: "selection"` で見分ける。
    // session.ts の `Applied` 参照)。verbの応答としてだけ `lastApplied` を動かす。
    if (applied && applied.change === 'selection') {
      // **この画面が出した選択変更だけ**を数える(上の `selfSelections` のdocstring)。
      // AIが書き換えた選択はここを素通りするので、即適用の引き金にならない。
      if (applied.client_op !== null && myClientOps.has(applied.client_op)) {
        selfSelections += 1
      }
      if (applied.client_op !== null && applied.client_op === clearingOp) clearingOp = null
      releasePending(applied.client_op)
      return
    }
    if (applied && applied.change === 'visibility') {
      // **可視性もverbではない**(操作ログに載らない)ので `lastApplied` を動かさない。
      // 効果は同じ配信の `state.bodies[].visible` に載っており、ここで見るのは
      // 「自分が出した操作か」の突き合わせだけ(表示切り替えは `pendingOp` を立てない
      // 経路なので、印は `myClientOps` の側が持っている)。
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
      // **開いた**(`open_document`)か**新規にした**(`new_document`)。保存と同じく、ここで
      // ファイル名を追い、一覧も取り直す(他の操作でファイルが増減している可能性は保存と同じく
      // 否定できない)。**新規は `name` が `null`** で来るので、触っているファイルの表示は
      // 「(無題)」へ戻る —— ただし表示そのものを更新するのは上の `next.current_name` で、
      // ここが見るのは「開いたのか新規にしたのか」の区別だけである。
      // **前の文書の最適化の表示を持ち越さない**(結果の通知・解像度の段・最小幅・`?voxel=1` で
      // 隠した結果ボディ)。段の添字は目盛りが文書ごとに違うので、既定(いちばん粗い)へ戻す。
      resetOptimizeView()
      display.closeSection() // 断面はその場限り(文書が替わったら切らない状態へ)
      // **開いた(`open_document`)ときだけ視点を合わせ直す。** 文書のスケールが不連続に
      // 変わるのは import_step と同じくここだけ(実測: 小さな部品→大きな板を保存し、開き直すと
      // 前の文書の視点のまま端が画面外に出た)。**新規(`new_document`)は空の文書**なので
      // 合わせる内容が無く、`frameToContent()` は箱が空のまま何もしない(呼んでも無害だが、
      // 完了条件が言う「文書のスケールが不連続に変わるのはインポートとopen_documentだけ」に
      // 忠実に、対象を開いた場合だけへ絞る)。
      // **新規のときは基準グリッドだけ縮める。** カメラは動かさないが、前の文書で広がった
      // グリッド(基準平面グリッドはモデルの大きさに追随する)は空の文書に戻ったので
      // `BASE_EXTENT` へ縮めてよい(`viewport.resetGrid` のヒステリシス許可。2026-08-30)。
      if (applied.name !== null) {
        viewport?.frameAll()
      } else {
        viewport?.resetGrid()
      }
      note(applied.name === null ? '新規の文書にした' : `開いた: ${applied.name}`)
      releasePending(applied.client_op)
      lastApplied = applied
      files.refresh()
      return
    }
    if (applied) {
      // 作ったスケッチへはそのまま入る(作ってから改めて「入る」を押させない)
      if (applied.verb === 'sketch_create' && applied.sketch_id) enterSketch(applied.sketch_id)
      // **読み込んだら何ができる状態になったか**を言う。読み込みの経路は1本だけ
      // (`import_step`)で、必ず編集可能なボディまでできる。
      if (applied.verb === 'import_step') {
        // **読み込んだモデル全体が入る視点へ合わせ直す**。文書のスケールが不連続に
        // 変わるのはインポート(2026-08-30の本人実測: 対角約640mmのSTEPを読んで
        // 端が画面外に出た)だけで、押し出し等ふつうの編集で視点が跳ぶと操作の妨害に
        // なるので、この分岐以外へは広げない。**このジオメトリはもう届いている**——
        // `handleState` は自身の先頭で `viewport?.update(changed...)` を呼んでおり
        // (このifより上)、import_step の応答は state 配信1通の中に delta.bodies(実体)と
        // applied(この分岐)が一緒に載って届く(`duocad/net/ws.py` の `_push`)ので、
        // 順序を気にする必要が無い。
        viewport?.frameAll()
        const count = applied.body_ids?.length ?? 0
        note(`STEPを読み込んだ(${count}部品)。そのまま面をクリックして押し出せる`)
      }
      if (releasePending(applied.client_op)) {
        // **CommandHostが出した確定が、実際に成功したと確認できた**——ここで
        // 初めて無起動へ戻す。`commit()` の時点(`onRun` を呼んだ直後)ではまだ成否が
        // 分からず、先に戻すとOCCTのフィレット失敗時の打ち直し(`CommandHost.commit()`の
        // コメント参照)が壊れる。**`CommandHost` を通るverbだけが対象**で、`CATEGORY_OF` に
        // 載っていないもの(`export_step`/`export_stl`/`save_document` 等、`FilesPanel` が
        // 直接送るぶん)は `undefined` になってここを素通りする —— 人が別コマンドに
        // 入力しているCommandHostを、外で走った操作の成功で畳んではならない。
        const category = CATEGORY_OF.get(applied.verb)
        if (category === 'sketch' || category === 'feature' || category === 'file') {
          host?.commandSucceeded()
        }
        // **荷重の2つは確定のたびに選択を空へ戻す**(`OPTIMIZE_EMPTY_START_VERBS`)。
        // 他のverbは確定後も選択が残ったまま構え続けるが、荷重は上書き代入なので
        // 残ったままだと**次の確定がその面の大きさ・向きまで書き換える** ——
        // 面Aへ50Nを置いた直後に面Bを足して確定すると、面Aも同じ値になる。
        // 起動時に空にするだけでは足りない(同じverbを続けて使う経路がここを通る)。
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
      // 分裂は確定時にトーストで拒まれる形。入力中はトーストを出さず色だけで知らせる(本人決定)。
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
    // 結果を先に入れる —— `commandSucceeded()` が呼ぶ `onCommandEnd` が結果の有無で
    // 選択を残すかを決める。
    measure.onResult(payload)
    if (releasePending(payload.client_op)) {
      // 測定は成否を待つ入力欄が無いので、返事が来た時点で無起動へ戻してよい
      host?.commandSucceeded()
    }
  }

  // ---------------------------------------------------------------- 選択(コアを叩くだけ)
  const selection = $derived(model?.selection ?? null)
  /** 形状最適化の条件(第3のモード)。**配りものの正はコア**(`duocad/optimize.py`)。 */
  const optimize = $derived(model?.optimize ?? null)
  /**
   * 反復のverbが引数で取る図形のID。
   *
   * **共有選択集合から導く**(2026-08-30)。スケッチの図形が選択の一種になった
   * (`sketch_entity`)ので、以前の「スケッチパネルの一覧でだけ選べる」局所状態は要らない ——
   * パネルごと消し、3Dのクリック/ドラッグで選んだものをそのまま反復の対象にする。
   * 点で選んでも辺で選んでも**その図形1つ**を指す(重複は畳む)。
   *
   * **コア側では今までどおり引数**である(`entities` は選択集合から取らない。
   * `duocad/catalog.py` の `pattern_rectangular` の note)—— 変えたのは人が選ぶ場所だけ。
   */
  const selectedEntities = $derived([
    ...new Set(
      refsOfKind(selection?.items ?? [], 'sketch_entity')
        // **編集中のスケッチのものだけ**(反復verbは `sketch_id` と `entities` を別々に送るので、
        // 混ざると「このスケッチに無い図形ID」をサーバへ渡すことになる)。`enterSketch` が
        // 他のスケッチの選択を外すのが本筋で、ここはそれを二重に守る側である。
        .filter((item) => item.sketch === sketchId)
        .map((item) => item.entity),
    ),
  ])

  const selectedFaces = $derived(refsOfKind(selection?.items ?? [], 'face'))
  /**
   * 選択中のボディ(形状最適化の保持/除外を決めているあいだだけ現れる。
   * `refKind` で見分ける —— `BodyRef` は `kind: 'body'` を持つ)。
   */
  const selectedBodies = $derived(refsOfKind(selection?.items ?? [], 'body'))
  /**
   * `move_body` の幽霊を出す対象のボディ。**コアの `selection.require_bodies` の写し** ——
   * 面・辺・頂点・ボディの参照から `body` を逆引きし、最初に現れた順で重複を畳む。
   *
   * `selectedBodies`(`kind === 'body'` の参照だけ)とは別物である。あちらは形状最適化の
   * 役割付けが「ボディを直に指した」ものだけを見るのに対し、こちらは**指し方を問わず
   * どのボディが動くか**を答える —— コアが同じ規則で動かすので、ここを絞ると
   * 「面で指したら幽霊が出ないのに実行すると動く」という食い違いになる。
   */
  const ghostBodyIds = $derived([
    ...new Set(
      (selection?.items ?? [])
        .filter((item): item is ShapeRef | BodyRef => 'body' in item)
        .map((item) => item.body),
    ),
  ])
  /** 選択中の辺(**辺を出している間だけ**3Dで色が変わる)。 */
  const selectedEdges = $derived(refsOfKind(selection?.items ?? [], 'edge'))

  /** 選択中の頂点(**選ばれているものだけ**が3Dにマーカーで出る)。 */
  const selectedVertices = $derived(refsOfKind(selection?.items ?? [], 'vertex'))

  /**
   * 取り寄せた辺・頂点(`edges` / `vertices`)のうち、**ボディの版(`revision`)が今と同じもの**だけを返す(§7)。
   * 形が変わった後の古い座標・添字は、実物のどこにも無い所にマーカーが浮く・別の辺を指す。
   * `requireVisible` は頂点の表示・取り寄せの側だけ(隠れたボディの頂点はサーバが断る)。辺と幽霊の解決は見ない。
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
   * 画面に出す頂点マーカー(参照 + 座標)。
   *
   * **版が合っている座標しか使わない**(辺と同じ規律 §7)—— 形が変わった後の古い座標を
   * 描くと、実物のどこにも無い所にマーカーが浮く。座標がまだ届いていないBodyは黙って
   * 落ちるが、下の `$effect` が取りに行くので次の配信で出る。
   */
  const vertexMarkers = $derived.by(() => {
    const markers: VertexMarker[] = []
    for (const item of selectedVertices) {
      const known = freshPayload(vertices, item.body, { requireVisible: true })
      if (known === null) continue
      const point = known.vertices.find((candidate) => candidate.index === item.index)?.point
      if (point) markers.push({ bodyId: item.body, vertexIndex: item.index, point })
    }
    // **頂点を直接ピックさせている間だけ候補を全部出す**(`vertexCandidateBody`)。
    // 選ばれた頂点しか出ていない既定のままだと、回転中心に使う頂点を**指せる的が画面に
    // 1つも無い**(頂点を選択へ足すコマンドが無いので、そもそも出しようがない)。
    // **辺の折れ線と同じ規律** —— 要求しているコマンドを起動している間だけに限る。
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
    // **選択が勝つ**(タスク仕様の色の規則)。ボディが選ばれているあいだは保持/除外の
    // 役割色より優先してハイライト色を出す(`viewport.applyFaceMaterials` が実体)。
    viewport?.highlightBodies(selectedBodies.map((item) => item.body))
    viewport?.highlightEdges(selectedEdges.map((item) => ({ bodyId: item.body, edgeIndex: item.index })))
    viewport?.showVertices(vertexMarkers)
  }

  /**
   * 選択に入った頂点の座標を取りに行く。
   *
   * **辺と違って「コマンドが要求している間」ではない** —— 頂点を引数に取るコマンドが
   * 無いので、要求の有無ではなく**選択に入っているかどうか**が出す条件になる
   * (`viewport.showVertices` のdocstring)。
   */
  // 取り寄せは候補のボディと1本の `$effect` で行う(`vertexCandidateBody` の下)。

  // ---------------------------------------------------------------- D: 辺を取りに行く
  /**
   * 辺を出す対象のBody。**選んでいる形のBody**を優先し、無ければアクティブボディ。
   *
   * アクティブ固定にすると、27部品を読んだ文書で「見ている部品ではない方」の辺が出る。
   * 選択優先なら、面をクリックしてからフィレットを起動すれば必ずその部品の辺が出る。
   */
  const edgeTargetBody = $derived.by(() => {
    // **見えているボディだけを対象にする**。AIが述語で提案した集合は
    // 隠したボディにまたがりうる。そこを対象にするとサーバが「隠れているものの辺は
    // 指せない」と断り、画面にはエラーだけが出て**見えている辺も出なくなる**。
    // 選択のうち見えているものを先に見る。**メッシュボディも同じ理由で外す** —— 辺も頂点も
    // 持たないので、頼むとサーバが「mesh body」と断ってエラーの札だけが出る。
    const visible = new Set(
      (model?.bodies ?? [])
        .filter((body) => body.visible && body.kind !== 'mesh')
        .map((body) => body.body_id),
    )
    const picked = (selection?.items ?? []).find(
      (item): item is ShapeRef => !('sketch' in item) && visible.has((item as ShapeRef).body),
    )
    if (picked) return picked.body
    // アクティブボディへ落ちるときも同じ判定をする —— 隠したまま辺を頼むと、
    // 頼んでもいないエラーが画面に出るだけで何も得られない(自分で断るほうが正しい)。
    const active = model?.active_body_id ?? null
    return active !== null && visible.has(active) ? active : null
  })

  /**
   * **回転中心の頂点を指させている間**、または**頂点そのものを対象にするコマンド(測定)を
   * 起動している間**、候補を出すボディ(出さないなら null)。
   *
   * 対象は辺と同じ1つ(`edgeTargetBody`) —— 全ボディの頂点を出すと、27部品の文書では
   * 数千個の的が画面を覆う。**軸に使う辺と同じボディの頂点**が指せれば足りる。
   * 測定も同じ1つに絞る(別のボディとの距離は面で測る)。
   */
  const vertexCandidateBody = $derived(
    sketchId === null && (wantedPick === 'vertex' || wantedVertices) ? edgeTargetBody : null,
  )

  /**
   * 選択に入った頂点のボディと、候補として出すボディの頂点の座標を取りに行く。
   * **見えているBodyだけ**に頼む(隠れているものの頂点はサーバが断る。辺と同じ規律)。
   */
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

  /**
   * 今のコマンドが辺を要求しているか(スケッチモード中は辺を出さない)。
   *
   * **直接ピック(回転軸)も同じ口を通す** —— 選択から取る引数が辺を食わないコマンドでも、
   * 辺を指させるなら折れ線を配ってもらう必要がある。
   */
  const wantsEdges = $derived(
    sketchId === null &&
      // **面を1つ指させている間は辺を出さない。** 辺は面より先に拾われるので、出したままだと
      // 鏡映面を狙ったクリックが辺に取られ、**その辺が対象の選択へ入る**(素のクリックは
      // 選択を置き換えるので、選び直したつもりのない対象がその場で入れ替わる)。
      wantedPick !== 'face' &&
      (wantedKinds.includes('edge') || wantedPick === 'edge'),
  )

  $effect(() => {
    if (!wantsEdges || edgeTargetBody === null) {
      viewport?.showEdges(null, [])
      return
    }
    // **版が違う辺は使わない**(形が変わった後の古い添字は別の辺を指す。§7)。可視性は見ない
    // (`edgeTargetBody` が見えているボディしか返さない)。
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
   * 基準3平面を指せるのは**スケッチのコマンドを起動している間だけ**(辺と同じ規律)。
   * スケッチモード中は出さない —— 描いている最中のクリックは点か領域であり、
   * 奥の基準平面を掴むと線が引けなくなる。
   */
  const wantsBasePlanes = $derived(sketchId === null && wantedPlane)

  $effect(() => {
    // **先に読んでから渡す。** `viewport?.showBasePlanes(wantsBasePlanes)` と書くと、
    // 初回(まだ `onMount` 前で `viewport` が null)に `?.` が短絡して
    // `wantsBasePlanes` を一度も読まず、この $effect が依存を1つも持たないまま二度と
    // 再実行されない —— 起動しても平面が指せない、という形で出る(実測で踏んだ)。
    // 上の辺の $effect が同じ穴に落ちていないのは、`wantsEdges` を先に読んでいるから。
    const on = wantsBasePlanes
    viewport?.showBasePlanes(on)
  })

  /**
   * 通常モードで領域を指せるのは、**領域を要求しているコマンドを起動している間だけ**
   * (辺・基準平面と同じ規律。2026-08-23の本人裁定5)。通常モードの「押し出す」は面版なので、
   * 今は通常モードで領域を要求するコマンドは無い。
   *
   * 常時指せると、スケッチ平面と重なる面(領域から押し出した板の底面など)を狙う
   * クリックが、手前に浮いている領域に取られる。**スケッチモード中はゲートを立てない**
   * —— モード中の領域ピックは `setActiveSketch` 側の規律(そのスケッチの領域だけ)で
   * 動いており、ここから触ると別のスケッチの領域まで拾えるようになってしまう。
   */
  const wantsRegions = $derived(sketchId === null && wantedKinds.includes('region'))

  $effect(() => {
    // 先に読んでから渡す(上の基準平面と同じ理由。`?.` の短絡で依存を失わない)。
    const on = wantsRegions
    viewport?.pickRegions(on)
  })

  /**
   * **回転中心の頂点を待っている間だけ、頂点を辺より先に取る**(`viewport.preferVertices`)。
   *
   * `move_body` は対象を面/辺/頂点で指すので**辺が常に出ている** —— 既定の順序
   * (辺が先)のままだと、角の頂点を狙ったクリックが必ず辺に取られて中心を指せない。
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
   * 1つの参照を選択集合の編集へ翻訳する(面・辺・領域で同じ規則。すべて3Dのクリック経由)。
   *
   * Shift / Ctrl=トグル(選ばれていれば外す・いなければ足す) / 修飾なし=置き換え。
   * **除外専用の修飾キーは無い**(2026-08-30本人決定。間違えたらもう一度クリックする)。
   * 置き換えは **1つだけ選んでいるものを再クリックしたら外す**(狙いを外したときに素で解除できる)。
   *
   * 置き換えのとき**自分自身は remove に入れない** —— コアは remove を後に適用するので、
   * 入れると足したそばから外れる(`duocad/selection.py` の契約)。
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
   * ウィンドウ選択(左ドラッグ)を選択集合の編集へ翻訳する。
   *
   * - 修飾なし … 囲んだものだけが選ばれている状態にする(**囲まなかったものは全部外れる**)。
   *   空の矩形は「全部外す」であって空振りではない —— 何もない所を素でクリックしたときに
   *   選択が解除されるのと同じ規則。
   * - Shift / Ctrl … 囲んだものを1つずつトグルする(既存の選択は消さない)。
   *
   * **1回の `modify_selection` にまとめる**。参照ごとに送ると、20個囲んだドラッグが
   * 20往復になり、途中の状態が全部画面へ配信される。
   *
   * **突き合わせは `refKey` の `Set` で行う**(2026-09-04。総当たりのM×Nをやめた)。
   * 画面全体を囲むドラッグ(実測2835面)を、既に大きく選んだ状態の上へ重ねたときだけ
   * 効く経路なので、選択を空にして測ると表に出ない。
   */
  /**
   * 当たった面を選択の参照へ翻訳する。**メッシュボディだけはボディ参照へ畳む。**
   *
   * メッシュに指せる面は無い(`selection.select_face` が断る)が、**指せるものが
   * 何も無いわけではない** —— ボディ単位なら選べて、そのまま削除できる。畳んでおけば
   * 「クリックしても何も起きない」も「一括選択が丸ごと落ちる」も起きず、しかも
   * **編集verbは自然に押せないまま**になる(`move_body` / `fillet` / `chamfer` /
   * 領域押し出しはどれも `kinds` にボディを持たないので、選択要件を満たさない)。
   * **押せるのに断られる、を画面から消すのはこの1本**である。
   */
  function pickedRef(bodyId: string, faceIndex: number): SelectionRef {
    if (meshBodyIds.has(bodyId)) return bodyRef(bodyId)
    return faceRef(bodyId, faceIndex)
  }

  /**
   * **素のクリック・矩形選択をトグルとして読むか**(クリック `onPick` と矩形選択
   * `onBoxSelect` で共有する)。
   *
   * 保持/除外ソリッド・拘束面の集合 verb は、起動した時点で既存の割り当てが選択状態で現れる
   * 集合代入なので、置き換えのままだと足すたびに他の割り当てを全部外してしまう。測定は対象が
   * 「ちょうど2つ」なので、2つ目の素のクリックが1つ目を消しては永久に揃わない。
   * 立つのはコマンド起動中(`activeVerb`)だけ。
   */
  function toggleOnlyMode(mode: PickEvent['mode']): PickEvent['mode'] {
    return commandClickToggles() ? 'toggle' : mode
  }

  /** コマンド起動中の素のクリックをトグルにするか(`activeVerb`)。 */
  function commandClickToggles(): boolean {
    return activeVerb !== null
  }

  /**
   * **保持/除外ソリッドの間はボディ粒度**。`wantedKinds` に `'body'` が立つのは
   * `SET_PRESERVE_BODIES` / `SET_EXCLUDE_BODIES` だけ(`commands/optimize.ts` の宣言)。
   * 面の参照を確定すると `require_selected_bodies` が面を無視し、**集合が黙って空になる**。
   */
  function optimizeSetFaceRef(bodyId: string, faceIndex: number): SelectionRef {
    if (wantedKinds.includes('body')) return bodyRef(bodyId)
    return pickedRef(bodyId, faceIndex)
  }

  function onBoxSelect(event: BoxSelectEvent): void {
    const mode = toggleOnlyMode(event.mode)
    const picked: SelectionRef[] = [
      // **同じボディの面が何枚囲まれても参照は1つ**(`refKey` で重複を落とす)。
      ...dedupeRefs(event.faces.map((item) => optimizeSetFaceRef(item.bodyId, item.faceIndex))),
      ...event.edges.map((item) => edgeRef(item.bodyId, item.edgeIndex)),
      ...event.vertices.map((item) => vertexRef(item.bodyId, item.vertexIndex)),
      ...event.regions.map((item) => regionRef(item.sketchId, item.index)),
      ...event.sketchEntities.map((item) => sketchEntityRef(item.sketchId, item.entityId, item.point)),
    ]
    const current = currentItems()
    const currentKeys = new Set(current.map(refKey))
    if (mode === 'toggle') {
      const already = picked.filter((ref) => currentKeys.has(refKey(ref)))
      const fresh = picked.filter((ref) => !currentKeys.has(refKey(ref)))
      modifySelection(fresh, already)
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
   * その参照**だけ**が選ばれている状態にする(常に置き換え。再クリックで外さない)。
   *
   * `editSelection` の置き換えの実体でもあるが、**トグルの判定を通らない**のが違い ——
   * スケッチを貼る先として面を指す経路がこれを直接呼ぶ。既に選ばれている面をもう一度
   * クリックしたときに `editSelection` の解除に落ちると選択が空になり、直後に走る
   * `sketch_create` が「面が選ばれていない」で失敗する。ここでは「指した面に貼る」が常に成立する。
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
   * コマンドが起動した(`CommandHost.start`)。**形状最適化のverbだけが意味を持つ。**
   *
   * 集合代入の3つは、起動した役割の**既存の割り当て**をそのまま選択状態にして見せる
   * (タスク仕様「verb起動 → 既存の割り当てが選択状態で現れる」)。`duocad/optimize.py` の
   * 契約どおり `state.optimize` の該当配列はそのまま `modifySelection` へ渡せる形をしている。
   * 荷重の2つは**空にする**(`OPTIMIZE_EMPTY_START_VERBS`)。
   */
  function onCommandStart(verb: string): void {
    measure.clear() // 次の操作に入ったら、前の測定の札は用済み
    pendingOptimizePreset = null
    // **別のverbへ直接移ったら、前のverbの終わり**として選択を空にする(`onCommandEnd`)。
    // 同じverbの押し直し(構え直し)は終わりではない。
    // `model === null` の早期returnより前に置く —— 起動そのものは文書が無くても起きる。
    const switched = activeVerb !== null && activeVerb !== verb
    activeVerb = verb === DEFAULT_SKETCH_COMMAND_ID ? null : verb
    if (model === null) return
    // 荷重の2つは**空から始める**(`OPTIMIZE_EMPTY_START_VERBS` の理由を参照)。
    // 「何もしない」ではなく「空を代入する」でなければ、前のverbの選択が残る。
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
    // **非表示のボディに付いた割り当ては選択に並ばない**(`state.optimize` の配列は指せるものだけ)。
    // 集合 verb は集合全体を置き換えるので、並ばなかった割り当ては確定で黙って消える —— 先に
    // そのボディを表示へ戻し、戻った状態の配列を並べる(2026-09-24 本人判断。issue #33)。
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

  /** 選択を全解除する。配信が届くまでの判定は `currentItems()` が空として扱う。 */
  function clearSelection(): void {
    clearingOp = nextOp()
    session.clearSelection(clearingOp)
  }

  /**
   * **選択集合の差分を組むときに読む今の選択。** 送った `clear_selection` の配信が
   * まだ届いていない間は空を返す。
   *
   * 配信前の `selection` を読むと、Esc直後(終了で全解除を送った直後)に「唯一選ばれていた
   * 要素」をクリックしたとき `editSelection` が解除と判定し、コアでは「全解除 → その要素を外す」
   * と順に効いて**選んだはずの要素が選ばれない**。サーバは1接続の要求を順に処理するので、
   * 全解除の後に届く要求は空の集合に対して効く —— 判定もそれに合わせる。
   * 表示(ハイライト・件数)は配信どおりの `selection` を使う。
   */
  function currentItems(): SelectionRef[] {
    return clearingOp !== null ? [] : (selection?.items ?? [])
  }

  /**
   * コマンドが終わった(`CommandHost.onEnd`: Esc・中断ボタン・確定成功)。**選択を全解除する。**
   *
   * 形を変えるverbの確定ではコアが先に空にしているので何も起きない。ここで効くのは中断と、
   * 形を変えない確定(集合代入・測定の失敗等)である。
   *
   * **測定の成功だけは残す** —— 読み札と3Dの線は「測った選択が変わるまで」残る
   * (`measurement.svelte.ts`)ので、空にすると結果が出た瞬間に消える。
   * `activeVerb === null` は、モード遷移(`setMode`)が先に起動を畳んだ後の遅れて届いた通知
   * (スケッチ作成の成功等)で、選択に触らない。
   */
  function onCommandEnd(verb: string): void {
    if (activeVerb === null) return
    activeVerb = null
    if (verb === MEASURE.id && measure.current !== null) return
    clearSelectionIfAny()
  }

  /**
   * `CommandHost` の `onWants`。無起動の `command.args` は常に `[]` なので、`kinds` が空になった =
   * 中断/確定成功/モード切替のどれかで降りた、とみなして集合 verb の並べ直し待ちを捨てる。
   */
  function onWantsKinds(kinds: SelectableKind[]): void {
    wantedKinds = kinds
    if (kinds.length === 0) pendingOptimizePreset = null
  }

  /**
   * ピックを選択集合の編集へ翻訳する。**UI側に選択状態を持たない**ので、
   * ここが作るのは「何を足して何を引くか」だけで、結果はコアから返ってくる。
   */
  function onPick(event: PickEvent): void {
    if (sketchId !== null) {
      onSketchPick(event)
      return
    }
    const current = currentItems()
    // **スケッチを貼る先を待っているとき**。基準平面はその場で決まり(即適用が同期に走る)、
    // 面は「選択を置き換えたうえで `'face'` を入れる」——
    // 送るのは選択が確定して配信されてからで、その待ち受けは `CommandHost` が持つ。
    if (host?.wantsPlane()) {
      if (event.plane !== null) {
        host.setBasePlane(event.plane)
        return
      }
      if (event.hit !== null) {
        // **メッシュボディには貼れない**(平面が取れない。`sketch.resolve_plane` が断る)。
        // ここはピックを畳まずに理由を出す —— ボディ参照へ畳んでも `sketch_create` は
        // 面を要求するので、押した結果は変わらないまま理由だけが消える。
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
    // **コマンドが辺・頂点を直接待っているとき**(回転軸・回転中心)。`wantsPlane` と
    // 同じ位置・同じ順序 —— 下の辺・頂点の分岐より**前**に置く。ここを後にすると、
    // 指した辺が共有選択へ入り、コアの `require_bodies` がそのボディまで
    // 「動かす対象」に数える(= 軸に使った部品まで一緒に回る)。
    //
    // **待っているものが当たらなかったクリックは素通りさせる**(`wantsPlane` と同じ)——
    // 対象を選び直す・何もない所で解除する、が構えている間もできる。
    //
    // **修飾キー付き(Shift / Ctrl)のクリックはピックへ行かない** —— 待っている間も
    // 対象を足す・外す手が要る(軸や鏡映面を指す構えは、対象が1つ揃った時点で自動的に
    // 開くので、素のクリックだけを食うと2つ目の対象が永久に選べない)。
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
    // **素クリックのトグル(`toggleOnlyMode`)は面だけでなく辺・頂点・領域にも効かせる** ——
    // 測定は辺・頂点でも測るので、下の分岐すべてが同じ `pickMode` を通る。
    const pickMode: PickEvent['mode'] = toggleOnlyMode(event.mode)
    // **辺が当たったら辺**。フィレット・面取り・測定を起動している間だけ来る。
    if (event.edge !== null) {
      editSelection(edgeRef(event.edge.bodyId, event.edge.edgeIndex), pickMode)
      return
    }
    // **頂点が当たったら頂点**。マーカーが出ているのは選択に入っている頂点と、測定の
    // 候補(対象ボディの全頂点)だけなので、ここへ来るのは「AIが選んだ頂点を人が
    // Ctrl+クリックで外す」か「測る頂点を指す」のどちらか。参照の形も修飾キーの規則も面・辺と同じ。
    if (event.vertex !== null) {
      editSelection(vertexRef(event.vertex.bodyId, event.vertex.vertexIndex), pickMode)
      return
    }
    // **領域が当たったら領域**。領域を要求しているコマンドの起動中(スケッチ編集中を除く)だけ来る
    // (`wantsRegions` → `viewport.pickRegions`)。参照の形も修飾キーの規則も
    // スケッチモードの `onSketchPick` と同じ —— 同じものを同じように選ぶ。
    if (event.region !== null) {
      editSelection(regionRef(event.region.sketchId, event.region.index), pickMode)
      return
    }
    if (event.hit === null) {
      // 何もない所を素でクリック = 選択解除(修飾キー付きは空振りにする)。
      // **コマンド起動中は選択を変えない**(外したいものは足すのと同じクリックで外す)。
      if (commandClickToggles()) return
      if (event.mode === 'replace' && current.length > 0) clearSelection()
      else if (event.mode === 'replace') note('面を外した')
      return
    }
    // 保持/除外ソリッドの間だけボディ粒度へ畳む(`optimizeSetFaceRef`)。素クリックのトグルは
    // 上の `pickMode` が面・辺・頂点・領域へ一様に効かせる。
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
   * **モード遷移はここ1本だけ**(2026-09-03)。以前は `enterSketch` / `exitSketch` /
   * `enterOptimizeMode` / `exitOptimizeMode` の4つが**それぞれ違う流儀で**選択を掃除して
   * いた(外国スケッチの図形だけ / 図形を全部 / 何もしない / ボディだけ)。遷移が型として
   * 存在せず手書きで散っているのが、`duocad-sketch-switch-keeps-foreign-selection`
   * (スケッチAからBへ直接移る抜け道)や最適化⇄スケッチの両立の穴を生んだ形である。
   *
   * **掃除の規則は「行き先が使えない参照を落とす」の1つだけ。** 出口側で列挙するのを
   * やめたので、経路が増えても書き忘れる場所が無い:
   *
   * - **スケッチの図形** … 行き先のスケッチのものでなければ落とす(同じスケッチへ入り直す
   *   ときは何も落ちない。行き先がスケッチでなければ全部落ちる)。混ざったまま持ち越すと、
   *   Bで `move_sketch_entities` を実行したときにAの図形が動く —— サーバは `entity_id` を
   *   素直に解決するので黙って通る。
   * - **ボディ(`kind:'body'`)** … 行き先が最適化モードでなければ落とす。保持/除外の
   *   割り当てにしか使えない種別で、他のモードのverbは「種別が違う」と断るだけになる。
   * - **面・辺・頂点・領域はどのモードでも残す** —— スケッチを抜けてそのまま押し出す、
   *   最適化を抜けてそのまま面取りする、はどちらも実際に使う経路である。
   */
  function setMode(next: Mode): void {
    // **確定前の姿を持ち越さない。** モードが変わると下の `{#key mode.kind}` が
    // `CommandHost` ごと作り直すので、コマンド層からの中断(`onCancelPreview`)は**来ない**
    // —— ここで消さないと、起動したまま抜けた `move_body` の半透明ボディや、
    // `extrude` の投機実行の幽霊(本物を隠したまま)が画面に残り続ける。
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
    // **どの遷移でも描き直す**(`$effect` に任せない)。入る側は直後の `lookAtSketch` が
    // 平面の枠を要るので1手遅らせられない(`handleState` と同じ理由)。**抜ける側にも呼ぶ**
    // —— 旧 `exitSketch` はここを呼んでおらず、抜けたスケッチの点マーカーが次の状態配信まで
    // 画面に残っていた(選択の掃除が空振りするとWS往復が起きず、その配信も来ない)。
    // 残ったマーカーは手前に浮くのでクリックを奪いうる。**`smoke-move.mjs` の辺ピックの失敗がこれで直るかは未確認** ——
    // あの失敗は実測でflake(同じビルドで5回中2回緑)であり、因果は結べていない。
    drawSketches()
    if (next.kind === 'sketch') viewport?.lookAtSketch(next.id)
  }

  /** 今のモードから見た「スケッチを抜けたときの戻り先」。 */
  function resumeOf(current: Mode): ResumeMode {
    if (current.kind === 'optimize') return 'optimize'
    if (current.kind === 'sketch' || current.kind === 'sketch-pending') return current.resume
    return 'idle'
  }

  /**
   * **スケッチモードへ入る**(まだ貼る先は決まっていない)。「スケッチ」ボタンから。
   *
   * 入った先の `sketch-pending` では `SKETCH_PENDING_COMMANDS` が**起動済みで**現れる
   * (`CommandHost` の `startCommandId`)ので、次の1クリックで貼る先が決まる ——
   * ボタン→クリックの2手は変わらない。
   */
  function startSketchMode(): void {
    setMode({ kind: 'sketch-pending', resume: resumeOf(mode) })
  }

  /**
   * **そのスケッチを開く。** `sketch_create` の成功(`handleState`)と、ブラウザパネルの
   * スケッチ名クリックの2箇所から呼ばれる。
   *
   * **後者はもう「抜け道」ではない**(2026-09-03)。以前は最適化モード中でも無条件に
   * ここへ来て両モードを両立させかけたが、いまは他と同じ `setMode` の1本を通る ——
   * 戻り先(`resume`)を持って移るので、抜ければ最適化モードへ戻る。
   */
  function enterSketch(id: string): void {
    // **隠したスケッチに入るときは先に表示へ戻す**(拒否ではなく表示に戻して入る。本人判断
    // 2026-09-16)。編集中のスケッチは `buildSketchDraws` が可視性に関わらず描くので、
    // サーバの往復を待たずに入ってよい(抜けた後も表示のまま —— 隠し直しはしない)。
    const target = sketches.find((item) => item.sketch_id === id)
    if (target !== undefined && !target.visible) setSketchVisible(id, true)
    setMode({ kind: 'sketch', id, resume: resumeOf(mode) })
  }

  /** ブラウザパネルのスケッチ名クリック。**最適化が走っている間はモードを抜けさせない** —— 抜けると
   *  パネルごと中断ボタンが消える(「抜ける」ボタンを止めているのと同じ理由)。 */
  function enterSketchFromBrowser(id: string): void {
    if (optimizeBusy) {
      fail('形状最適化の実行中はスケッチへ入れない(終わるか中断してから)')
      return
    }
    enterSketch(id)
  }

  /** スケッチモードを抜ける。**戻り先は入る前のモード**(通常 or 最適化)。 */
  function exitSketch(): void {
    setMode(resumeOf(mode) === 'optimize' ? { kind: 'optimize' } : { kind: 'idle' })
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
   * スケッチモードのクリック。**描いている最中は点、そうでなければ領域の選択**。
   *
   * 点を待っているコマンド(線分・円…)が起動している間は、領域の上を押しても点になる
   * —— 既存の領域の内側に線を引けないと、分割そのものができない。既定のコマンドは
   * 「選ぶ」(点を待たない)なので、描いていないときのクリックは領域へ行く。
   */
  function onSketchPick(event: PickEvent): void {
    if (event.point && host?.wantsPoint()) {
      // **当たっている図形へ落としてから渡す**(`snappedPlanePoint`)。幽霊(`hoverPoint`)も
      // 同じ関数を通るので、吸い付いて見える位置と確定結果はずれない。
      const [u, v] = snappedPlanePoint(event.point.u, event.point.v, event.sketchEntity)
      if (host.addPoint(u, v)) return
    }
    // **コマンドがスケッチの点・図形を直接待っているとき**(回転中心・鏡映軸)。3Dの辺・
    // 頂点と同じ規律で、下の共有選択への翻訳より**前**に置く —— 中心や軸に使うものが
    // 選択へ入ると、それ自体が「動かす/鏡映する対象」になる。
    // **修飾キー付きがピックへ行かないのも3Dと同じ**(`onPick`)。
    if (event.sketchEntity && host !== null && event.mode === 'replace') {
      const hit = event.sketchEntity
      const wants = host.wantsPick()
      if (wants === 'sketchPoint' && host.setPick(sketchEntityRef(hit.sketchId, hit.entityId, hit.point))) {
        return
      }
      // **軸に要るのは線分そのもの**なので、端点の近くを押しても点を落として図形へ畳む
      // (`point` を残すと、コアの `mirror_sketch_entities` が軸の参照として読めない)。
      if (wants === 'sketchEntity' && host.setPick(sketchEntityRef(hit.sketchId, hit.entityId, null))) {
        return
      }
    }
    // **図形は領域より先**(点・辺は領域の縁に乗っているので、領域が勝つと端点を掴めない)。
    // 描くコマンドが起動している間は上の `addPoint` が先に食うので、ここへは来ない。
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
  /**
   * 揃った引数をサーバへ送る。**経路はコマンドの分類(`categoryOf`)だけで決まる。**
   *
   * **知らない名前はここで断る**(分類を書き忘れたverbが黙って別のverbになるのを防ぐ)。
   */
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
        // **`sketch_create` だけはスケッチモードの外で走る** —— スケッチを作るverbなので、
        // 入っていることを要求すると永久に起動できない。反復の `needsSketchId` と同じ形の
        // 判定を1つ置いて迂回させる: 反復が「分類は feature だが `sketch_id` が要る」で、
        // こちらは「分類は sketch だが `sketch_id` が要らない」という裏返しの位置にいる。
        if (runsOutsideSketch(verb)) {
          sendTracked(verb, args)
          return
        }
        if (sketchId === null) {
          fail('スケッチモードに入っていない')
          return
        }
        viewport?.showSketchGhosts(sketchId, []) // 本物を頼んだので幽霊は消す
        // **引数名はコマンド定義とサーバの宣言で既に揃っている**(p0/p1・center/radius・
        // corner/width/height・center/radius/start_deg/end_deg)。ここで詰め替えない。
        // **`move_sketch_entities` だけは `sketch_id` を足さない** —— どのスケッチかは
        // 選んだ参照(`{sketch, entity, point}`)が持っている(`runsWithoutSketchId`)。
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
          // **領域はパラメータ編集で分裂・消失しうる**(§7)。消えた領域を送ると
          // サーバは添字が範囲外だと言うだけなので、ここで何が起きたかを画面へ出す。
          fail('選んだ領域が今のスケッチに無い(図形が変わって分裂・消失した可能性)。選び直す')
          return
        }
        sendTracked(verb, needsSketchId(verb) ? { ...args, sketch_id: sketchId } : args)
        return
      }
      // -------------------------------------------------------------- F: ファイル
      case 'file': {
        // **`FilesPanel` からの操作と同じ口**(`files.run`)へ流す。ここで二重化すると
        // 「ツールバー経由の import_step だけ応答待ちの解き方が違う」という差が生まれる。
        files.run(verb, args)
        return
      }
      // -------------------------------------------------------------- 測定(読み取り専用)
      case 'query': {
        // **verb と同じ口で送る**(`{cmd, client_op}`)。返事は状態配信ではなく
        // `{type:'measure'}` で、`handleMeasure` が `client_op` を突き合わせて
        // コマンド層を無起動へ戻す(verb なら `applied.client_op` が担う役)。
        sendTracked(verb, args)
        return
      }
      default: {
        // **分類を1つ足すとここで型が落ちる**(`never` に狭まらなくなる)。
        // 送り方を書かずに分類だけ増やすことができない、というのがこの1行の役目。
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
   *
   * **クリック(`onSketchPick` の `addPoint`)と幽霊(`hoverPoint`)は必ずこの関数を
   * 通すこと**(20260906タスクの不変条件)。段の規則は `entityKinds.snapSketchPoint` の1か所
   * (viewport のカーソル強調も同じ関数を通る)。図形はサーバの `sketches[].entities` から引き、
   * 1mm への丸めは点を待っている(`host.wantsPoint()`)ときだけ —— 選択のクリック等では丸めない。
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
   * ブラウザだけで描く確定前の姿(`preview: 'local'`)——**3つの経路**。
   *
   * 1. **これから打つ図形**(`add_line` 等): `ghostEntity` が生の座標から組み立てる。
   * 2. **既にある図形の変換後**(`pattern_*` / `move_sketch_entities`): `ghostCopies` が
   *    今のスケッチ状態(`ghostSources`)を反復・平行移動・回転する。
   * 3. **既にあるボディの変換後**(`move_body`): 届いているメッシュをずらして/回して重ねる。
   *    **スケッチの外で走る唯一の経路**なので `sketchId` を要求しない。
   *
   * どの経路も**サーバへ何も投げない**。Escで中断しても文書が一切変わらないことを、
   * 「そもそも送らない」で構造的に成立させる(スケッチの幽霊と同じ契約)。
   */
  function showLocalGhost(verb: string, args: Record<string, unknown>): void {
    const view = viewport
    if (view === null) return
    if (verb === MOVE_BODY.id) {
      // **コアの `selection.require_bodies` と同じ導き方**(面/辺/頂点/ボディから
      // ボディを逆引きし、重複を畳む)。ここがずれると幽霊と結果が別のボディになる。
      // **空配列でも必ず呼ぶ** —— 使えない値で古い幽霊を残さない。
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
    // **空配列でも呼ぶ** —— 対象が消えた・値が使えないときに古い幽霊を残さない。
    view.showSketchGhosts(sketchId, ghostCopies(verb, args, ghostSources(ghostContext(), verb, args)))
  }

  /**
   * 確定前の姿。**ローカル方式はサーバへ投げない**(`mode === 'local'`)——
   * 形はブラウザ側で分かっており、投機実行にすると中断しても文書に一度は入る。
   */
  function requestPreview(
    verb: string,
    args: Record<string, unknown>,
    mode: 'server' | 'local',
  ): void {
    if (mode === 'local') {
      // **参照を数値へ解決してから渡す**(`resolvePicksForGhost`)。`showLocalGhost` は
      // 解決済みの数値しか読まない層のままにしておく。
      showLocalGhost(verb, resolvePicksForGhost(ghostContext(), verb, args))
      return
    }
    previewId += 1
    session.preview(verb, args, previewId)
  }

  function clearPreview(): void {
    previewId += 1 // 投げてある応答を無効にする(戻ってきても番号が合わない)
    previewing = false
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
    // 編集中のフィールドではブラウザ標準のテキスト取り消しに任せ、文書のundo/redoは
    // 発火しない(`CommandHost.onKeydown` と同じ書き方の判定)。**BUTTONとチェックボックスは
    // 除外しない** —— どちらも「編集中のテキスト」を持たない操作対象で、フォーカスが
    // 乗っているだけならCtrl+Zは文書のundoでよい(パーツ一覧の可視性チェックボックスに
    // フォーカスが残った状態のCtrl+Zがundoを起こすのは既存の実機検査が固定している)。
    // **戻るのは直前のverbであって、そのチェックボックスの表示切り替えではない** ——
    // 可視性は操作ログに載らない(2026-08-26。`duocad/selection.py` の `set_visible`)。
    const target = event.target as HTMLElement | null
    const isTextEditingField =
      target?.tagName === 'TEXTAREA' ||
      (target?.tagName === 'INPUT' && !['checkbox', 'radio'].includes((target as HTMLInputElement).type))
    // **Ctrl+S は今の文書を手で書き直す**(`files.save`)。保存は自動で済んでいる(issue #5)ので
    // 普段は何も変わらないが、キーは残す —— 自動保存が書けなかったときの押し直しと、
    // ブラウザの「ページを保存」を止めるため。
    //
    // **`preventDefault()` はフォーカスがどこにあっても必ず通す** —— 止めないと
    // ブラウザの「ページを保存」ダイアログが開き、こちらの保存が起きたのかどうかが
    // 人から見えなくなる(実機検査ではモーダルで止まる)。
    //
    // **ここだけ `isTextEditingField` で抜けない**のは、Ctrl+S にはテキスト編集としての
    // 意味が無いからである。Ctrl+Z/Y を入力欄で見送るのは「ブラウザ標準のテキスト取り消しに
    // 任せる」ためで、譲る相手がいる。Ctrl+S に譲る相手はページ保存ダイアログしかなく、
    // それは止めると決めた側なので、見送ると**何も起きない死んだキー**になる
    // (判定式そのものは undo/redo と同じものを使い、意味の違いだけをここに書く)。
    if (key === 's') {
      event.preventDefault()
      files.save()
      return
    }
    if (isTextEditingField) return
    // **実行中は送らない**(サーバも `DocumentBusy` で断る)。Undo した条件のまま結果が足される。
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
  // **コアの `set_visible` verb を呼ぶだけ**(アプリ層に可視性の状態を持たせない)。
  //
  // `model.bodies` は常に編集用ボディ(`kind="solid"`)だけを持つ。参照ボディ
  // (`kind="reference"`・読み込みの原本を不可視のまま持つ二重管理)は2026-09-03に
  // 概念ごと撤去した。
  const partBodies = $derived(model?.bodies ?? [])
  /**
   * **面を持たないボディ**(三角形メッシュ)のID。`duocad.document.MESH_KIND` と同じ綴り。
   *
   * これが要るのは**ピックの畳み込み**のためである(`pickedRef`)。viewport は
   * メッシュも1枚の面(添字1)として描くので、素直に翻訳すると `{kind:'face'}` の
   * 参照が飛び、コアの `select_face` が「メッシュボディには面が無い」と断る
   * (`20260904-1703` の契約②)。**ドラッグの一括選択はまとめて1回で送る**ので、
   * 画面を囲んだドラッグにメッシュが1つ入っているだけで**選択が丸ごと落ちる**。
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
    // 可変解像度 —— ズームで見え方が変わったらサーバへ知らせ、ソリッド・辺・領域を
    // その粗さで測り直させる。**スケッチの折れ線はビューポート側が自分で折り直す**
    // (ブラウザ内で完結するので往復が要らない)。連打の抑制も向こうが持っている。
    viewport.onResolutionChange(sendViewResolution)
    session.start()
    return () => {
      session.dispose()
      viewport?.dispose()
      viewport = null
    }
  })

  /**
   * 今の見え方をサーバへ知らせる(可変解像度)。**繋がっていないときは黙って捨てる。**
   *
   * `session.send` は送れなかった操作をトーストに出すが、これは人が起こした操作ではなく
   * 画面の都合の連絡なので、切れている間にホイールを回しただけで「操作を送れなかった」の
   * 札が出るのは誤報である。繋ぎ直したときは下の `$effect` が送り直す。
   */
  function sendViewResolution(resolution: {
    worldPerPixel: number
    sizeHint: number
    focus?: Record<string, number[] | null> | null
  }): void {
    if (status !== 'open') return
    session.setViewResolution(resolution.worldPerPixel, resolution.sizeHint, resolution.focus ?? null)
  }

  // 繋がった(繋ぎ直した)ら、今の見え方をもう一度知らせる。**サーバ側の目標精度は
  // セッションの持ち物**なので、サーバが立ち上がり直すと既定の粗さへ戻る —— 次に人が
  // ズームするまで粗いままになるのを避ける。
  $effect(() => {
    if (status !== 'open') return
    const resolution = viewport?.viewResolution()
    if (resolution && resolution.worldPerPixel > 0) sendViewResolution(resolution)
  })

  // 選択が変わったら描き直す(コア発 → 購読 → 再描画。上り方向の出口)
  $effect(() => {
    void selection
    drawHighlight()
    // **スケッチはここで描き直さない**。選択もスケッチも `handleState` 経由でしか変わらず、
    // そこで既に描き直している。ここにも置くと、1回の状態受信で全スケッチのジオメトリを
    // 2回作り直すことになる(面のハイライトと違って作り直しが重い)。
  })

  /**
   * 形状最適化(第3のモード)の見た目を描き直す —— 保持/除外ソリッドの色、拘束面の
   * 色分けと固定記号、荷重の矢印+ラベル。**モードを抜けているあいだは描かない**
   * (タスク仕様)。`viewport.ts` の `FaceRef` はコアの `ShapeRef`(`{body, kind, index}`)
   * とは形が違う(`{bodyId, faceIndex}`)ので、ここで詰め替える。
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
      // **面ごとの how を突き合わせて渡す。** `loads`(いま指せる面)と `load_specs`
      // (文書が持つ how)は長さが違いうるので `(body, index)` で引く —— 荷重面のボディを
      // 隠すと前者からだけ消える。突き合わない面は描かない(描く向きも大きさも無いため)。
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
   * 最適化モードにいる間、**条件の点検と解像度の目盛りを取り直す**。
   *
   * 引き金は2つ: モードの出入り / 配りものの `state.optimize` が変わった(4 verb を
   * 打った・undoした・部品を消した)。**どちらも軽い**
   * (点検は文書を舐めるだけ、解像度は条件面のbboxを数枚測るだけ)ので、
   * 状態が届くたびに聞き直してよい。
   *
   * **走っている最中は聞き直さない** —— 実行の途中で `state` が動く経路は無いのに、
   * 進捗のたびに往復を足す理由が無い。
   */
  $effect(() => {
    void optimize
    if (!optimizeMode || status !== 'open' || optimizeBusy) return
    session.optimizeConditions()
    session.optimizeResolution()
  })

  /**
   * `state.optimize.stale`(いま使えない割り当て)をトーストで知らせる。
   *
   * **`code` で分岐する**(`duocad/optimize.py` の契約) —— `"hidden"` は部品を隠す
   * たびに出るので出さない側、`"fingerprint"`/`"missing"` は出す側。文言では分岐しない。
   * **モードに関係なく見る** —— 割り当てが壊れるのは他のverb(undo・部品の削除)が
   * 起こすので、最適化モードに入っていないときにも気づけて良い。
   *
   * **鍵に `code` を含める**(2026-08-31 QA指摘で修正)。`role:body:index` だけだと、
   * `hidden`(出さない側)として一度記録した対象が後で `fingerprint`/`missing`(出す側)へ
   * 変わっても「もう見た」と誤判定され、二度と通知されない —— 表示に戻して直したつもりが
   * 実は指す面が変わっていた、のような事故に人が気づけなくなる。`code` を鍵に混ぜれば、
   * コードが変わった時点で別の鍵として扱われ、正しく再通知される。
   */
  $effect(() => {
    const stale = optimize?.stale ?? []
    const next = new Set<string>()
    for (const item of stale) {
      const key = `${item.role}:${item.body}:${item.index ?? ''}:${item.code}`
      next.add(key)
      if (item.code !== 'hidden' && !seenStaleKeys.has(key)) fail(item.reason)
    }
    seenStaleKeys = next
  })

  /** 状態配信を受けた回数。**実機検査が往復の完了を知るための穴**(`handleState` が増やす)。 */
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
    optimizeStepIndex: () => optimizeStepIndex,
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
    <!-- 左サイドバー(ファイル一覧)の開閉。**実物をクリックできない対象なのでボタンを置く**
         (UI指針1「実物クリックできるものにボタンを作らない」の裏返し —— パネルそのものは
         畳むと画面から消えるので、外に手を出す場所が要る)。 -->
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
    <!-- **現在開いているファイル名**(完了条件4)。**常に名前だけを出す**(2026-08-27) ——
         まだ保存も読み込みもしていない文書には名前が無いので「(無題)」と置く。
         書けているかどうかは隣の `#save-state` が言う。 -->
    <span id="current-file" class="dim">{files.currentName ?? '(無題)'}</span>
    <!-- **自動保存が書けなかったときだけ出る札**(`state.saved`。issue #5)。保存は1操作ごとに
         自動で走るので「保存済み」は黙っていてよく、人の手が要る「未保存」だけを出す
         (Ctrl+S で書き直せる)。**明示の保存ボタンは無い**(2026-09-16本人判断で撤去)。 -->
    {#if !saved}
      <span id="save-state" class="tag on" title="自動保存が書けなかった(Ctrl+Sで書き直す)">未保存</span>
    {/if}
    <!-- **常設の指標はこれで全部**(2026-08-27に本人裁定で4つ外した)。外したものと行き先:
         - **接続状態** → 常時光らせず、**操作したのに送れなかった瞬間**にトーストで言う
           (`session.ts` の `send`)。見ても何もしなくてよい間ずっと視線を取るのをやめた。
           再接続が開けないときの通知(`session.ts` の `onError` / `socket`)だけ残る。
         - **部品数 / 操作数** → 数そのものは人の判断に使わない。何があるかはブラウザ
           (`BrowserPanel.svelte`)を見れば分かる。操作数はファイルの一覧にも出さない。
         - **プレビュー中の印** → **印が要らない見え方を目指す**方針(本人)。幽霊が
           出ていることは幽霊自身が示すべきで、札で補うのは見え方が弱い証拠になる。
           状態(`previewing`)は実機検査が見るので残してある。 -->
    <!-- 設定ダイアログを開く口(2026-08-28)。**一度決めたらほとんど変更しない設定**は
         常設パネルに置かずここへまとめる(本人決定。線引きは「作業中に切り替えるものは
         その場のパネル、一度決めたら触らないものは設定モーダル」)。
         **このボタンは開閉のトグル**(2026-09-04)。ダイアログを非モーダル(`show()`)に
         した結果、開いている間もこのボタンが押せるようになったので、押して何も起きない
         死んだボタンにせず、そのまま閉じる口にする(閉じる手段は他に `#settings-close`
         とEscもある)。**idは `settings-open` のまま**変えない —— 実機検査が名前で
         掴んでおり、開く口であることに変わりはない。 -->
    <button
      id="settings-open"
      class="tag"
      onclick={() => (settingsOpen = !settingsOpen)}
      title={settingsOpen ? '設定を閉じる' : '設定を開く'}
    >
      設定
    </button>
    <!-- 右サイドバー(AI)の開閉。**ヘッダーの右端**(ファイル一覧の開閉ボタンと同じ段の逆端。
         2026-08-30本人指定)。`margin-left:auto` で押し出すので、間に何を足しても右端に残る。 -->
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
       `FilesPanel` はここには載らない —— 2026-08-23にいったんここへ統合したが、
       2026-08-26に本人が左サイドバーへの復元を決定した(下の `.main` 直下の `.data-panel` 参照)。 -->
  <section class="toolbar">
    <!-- **モードの行**(`.mode-row`)。**ここがverbより1つ上の次元**(2026-09-03) ——
         入口・札・「抜ける」がここに揃い、verbは下の `CommandHost` にだけ並ぶ。
         **常設の表示は増えていない**: 通常モードに出るのは入口ボタン2つで、
         そのうち「スケッチ」は下のverbの行から**移設**したもの、「形状最適化」は
         元からこの位置にいた(「画面に何かを足して
         既定に据えるのではない」に触れない)。 -->
    {#if mode.kind === 'idle'}
      <div class="mode-row">
        <!-- **行の見出し**(2026-09-05)。下の `CommandHost` の「コマンド」と同じ
             `.label` の列に揃える —— 揃っていなかった間、モードの2つとverbの6つは
             ラベル列の有無だけがずれた**同じ形のボタンの帯**が2本並んでいるように見え、
             その「1つ上の次元」が画面から
             読み取れなかった。スケッチ中/形状最適化中の行はすでにモード名を同じ列に
             置いており(下の `{#if}`)、通常モードだけが欠けていた。 -->
        <span class="label">モード</span>
        <!-- **スケッチの入口**(2026-09-03にverbの行から移設)。idは `sketch_create` の
             コマンドボタンと同じ `sketch-create` のまま —— 押したときに起きること
             (スケッチモードへ入り、貼る先のクリックを待つ)は変わっておらず、
             実機検査が叩く口を変える理由が無い。**両方が同時にDOMへ出ることはない**
             (この行は通常モードだけ、コマンドボタンは貼る先待ちのときだけ)。 -->
        <button
          id="sketch-create"
          class="tag"
          onclick={startSketchMode}
          title="スケッチモードに入る(このあと貼る先の基準平面か面をクリックする)"
        >
          スケッチ
        </button>
        <!-- **形状最適化(第3のモード)への入口**(2026-08-31)。対応する文書実体が無く
             viewport上に「これをクリックすれば入れる」実物が存在しないので、UI指針1の
             裏返しとしてボタンを置く(スケッチと同じ「モードに入るボタン」の作法)。 -->
        <button
          id="optimize-enter"
          class="tag"
          onclick={enterOptimizeMode}
          title="形状最適化モードに入る(保持/除外ソリッド・拘束面・荷重面を決める)"
        >
          形状最適化
        </button>
      </div>
    {/if}
    <!-- **スケッチモードだが、まだどのスケッチも開いていない**(貼る先のクリック待ち)。
         下の `CommandHost` には `sketch_create` が1つだけ、**起動済みで**出ている。 -->
    {#if mode.kind === 'sketch-pending'}
      <div class="mode-row">
        <span class="label">スケッチ</span>
        <span id="sketch-mode" class="tag on">貼る先をクリック(基準平面 XY/XZ/YZ か、ソリッドの面)</span>
        <button id="sketch-exit" onclick={exitSketch}>抜ける</button>
      </div>
    {/if}
    <!-- **スケッチモードの札とモード操作**(2026-08-30に `SketchPanel.svelte` から移設)。
         右サイドバーをAI専用にするため、モード表示・「正面から見る」・「抜ける」は
         モードに応じて作り直されるこのツールバー側へ移した(本人決定)。
         idは移設前と同じにしてある —— 実機検査が叩く口を変える理由が無い。 -->
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
    {#if mode.kind === 'optimize'}
      <div class="mode-row">
        <span class="label">形状最適化</span>
        <span id="optimize-mode" class="tag on">
          保持 {optimize?.preserve_bodies.length ?? 0} ・除外 {optimize?.exclude_bodies.length ?? 0}
          ・拘束 {optimize?.fixtures.length ?? 0} ・荷重 {optimize?.loads.length ?? 0}
        </span>
        <!-- **走っている間は抜けさせない。** 抜けるとパネルごと消えて中断ボタンが
             画面から無くなる(止める手段がゼロになる)。 -->
        <button id="optimize-exit" disabled={optimizeBusy} onclick={exitOptimizeMode}>
          抜ける
        </button>
      </div>
    {/if}
    <!-- **verbの行。** モードが変わったらコマンド層ごと作り直す(集めかけの引数を持ち越さない)。
         鍵はモードのタグそのもの —— スケッチAからBへ移るときは作り直さない(今までどおり)。 -->
    {#key mode.kind}
      <CommandHost
        bind:this={host}
        {selection}
        busy={pendingOp !== null || optimizeBusy}
        {selfSelections}
        commands={mode.kind === 'sketch'
          ? SKETCH_MODE_COMMANDS
          : mode.kind === 'sketch-pending'
            ? SKETCH_PENDING_COMMANDS
            : mode.kind === 'optimize'
              ? OPTIMIZE_MODE_COMMANDS
              : SOLID_COMMANDS}
        defaultCommandId={mode.kind === 'sketch' ? DEFAULT_SKETCH_COMMAND_ID : undefined}
        startCommandId={mode.kind === 'sketch-pending' ? SKETCH_CREATE.id : undefined}
        hover={hoverPoint}
        entities={selectedEntities}
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

  <div class="main">
    <!-- 左サイドバー: 保存済みファイルの一覧(Fusion 360のデータパネルに対応)。
         **開閉で `.viewport` の幅が変わる**が、変えているのは相変わらず flex の配分だけで、
         `.viewport` 自身のサイズ指定は動かない(styles.css の不変条件)。 -->
    {#if dataPanelOpen}
      <aside class="data-panel">
        <FilesPanel
          documents={files.list}
          currentName={files.currentName}
          onRename={files.rename}
          locked={optimizeBusy}
          onOpen={files.open}
          onNew={files.create}
          onCopy={files.copy}
          onDelete={files.remove}
          onExportStep={files.exportStep}
          onExportStl={files.exportStl}
        />
      </aside>
    {/if}

    <!-- ビューポートと、その上に浮かぶブラウザの入れ物。**`.stage` が浮かせる側の位置の基準**で、
         大きさを決めているのは `.stage` の中の `.viewport` 1つだけ(ブラウザは position:absolute で
         フローの外にいるので、開閉してもカメラのアスペクトに触らない)。 -->
    <div class="stage">
      <!-- canvas はこの div の中で three.js が作る。Svelte はここの中身を触らない -->
      <div class="viewport" bind:this={container}></div>

      <!-- 測定の読み札(issue #10)。**結果があるときだけ**浮く(常設の指標ではない —— docs/UI.md
           「常設の指標を置かない」)。`.stage` の中の position:absolute なので `.viewport` の
           寸法＝カメラのアスペクトには触らない(ブラウザ・設定ダイアログと同じ)。 -->
      {#if measure.text !== ''}
        <div id="measure-result" class="measure-readout">{measure.text}</div>
      {/if}

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
        onSectionToggle={display.toggleSection}
        onSectionPlane={display.setSectionPlane}
        onSectionPosition={display.setSectionPosition}
        onSectionInvert={display.setSectionInverted}
      />

      <!-- 設定ダイアログ。**`.stage` の中に置くのは「ブラウザの右」に出すため**(2026-09-04
           本人指示) —— 位置の基準が `.stage` になるので、左右のサイドバーを開閉しても
           ブラウザとの隣接が保たれる。以前は画面右上(`position: fixed`)に出しており、
           **AIサイドバーを閉じるとビューキューブの70%を覆っていた**(同日実測。キューブは
           ビューポートの右上アンカーなので、ビューポートが右へ伸びるとダイアログの下へ入る)。
           **大きさは持たない**(`position: absolute` でフローの外)ので、ブラウザや Toast と
           同じく `.viewport` の寸法＝カメラのアスペクトには触らない。 -->
      <!-- 形状最適化を走らせるパネル。**最適化モードのあいだだけ**出す。
           `SettingsDialog` と同じく `.stage` の中の `position: absolute` なので、
           `.viewport` の寸法＝カメラのアスペクトには触らない(styles.css の不変条件)。 -->
      {#if optimizeMode}
        <OptimizePanel
          conditions={optimizeConditions}
          resolution={optimizeResolution}
          stepIndex={optimizeStepIndex}
          minWidthMm={optimizeMinWidthMm}
          running={optimizeBusy}
          cancelling={optimizeCancelling}
          progress={optimizeProgress}
          notice={optimizeNotice}
          onStep={handleOptimizeStep}
          onMinWidth={handleOptimizeMinWidth}
          onRun={runOptimization}
          onCancel={() => session.cancelOptimization()}
        />
      {/if}

      <SettingsDialog
        open={settingsOpen}
        fov={display.fov}
        onFov={display.applyFov}
        onClose={() => (settingsOpen = false)}
      />
    </div>

    <!-- 右サイドバー。**AiPanel だけ = 完全にAI専用**(2026-08-30本人決定)。
         `SketchPanel.svelte` はここから外し、モード表示・「正面から見る」・「抜ける」は
         上のコマンドツールバーへ移した(数値の打ち直しは `move_sketch_entities` が引き受ける)。
         `{#if}` で `<aside>` ごと出し分けるのは左サイドバーと同じ形で、`.viewport` の
         サイズ指定は動かさず flex の配分だけが変わる。 -->
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
        />
      </aside>
    {/if}

    <!-- エラーの札。**`.main` の中に置くが大きさは持たない**(position:absolute で浮く)ので、
         ビューポートの寸法＝カメラのアスペクトには触らない。出す側は `fail()` の1本だけ。 -->
    <Toast />
  </div>
</div>
