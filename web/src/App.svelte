<script lang="ts">
  /**
   * アプリ層の組み立て — 接続・ビューポート・パネルを繋ぐだけの場所。
   *
   * **B(2026-08-13)で「面を選ぶ → 距離を打つ → Enter」の状態機械はここから出た。**
   * 引数を集める仕組みは `commands/CommandHost.svelte` に1つだけあり、verbはそこへ
   * 載せるだけになる(C・D・Eがコマンドを足しても、ここは変わらない)。
   *
   * **選択の正はコアにある(M1由来の債務を返した)。** M1はブラウザ側の `selected` が
   * 正で、実装計画書§3.5が定める「選択集合もコア側(永続・双方向)」を満たしていなかった。
   * A2でコアに入り、Bでアプリ層がそれに乗り換えた —— ここは `state.selection` を
   * 描き直すだけで、**選択の値を1つも覚えていない**。可視性(M3.5)と同じ形である。
   *
   * ここに残っている責務は3つだけ:
   *
   * 1. **ピック → コアの選択への翻訳**(修飾キーで追加・除去・置き換えを決める)
   * 2. **プレビューの往復**(コマンド層が「何を試すか」を決め、ここが投げて幽霊を描く)
   * 3. **押し出した面の後継を選び直す**(「同じ面を10回押す」を再ピックなしで回す。M1から)
   */
  import { onMount } from 'svelte'
  import CommandHost from './commands/CommandHost.svelte'
  import {
    COMMANDS,
    faceRef,
    sameRef,
    type Point2,
    type SelectableKind,
  } from './commands/command'
  import {
    SKETCH_FEATURE_COMMANDS,
    SOLID_FEATURE_COMMANDS,
    isFeatureVerb,
    needsSketchId,
  } from './commands/features'
  import { FILE_COMMANDS, changesDocument, isFileVerb } from './commands/files'
  import HistoryPanel from './HistoryPanel.svelte'
  import ParametersPanel from './ParametersPanel.svelte'
  import {
    DEFAULT_SKETCH_COMMAND_ID,
    SKETCH_COMMANDS,
    ghostEntity,
    isSketchVerb,
  } from './commands/sketch'
  import PartsPanel from './PartsPanel.svelte'
  import SelectionPanel from './SelectionPanel.svelte'
  import SketchPanel from './SketchPanel.svelte'
  import {
    createSession,
    type Applied,
    type BasePlaneName,
    type BodyGeometry,
    type ConnectionStatus,
    type EdgesPayload,
    type FilePayload,
    type PreviewPayload,
    type SelectionRef,
    type ServerState,
    type ShapeRef,
  } from './session'
  import {
    createViewport,
    type GeometryPayload,
    type PickEvent,
    type SketchDraw,
    type Viewport,
  } from './viewport'

  /**
   * ツールバーの中身(モードごと)。**状態機械は1つのまま**で、変わるのは一覧と既定だけ
   * (B・Cからの約束)。Dが足した3つはソリッド側、反復2つはスケッチ側に載る ——
   * 領域押し出しは**両方に居る**(描いた直後に押し出すのと、モードを抜けてから
   * 領域を選んで押し出すのは、どちらも実際に使う)。
   */
  const SOLID_COMMANDS = [...COMMANDS, ...SOLID_FEATURE_COMMANDS, ...FILE_COMMANDS]
  const SKETCH_MODE_COMMANDS = [...SKETCH_COMMANDS, ...SKETCH_FEATURE_COMMANDS]

  let container: HTMLDivElement
  let viewport: Viewport | null = null

  let status = $state<ConnectionStatus>('connecting')
  let model = $state<ServerState | null>(null)
  let messages = $state<string[]>([])
  let lastApplied = $state<Applied | null>(null)
  // **テスト用フック `__duocad.bodyGeometry` のためだけの写し**(body_id -> 直近のgeometry)。
  // ドメインの状態ではない(可視性・選択の正はコア) — viewport が three.jsの中に既に
  // 持っている形状を、実機検査から読める形で複製しているだけ。
  // 2026-08-15まではM1の「面」の行(面積・法線の表示)も読んでいたが、その行は
  // 選択パネルと重複していたので外した。**画面はもうこれを読まない。**
  let bodyGeometry = $state<Record<string, GeometryPayload>>({})

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
    restoreLastInputs(): void
  } | null>(null)

  // ---------------------------------------------------------------- D: 辺とフィーチャ
  /** 起動中のコマンドが選択から取る種別(`CommandHost` が知らせる)。 */
  let wantedKinds = $state<SelectableKind[]>([])
  /** 取り寄せた辺(body_id -> その版の折れ線)。**版が変われば捨てる**。 */
  let edges = $state<Record<string, EdgesPayload>>({})
  /** 取り寄せ中のもの(`${body_id}:${revision}`)。同じ要求を何度も投げないため。 */
  let edgesAsked = new Set<string>()
  /** スケッチパネルで選んだ図形(反復のverbの引数。共有選択集合には入らない)。 */
  let selectedEntities = $state<string[]>([])

  // ---------------------------------------------------------------- E: ファイルと履歴
  /** 直前に書いたファイル(画面の右上に出す。書き出しは他に痕跡が残らない)。 */
  let lastFile = $state<FilePayload | null>(null)
  /** 直前に出したファイル操作の `client_op`(応答が `file` で返ったときの待ちを解く)。 */
  let lastFileOp: number | null = null
  /**
   * **この画面が出した操作の seq**(操作履歴パネルの「自分」の印)。
   *
   * 操作ログに主体は入っていない(§3.6)ので、区別できるのはここまで ——
   * 再読込すると消えるし、AIが出した操作は「印が無い」だけで区別されたわけではない
   * (`HistoryPanel.svelte` のdocstringに理由を書いてある)。
   */
  let myOperations = $state<number[]>([])
  /**
   * この画面が出した `client_op` の控え(QA指摘 2026-08-14)。
   *
   * **`pendingOp` では足りない** —— パラメータの打ち直しや可視性の切り替えは
   * 「応答待ち」を立てない経路なので、`pendingOp` と突き合わせると
   * **自分でやったのに印が付かない**(パラメータ編集はEの中心操作そのもの)。
   * 出した番号を全部覚えておいて、返ってきた `applied.client_op` で照合する。
   */
  const myClientOps = new Set<number>()

  const session = createSession({
    onState: handleState,
    onError: (message, where) => {
      note(`エラー[${where}] ${message}`)
      pendingOp = null
      if (where === 'preview') clearPreview()
      // 辺の取り寄せが失敗したら「頼んだ」印も消す(次に起動したときに取り直せる)
      if (where === 'get_edges' || where === 'send') edgesAsked.clear()
      // **失敗しても、やり直せる状態にして返す**(D)。OCCTのフィレットは半径に対して
      // 単調でなく(§7: r=10.0 失敗 / r=10.5 成功)、すぐ近くの値で打ち直すのが実際の
      // 使い方になる。選択はコア側にあるので失敗では消えない —— ここで戻すのは
      // 打った値だけでよく、24本選び直しにはならない。
      if (isFeatureVerb(where)) host?.restoreLastInputs()
    },
    onWarning: (message) => note(`警告 ${message}`),
    onStatus: (next) => {
      status = next
      // 応答待ちの最中に接続が切れたら、その `client_op` はもう返ってこない。
      // 捨てないと再接続後も「直前の操作の応答待ち」が残り続け、Enterが永久に拒否される
      // (`onerror` を伴わない正常クローズで踏む。QA 2026-08-13)。
      if (next !== 'open' && pendingOp !== null) {
        pendingOp = null
        clearPreview()
      }
      // **投げ損ねた辺の要求を覚えたままにしない**(D。QA指摘 2026-08-14)。接続が切れて
      // いる間の要求は送られずに捨てられるが、`edgesAsked` には残る —— 再接続しても
      // 版が同じなら「もう頼んだ」と判断して二度と取りに行かず、**そのBodyの辺が
      // 出ないまま**フィレットが使えなくなる(`pendingOp` を捨てるのと同じ形の穴)。
      if (next !== 'open') edgesAsked.clear()
    },
    onPreview: handlePreview,
    onEdges: handleEdges,
    onFile: handleFile,
  })

  function note(message: string): void {
    if (!message) return
    messages = [message, ...messages].slice(0, 4)
  }
  /** パネルへ渡す通知(プロパティ名と関数名を揃えるためだけの別名)。 */
  const onNote = note

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

    if (changed.length > 0) {
      const nextGeometry = { ...bodyGeometry }
      for (const body of changed) {
        if (body.geometry) nextGeometry[body.body_id] = body.geometry
        else delete nextGeometry[body.body_id]
      }
      bodyGeometry = nextGeometry
    }
    // このキャッシュも、消えたBodyのぶんは掃除しておく(viewport.prune と同じ理由)。
    const validIds = new Set(next.bodies.map((body) => body.body_id))
    for (const bodyId of Object.keys(bodyGeometry)) {
      if (!validIds.has(bodyId)) {
        const pruned = { ...bodyGeometry }
        delete pruned[bodyId]
        bodyGeometry = pruned
      }
    }

    drawHighlight()
    // スケッチも「コアが持ち、ここは描き直すだけ」(可視性・選択と同じ形)。
    // **$effect ではなくここで同期に描く** —— 直後の `lookAtSketch` が平面の枠を要るので、
    // 描画が1手遅れると「入った瞬間に正面を向く」が効かない。
    drawSketches()
    if (sketchId !== null && !sketches.some((item) => item.sketch_id === sketchId)) {
      exitSketch() // undoでスケッチごと消えた
    }
    dropStaleRegions()
    // 反復に使った図形は反復に吸収され、undoで戻ればまた現れる(D)。**消えたIDを
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
      if (applied.client_op !== null && applied.client_op === pendingOp) pendingOp = null
      return
    }
    if (applied) {
      // 作ったスケッチへはそのまま入る(作ってから改めて「入る」を押させない)
      if (applied.verb === 'sketch_create' && applied.sketch_id) enterSketch(applied.sketch_id)
      // **読み込んだら何ができる状態になったか**を言う(2026-08-15)。以前ここは
      // 「読み込んだのは参照ボディ(編集不可)。編集するには…」と出していたが、
      // 読み込みが編集可能なボディまで作るようになった今それは嘘になる。
      // 参照だけを足す経路(`import_step`)はコアに残っているので、両方に文言を用意する。
      if (applied.verb === 'import_step_editable') {
        const count = applied.body_ids?.length ?? 0
        note(`STEPを読み込んだ(${count}部品)。そのまま面をクリックして押し出せる`)
      }
      if (applied.verb === 'import_step') {
        note('読み込んだのは参照ボディ(編集不可)。編集するには面を選んで「編集用に複製」')
      }
      dropStaleEdges(applied)
      reselectAfter(applied)
      if (applied.client_op !== null && applied.client_op === pendingOp) pendingOp = null
      markOwnership(applied)
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

  /**
   * 「自分が出した操作」の印を更新する(E。QA指摘 2026-08-14で作り直した)。
   *
   * **seqは使い回される**。undoで捨てた操作の番号は次に積まれた操作が再び使うので
   * (`OpLog.append` はカーソル以降を捨ててから振る)、控えを消さずに溜めると
   * **AIが積んだ操作に「自分」が付く**(取りこぼしより悪い)。新しい操作の seq を
   * 受けたら、その番号以上の控えを先に落とす。文書ごと差し替わったら全部捨てる。
   */
  function markOwnership(applied: Applied): void {
    if (applied.change === 'document') {
      myOperations = []
      return
    }
    if (typeof applied.seq !== 'number') return
    if (applied.verb === 'undo' || applied.verb === 'redo') return // 積み直しではない
    const seq = applied.seq
    const kept = myOperations.filter((item) => item < seq)
    const mine = applied.client_op !== null && myClientOps.has(applied.client_op as number)
    myOperations = mine ? [...kept, seq] : kept
  }

  function handleEdges(payload: EdgesPayload): void {
    edges = { ...edges, [payload.body_id]: payload }
  }

  /**
   * ファイルを書いた(E)。**どこへ何バイト書いたかを必ず画面へ出す** ——
   * 書き出しはブラウザの中に痕跡が残らない操作なので、通知が唯一の確認手段になる。
   */
  function handleFile(payload: FilePayload): void {
    lastFile = payload
    // **応答が `file` で返ったなら、その操作の待ちはここで解く**(QA指摘 2026-08-14)。
    // 「文書を変えるか」の判定はブラウザとサーバで別々に持っているので、片方だけ
    // 直したときに**状態配信を待ち続けて確定が永久に拒否される**経路ができる。
    // 出した番号を控えてあるので、それと一致するときだけ解けばよい(取りすぎない)。
    if (pendingOp !== null && pendingOp === lastFileOp) pendingOp = null
    const size = `${payload.bytes.toLocaleString()} B`
    const extra = payload.operations !== undefined ? ` / 操作 ${payload.operations}件` : ''
    note(`${payload.action}: ${payload.path}(${size}${extra})`)
  }

  // ---------------------------------------------------------------- 選択(コアを叩くだけ)
  const selection = $derived(model?.selection ?? null)
  const selectedFaces = $derived(
    (selection?.items ?? []).filter(
      (item): item is ShapeRef => !('sketch' in item) && (item as ShapeRef).kind === 'face',
    ),
  )
  /** 選択中の辺(D。**辺を出している間だけ**3Dで色が変わる)。 */
  const selectedEdges = $derived(
    (selection?.items ?? []).filter(
      (item): item is ShapeRef => !('sketch' in item) && (item as ShapeRef).kind === 'edge',
    ),
  )

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
  }

  // ---------------------------------------------------------------- D: 辺を取りに行く
  /**
   * 辺を出す対象のBody。**選んでいる形のBody**を優先し、無ければアクティブボディ。
   *
   * アクティブ固定にすると、27部品を読んだ文書で「見ている部品ではない方」の辺が出る。
   * 選択優先なら、面をクリックしてからフィレットを起動すれば必ずその部品の辺が出る。
   */
  const edgeTargetBody = $derived.by(() => {
    // **見えているボディだけを対象にする**(G・2026-08-14の受入で踏んだ)。AIが述語で
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

  function modifySelection(add: SelectionRef[], remove: SelectionRef[]): void {
    if (add.length === 0 && remove.length === 0) return
    session.modifySelection(add, remove, nextOp())
  }

  /**
   * 1つの参照を選択集合の編集へ翻訳する(面・辺・領域で同じ規則)。
   *
   * Shift=追加 / Ctrl=除去 / 修飾なし=置き換え。**解除の仕方だけ入口で違う**:
   *
   * - `'replace'`(3Dのクリック) … **1つだけ選んでいるものを再クリックしたら外す**
   *   (狙いを外したときに素で解除できる)
   * - `'toggle'`(一覧のクリック。スケッチパネルの領域) … **既に選ばれていたら常に外す**。
   *   一覧はチェックボックスとして読まれるので、2つ選んだ状態で片方を押したときに
   *   「置き換え」になると驚く
   *
   * 置き換えのとき**自分自身は remove に入れない** —— コアは remove を後に適用するので、
   * 入れると足したそばから外れる(`duocad/selection.py` の契約)。
   */
  function editSelection(ref: SelectionRef, mode: PickEvent['mode'] | 'toggle'): void {
    const current = selection?.items ?? []
    const already = current.some((item) => sameRef(item, ref))
    if (mode === 'add') modifySelection([ref], [])
    else if (mode === 'remove') modifySelection([], [ref])
    else if (already && (mode === 'toggle' || current.length === 1)) modifySelection([], [ref])
    else modifySelection([ref], current.filter((item) => !sameRef(item, ref)))
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
    // **辺が当たったら辺**(D)。フィレット・面取りを起動している間だけ来る。
    if (event.edge !== null) {
      editSelection(
        { body: event.edge.bodyId, kind: 'edge', index: event.edge.edgeIndex },
        event.mode,
      )
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

  /**
   * 丸めた・面取りした辺の選択を外す(D・2026-08-14。**QA指摘 2026-08-14**)。
   *
   * **添字は形状変更で壊れる**(§7)。フィレットが成功すると辺の並びは作り直されるので、
   * 選んだままの添字は**別の辺**を指す —— それを強調表示のまま残すと、続けて半径を打った
   * ときに「選んだ覚えのない辺が丸まる」。このタスクが最も警戒している事故そのものである。
   *
   * **コア側では落とさない**。「選択は操作をまたいで残る」はA1で決めた契約
   * (`tests/test_mcp_selection.py` の交互操作の一巡が、fillet の後も選択が残ることを
   * 固定している)で、AIは次の一手をそのまま打てることを前提にしている。`extrude_face` は
   * 後継の添字(`after_face_index`)をサーバが返すので選び直せるが、**フィレットには
   * 辺の後継の対応が無い**ので、UIができる正直なことは「外して選び直させる」しかない。
   */
  function dropStaleEdges(applied: Applied): void {
    if (applied.verb !== 'fillet' && applied.verb !== 'chamfer') return
    const changed = applied.body_id
    if (!changed) return
    const stale = (selection?.items ?? []).filter(
      (item): item is ShapeRef =>
        !('sketch' in item) && (item as ShapeRef).kind === 'edge' && (item as ShapeRef).body === changed,
    )
    if (stale.length === 0) return
    modifySelection([], stale)
    note(`${applied.verb} した辺の選択を外した(形が変わって添字が別の辺を指すため)`)
  }

  /**
   * 押し出し・undo・redo の後で、次に押す面を選び直す(M1からの挙動)。
   *
   * **添字は形状変更で壊れる**ので持ち越さず、サーバが返した後継の添字を使う。
   */
  function reselectAfter(applied: Applied): void {
    let next: ShapeRef | null = null
    if (applied.verb === 'extrude_face' && typeof applied.after_face_index === 'number' && applied.body_id) {
      next = faceRef(applied.body_id, applied.after_face_index)
    } else if (
      applied.verb === 'undo' &&
      applied.undone === 'extrude_face' &&
      typeof applied.face_index === 'number' &&
      applied.body_id
    ) {
      next = faceRef(applied.body_id, applied.face_index)
    } else if (
      applied.verb === 'redo' &&
      applied.redone === 'extrude_face' &&
      typeof applied.after_face_index === 'number' &&
      applied.body_id
    ) {
      next = faceRef(applied.body_id, applied.after_face_index)
    }
    if (next === null) return
    const target = next
    const current = selection?.items ?? []
    if (current.length === 1 && sameRef(current[0], target)) return
    modifySelection([target], current.filter((item) => !sameRef(item, target)))
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
    selectedEntities = [] // 反復の対象はモードを抜けたら持ち越さない(D)
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

  function createSketchOnBase(name: BasePlaneName): void {
    session.runVerb('sketch_create', { plane: { kind: 'base', name, provenance: null } }, nextOp())
  }

  function createSketchOnFace(face: ShapeRef): void {
    session.runVerb(
      'sketch_create',
      { plane: { kind: 'face', body: face.body, index: face.index, provenance: null } },
      nextOp(),
    )
  }

  function setParameter(name: string, value: number): void {
    session.runVerb('set_parameter', { name, value }, nextOp())
  }

  /**
   * 反復の対象にする図形を選ぶ/外す(D)。**共有選択集合には入れない** ——
   * コアの選択集合が持てるのは面・辺・頂点・領域の4種で、図形はそこに入らない
   * (MCP側も反復だけは図形IDを引数で受けている)。ここがその人間側の対。
   */
  function toggleEntity(entityId: string): void {
    selectedEntities = selectedEntities.includes(entityId)
      ? selectedEntities.filter((item) => item !== entityId)
      : [...selectedEntities, entityId]
  }

  function selectRegion(sketch: string, index: number): void {
    editSelection({ sketch, index }, 'toggle')
  }

  // ---------------------------------------------------------------- コマンド層との繋ぎ
  function runCommand(verb: string, args: Record<string, unknown>): void {
    if (isSketchVerb(verb)) {
      if (sketchId === null) {
        note('スケッチモードに入っていない')
        return
      }
      const op = nextOp()
      pendingOp = op
      viewport?.showSketchGhost(sketchId, null) // 本物を頼んだので幽霊は消す
      // **引数名はコマンド定義とサーバの表で既に揃っている**(p0/p1・center/radius・
      // corner/width/height・center/radius/start_deg/end_deg)。ここで詰め替えない。
      session.runVerb(verb, { ...args, sketch_id: sketchId }, op)
      return
    }
    // ---------------------------------------------------------------- D: フィーチャ
    if (isFeatureVerb(verb)) {
      if (needsSketchId(verb) && sketchId === null) {
        note('スケッチモードに入っていない(反復はスケッチの図形が対象)')
        return
      }
      if (verb === 'extrude' && !regionsStillExist(args.regions)) {
        // **領域はパラメータ編集で分裂・消失しうる**(§7)。消えた領域を送ると
        // サーバは添字が範囲外だと言うだけなので、ここで何が起きたかを画面へ出す。
        note('選んだ領域が今のスケッチに無い(図形が変わって分裂・消失した可能性)。選び直す')
        return
      }
      const op = nextOp()
      pendingOp = op
      session.runVerb(verb, needsSketchId(verb) ? { ...args, sketch_id: sketchId } : args, op)
      return
    }
    // ---------------------------------------------------------------- E: ファイル操作
    if (isFileVerb(verb)) {
      const op = nextOp()
      lastFileOp = op
      // **文書を変えないもの(保存・書き出し)は状態配信では返ってこない**ので、
      // 「応答待ち」は立てない —— 立てると `handleFile` が来るまでEnterが拒否される。
      if (changesDocument(verb)) pendingOp = op
      session.runVerb(verb, args, op)
      return
    }
    if (verb !== 'extrude_face') {
      note(`未知のコマンド: ${verb}`)
      return
    }
    const op = nextOp()
    pendingOp = op
    session.runVerb(verb, args, op)
  }

  /** 送ろうとしている領域が、今のスケッチに実在するか。 */
  function regionsStillExist(value: unknown): boolean {
    const refs = (value ?? []) as Array<{ sketch: string; index: number }>
    return refs.every((ref) =>
      sketches.some(
        (item) =>
          item.sketch_id === ref.sketch &&
          item.regions.some((candidate) => candidate.index === ref.index),
      ),
    )
  }

  /**
   * そのスケッチの領域の姿を1つの文字列にする(**中身が変わったかを見るための印**)。
   *
   * 添字だけでは足りない —— 領域の添字は計算結果の1始まりの連番なので、**分裂しても
   * 古い添字は範囲内に残り、別の領域を指したまま生き続ける**(§7「領域はパラメータ編集で
   * 変わる」)。面積と重心まで入れると、割れた・入れ替わった・数が変わったのどれでも印が変わる。
   */
  function regionPrint(sketch: (typeof sketches)[number]): string {
    return sketch.regions
      .map((item) => `${item.index}:${item.area.toFixed(6)}:${item.centroid.map((n) => n.toFixed(6)).join(',')}`)
      .join('|')
  }

  /** スケッチID -> 直前に見た領域の印。 */
  let regionPrints = new Map<string, string>()

  /**
   * 領域の姿が変わったスケッチの**領域の選択を外す**(D・2026-08-14。QA指摘)。
   *
   * 消えた領域は `regionsStillExist` が送信前に止められるが、**分裂**は止められない ——
   * 添字が範囲内に残るので、黙って「別の領域」を押し出すことになる。姿が変わったら
   * 選び直させるのが、この壊れ方に対して画面が取れる唯一の正直な手である。
   */
  function dropStaleRegions(): void {
    const stale: SelectionRef[] = []
    const next = new Map<string, string>()
    for (const sketch of sketches) {
      const print = regionPrint(sketch)
      next.set(sketch.sketch_id, print)
      const seen = regionPrints.get(sketch.sketch_id)
      if (seen === undefined || seen === print) continue
      stale.push(
        ...(selection?.items ?? []).filter(
          (item): item is { sketch: string; index: number } =>
            'sketch' in item && item.sketch === sketch.sketch_id,
        ),
      )
    }
    regionPrints = next
    if (stale.length === 0) return
    modifySelection([], stale)
    note('領域が作り直されたので選択を外した(分裂・消失しうる。選び直す)')
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

  function showOnlyParts(bodyIds: string[]): void {
    const keep = new Set(bodyIds)
    for (const body of partBodies) {
      const desired = keep.has(body.body_id)
      if (body.visible !== desired) setVisible(body.body_id, desired)
    }
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
    // 2回作り直すことになる(面のハイライトと違って作り直しが重い。QA 2026-08-13)。
  })

  /** 選択の先頭の面。スケッチを貼る対象と、テスト用フックの `selected` が使う。 */
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
      bodyGeometry,
      // **選択の正はコア**。ここは共有集合の写しであって、ブラウザ側の状態ではない。
      selection: model?.selection ?? null,
      // M1からの互換の穴: 選択のうち最初の面を `{bodyId, faceIndex}` で出す
      selected: firstFace === null ? null : { bodyId: firstFace.body, faceIndex: firstFace.index },
      previewing,
      lastApplied,
      messages,
      projectFace: (bodyId: string, faceIndex: number) => viewport?.project(bodyId, faceIndex) ?? null,
      // C: スケッチ。**実機検査が本物のクリックで描く**ための穴
      sketches,
      activeSketch: sketchId,
      hoverPoint,
      projectSketchPoint: (u: number, v: number) =>
        sketchId === null ? null : (viewport?.projectSketchPoint(sketchId, u, v) ?? null),
      projectRegion: (index: number) =>
        sketchId === null ? null : (viewport?.projectRegion(sketchId, index) ?? null),
      // D: 辺とフィーチャ。**実機検査が本物のクリックで辺を選ぶ**ための穴
      edges: edgeTargetBody === null ? null : (edges[edgeTargetBody] ?? null),
      edgeTargetBody,
      wantsEdges,
      selectedEntities,
      projectEdge: (bodyId: string, edgeIndex: number) =>
        viewport?.projectEdge(bodyId, edgeIndex) ?? null,
      // E: パラメータ・履歴・ファイル。実機検査が「打ち直したら形が追随する」を見る穴
      parameters: model?.parameters ?? {},
      redoCount: model?.redo_count ?? 0,
      myOperations,
      lastFile,
    }
  })
</script>

<svelte:window onkeydown={onWindowKeydown} />

<div class="app">
  <header>
    <strong>duocad</strong>
    <span class="tag" class:on={status === 'open'}>{status}</span>
    {#if model}
      <span>部品 {partBodies.length}</span>
      <span>操作 {model.applied_count}</span>
      {#if previewing}<span class="tag preview">プレビュー</span>{/if}
    {/if}
  </header>

  <!-- コマンドのツールバー(2026-08-19: 下部から上部へ移した。中身・状態機械は
       CommandHost.svelte 側で不変 —— ここは置き場所を変えただけ)。
       モードが変わったらコマンド層を作り直す(集めかけの引数を持ち越さない) -->
  <section class="toolbar">
    {#key sketchId === null}
      <CommandHost
        bind:this={host}
        {selection}
        busy={pendingOp !== null}
        commands={sketchId === null ? SOLID_COMMANDS : SKETCH_MODE_COMMANDS}
        defaultCommandId={sketchId === null ? undefined : DEFAULT_SKETCH_COMMAND_ID}
        hover={hoverPoint}
        entities={selectedEntities}
        onRun={runCommand}
        onPreview={requestPreview}
        onCancelPreview={clearPreview}
        onNote={note}
        onWants={(kinds) => (wantedKinds = kinds)}
      />
    {/key}
  </section>

  <div class="main">
    <PartsPanel
      bodies={partBodies}
      onToggle={setVisible}
      onShowAll={showAllParts}
      onHideAll={hideAllParts}
      onShowOnly={showOnlyParts}
    />
    <!-- canvas はこの div の中で three.js が作る。Svelte はここの中身を触らない -->
    <div class="viewport" bind:this={container}></div>

    <!-- 選択・パラメータ・スケッチをまとめた右サイドバー(2026-08-19)。
         .sidebar は overflow-y:auto で自分の中だけスクロールするので、
         中のパネルが増減しても .viewport のサイズは変わらない(射影が動かない)。 -->
    <aside class="sidebar">
      <SelectionPanel
        {selection}
        bodies={model?.bodies ?? []}
        onRemove={(ref) => modifySelection([], [ref])}
        onClear={() => session.clearSelection(nextOp())}
        onCommit={() => session.commitSelection(nextOp())}
      />

      <ParametersPanel
        parameters={model?.parameters ?? {}}
        {sketches}
        onSetParameter={setParameter}
        {onNote}
      />

      <SketchPanel
        {sketches}
        activeSketchId={sketchId}
        selectedFace={firstFace}
        selection={selection?.items ?? []}
        onCreateOnBase={createSketchOnBase}
        onCreateOnFace={createSketchOnFace}
        onEnter={enterSketch}
        onExit={exitSketch}
        onLookAt={() => sketchId && viewport?.lookAtSketch(sketchId)}
        onSetParameter={setParameter}
        onSelectRegion={selectRegion}
        selectedEntities={selectedEntities}
        onToggleEntity={toggleEntity}
      />
    </aside>
  </div>

  <!-- 操作履歴は下部の横長タイムラインとして表示(HistoryPanel.svelte側で横並びに restyle)。 -->
  <HistoryPanel
    log={model?.log ?? []}
    redoCount={model?.redo_count ?? 0}
    mine={myOperations}
  />

  <div class="statusbar">
    <!-- M1の「面」「直前」の行はここにあったが、選択パネルと履歴パネルが同じことを
         見せるようになったので外した(2026-08-15)。残すのは、どのパネルにも属さない
         **視点と履歴のボタン**だけ。「全体」はスケッチに寄った視点から戻る手で、
         これが無いと小さい図形を描いた後に部品が画面の外にあるまま操作することになる。 -->
    <div class="row">
      <button id="view-fit" onclick={() => viewport?.frameAll()}>全体</button>
      <button id="undo" onclick={() => session.undo()} disabled={!model?.can_undo}>undo</button>
      <button id="redo" onclick={() => session.redo()} disabled={!model?.can_redo}>redo</button>
    </div>

    {#if messages.length}
      <div class="row">
        <span class="label">通知</span>
        <span class="value">{messages.join(' / ')}</span>
      </div>
    {/if}
  </div>
</div>

<style>
  .tag.preview {
    background: #7ee081;
    color: #0b1a0d;
  }
</style>
