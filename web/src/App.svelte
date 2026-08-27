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
    COMMANDS,
    faceRef,
    sameRef,
    type CommandCategory,
    type Point2,
    type SelectableKind,
  } from './commands/command'
  import {
    SKETCH_FEATURE_COMMANDS,
    SOLID_FEATURE_COMMANDS,
    needsSketchId,
  } from './commands/features'
  import { FILE_COMMANDS, changesDocument } from './commands/files'
  import {
    DEFAULT_SKETCH_COMMAND_ID,
    SKETCH_COMMANDS,
    SKETCH_CREATE,
    ghostEntity,
    runsOutsideSketch,
  } from './commands/sketch'
  import FilesPanel from './FilesPanel.svelte'
  import BrowserPanel from './BrowserPanel.svelte'
  import SketchPanel from './SketchPanel.svelte'
  import Toast, { showToast } from './Toast.svelte'
  import {
    createSession,
    type AiStatus,
    type AiTranscriptEntry,
    type Applied,
    type BasePlaneName,
    type BodyGeometry,
    type ConnectionStatus,
    type DocumentInfo,
    type EdgesPayload,
    type FilePayload,
    type PreviewPayload,
    type SelectionRef,
    type SelectionState,
    type ServerState,
    type ShapeRef,
    type SketchState,
    type VerticesPayload,
  } from './session'
  import AiPanel from './AiPanel.svelte'
  import {
    createViewport,
    type PickEvent,
    type SketchDraw,
    type VertexMarker,
    type Viewport,
  } from './viewport'

  /**
   * ツールバーの中身(モードごと)。**状態機械は1つのまま**で、変わるのは一覧と既定だけ。
   * ソリッド側の一覧には3つ、スケッチ側には反復の2つが載る ——
   * 領域押し出しは**両方に居る**(描いた直後に押し出すのと、モードを抜けてから
   * 領域を選んで押し出すのは、どちらも実際に使う)。
   */
  const SOLID_COMMANDS = [
    ...COMMANDS,
    ...SOLID_FEATURE_COMMANDS,
    // **`sketch_create` はスケッチ分類だがここに載る** —— `sketch_id` を取らず、
    // スケッチモードの外で走る唯一のverbだからである(`commands/sketch.ts` のdocstring)。
    SKETCH_CREATE,
    ...FILE_COMMANDS,
  ]
  const SKETCH_MODE_COMMANDS = [...SKETCH_COMMANDS, ...SKETCH_FEATURE_COMMANDS]

  /**
   * verb名 -> そのコマンドの分類。**送信経路の分岐はこれだけで決まる。**
   *
   * 知らない名前は `undefined` になり、`runCommand` がその場で断る。
   */
  const CATEGORY_OF: Map<string, CommandCategory> = new Map(
    [...SOLID_COMMANDS, ...SKETCH_MODE_COMMANDS].map((command) => [command.id, command.category]),
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

  // ---------------------------------------------------------------- C: スケッチモード
  /** 今どのスケッチを編集しているか(null なら通常モード)。**図形も領域もコアが持つ**。 */
  let sketchId = $state<string | null>(null)
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
  } | null>(null)

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
  /** スケッチパネルで選んだ図形(反復のverbの引数。共有選択集合には入らない)。 */
  let selectedEntities = $state<string[]>([])

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
    // 反復に使った図形は反復に吸収され、undoで戻ればまた現れる。**消えたIDを
    // 持ち続けない** —— 存在しない図形を引数にした反復は、押した瞬間にサーバで
    // KeyError になるだけで、なぜ失敗したかが画面から分からない。
    const known = new Set(
      sketches.flatMap((item) => item.entities.map((entity) => entity.entity_id)),
    )
    if (selectedEntities.some((id) => !known.has(id))) {
      selectedEntities = selectedEntities.filter((id) => known.has(id))
    }

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
      // 「(無題)」へ戻る。
      currentDocumentName = applied.name
      // **開いた直後の文書はファイルの中身そのもの**なので保存済みから始める。新規は
      // どこにも書かれていないので `null`(＝常に未保存)へ戻す。`next` は上で `model` に
      // 入れた今回の状態で、開いた版の `applied_count` / 操作数がもう入っている。
      savedAt = applied.name === null ? null : [next.applied_count, next.log.length]
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
        const count = applied.body_ids?.length ?? 0
        note(`STEPを読み込んだ(${count}部品)。そのまま面をクリックして押し出せる`)
      }
      if (applied.client_op !== null && applied.client_op === pendingOp) pendingOp = null
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
    if (payload.name) currentDocumentName = payload.name
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
   * ファイル名を渡して保存する(新規保存も一覧からの上書きも同じverb)。
   *
   * **`pendingOp` は立てない** —— `SketchPanel.svelte` の `onSetParameter`(打ち直し)と同じ
   * 理由で、`FilesPanel.svelte` は `CommandHost` の応答待ちの外にいる。
   */
  function saveDocumentAs(name: string): void {
    const op = nextOp()
    lastFileOp = op
    session.runVerb('save_document', { name }, op)
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

  // ---------------------------------------------------------------- 選択(コアを叩くだけ)
  const selection = $derived(model?.selection ?? null)
  const selectedFaces = $derived(
    (selection?.items ?? []).filter(
      (item): item is ShapeRef => !('sketch' in item) && (item as ShapeRef).kind === 'face',
    ),
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
    // **見えているボディだけを対象にする**。AIが述語で
    // 提案した集合は参照ボディ(既定で隠れている)にまたがりうる。そこを対象にすると
    // サーバが「隠れているものの辺は指せない」と断り、画面にはエラーだけが出て
    // **見えている辺も出なくなる**。選択のうち見えているものを先に見る。
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
   * Shift=追加 / Ctrl=除去 / 修飾なし=置き換え(`'replace'`)。置き換えは
   * **1つだけ選んでいるものを再クリックしたら外す**(狙いを外したときに素で解除できる)。
   *
   * 置き換えのとき**自分自身は remove に入れない** —— コアは remove を後に適用するので、
   * 入れると足したそばから外れる(`duocad/selection.py` の契約)。
   */
  function editSelection(ref: SelectionRef, mode: PickEvent['mode']): void {
    const current = selection?.items ?? []
    const already = current.some((item) => sameRef(item, ref))
    if (mode === 'add') modifySelection([ref], [])
    else if (mode === 'remove') modifySelection([], [ref])
    else if (already && current.length === 1) modifySelection([], [ref])
    else selectOnly(ref)
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
    editSelection(faceRef(event.hit.bodyId, event.hit.faceIndex), event.mode)
  }

  // ---------------------------------------------------------------- C: スケッチ
  const sketches = $derived(model?.sketches ?? [])

  /** 描画指示を組み立てる(選択中の領域だけ強調色になる)。 */
  function sketchDraws(): SketchDraw[] {
    const selectedRegions = (selection?.items ?? [])
      .filter((item): item is { sketch: string; index: number } => 'sketch' in item)
    return sketches
      .filter((item) => item.frame !== null)
      .map((item) => ({
        sketchId: item.sketch_id,
        frame: item.frame as NonNullable<typeof item.frame>,
        entities: item.drawn,
        regions: item.regions,
        selectedRegions: selectedRegions
          .filter((ref) => ref.sketch === item.sketch_id)
          .map((ref) => ref.index),
      }))
  }

  function drawSketches(): void {
    viewport?.showSketches(sketchDraws())
  }

  function enterSketch(id: string): void {
    sketchId = id
    hoverPoint = null
    viewport?.setActiveSketch(id)
    drawSketches()
    viewport?.lookAtSketch(id)
  }

  function exitSketch(): void {
    sketchId = null
    hoverPoint = null
    selectedEntities = [] // 反復の対象はモードを抜けたら持ち越さない
    viewport?.setActiveSketch(null)
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
    if (event.region) {
      editSelection({ sketch: event.region.sketchId, index: event.region.index }, event.mode)
      return
    }
    if (event.mode === 'replace' && (selection?.items.length ?? 0) > 0) {
      session.clearSelection(nextOp())
    }
  }

  function setParameter(name: string, value: number): void {
    session.runVerb('set_parameter', { name, value }, nextOp())
  }

  /**
   * 反復の対象にする図形を選ぶ/外す。**共有選択集合には入れない** ——
   * コアの選択集合が持てるのは面・辺・頂点・領域の4種で、図形はそこに入らない
   * (MCP側も反復だけは図形IDを引数で受けている)。ここがその人間側の対。
   */
  function toggleEntity(entityId: string): void {
    selectedEntities = selectedEntities.includes(entityId)
      ? selectedEntities.filter((item) => item !== entityId)
      : [...selectedEntities, entityId]
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
        session.runVerb(verb, { ...args, sketch_id: sketchId }, op)
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
      // -------------------------------------------------------------- E: ファイル操作
      case 'file': {
        const op = nextOp()
        lastFileOp = op
        // **文書を変えないもの(保存・書き出し)は状態配信では返ってこない**ので、
        // 「応答待ち」は立てない —— 立てると `handleFile` が来るまでEnterが拒否される。
        if (changesDocument(verb)) pendingOp = op
        session.runVerb(verb, args, op)
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
   * 実際に消えている)は、`duocad/catalog.py` の `resets_selection=True`(13verb全て。
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
      (item): item is { sketch: string; index: number } => 'sketch' in item,
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
    if (key !== 'z' && key !== 'y') return
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
  // `model.bodies` には**参照ボディ(`kind="reference"`)も混ざっている**ので、
  // パーツ一覧・一括操作とも `kind==="solid"` だけを扱う(`duocad/server.py` の
  // `load_step` docstring参照)。ここを `bodies` 全部にすると「全表示」が参照ボディまで
  // 可視にしてしまい、1ソリッドにつき2つの同じ形が重なって描かれる。
  const partBodies = $derived((model?.bodies ?? []).filter((body) => body.kind === 'solid'))

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
    viewport.onPick(onPick)
    viewport.onPlaneMove((point) => {
      hoverPoint = point === null ? null : [point.u, point.v]
    })
    session.start()
    return () => {
      session.dispose()
      viewport?.dispose()
      viewport = null
    }
  })

  // 選択が変わったら描き直す(コア発 → 購読 → 再描画。上り方向の出口)
  $effect(() => {
    void selection
    drawHighlight()
    // **スケッチはここで描き直さない**。選択もスケッチも `handleState` 経由でしか変わらず、
    // そこで既に描き直している。ここにも置くと、1回の状態受信で全スケッチのジオメトリを
    // 2回作り直すことになる(面のハイライトと違って作り直しが重い)。
  })

  /** 選択の先頭の面。**テスト用フックの `selected` だけが使う**(互換のための穴)。 */
  const firstFace = $derived(selectedFaces[0] ?? null)

  // 自動テスト(scripts/smoke.mjs)がブラウザの中を見るための穴。ローカル専用ツールなので
  // 常に生やしてある(「動くはず」で納品しないための道具であり、隠すと使えない)。
  $effect(() => {
    ;(window as unknown as Record<string, unknown>).__duocad = {
      ready: status === 'open' && model !== null,
      status,
      state: model,
      // ユーザーに見せる27部品(参照ボディを除いた kind==="solid" だけ)
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
    <!-- **常設の指標はこれで全部**(2026-08-27に本人裁定で4つ外した)。外したものと行き先:
         - **接続状態** → 常時光らせず、**操作したのに送れなかった瞬間**にトーストで言う
           (`session.ts` の `send`)。見ても何もしなくてよい間ずっと視線を取るのをやめた。
           繋がっていたのが切れたときの通知(`onStatus`)と、再接続の連続失敗の通知は残る。
         - **部品数 / 操作数** → 数そのものは人の判断に使わない。何があるかはブラウザ
           (`BrowserPanel.svelte`)を見れば分かり、操作数は保存したファイルの一覧に出る。
         - **プレビュー中の印** → **印が要らない見え方を目指す**方針(本人)。幽霊が
           出ていることは幽霊自身が示すべきで、札で補うのは見え方が弱い証拠になる。
           状態(`previewing`)は実機検査が見るので残してある。 -->
  </header>

  <!-- コマンドのツールバー。モードが変わったらコマンド層を作り直す(集めかけの引数を持ち越さない)。
       `FilesPanel` はここには載らない —— 2026-08-23にいったんここへ統合したが、
       2026-08-26に本人が左サイドバーへの復元を決定した(下の `.main` 直下の `.data-panel` 参照)。 -->
  <section class="toolbar">
    {#key sketchId === null}
      <CommandHost
        bind:this={host}
        {selection}
        busy={pendingOp !== null}
        {selfSelections}
        commands={sketchId === null ? SOLID_COMMANDS : SKETCH_MODE_COMMANDS}
        defaultCommandId={sketchId === null ? undefined : DEFAULT_SKETCH_COMMAND_ID}
        hover={hoverPoint}
        entities={selectedEntities}
        onRun={runCommand}
        onPreview={requestPreview}
        onCancelPreview={clearPreview}
        onNote={note}
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
    </div>

    <!-- AI・パラメータ・スケッチをまとめた右サイドバー。
         .sidebar は overflow-y:auto で自分の中だけスクロールするので、
         中のパネルが増減しても .viewport のサイズは変わらない(射影が動かない)。 -->
    <aside class="sidebar">
      <AiPanel
        status={aiStatus}
        transcript={aiTranscript}
        onSend={sendAiMessage}
        onCancel={() => session.aiCancel()}
        onConfigure={(providerKind, modelName, baseUrl) =>
          session.aiConfigure(providerKind, modelName, baseUrl)}
      />

      <SketchPanel
        {sketches}
        activeSketchId={sketchId}
        onEnter={enterSketch}
        onExit={exitSketch}
        onLookAt={() => sketchId && viewport?.lookAtSketch(sketchId)}
        onSetParameter={setParameter}
        selectedEntities={selectedEntities}
        onToggleEntity={toggleEntity}
      />
    </aside>

    <!-- エラーの札。**`.main` の中に置くが大きさは持たない**(position:absolute で浮く)ので、
         ビューポートの寸法＝カメラのアスペクトには触らない。出す側は `fail()` の1本だけ。 -->
    <Toast />
  </div>
</div>
