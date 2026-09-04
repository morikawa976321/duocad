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
  import { onMount, tick } from 'svelte'
  import CommandHost from './commands/CommandHost.svelte'
  import {
    COMMANDS,
    faceRef,
    refKey,
    refKind,
    sameRef,
    sketchEntityRef,
    type CommandCategory,
    type Point2,
    type SelectableKind,
  } from './commands/command'
  import {
    SKETCH_FEATURE_COMMANDS,
    SOLID_FEATURE_COMMANDS,
    needsSketchId,
  } from './commands/features'
  import { changesDocument } from './commands/files'
  import { OPTIMIZE_MODE_COMMANDS, OPTIMIZE_PRESET_ROLE } from './commands/optimize'
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
  import BrowserPanel from './BrowserPanel.svelte'
  import Toast, { showToast } from './Toast.svelte'
  import {
    createSession,
    type AiStatus,
    type AiTranscriptEntry,
    type Applied,
    type BasePlaneName,
    type BodyGeometry,
    type BodyRef,
    type ConnectionStatus,
    type DocumentInfo,
    type EdgesPayload,
    type FilePayload,
    type PreviewPayload,
    type SelectionRef,
    type SelectionState,
    type ServerState,
    type ShapeRef,
    type SketchEntityRef,
    type SketchState,
    type VerticesPayload,
  } from './session'
  import AiPanel from './AiPanel.svelte'
  import SettingsDialog from './SettingsDialog.svelte'
  import { loadFov, saveFov } from './viewSettings'
  import {
    createViewport,
    FOV_DEFAULT,
    FOV_ORTHO,
    type BoxSelectEvent,
    type PickEvent,
    type SketchDraw,
    type VertexMarker,
    type Viewport,
  } from './viewport'

  /**
   * ツールバーの中身(モードごと)。**状態機械は1つのまま**で、変わるのは一覧と既定だけ。
   * ソリッド側の一覧には2つ、スケッチ側には反復の2つが載る ——
   * 領域押し出しは**両方に居る**(描いた直後に押し出すのと、モードを抜けてから
   * 領域を選んで押し出すのは、どちらも実際に使う)。
   *
   * **`sketch_create` はもうここに載らない**(2026-09-03)。あれはverbの行に置くと
   * 「押すと戻ってくる操作」(押し出し・フィレット)と同じ見た目で**モードを切り替える**
   * ボタンになる —— 入口だけがverbの次元に取り残されていた。いまはモードの行
   * (`.mode-row`)に上げてあり、押すと `sketch-pending` へ入って
   * `SKETCH_PENDING_COMMANDS` が起動済みで現れる(下の `mode` のdocstring)。
   */
  const SOLID_COMMANDS = [
    ...COMMANDS,
    ...SOLID_FEATURE_COMMANDS,
    // **`file` 分類の `CommandDef` はもう1つも無い**(2026-08-28。`commands/files.ts` の
    // モジュールdocstring)。`IMPORT_SVG` はスケッチモードのコマンドへ移り、`SKETCH_COMMANDS`
    // (下)側に載っている。
  ]
  const SKETCH_MODE_COMMANDS = [...SKETCH_COMMANDS, ...SKETCH_FEATURE_COMMANDS]

  /**
   * verb名 -> そのコマンドの分類。**送信経路の分岐はこれだけで決まる。**
   *
   * 知らない名前は `undefined` になり、`runCommand` がその場で断る。
   * **モードごとの一覧を全部含める** —— 1つ忘れると `runCommand` がそのモードのverbを
   * 「未知のコマンド」で断ってしまう(`sketch_create` が `SOLID_COMMANDS` を離れた
   * 2026-09-03に、`SKETCH_PENDING_COMMANDS` を足し忘れると即これを踏む)。
   */
  const CATEGORY_OF: Map<string, CommandCategory> = new Map(
    [
      ...SOLID_COMMANDS,
      ...SKETCH_PENDING_COMMANDS,
      ...SKETCH_MODE_COMMANDS,
      ...OPTIMIZE_MODE_COMMANDS,
    ].map((command) => [command.id, command.category]),
  )

  let container: HTMLDivElement
  let viewport: Viewport | null = null

  let status = $state<ConnectionStatus>('connecting')
  let model = $state<ServerState | null>(null)
  let messages = $state<string[]>([])
  /**
   * 画面へ出したエラーの控え。**表示には使わない** —— 出すのはトースト(消える)で、
   * ここは「消えた後も見られる場所」である。
   *
   * トーストにしたことで実機検査から検出力が落ちる経路が1つある: `smoke-files.mjs` は
   * 最後に一度だけ「画面にエラーが出ていないか」を見ており、**通知欄は消えないから**
   * その1回で走行全体を覆えていた。8秒で消える札を最後に見ても、途中で出て消えたエラーは
   * 拾えない。控えを残して `__duocad.errors` から読ませることで、その検査を弱めずに済む。
   */
  let errorLog = $state<string[]>([])
  let lastApplied = $state<Applied | null>(null)

  /** 送信中のコマンドの `client_op`(応答が返るまでの間だけ生きる)。 */
  let pendingOp = $state<number | null>(null)
  let opCounter = 0

  /** 投げたプレビューの番号(古い応答を捨てるため。0は「投げていない」)。 */
  let previewId = 0
  /** 画面に幽霊が出ているか(テスト用フックが見る)。 */
  let previewing = $state(false)

  // ---------------------------------------------------------------- モード(verbより1つ上の次元)
  /**
   * スケッチを抜けたときの戻り先。**「一時停止からの復帰」を型で持つ**(2026-09-03) ——
   * 最適化モードの最中に(ブラウザのスケッチ名クリックで)スケッチへ入ったら、抜けたときに
   * 最適化モードへ戻る。以前は独立した `optimizeModeRequested` という掛けっぱなしの
   * ラッチから**偶然そうなっていた**だけで、どこにも書かれていなかった。
   */
  type ResumeMode = 'idle' | 'optimize'
  /**
   * **モードはverbより1つ上の次元**(2026-09-03の本人決定)。
   *
   * モード = **文書のどの部分系を編集対象にしているか**(通常=ボディ群 / スケッチ=`sketches[i]` /
   * 最適化=`Document.optimize`)であり、verb = **その対象に1操作を適用すること**。次元が
   * 違うので、同じツールバーの行に並べない —— モードの行(`.mode-row`)が上、verbの行
   * (`CommandHost`)が下。以前は最適化だけが上の行にいて、**スケッチの入口だけが
   * `SOLID_COMMANDS` の中に取り残されていた**(出口の「抜ける」は既に上の行にあった)。
   *
   * **不正な状態が表現できない**のが型にした理由である。以前は `sketchId`(string|null)と
   * `optimizeModeRequested`(boolean)の独立した2変数で、「両方が同時に真」という状態が
   * 組み合わせとして存在し、`optimizeMode` を派生値にする防御(`sketchId === null` を
   * 先に掛ける)で潰していた —— その防御が要るのは、`BrowserPanel` のスケッチ名クリックが
   * 両立させる穴を実際に開けたからである(2026-08-31 QA指摘)。タグ付きユニオンなら
   * その状態が書けない。
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
   * verbではない(engine [[cad-verb-granularity-rule]])—— ここは画面だけの状態である。
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
    restoreLastInputs(): void
    commandSucceeded(): void
  } | null>(null)

  /**
   * 第3のモード(形状最適化)に入っているか。**対応する文書実体が無い**ので、スケッチと違い
   * 画面側だけの状態でよい(バックエンドには一切知らせない)。**状態機械は増やさない** ——
   * 変わるのはツールバーの一覧(`OPTIMIZE_MODE_COMMANDS`)と、それに応じた描画だけ。
   *
   * **スケッチとの排他は `Mode` が持つ**(2026-09-03)。以前の `optimizeModeRequested`
   * (要求)と `optimizeMode`(実効)の2段構えは、独立した2変数が両立しうることへの防御
   * だった —— タグ付きユニオンでは `sketch` と `optimize` が同じ1つの値の別の枝なので、
   * 「同時に真」が書けない。要求の保持(スケッチから戻ってくる)は `ResumeMode` が担う。
   */
  const optimizeMode = $derived(mode.kind === 'optimize')
  /**
   * 直近にトーストへ出した `stale` の鍵(`role:body:index:code`)。**`code` を含める**
   * (2026-08-31 QA指摘で修正) —— 含めないと、`hidden`(出さない側)として一度記録された
   * 対象が後で `fingerprint`/`missing`(出す側)に変わっても、role/body/indexだけが一致する
   * ために「既に見た」と誤判定され、二度と通知されない。**消えたら忘れる**
   * (次に見た集合で作り直す)ので、直ってからまた壊れれば同じ組でも再度出す。
   */
  let seenStaleKeys = new Set<string>()

  // ---------------------------------------------------------------- D: 辺とフィーチャ
  /** 起動中のコマンドが選択から取る種別(`CommandHost` が知らせる)。 */
  let wantedKinds = $state<SelectableKind[]>([])
  /**
   * 起動中のコマンドが**基準平面を要求している**か(`CommandHost` が知らせる)。
   * 辺と同じ規律で、要求している間だけビューポートで指せるようにする。
   */
  let wantedPlane = $state(false)
  /** 取り寄せた辺(body_id -> その版の折れ線)。**版が変われば捨てる**。 */
  let edges = $state<Record<string, EdgesPayload>>({})
  /** 取り寄せ中のもの(`${body_id}:${revision}`)。同じ要求を何度も投げないため。 */
  let edgesAsked = new Set<string>()
  /** 頂点の座標(**選択に頂点が入っているBodyの分だけ**取り寄せる)。 */
  let vertices = $state<Record<string, VerticesPayload>>({})
  /** 取り寄せ中のもの(辺と同じ形の印)。 */
  let verticesAsked = new Set<string>()

  // ---------------------------------------------------------------- E: ファイルと履歴
  /** 直前に書いたファイル(画面の右上に出す。書き出しは他に痕跡が残らない)。 */
  let lastFile = $state<FilePayload | null>(null)
  /** 直前に出したファイル操作の `client_op`(応答が `file` で返ったときの待ちを解く)。 */
  let lastFileOp: number | null = null
  /**
   * `duocad/models/` の中身(`list_documents` の写し。`FilesPanel.svelte` が描く)。
   *
   * **状態はコアが持つ**。ここはサーバの応答をそのまま置くだけで、
   * 保存/開くのたびに `refreshDocuments()` を呼んで最新化する(サーバ側でファイルが
   * 増減してもここが古いままにならないよう、開いたとき・保存/開くの直後に取り直す)。
   */
  let documentList = $state<DocumentInfo[]>([])
  /**
   * 直近に保存/開いたファイル名(画面に「今どれを触っているか」を出す)。
   *
   * **持つのは名前だけ**(2026-08-27に分離)。それまではこの値が `null` であること
   * 自体を「(未保存)」として出していたが、名前と保存状態は別の問い(どれを触っているか /
   * それが書けているか)なので、後者は `savedAt` が持つ。**自動保存は引き続きスコープ外**
   * (2026-08-22決定)。
   */
  let currentDocumentName = $state<string | null>(null)
  /**
   * **最後に書けた/読み込んだ時点の文書の位置** `[applied_count, 操作数]`。
   * `null` は「この画面ではまだ一度もファイルにしていない」(＝常に未保存)。
   *
   * **内容は比べない**。比べるのは「あれから文書が動いたか」だけで、それには
   * `applied_count`(verbを適用した回数。**undo/redoでは動かない**)と操作数
   * (`log.length`。undoで減りredoで戻る)の2つが要る —— 片方だけでは
   * 「undoで保存時点へ戻した文書」(操作数は一致・applied_countも不変)と
   * 「undoしてから別のverbを打った文書」(操作数は一致・applied_countが増える)が
   * 見分けられず、後者を保存済みと言ってしまう。
   *
   * **偽るとしたら「未保存」の側だけ**、という非対称になっている。保存後にverbを打って
   * からundoで同じ形へ戻した文書は、中身は保存済みと同じでも `applied_count` が増えた
   * ままなので「未保存」と出る。逆(書けていないのに保存済みと出る)は起こらない ——
   * `applied_count` が動いていないなら新しいverbは1つも適用されておらず、そのうえで
   * 操作数まで一致するなら履歴の位置も同じだからである。
   *
   * 可視性の切り替えはverbではなくファイルにも載らないので、どちらも動かない
   * (＝保存済みのまま。これは正しい)。
   */
  let savedAt = $state<[number, number] | null>(null)
  /** 今の文書がそのままファイルに載っているか(ヘッダの `#save-state`)。 */
  const saved = $derived(
    savedAt !== null &&
      model !== null &&
      model.applied_count === savedAt[0] &&
      model.log.length === savedAt[1],
  )
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
   * 右サイドバー(`AiPanel` だけ = **完全にAI専用**)を開いているか(2026-08-30本人決定)。
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
  /**
   * 起動時に読んだ画角。**下の2つの `$state` の初期値はここから作る** ——
   * `$state` の初期化で別の `$state` を読むと「初期値しか見ていない」警告になる。
   */
  const savedFov = loadFov()
  /**
   * カメラの画角[度]。**画面の設定であって文書の状態ではない** ——
   * コアに送らず(`session.*` を通らない)、操作ログにも残らず、undoの対象にもならない。
   * 永続化はブラウザのlocalStorageだけ(`viewSettings.ts`)。`FOV_ORTHO`(0)は完全正投影。
   */
  let fov = $state(savedFov)
  /**
   * **正投影から戻る先の角度**(`FOV_MIN`〜`FOV_MAX`)。0は角度ではないので別に覚える ——
   * これが無いと、正投影を外したときに戻る先が既定の45へ落ち、直前まで見ていた画角が失われる。
   */
  let fovAngle = $state(savedFov === FOV_ORTHO ? FOV_DEFAULT : savedFov)

  /** 設定からの変更。**その場で効かせて、そのまま覚える**(「適用」を挟まない)。 */
  function applyFov(deg: number): void {
    if (!Number.isFinite(deg)) return
    fov = deg
    if (deg !== FOV_ORTHO) fovAngle = deg
    saveFov(deg)
  }
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
  /** 今の設定(`session.aiGetStatus()` の返事・`ai_configure`/実行の開始終了で更新)。 */
  let aiStatus = $state<AiStatus | null>(null)
  /** 会話の時系列。**コアには無い**(`AiPanel.svelte` のdocstring参照)。 */
  let aiTranscript = $state<AiTranscriptEntry[]>([])

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
      // **繋がっていたのに切れた**のはエラー(操作が届かなくなる)。最初の接続待ちや
      // 再接続の試行を毎回トーストにしても人間にできることは無いので、落差だけを見る。
      const wasOpen = status === 'open'
      status = next
      if (wasOpen && next !== 'open') fail('サーバとの接続が切れた(自動で繋ぎ直しを試みる)')
      // (再)接続したら今のAI設定を取り直す(サーバは再起動していなくても、
      // 前回の接続が切れている間に別の画面が `ai_configure` した可能性がある)。
      if (next === 'open') session.aiGetStatus()
      // (再)接続したら `models/` の一覧も取り直す(サーバの再起動・別セッションの
      // 保存で増減しうる。`FilesPanel.svelte` が空のまま固まらないようにする)。
      if (next === 'open') refreshDocuments()
      // 応答待ちの最中に接続が切れたら、その `client_op` はもう返ってこない。
      // 捨てないと再接続後も「直前の操作の応答待ち」が残り続け、Enterが永久に拒否される
      // (`onerror` を伴わない正常クローズで踏む)。
      if (next !== 'open' && pendingOp !== null) {
        pendingOp = null
        clearPreview()
      }
      // **投げ損ねた辺の要求を覚えたままにしない**。接続が切れて
      // いる間の要求は送られずに捨てられるが、`edgesAsked` には残る —— 再接続しても
      // 版が同じなら「もう頼んだ」と判断して二度と取りに行かず、**そのBodyの辺が
      // 出ないまま**フィレットが使えなくなる(`pendingOp` を捨てるのと同じ形の穴)。
      if (next !== 'open') {
        edgesAsked.clear()
        verticesAsked.clear()
      }
    },
    onPreview: handlePreview,
    onEdges: handleEdges,
    onVertices: handleVertices,
    onFile: handleFile,
    onAiStatus: handleAiStatus,
    onAiToolCall: (call) => (aiTranscript = [...aiTranscript, { kind: 'tool', call }]),
    onAiResult: (result) => (aiTranscript = [...aiTranscript, { kind: 'result', result }]),
    onAiError: handleAiError,
  })

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
  /**
   * **エラー**を1件、トーストで出す(拒まれた・失敗した・繋がらない)。
   *
   * **エラーが画面に出る経路はこの関数1本だけ**にしてある。各パネルが個別に
   * `showToast` を叩き始めると、同じ失敗が2枚出たり、パネルを畳むとエラーが消えたりする
   * (WSの応答を受けるのはこの層だけ、という形をそのまま使う)。
   */
  function fail(message: string): void {
    if (!message) return
    errorLog = [message, ...errorLog].slice(0, 20)
    showToast(message)
  }

  // ---------------------------------------------------------------- G: アプリ内AIエージェント
  function handleAiStatus(next: AiStatus): void {
    aiStatus = next
    // **繋がらない理由を黙って捨てない**(`ai_configure` の拒否。タスクファイルの要件)。
    if (next.error) fail(`AI設定: ${next.error}`)
  }

  function handleAiError(message: string): void {
    aiTranscript = [...aiTranscript, { kind: 'error', message }]
    fail(`AI ${message}`)
  }

  function sendAiMessage(text: string): void {
    aiTranscript = [...aiTranscript, { kind: 'user', text }]
    session.aiSend(text)
  }

  function nextOp(): number {
    opCounter += 1
    myClientOps.add(opCounter) // この番号で返ってきた操作は「自分」(上のdocstring)
    return opCounter
  }

  // ---------------------------------------------------------------- コアからの状態
  function handleState(next: ServerState, changed: BodyGeometry[], applied: Applied | null): void {
    model = next
    // **編集中のファイル名はサーバの写し**(2026-08-30)。`applied` や `file` の応答から
    // 拾い直さない —— 正は `Session._current_name` 1つで、こちらは配信のたびに映すだけ。
    // これでリロードしても名前が戻る(以前は文書だけサーバに残って名前が失われた)。
    currentDocumentName = next.current_name
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
    if (changed.length > 0) previewing = false // 本物が届いたら幽霊は消えている(viewport.update)

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
      if (applied.client_op !== null && applied.client_op === pendingOp) pendingOp = null
      return
    }
    if (applied && applied.change === 'visibility') {
      // **可視性もverbではない**(操作ログに載らない)ので `lastApplied` を動かさない。
      // 効果は同じ配信の `state.bodies[].visible` に載っており、ここで見るのは
      // 「自分が出した操作か」の突き合わせだけ(表示切り替えは `pendingOp` を立てない
      // 経路なので、印は `myClientOps` の側が持っている)。
      if (applied.client_op !== null && applied.client_op === pendingOp) pendingOp = null
      return
    }
    if (applied && applied.change === 'document') {
      // **開いた**(`open_document`)か**新規にした**(`new_document`)。保存と同じく、ここで
      // ファイル名を追い、一覧も取り直す(開いた版が一覧の「操作数」に含まれるのはこの
      // ファイル自身なので変わらないが、他の操作で増減している可能性は保存と同じく
      // 否定できない)。**新規は `name` が `null`** で来るので、触っているファイルの表示は
      // 「(無題)」へ戻る —— ただし表示そのものを更新するのは上の `next.current_name` で、
      // ここが見るのは「開いたのか新規にしたのか」の区別だけである。
      // **開いた直後の文書はファイルの中身そのもの**なので保存済みから始める。新規は
      // どこにも書かれていないので `null`(＝常に未保存)へ戻す。`next` は上で `model` に
      // 入れた今回の状態で、開いた版の `applied_count` / 操作数がもう入っている。
      savedAt = applied.name === null ? null : [next.applied_count, next.log.length]
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
      if (applied.client_op !== null && applied.client_op === pendingOp) pendingOp = null
      lastApplied = applied
      refreshDocuments()
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
      if (applied.client_op !== null && applied.client_op === pendingOp) {
        pendingOp = null
        // **CommandHostが出した確定が、実際に成功したと確認できた**(2026-08-31)——ここで
        // 初めて無起動へ戻す。`commit()` の時点(`onRun` を呼んだ直後)ではまだ成否が
        // 分からず、先に戻すとOCCTのフィレット失敗時の打ち直し(`CommandHost.commit()`の
        // コメント参照)が壊れる。`sketch`/`feature` 以外(`file`/`history`等)は
        // CommandHostの外で走るので対象にしない——`import_step`(FilesPanel経由)の成功で
        // 人が別コマンドに入力中のCommandHostを畳んではならない。
        const category = CATEGORY_OF.get(applied.verb)
        if (category === 'sketch' || category === 'feature') host?.commandSucceeded()
      }
      lastApplied = applied
    }
  }

  function handlePreview(payload: PreviewPayload): void {
    if (payload.preview_id !== previewId) return // 追い越された応答・中断後の応答は捨てる
    viewport?.showPreview(
      payload.bodies.map((body) => ({ bodyId: body.body_id, geometry: body.geometry })),
    )
    previewing = payload.bodies.length > 0
  }

  function handleEdges(payload: EdgesPayload): void {
    edges = { ...edges, [payload.body_id]: payload }
  }

  function handleVertices(payload: VerticesPayload): void {
    vertices = { ...vertices, [payload.body_id]: payload }
  }

  /**
   * ファイルを書いた/一覧が届いた。**どこへ何バイト書いたかを必ず画面へ出す** ——
   * 書き出しはブラウザの中に痕跡が残らない操作なので、通知が唯一の確認手段になる。
   */
  function handleFile(payload: FilePayload): void {
    // **`list_documents` の応答はここで分岐して抜ける**。一覧の取得は保存/開くの
    // たびに黙って裏で走る(`refreshDocuments` 参照)ので、他のファイル操作と同じ
    // 「保存した/開いた」通知を出すと、一覧を取り直すたびに無意味な行が積まれる。
    if (payload.action === 'list') {
      documentList = payload.files ?? []
      return
    }
    lastFile = payload
    // **ここでファイル名を控えない**(2026-08-30) —— 編集中のファイルはサーバが持ち
    // (`state.current_name`)、保存の直後にはサーバが状態も配り直す(`net/ws.py` の
    // `save_document` 分岐)。応答から拾い直すと真実源が2つになる。
    // **書けた時点の位置を控える**(ヘッダの `#save-state` が「保存済み」を言う根拠)。
    // 数えるのは `save` だけ —— `export_step`/`export_stl` は形を別形式で吐くだけで、
    // 文書(操作ログ)を書いてはいない。操作数はサーバが実際に書いた件数を返してくるので
    // それを使う(こちらで数え直さない)。
    if (payload.action === 'save' && model !== null) {
      savedAt = [model.applied_count, payload.operations ?? model.log.length]
    }
    // **応答が `file` で返ったなら、その操作の待ちはここで解く**。
    // 「文書を変えるか」の判定はブラウザとサーバで別々に持っているので、片方だけ
    // 直したときに**状態配信を待ち続けて確定が永久に拒否される**経路ができる。
    // 出した番号を控えてあるので、それと一致するときだけ解けばよい(取りすぎない)。
    if (pendingOp !== null && pendingOp === lastFileOp) pendingOp = null
    const size = `${payload.bytes.toLocaleString()} B`
    const extra = payload.operations !== undefined ? ` / 操作 ${payload.operations}件` : ''
    // **export_step/export_stlのみ**body_idsが載る。書き出した個数と、非表示で除外した
    // 個数(0件なら文言を出さない)を並べる —— 「隠したつもりが無いのに減っている」を
    // 見えない驚きにしないため。
    const exported = payload.body_ids !== undefined ? ` / ${payload.body_ids.length}個` : ''
    const skippedCount = payload.skipped_hidden?.length ?? 0
    const skipped = skippedCount > 0 ? ` ※非表示の${skippedCount}個は除外` : ''
    note(`${payload.action}: ${payload.path}(${size}${extra}${exported}${skipped})`)
    // **保存/開いた直後に一覧を取り直す**。上書きで更新日時・サイズ・操作数が変わり、
    // 新規保存では一覧に新しい行が要る —— 応答をここで待たず、次のWS往復に任せる。
    if (payload.action === 'save') refreshDocuments()
  }

  /** `duocad/models/` の一覧をサーバへ取り直しに行く(`FilesPanel.svelte` が描く)。 */
  function refreshDocuments(): void {
    session.runVerb('list_documents', {}, nextOp())
  }

  /**
   * **新しい名前で**保存する(「名前を付けて保存」。`FilesPanel` の名前欄だけが呼ぶ)。
   *
   * **既存名は送っても通らない** —— サーバが拒否してトースト1枚になる
   * (`Session.save_document`)。ここで `documentList` を見た実在チェックは**足さない**:
   * パス解釈の正を2箇所に持たないという既存の規律(`FilesPanel.nameProblem` のdocstring)。
   *
   * **`pendingOp` は立てない** —— `FilesPanel.svelte` は `CommandHost` の応答待ちの外にいる
   * (一覧からのクリックは「対象を選ぶ」そのもので、引数収集の状態機械を通す理由が無い)。
   */
  function saveDocumentAs(name: string): void {
    const op = nextOp()
    lastFileOp = op
    session.runVerb('save_document', { name }, op)
  }

  /**
   * **編集中のファイルへ上書き保存する**(ヘッダの `#file-save` と Ctrl+S。2026-08-30)。
   *
   * 引数を送らない —— 書き込み先を決めるのはサーバ(`Session._current_name`)であって、
   * 画面が名前を送り返す形だと「表示されている名前」と「実際に開いているファイル」が
   * ずれた瞬間に別のファイルを潰す。
   *
   * **編集中のファイルが無いとき(起動直後・`new` の直後)は押せないボタンにしない。**
   * 新規の文書では必ず通る道なので、`disabled` にすると**なぜ押せないかを言わないまま
   * 死んでいる札**になる(本人決定)。代わりにファイルパネルを開いて名前欄へ入れる ——
   * 「保存したい」に対する答えは「名前を付けろ」であり、その場所まで連れて行く。
   */
  function saveCurrentDocument(): void {
    if (currentDocumentName === null) {
      dataPanelOpen = true
      note('まだファイル名が無い。名前を打って「新規保存」すると、以後はここへ上書きできる')
      // パネルを開いた直後は入力欄がまだDOMに無い(Svelteの更新は非同期)。
      void tick().then(() => document.querySelector<HTMLInputElement>('#file-name-input')?.focus())
      return
    }
    const op = nextOp()
    lastFileOp = op
    session.runVerb('save_document', {}, op)
  }

  /** ファイル名を渡して開く。応答は `state`(`applied.change === 'document'`)で来る。 */
  function openDocumentByName(name: string): void {
    session.runVerb('open_document', { name }, nextOp())
  }

  /**
   * 空の文書にする(「新規」)。応答は開くのと同じ `state`
   * (`applied.change === 'document'` / `action: "new"`)で来る。
   *
   * **確認を挟まない**(`FilesPanel.svelte` の「開くのに確認ダイアログを挟まない」と同じ線)。
   */
  function newDocument(): void {
    session.runVerb('new_document', {}, nextOp())
  }

  /**
   * STEP/STL入出力(2026-08-27に`FilesPanel.svelte`へ移設。ツールバーの`CommandHost`を
   * 経由していた頃の `runCommand` の `case 'file'` と**同じ挙動**にする —— 二重化しないよう
   * `changesDocument` をそのまま呼ぶ(`import_step` だけが文書を変える)。
   *
   * **書き出しは`pendingOp`を立てない**(立てると`handleFile`が来るまでEnterが拒否される)。
   */
  function runFileOp(verb: string, args: Record<string, unknown>): void {
    const op = nextOp()
    lastFileOp = op
    if (changesDocument(verb)) pendingOp = op
    session.runVerb(verb, args, op)
  }

  function importStep(path: string): void {
    runFileOp('import_step', { path })
  }

  function exportStep(path: string): void {
    runFileOp('export_step', { path })
  }

  function exportStl(path: string, deviation: number): void {
    runFileOp('export_stl', { path, deviation })
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
      (selection?.items ?? [])
        .filter((item): item is SketchEntityRef => refKind(item) === 'sketch_entity')
        // **編集中のスケッチのものだけ**(反復verbは `sketch_id` と `entities` を別々に送るので、
        // 混ざると「このスケッチに無い図形ID」をサーバへ渡すことになる)。`enterSketch` が
        // 他のスケッチの選択を外すのが本筋で、ここはそれを二重に守る側である。
        .filter((item) => item.sketch === sketchId)
        .map((item) => item.entity),
    ),
  ])

  const selectedFaces = $derived(
    (selection?.items ?? []).filter(
      (item): item is ShapeRef => !('sketch' in item) && (item as ShapeRef).kind === 'face',
    ),
  )
  /**
   * 選択中のボディ(形状最適化の保持/除外を決めているあいだだけ現れる。
   * `refKind` で見分ける —— `BodyRef` は `kind: 'body'` を持つ)。
   */
  const selectedBodies = $derived(
    (selection?.items ?? []).filter((item): item is BodyRef => refKind(item) === 'body'),
  )
  /** 選択中の辺(**辺を出している間だけ**3Dで色が変わる)。 */
  const selectedEdges = $derived(
    (selection?.items ?? []).filter(
      (item): item is ShapeRef => !('sketch' in item) && (item as ShapeRef).kind === 'edge',
    ),
  )

  /** 選択中の頂点(**選ばれているものだけ**が3Dにマーカーで出る)。 */
  const selectedVertices = $derived(
    (selection?.items ?? []).filter(
      (item): item is ShapeRef => !('sketch' in item) && (item as ShapeRef).kind === 'vertex',
    ),
  )

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
      const body = model?.bodies.find((candidate) => candidate.body_id === item.body)
      const known = vertices[item.body]
      if (!body || !body.visible || known?.revision !== body.revision) continue
      const point = known.vertices.find((candidate) => candidate.index === item.index)?.point
      if (point) markers.push({ bodyId: item.body, vertexIndex: item.index, point })
    }
    return markers
  })

  function drawHighlight(): void {
    const origin = selection?.origin === 'proposed' ? 'proposed' : 'user'
    viewport?.highlight(
      selectedFaces.map((item) => ({ bodyId: item.body, faceIndex: item.index })),
      origin,
    )
    // **選択が勝つ**(タスク仕様の色の規則)。ボディが選ばれているあいだは保持/除外の
    // 役割色より優先してハイライト色を出す(`viewport.applyFaceMaterials` が実体)。
    viewport?.highlightBodies(
      selectedBodies.map((item) => item.body),
      origin,
    )
    viewport?.highlightEdges(
      selectedEdges.map((item) => ({ bodyId: item.body, edgeIndex: item.index })),
      origin,
    )
    viewport?.showVertices(vertexMarkers, origin)
  }

  /**
   * 選択に入った頂点の座標を取りに行く。
   *
   * **辺と違って「コマンドが要求している間」ではない** —— 頂点を引数に取るコマンドが
   * 無いので、要求の有無ではなく**選択に入っているかどうか**が出す条件になる
   * (`viewport.showVertices` のdocstring)。
   */
  $effect(() => {
    for (const item of selectedVertices) {
      const body = model?.bodies.find((candidate) => candidate.body_id === item.body)
      // **見えているBodyだけ**に頼む(隠れているものの頂点はサーバが断る。辺と同じ規律)。
      if (!body || !body.visible) continue
      if (vertices[item.body]?.revision === body.revision) continue
      const key = `${item.body}:${body.revision}`
      if (verticesAsked.has(key)) continue
      verticesAsked.add(key)
      session.getVertices(item.body)
    }
  })

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
    // 選択のうち見えているものを先に見る。
    const visible = new Set(
      (model?.bodies ?? []).filter((body) => body.visible).map((body) => body.body_id),
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

  /** 今のコマンドが辺を要求しているか(スケッチモード中は辺を出さない)。 */
  const wantsEdges = $derived(sketchId === null && wantedKinds.includes('edge'))

  $effect(() => {
    if (!wantsEdges || edgeTargetBody === null) {
      viewport?.showEdges(null, [])
      return
    }
    const body = model?.bodies.find((item) => item.body_id === edgeTargetBody)
    const known = edges[edgeTargetBody]
    // **版が違う辺は使わない**(形が変わった後の古い添字は別の辺を指す。§7)
    if (body && known?.revision === body.revision) {
      viewport?.showEdges(edgeTargetBody, known.edges)
      drawHighlight()
      return
    }
    viewport?.showEdges(null, [])
    const key = `${edgeTargetBody}:${body?.revision ?? '?'}`
    if (edgesAsked.has(key)) return
    edgesAsked.add(key)
    session.getEdges(edgeTargetBody)
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
   * 通常モードで領域を指せるのは、**領域を要求しているコマンド(「領域を押し出す」)を
   * 起動している間だけ**(辺・基準平面と同じ規律。2026-08-23の本人裁定5)。
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
    const current = selection?.items ?? []
    const already = current.some((item) => sameRef(item, ref))
    if (mode === 'toggle') {
      if (already) modifySelection([], [ref])
      else modifySelection([ref], [])
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
  function onBoxSelect(event: BoxSelectEvent): void {
    const picked: SelectionRef[] = [
      ...event.faces.map((item) => faceRef(item.bodyId, item.faceIndex)),
      ...event.edges.map((item) => ({ body: item.bodyId, kind: 'edge' as const, index: item.edgeIndex })),
      ...event.vertices.map((item) => ({ body: item.bodyId, kind: 'vertex' as const, index: item.vertexIndex })),
      ...event.regions.map((item) => ({ sketch: item.sketchId, index: item.index })),
      ...event.sketchEntities.map((item) => sketchEntityRef(item.sketchId, item.entityId, item.point)),
    ]
    const current = selection?.items ?? []
    const currentKeys = new Set(current.map(refKey))
    if (event.mode === 'toggle') {
      const already = picked.filter((ref) => currentKeys.has(refKey(ref)))
      const fresh = picked.filter((ref) => !currentKeys.has(refKey(ref)))
      modifySelection(fresh, already)
      return
    }
    if (picked.length === 0) {
      if (current.length > 0) session.clearSelection(nextOp())
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
    const current = selection?.items ?? []
    modifySelection([ref], current.filter((item) => !sameRef(item, ref)))
  }

  /** `selectOnly` の複数版 —— 選択集合を `refs` そのものに置き換える。 */
  function presetSelection(refs: SelectionRef[]): void {
    const current = selection?.items ?? []
    // `onBoxSelect` と同じ理由で `Set` 引き(保持/除外ソリッドは数百件になりうる)。
    const refKeys = new Set(refs.map(refKey))
    modifySelection(
      refs,
      current.filter((item) => !refKeys.has(refKey(item))),
    )
  }

  /**
   * コマンドが起動した(`CommandHost.start`)。**形状最適化の4つだけが意味を持つ** ——
   * 起動した役割の**既存の割り当て**をそのまま選択状態にして見せる(タスク仕様
   * 「verb起動 → 既存の割り当てが選択状態で現れる」)。`duocad/optimize.py` の契約どおり
   * `state.optimize` の該当配列はそのまま `modifySelection` へ渡せる形をしている。
   */
  function onCommandStart(verb: string): void {
    const role = OPTIMIZE_PRESET_ROLE[verb]
    if (role === undefined || model === null) return
    presetSelection(model.optimize[role])
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
    const current = selection?.items ?? []
    // **スケッチを貼る先を待っているとき**。基準平面はその場で決まり(即適用が同期に走る)、
    // 面は「選択を置き換えたうえで `'face'` を入れる」——
    // 送るのは選択が確定して配信されてからで、その待ち受けは `CommandHost` が持つ。
    if (host?.wantsPlane()) {
      if (event.plane !== null) {
        host.setBasePlane(event.plane)
        return
      }
      if (event.hit !== null) {
        host.useSelectedFaceAsPlane()
        selectOnly(faceRef(event.hit.bodyId, event.hit.faceIndex))
        return
      }
      // 何も無い所をクリックしたら、通常どおり選択解除だけして待ち続ける(下へ落とす)
    }
    // **辺が当たったら辺**。フィレット・面取りを起動している間だけ来る。
    if (event.edge !== null) {
      editSelection(
        { body: event.edge.bodyId, kind: 'edge', index: event.edge.edgeIndex },
        event.mode,
      )
      return
    }
    // **頂点が当たったら頂点**。選択に入っている頂点だけがマーカーとして出ているので、
    // ここへ来るのは実質「AIが選んだ頂点を人がCtrl+クリックで外す」経路である
    // (頂点を選択へ新規追加するコマンドは無い)。参照の形も修飾キーの規則も面・辺と同じ。
    if (event.vertex !== null) {
      editSelection(
        { body: event.vertex.bodyId, kind: 'vertex', index: event.vertex.vertexIndex },
        event.mode,
      )
      return
    }
    // **領域が当たったら領域**。「領域を押し出す」を起動している間だけ来る
    // (`wantsRegions` → `viewport.pickRegions`)。参照の形も修飾キーの規則も
    // スケッチモードの `onSketchPick` と同じ —— 同じものを同じように選ぶ。
    if (event.region !== null) {
      editSelection({ sketch: event.region.sketchId, index: event.region.index }, event.mode)
      return
    }
    if (event.hit === null) {
      // 何もない所を素でクリック = 選択解除(修飾キー付きは空振りにする)
      if (event.mode === 'replace' && current.length > 0) session.clearSelection(nextOp())
      else if (event.mode === 'replace') note('面を外した')
      return
    }
    // **保持/除外ソリッドのverbが起動している間はボディ粒度**(タスク仕様)。
    // `wantedKinds` に `'body'` が立つのは `commands/optimize.ts` の `SET_PRESERVE_BODIES` /
    // `SET_EXCLUDE_BODIES` だけなので、面が当たってもクリックした面ではなくその
    // ボディ全体を選択へ足す。
    if (wantedKinds.includes('body')) {
      editSelection({ body: event.hit.bodyId, kind: 'body', index: null }, event.mode)
      return
    }
    editSelection(faceRef(event.hit.bodyId, event.hit.faceIndex), event.mode)
  }

  // ---------------------------------------------------------------- C: スケッチ
  const sketches = $derived(model?.sketches ?? [])
  /** 今編集しているスケッチ(ツールバーの札が読む)。モード外・まだ届いていなければ null。 */
  const activeSketch = $derived(
    sketches.find((item) => item.sketch_id === sketchId) ?? null,
  )

  /** 描画指示を組み立てる(選択中の領域だけ強調色になる)。 */
  function sketchDraws(): SketchDraw[] {
    const selectedRegions = (selection?.items ?? [])
      // **`sketch` を持つ参照は2種類ある**(領域とスケッチの図形)。`refKind` で分ける ——
      // `'sketch' in item` だけで拾うと、選んだ点が「領域の添字」として読まれる。
      .filter((item): item is { sketch: string; index: number } => refKind(item) === 'region')
    const selectedHandles = (selection?.items ?? []).filter(
      (item): item is SketchEntityRef => refKind(item) === 'sketch_entity',
    )
    return sketches
      .filter((item) => item.frame !== null)
      .map((item) => ({
        sketchId: item.sketch_id,
        frame: item.frame as NonNullable<typeof item.frame>,
        plane:
          item.plane.kind === 'base'
            ? { kind: 'base' as const }
            : { kind: 'face' as const, bodyId: item.plane.body, faceIndex: item.plane.index },
        entities: item.drawn,
        regions: item.regions,
        selectedRegions: selectedRegions
          .filter((ref) => ref.sketch === item.sketch_id)
          .map((ref) => ref.index),
        // **点・辺を出すのは編集中のスケッチだけ**。全スケッチに出すと、通常モードの
        // 画面が点だらけになるうえ、指せないもの(モード外のスケッチの図形)が
        // 指せそうに見える(UI指針: 必要なものだけを出す)。
        handles:
          item.sketch_id === sketchId
            ? item.entities.map((entity) => ({
                entityId: entity.entity_id,
                kind: entity.kind as string,
                params: entity.params,
              }))
            : [],
        selectedHandles: selectedHandles
          .filter((ref) => ref.sketch === item.sketch_id)
          .map((ref) => ({ entityId: ref.entity, point: ref.point })),
      }))
  }

  function drawSketches(): void {
    viewport?.showSketches(sketchDraws())
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
    const nextSketch = next.kind === 'sketch' ? next.id : null
    const unusable = (selection?.items ?? []).filter((item) => {
      const kind = refKind(item)
      if (kind === 'sketch_entity') return (item as SketchEntityRef).sketch !== nextSketch
      if (kind === 'body') return next.kind !== 'optimize'
      return false
    })
    if (unusable.length > 0) modifySelection([], unusable)
    mode = next
    hoverPoint = null
    viewport?.setActiveSketch(nextSketch)
    // **どの遷移でも描き直す**(`$effect` に任せない)。入る側は直後の `lookAtSketch` が
    // 平面の枠を要るので1手遅らせられない(`handleState` と同じ理由)。**抜ける側にも呼ぶ**
    // —— 旧 `exitSketch` はここを呼んでおらず、抜けたスケッチの点マーカーが次の状態配信まで
    // 画面に残っていた(選択の掃除が空振りするとWS往復が起きず、その配信も来ない)。
    // 残ったマーカーは手前に浮くのでクリックを奪いうる([[duocad-sketch-handles-crowd-region-picking]]
    // と同じ型)。**`smoke-move.mjs` の辺ピックの失敗がこれで直るかは未確認** ——
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
   * ボタン→クリックの2手は変わらない([[duocad-sketch-start-is-two-steps-by-pointing]])。
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
    setMode({ kind: 'sketch', id, resume: resumeOf(mode) })
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
      if (host.addPoint(event.point.u, event.point.v)) return
    }
    // **図形は領域より先**(点・辺は領域の縁に乗っているので、領域が勝つと端点を掴めない)。
    // 描くコマンドが起動している間は上の `addPoint` が先に食うので、ここへは来ない。
    if (event.sketchEntity) {
      editSelection(
        sketchEntityRef(
          event.sketchEntity.sketchId,
          event.sketchEntity.entityId,
          event.sketchEntity.point,
        ),
        event.mode,
      )
      return
    }
    if (event.region) {
      editSelection({ sketch: event.region.sketchId, index: event.region.index }, event.mode)
      return
    }
    if (event.mode === 'replace' && (selection?.items.length ?? 0) > 0) {
      session.clearSelection(nextOp())
    }
  }



  // ---------------------------------------------------------------- コマンド層との繋ぎ
  /**
   * 揃った引数をサーバへ送る。**経路は `CommandDef.category` だけで決まる。**
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
          const op = nextOp()
          pendingOp = op
          session.runVerb(verb, args, op)
          return
        }
        if (sketchId === null) {
          fail('スケッチモードに入っていない')
          return
        }
        const op = nextOp()
        pendingOp = op
        viewport?.showSketchGhost(sketchId, null) // 本物を頼んだので幽霊は消す
        // **引数名はコマンド定義とサーバの宣言で既に揃っている**(p0/p1・center/radius・
        // corner/width/height・center/radius/start_deg/end_deg)。ここで詰め替えない。
        // **`move_sketch_entities` だけは `sketch_id` を足さない** —— どのスケッチかは
        // 選んだ参照(`{sketch, entity, point}`)が持っている(`runsWithoutSketchId`)。
        session.runVerb(
          verb,
          runsWithoutSketchId(verb) ? args : { ...args, sketch_id: sketchId },
          op,
        )
        return
      }
      // -------------------------------------------------------------- D: フィーチャ
      case 'feature': {
        if (needsSketchId(verb) && sketchId === null) {
          fail('スケッチモードに入っていない(反復はスケッチの図形が対象)')
          return
        }
        if (verb === 'extrude' && !regionsStillExist()) {
          // **領域はパラメータ編集で分裂・消失しうる**(§7)。消えた領域を送ると
          // サーバは添字が範囲外だと言うだけなので、ここで何が起きたかを画面へ出す。
          fail('選んだ領域が今のスケッチに無い(図形が変わって分裂・消失した可能性)。選び直す')
          return
        }
        const op = nextOp()
        pendingOp = op
        session.runVerb(verb, needsSketchId(verb) ? { ...args, sketch_id: sketchId } : args, op)
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

  /**
   * これから押し出す領域(=コアの選択にある領域)が、指定したスケッチ一覧に実在するか。
   *
   * **選択そのものを見る**。押し出す領域はサーバがコアの選択から取るので、
   * 送る引数には入っていない —— 引数を見に行くと常に空になり、この検査が
   * 黙って空振りする。
   *
   * **引数を取る形にしてある**(2026-08-27)。呼び出し側(`runCommand`)は既定値の
   * 現在の `selection`/`sketches` で呼ぶが、テスト(`window.__duocad.regionsStillExist`)は
   * 合成データを渡して分岐を直接検査できる —— この関数がfalseを返す状況(選んだ領域が
   * 実際に消えている)は、`duocad/catalog.py` の `resets_selection=True`(17verb全て。
   * `tests/test_server_selection_reset.py` が過不足なく固定)が領域を変えうるverbの
   * すべてに掛かっているため、**通常のUI操作では選択が先に空になり到達できない**
   * (`tests/test_server_selection_reset.py::test_set_parameter_clears_a_selected_region_that_the_edit_makes_vanish`
   * が実測)。ロジック自体の正しさをpuppeteerから検算するための穴が要る理由はこれ。
   */
  function regionsStillExist(
    sel: SelectionState | null = selection,
    sks: SketchState[] = sketches,
  ): boolean {
    const refs = (sel?.items ?? []).filter(
      // 領域だけ(スケッチの図形の参照も `sketch` を持つので `refKind` で分ける)
      (item): item is { sketch: string; index: number } => refKind(item) === 'region',
    )
    return refs.every((ref) =>
      sks.some(
        (item) =>
          item.sketch_id === ref.sketch &&
          item.regions.some((candidate) => candidate.index === ref.index),
      ),
    )
  }

  /**
   * 確定前の姿。**スケッチの図形はサーバへ投げない**(`mode === 'local'`)——
   * 形はブラウザ側で分かっており、投機実行にすると中断しても文書に一度は入る。
   */
  function requestPreview(
    verb: string,
    args: Record<string, unknown>,
    mode: 'server' | 'local',
  ): void {
    if (mode === 'local') {
      if (sketchId !== null) viewport?.showSketchGhost(sketchId, ghostEntity(verb, args))
      return
    }
    previewId += 1
    session.preview(verb, args, previewId)
  }

  function clearPreview(): void {
    previewId += 1 // 投げてある応答を無効にする(戻ってきても番号が合わない)
    previewing = false
    viewport?.clearPreview()
    if (sketchId !== null) viewport?.showSketchGhost(sketchId, null)
  }

  function onWindowKeydown(event: KeyboardEvent): void {
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
    // **Ctrl+S はヘッダの保存ボタン(`#file-save`)と同じ操作**(2026-08-30)。編集中の
    // ファイルへ上書きし、まだ無ければファイルパネルの名前欄へ連れて行く
    // (`saveCurrentDocument`)。
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
      saveCurrentDocument()
      return
    }
    if (isTextEditingField) return
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
  // 概念ごと撤去した([[duocad-reference-body-removal-decided]])。
  const partBodies = $derived(model?.bodies ?? [])

  function setVisible(bodyId: string, visible: boolean): void {
    session.setVisible(bodyId, visible, nextOp())
  }

  function showAllParts(): void {
    for (const body of partBodies) if (!body.visible) setVisible(body.body_id, true)
  }

  function hideAllParts(): void {
    for (const body of partBodies) if (body.visible) setVisible(body.body_id, false)
  }

  onMount(() => {
    viewport = createViewport(container)
    // **ここで1回明示的に呼ぶ**。`viewport` は `$state` ではないただの `let` なので、
    // 下の `$effect(() => viewport?.setFov(fov))` は**この代入では再走しない** ——
    // 初回のエフェクトは `viewport === null` のまま走り終わっており、保存されたFOVが
    // 画面に出ないままになる(Svelte 5 runesの罠)。
    viewport.setFov(fov)
    viewport.onPick(onPick)
    viewport.onBoxSelect(onBoxSelect)
    viewport.onPlaneMove((point) => {
      hoverPoint = point === null ? null : [point.u, point.v]
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
      loads: optimize.loads.map((item) => ({ bodyId: item.body, faceIndex: item.index })),
      load: optimize.load,
    })
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

  // 画角が変わったらビューポートへ流す。**初回の反映はここではなく `onMount` が行う**
  // (`viewport` は `$state` ではないので、代入ではこのエフェクトが再走しない)。
  $effect(() => {
    viewport?.setFov(fov)
  })

  /** 状態配信を受けた回数。**実機検査が往復の完了を知るための穴**(`handleState` が増やす)。 */
  let stateEpoch = $state(0)

  /** 選択の先頭の面。**テスト用フックの `selected` だけが使う**(互換のための穴)。 */
  const firstFace = $derived(selectedFaces[0] ?? null)

  // 自動テスト(scripts/smoke.mjs)がブラウザの中を見るための穴。ローカル専用ツールなので
  // 常に生やしてある(「動くはず」で納品しないための道具であり、隠すと使えない)。
  $effect(() => {
    ;(window as unknown as Record<string, unknown>).__duocad = {
      ready: status === 'open' && model !== null,
      status,
      state: model,
      // ユーザーに見せる部品一覧(常に kind==="solid" だけ)
      parts: partBodies,
      // 直近にviewportへ渡した生geometryを問い合わせる(正はviewportの描画に一本化してある)。
      getGeometry: (bodyId: string) => viewport?.getGeometry(bodyId) ?? null,
      // **選択の正はコア**。ここは共有集合の写しであって、ブラウザ側の状態ではない。
      selection: model?.selection ?? null,
      // 互換のための穴: 選択のうち最初の面を `{bodyId, faceIndex}` で出す
      selected: firstFace === null ? null : { bodyId: firstFace.body, faceIndex: firstFace.index },
      previewing,
      lastApplied,
      messages,
      // **エラーはトーストへ出る**(消える)ので、実機検査が
      // 「走行中にエラーが出ていないか」を最後に一度で見られるよう控えを渡す。
      // 今まさに画面に出ている札は `#toasts` のDOMが正 —— 文言の検証はそちらを見ること。
      errors: errorLog,
      projectFace: (bodyId: string, faceIndex: number) => viewport?.project(bodyId, faceIndex) ?? null,
      // 視点をシーン全体へ戻す。**2026-08-23に画面のボタンを削除**した後も、実機検査が
      // 図形を画面内へ収めるために呼ぶ手だけはここに残す。
      frameAll: () => viewport?.frameAll(),
      // 画角。**実機検査が「FOVを変えてから frameAll して収まるか」を見るための穴**
      // (スライダーを本物で動かすのとは別に、値を直接置ける口が要る)。
      // 文書の状態ではないので、ここを叩いてもコアへは何も飛ばない。
      fov,
      setFov: (deg: number) => applyFov(deg),
      // **視点の向きそのものを見るための穴**(2026-08-28のZ-up化で足した)。カメラの姿勢と
      // 「この世界座標が画面のどこに写るか」を返す。`projectFace` では測れない ——
      // あちらは遮蔽された面に null を返すので、**上面と下面のどちらが画面で上か**のように
      // 見えていない側を含む比較ができない。どちらも読むだけで、コアへは何も飛ばない。
      cameraPose: () => viewport?.cameraPose() ?? null,
      // ビューキューブ(常設・26領域)の画面矩形。**実機検査がキューブの当たり判定とドラッグを
      // 本物の座標で叩くための穴**(タスク仕様の落とし穴3。右上アンカーなので寸法が変わると
      // 動く —— 決め打ちの座標を使わずここから読む)。
      viewCubeRect: () => viewport?.viewCubeRect() ?? null,
      // 基準平面の**ピック範囲**の今の半幅[mm]。**実機検査が「指せる範囲がモデルの
      // 大きさへ広がった/縮んだ」を数値で見るための穴**(2026-08-30。`viewport.gridScale`)。
      gridScale: () => viewport?.gridScale() ?? null,
      // グリッドの3段(1/10/100mm)の今の不透明度と、出している平面・面外の軸。
      // **実機検査が「段が連続にフェードする」「状態で平面が入れ替わる」を数値で見るための穴**
      // (2026-09-03。刻みが固定になり `gridScale().step` が何も語らなくなった置き換え)。
      gridTiers: () => viewport?.gridTiers() ?? null,
      // 可変解像度。**実機検査が「寄ったら本当に細かくなったか」を数値で見るための穴**
      // (見た目の滑らかさは目でしか分からないので、代わりに目標偏差と分割数を出す)。
      viewResolution: () => viewport?.viewResolution() ?? null,
      // 状態配信を受けた回数。**測り直しの往復が終わったかを外から見る唯一の手段**。
      stateEpoch,
      // face単位の高精度化(視錐台+バックフェイスカリング)を切る/入れる穴。
      // **導入前後を同じ走行の中で比べる**ために要る(`smoke-face-culling.mjs`)。
      setFaceCulling: (enabled: boolean) => viewport?.setFaceCulling(enabled),
      sketchPolylineCounts: () => viewport?.sketchPolylineCounts() ?? null,
      projectPoint: (point: [number, number, number]) => viewport?.projectPoint(point) ?? null,
      // F: 形状最適化(第3のモード)。**実機検査がモードの出入りと`wantedKinds`(ボディ粒度)
      // を見るための穴** —— 配りものそのものは `state.optimize` にある。
      optimizeMode,
      // **今のモード**(`'idle' | 'sketch-pending' | 'sketch' | 'optimize'`。2026-09-03)。
      // `optimizeMode`(真偽)と `activeSketch`(ID or null)だけでは**貼る先待ち**が
      // 「通常モード」と同じ見え方になり、モードの遷移を外から検算できない。
      mode: mode.kind,
      wantedKinds,
      faceColor: (bodyId: string, faceIndex: number) => viewport?.faceColor({ bodyId, faceIndex }) ?? null,
      optimizeAnnotationCount: () => viewport?.optimizeAnnotationCount() ?? 0,
      optimizeAnnotations: () => viewport?.optimizeAnnotations() ?? { fixtures: [], loads: [] },
      // C: スケッチ。**実機検査が本物のクリックで描く**ための穴
      sketches,
      activeSketch: sketchId,
      hoverPoint,
      // **第2の呼び方**: スケッチを指定すると通常モードでも射影できる(領域の
      // `projectRegion` と同じ形)。**同じ世界座標の点を、画面の寸法が変わった後でも
      // もう一度指せる**ようにするための穴で、`smoke-regions` の「同じ場所が、要求中は
      // 領域・外すと面になる」がこれを使う(コマンドを移るとツールバーの高さが変わり、
      // 画素そのものは持ち越せない)。
      projectSketchPoint: (u: number, v: number, targetSketchId?: string) => {
        const target = targetSketchId ?? sketchId
        return target === null ? null : (viewport?.projectSketchPoint(target, u, v) ?? null)
      },
      // C: 基準平面。**実機検査が本物のクリックで貼る先を指す**ための穴
      // (指せる状態=スケッチのコマンドを起動している間しか座標を返さない)。
      wantsBasePlanes,
      projectBasePlane: (name: BasePlaneName) => viewport?.projectBasePlane(name) ?? null,
      // 領域。**第2引数を渡せば通常モードでも射影できる**(スケッチモードの外では
      // 「今のスケッチ」が無いので、どのスケッチの領域かを検査側が言う必要がある)。
      // 指せる状態でなければ null が返る(`viewport.projectRegion` が実際に当てて確かめる)。
      wantsRegions,
      projectRegion: (index: number, targetSketchId?: string) => {
        const target = targetSketchId ?? sketchId
        return target === null ? null : (viewport?.projectRegion(target, index) ?? null)
      },
      // D: 辺とフィーチャ。**実機検査が本物のクリックで辺を選ぶ**ための穴
      edges: edgeTargetBody === null ? null : (edges[edgeTargetBody] ?? null),
      edgeTargetBody,
      wantsEdges,
      selectedEntities,
      projectEdge: (bodyId: string, edgeIndex: number) =>
        viewport?.projectEdge(bodyId, edgeIndex) ?? null,
      // 頂点。**実機検査が本物のクリックで頂点を外す**ための穴(出ていなければ null)。
      vertexMarkers,
      // **提案(水色)と確定(橙)の区別は3Dの色だけ**になったので、色そのものを読める穴を出す。
      highlightColors: () => viewport?.highlightColors() ?? null,
      // ホバー強調で今明るくしているもの。**「クリックすれば取れるもの」と実際に
      // クリックした結果が一致していること**を実機検査が確かめるための穴
      // (明度しか変えないので highlightColors では読めない)。
      hoveredRef: () => viewport?.hoveredRef() ?? null,
      projectVertex: (bodyId: string, vertexIndex: number) =>
        viewport?.projectVertex(bodyId, vertexIndex) ?? null,
      // E: パラメータ・履歴・ファイル。実機検査が「打ち直したら形が追随する」を見る穴
      parameters: model?.parameters ?? {},
      redoCount: model?.redo_count ?? 0,
      lastFile,
      documentList,
      currentDocumentName,
      // **`regionsStillExist` を合成データで直接叩く穴**(2026-08-27)。通常のUI操作では
      // `resets_selection` が先に選択を空にするため、この関数がfalseを返す分岐へ
      // クリックの積み重ねでは到達できない(理由は関数自身のdocstring参照)。
      // ロジックそのものの正しさは実機検査(`smoke-regions.mjs`)がここ経由で検算する。
      regionsStillExist: (sel: SelectionState | null, sks: SketchState[]) =>
        regionsStillExist(sel, sks),
    }
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
    <span id="current-file" class="dim">{currentDocumentName ?? '(無題)'}</span>
    <!-- **今の文書がファイルに載っているか**(`savedAt` 参照)。**目立たせるのは
         「未保存」の側** —— 人の手が要るのはこちらだけで、保存済みは黙っていてよい。 -->
    <span id="save-state" class="tag" class:on={!saved}>{saved ? '保存済み' : '未保存'}</span>
    <!-- **保存は1箇所だけ**(2026-08-30本人決定)。押すと**編集中のファイルへ上書き**する。
         一覧の行にあった「上書き」ボタンは撤去した —— A を編集中に B の行を押せば B が
         今の文書で潰れ、操作ログが唯一の正である以上それは復旧できなかった。
         **`.toolbar` には置かない** —— あちらはverbの行で、保存はverbではない
         (操作ログに載らず、undoの対象でもない)。ここが `#current-file` /
         `#save-state` と同じ行なのは、指しているのが同じもの(編集中のファイル)だから。
         **編集中のファイルが無くても `disabled` にしない** —— 理由は
         `saveCurrentDocument` のdocstring(押せない札より、名前欄へ連れて行く)。 -->
    <button id="file-save" class="tag" onclick={saveCurrentDocument} title="編集中のファイルへ上書き保存(Ctrl+S)">
      保存
    </button>
    <!-- **常設の指標はこれで全部**(2026-08-27に本人裁定で4つ外した)。外したものと行き先:
         - **接続状態** → 常時光らせず、**操作したのに送れなかった瞬間**にトーストで言う
           (`session.ts` の `send`)。見ても何もしなくてよい間ずっと視線を取るのをやめた。
           繋がっていたのが切れたときの通知(`onStatus`)と、再接続の連続失敗の通知は残る。
         - **部品数 / 操作数** → 数そのものは人の判断に使わない。何があるかはブラウザ
           (`BrowserPanel.svelte`)を見れば分かり、操作数は保存したファイルの一覧に出る。
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
         元からこの位置にいた([[duocad-idle-default-state]] の「画面に何かを足して
         既定に据えるのではない」に触れない)。 -->
    {#if mode.kind === 'idle'}
      <div class="mode-row">
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
          編集中 {activeSketch.plane.kind === 'base' ? activeSketch.plane.name : '面に貼ったスケッチ'}
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
        <button id="optimize-exit" onclick={exitOptimizeMode}>抜ける</button>
      </div>
    {/if}
    <!-- **verbの行。** モードが変わったらコマンド層ごと作り直す(集めかけの引数を持ち越さない)。
         鍵はモードのタグそのもの —— スケッチAからBへ移るときは作り直さない(今までどおり)。 -->
    {#key mode.kind}
      <CommandHost
        bind:this={host}
        {selection}
        busy={pendingOp !== null}
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
        onWants={(kinds) => (wantedKinds = kinds)}
        onWantsPlane={(wants) => (wantedPlane = wants)}
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
          documents={documentList}
          currentName={currentDocumentName}
          onSave={saveDocumentAs}
          onOpen={openDocumentByName}
          onNew={newDocument}
          onImportStep={importStep}
          onExportStep={exportStep}
          onExportStl={exportStl}
        />
      </aside>
    {/if}

    <!-- ビューポートと、その上に浮かぶブラウザの入れ物。**`.stage` が浮かせる側の位置の基準**で、
         大きさを決めているのは `.stage` の中の `.viewport` 1つだけ(ブラウザは position:absolute で
         フローの外にいるので、開閉してもカメラのアスペクトに触らない)。 -->
    <div class="stage">
      <!-- canvas はこの div の中で three.js が作る。Svelte はここの中身を触らない -->
      <div class="viewport" bind:this={container}></div>

      <BrowserPanel
        bodies={partBodies}
        {sketches}
        activeSketchId={sketchId}
        onToggle={setVisible}
        onShowAll={showAllParts}
        onHideAll={hideAllParts}
        onEnterSketch={enterSketch}
      />

      <!-- 設定ダイアログ。**`.stage` の中に置くのは「ブラウザの右」に出すため**(2026-09-04
           本人指示) —— 位置の基準が `.stage` になるので、左右のサイドバーを開閉しても
           ブラウザとの隣接が保たれる。以前は画面右上(`position: fixed`)に出しており、
           **AIサイドバーを閉じるとビューキューブの70%を覆っていた**(同日実測。キューブは
           ビューポートの右上アンカーなので、ビューポートが右へ伸びるとダイアログの下へ入る)。
           **大きさは持たない**(`position: absolute` でフローの外)ので、ブラウザや Toast と
           同じく `.viewport` の寸法＝カメラのアスペクトには触らない。 -->
      <SettingsDialog
        open={settingsOpen}
        {fov}
        angle={fovAngle}
        onFov={applyFov}
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
          status={aiStatus}
          transcript={aiTranscript}
          onSend={sendAiMessage}
          onCancel={() => session.aiCancel()}
          onConfigure={(providerKind, modelName, baseUrl) =>
            session.aiConfigure(providerKind, modelName, baseUrl)}
        />
      </aside>
    {/if}

    <!-- エラーの札。**`.main` の中に置くが大きさは持たない**(position:absolute で浮く)ので、
         ビューポートの寸法＝カメラのアスペクトには触らない。出す側は `fail()` の1本だけ。 -->
    <Toast />
  </div>
</div>
