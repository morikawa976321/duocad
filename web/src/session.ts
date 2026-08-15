/**
 * コアとの接続 — コマンドは下り、イベントは上り(実装計画書 §2)。
 *
 * ここが持つのは「WebSocketの接続」と「途中まで入力していたコマンド」だけで、
 * 文書・操作ログ・undo履歴・**可視性**はコア側にある
 * (§2「アプリ層が落ちて失われてよいのは、途中まで入力していたコマンドだけ」)。
 *
 * **選択も可視性もコアが正**。このファイルは `modify_selection` / `commit_selection` /
 * `clear_selection` / `set_visible` を送るだけで、どちらの状態も覚えない。
 *
 * **M3.5(2026-08-11)で `state` は部品ごとの配列になった**。`state.bodies` は軽量
 * (geometryを含まない)で、形状は `delta.bodies`(版が変わったBodyだけ)で届く
 * (`duocad/server.py` のモジュールdocstring参照)。
 *
 * 選択の正は `state.selection`(コア側)。**ブラウザは選択を1つも覚えない。**
 * あわせて `preview`(投機的実行)を足した —— **これだけは状態を配信しない**ので、
 * 応答は `onState` ではなく `onPreview` へ来る。
 */
import type { GeometryPayload } from './viewport'

/** 選べるものの種別。領域(region)は `RegionRef` 側で表す(`duocad/selection.py` §3.5)。 */
export type ShapeKind = 'face' | 'edge' | 'vertex'

/** 面・辺・頂点を指す参照。添字は**1始まり**(コアの `select_*` と同じ)。 */
export interface ShapeRef {
  body: string
  kind: ShapeKind
  index: number
  provenance?: unknown
}

/** スケッチの領域を指す参照(`kind` キーを持たないのが `ShapeRef` との見分け方)。 */
export interface RegionRef {
  sketch: string
  index: number
  provenance?: unknown
}

export type SelectionRef = ShapeRef | RegionRef

/**
 * 共有される選択集合(`duocad/selection.py` の `Selection.to_dict()`)。
 *
 * **origin は集合単位**。`"proposed"` はAIが提案した状態で、そのままではverbに渡せない
 * (コアが `SelectionNotCommitted` で止める)。人が `commit_selection` を踏むか、
 * `modify_selection` で触ると `"user"` になる。
 */
export interface SelectionState {
  items: SelectionRef[]
  origin: 'user' | 'proposed'
  count: number
}

/** 1Bodyぶんの軽量な状態(geometryを含まない)。パーツ一覧はこれだけで描ける。 */
export interface BodyMeta {
  body_id: string
  name: string
  kind: string
  visible: boolean
  revision: number
  face_count: number
  volume: number
}

/** `BodyMeta` + 形状。版が変わったBodyだけ届く(`geometry` は非表示なら null)。 */
export interface BodyGeometry extends BodyMeta {
  geometry: GeometryPayload | null
}

// --------------------------------------------------------------------------------------
// C(2026-08-13): スケッチ — **手動操作が一級の操作モード**になる場所
// --------------------------------------------------------------------------------------
/** スケッチを貼る平面(コアの PlaneRef。基準3面か、既存ソリッドの平面状の面)。 */
export type PlaneRef =
  | { kind: 'base'; name: BasePlaneName; provenance?: unknown }
  | { kind: 'face'; body: string; index: number; provenance?: unknown }

export type BasePlaneName = 'XY' | 'XZ' | 'YZ'

/**
 * スケッチ平面の3Dの枠。`p = origin + u*uAxis + v*vAxis` が (u,v) と3Dの往復の規則で、
 * **コアが配る**(`kernel.sketch_plane_frame`)。アプリ層で同じ計算を持たない —— 基準3面の
 * 軸の取り方と、面に貼ったときの法線反転の扱いが必ずずれる。
 */
export interface SketchFrame {
  origin: number[]
  u: number[]
  v: number[]
  normal: number[]
}

export type EntityKind = 'line' | 'circle' | 'rectangle' | 'arc'

/**
 * スケッチ上の1図形。`params` は**解決済みの数値**、`refs` は同じキーの
 * **パラメータ名**(`set_parameter` で打ち直せる。名前が無ければ null)。
 *
 * この2つが揃っているのが「クリックだけで置いた後も値を打ち直せる」の実体である
 * (実装計画書§3.4 パラメトリック・プリミティブ)。
 */
export interface SketchEntity {
  entity_id: string
  kind: EntityKind
  params: Record<string, number>
  refs: Record<string, string | null>
}

/** 計算された閉領域。`positions`/`indices` は画面へ描くための三角形(コアが測る)。 */
export interface SketchRegion {
  index: number
  area: number
  centroid: number[]
  positions: number[]
  indices: number[]
}

export interface SketchState {
  sketch_id: string
  plane: PlaneRef
  /** 平面が壊れている(貼った面が消えた等)ときだけ null。 */
  frame: SketchFrame | null
  /** 反復を展開した**実際に見えるもの**。描画は必ずこちらを使う。 */
  drawn: Array<{ kind: EntityKind; params: Record<string, number> }>
  /** 人が値を打ち直す対象(反復は展開しない)。 */
  entities: SketchEntity[]
  regions: SketchRegion[]
}

export interface ServerState {
  units: string
  active_body_id: string | null
  /** 全Body。**geometryを含まない**(常に軽い。27部品でも数KB)。 */
  bodies: BodyMeta[]
  log: Array<{ seq: number; verb: string; args: Record<string, unknown> }>
  can_undo: boolean
  can_redo: boolean
  applied_count: number
  /** 共有される選択集合。**ここが正**で、ブラウザは自前の選択を持たない(A2以降)。 */
  selection: SelectionState
  /** 文書の全スケッチ(C以降)。図形も領域もここが正で、ブラウザは溜めない。 */
  sketches: SketchState[]
  /** あと何回やり直せるか(E)。**真偽だけだと履歴の位置が出せない**。 */
  redo_count: number
  /** 名前つきの寸法値(E・§3.4)。パラメータ編集パネルが出す対象そのもの。 */
  parameters: Record<string, number>
}

/**
 * ファイル操作の応答(E)。**状態の配信ではない** —— 保存・書き出しは文書を変えないので、
 * 要求した接続にだけ「どこへ何バイト書いたか」が返る(`PreviewPayload` と同じ扱い)。
 *
 * `path` は**サーバが解決した絶対パス**。相対パスの基準はサーバの作業ディレクトリで、
 * ブラウザ側では解決できない(2箇所で別々に解釈すると必ずずれる)。
 */
export interface FilePayload {
  action: string
  path: string
  bytes: number
  operations?: number
  deviation?: number
  body_ids?: string[]
}

/**
 * 操作を出したクライアントへの確認応答。
 *
 * **2種類ある**。verbの適用は `verb` を持ち、選択の変更は `change: "selection"` を持つ
 * (選択はverbではなく、操作ログにも載らない。`duocad/server.py` の `_selection_event`)。
 * どちらか判らないまま `applied.verb` を見ると、選択変更のたびに `undefined` と
 * 比較することになるので、**読む側は必ず先に見分ける**こと。
 */
export interface Applied {
  verb?: string
  seq?: number
  client_op: number | null
  body_id?: string
  face_index?: number
  distance?: number
  after_face_index?: number | null
  undone?: string
  redone?: string
  visible?: boolean
  changed_bodies?: string[]
  /** 選択の変更イベントのときだけ `"selection"`。 */
  change?: string
  selection?: SelectionState
  /** C: `sketch_create` が返す新しいスケッチのID。 */
  sketch_id?: string
  /** C: 図形追加verbが返す新しい図形のID。 */
  entity_id?: string
  /** C: `set_parameter` の名前と値。 */
  name?: string
  value?: number
  /**
   * STEP読み込みが作ったボディ(2026-08-15)。`import_step_editable` は編集用と原本の両方、
   * `import_step` は参照ボディだけを返す。
   *
   * **`svelte-check` を入れて初めて見つかった穴** —— サーバは前から返していたのに
   * ここに宣言が無く、`.svelte` の中で `applied.body_ids` を読んでいた
   * (`.svelte` は tsc の対象外だったので誰も気づかなかった)。実行時は動いていたが、
   * サーバ側がキー名を変えても型では止まらない状態だった。
   */
  body_ids?: string[]
  reference_ids?: string[]
}

/**
 * プレビュー(投機的実行)の応答。**状態の配信ではない** —— 要求した接続にだけ届き、
 * サーバ側の文書は既に元へ戻っている(`Session.preview`)。
 *
 * `preview_id` は要求時に付けた番号。**古い応答を捨てるために要る** ——
 * 数値を打ち替えると要求が追い越しうるし、Escで中断した後に届くこともある。
 */
export interface PreviewPayload {
  preview_id: number | null
  verb: string
  bodies: Array<{
    body_id: string
    name: string
    face_count: number
    volume: number
    geometry: GeometryPayload
  }>
}

/**
 * 1本の辺の折れ線(D・2026-08-14。`kernel.tessellate_edges` の1件そのまま)。
 *
 * `index` は**コアの数え方の1始まり**で、`fillet(edges=[...])` が食う添字と同じ。
 * ここがずれると「指した辺と違う辺が丸まる」という、例外も出ない事故になる。
 */
export interface EdgeGeometry {
  index: number
  kind: string
  length: number
  closed: boolean
  points: number[]
}

/**
 * 辺の応答。**状態の配信ではない**(要求した接続にだけ届く。`PreviewPayload` と同じ扱い)。
 *
 * `revision` は要求した時点のBodyの版。**形が変われば版が上がる**ので、古い辺を
 * 掴んだままフィレットをかける経路が閉じる(§7 添字は形状変更で壊れる、の辺版)。
 */
export interface EdgesPayload {
  body_id: string
  revision: number
  edges: EdgeGeometry[]
}

export type ConnectionStatus = 'connecting' | 'open' | 'closed'

export interface SessionHooks {
  /**
   * `changed` は今回の版で届いたBodyだけ(geometry付き。非表示になったものは
   * `geometry: null`)。それ以外のBodyは前回のまま描き直さない(差分更新)。
   */
  onState(state: ServerState, changed: BodyGeometry[], applied: Applied | null): void
  onError(message: string, where: string): void
  onWarning(message: string): void
  onStatus(status: ConnectionStatus): void
  /** プレビューが返ってきた(状態は変わっていない)。 */
  onPreview(payload: PreviewPayload): void
  /** 頼んだ辺が返ってきた(D。状態は変わっていない)。 */
  onEdges(payload: EdgesPayload): void
  /** ファイルを書いた(E。状態は変わっていない)。 */
  onFile(payload: FilePayload): void
}

export interface Session {
  start(): void
  /**
   * verbを1つ送る。**引数のdictをそのまま流す**(`cmd` と `client_op` を足すだけ)。
   *
   * 引数を作るのは `CommandDef.build` の仕事で、ここは運ぶだけ —— verbごとにメソッドを
   * 書き写すと、キー名を1文字打ち間違えた経路が**そのverbでだけ**静かに壊れる。
   * **受け取れるverbの一覧はサーバの表(`SKETCH_COMMANDS` / `FEATURE_COMMANDS` /
   * `FILE_COMMANDS`)が正**で、そこに無い文字列は `unknown command` で返る
   * (任意のメソッドを呼べる作りにしない、はサーバ側で担保する)。
   */
  runVerb(verb: string, args: Record<string, unknown>, clientOp: number): void
  setVisible(bodyId: string, visible: boolean, clientOp: number): void
  /** 人が直す経路。差分編集で `origin` は `"user"` になる(触ること自体が承認)。 */
  modifySelection(add: SelectionRef[], remove: SelectionRef[], clientOp: number): void
  /** AIの提案を中身を変えずに承認する。 */
  commitSelection(clientOp: number): void
  clearSelection(clientOp: number): void
  /**
   * verbを投機的に実行して形だけ測らせる。**文書は変わらない**(サーバが元へ戻す)。
   * 応答は `onPreview` へ。`previewId` は古い応答を捨てるための番号。
   */
  preview(verb: string, args: Record<string, unknown>, previewId: number): void
  // ---------------------------------------------------------------- D: フィーチャ
  /**
   * 1Bodyの辺を折れ線で取りに行く(応答は `onEdges`)。**状態配信ではない**ので
   * 他のクライアントには何も起きない。フィレット・面取りを起動している間だけ呼ぶ
   * (常時配ると27部品ぶんの辺が乗る。`Session.edges` のdocstring)。
   */
  getEdges(bodyId: string): void
  undo(): void
  redo(): void
  dispose(): void
}

/** スケッチ平面上の点 [u, v](mm)。 */
export type Point2 = [number, number]

const RECONNECT_DELAY_MS = 2000

async function resolveWebSocketUrl(): Promise<string> {
  // サーバが `/duocad-config.json` で自分のWebSocketのURLを教える。
  // ポートを推測しない(静的配信とWebSocketでポートが別であり、既定値を2箇所に書くと必ずずれる)。
  try {
    const response = await fetch(new URL('duocad-config.json', location.href), {
      cache: 'no-store',
    })
    if (response.ok) {
      const config = (await response.json()) as { ws_url?: string }
      if (config.ws_url) return config.ws_url
    }
  } catch {
    // 取れなければ既定へ落ちる(dev サーバから開いた場合など)
  }
  return `ws://${location.hostname || '127.0.0.1'}:8765`
}

export function createSession(hooks: SessionHooks): Session {
  let socket: WebSocket | null = null
  let disposed = false
  let retry: ReturnType<typeof setTimeout> | null = null

  function send(payload: Record<string, unknown>): void {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      hooks.onError('サーバに接続していない', 'send')
      return
    }
    socket.send(JSON.stringify(payload))
  }

  function handle(raw: string): void {
    let message: {
      type?: string
      state?: ServerState
      delta?: { bodies?: BodyGeometry[] }
      applied?: Applied
      message?: string
      where?: string
      preview_id?: number | null
      verb?: string
      bodies?: PreviewPayload['bodies']
      body_id?: string
      revision?: number
      edges?: EdgeGeometry[]
      action?: string
      path?: string
      bytes?: number
      operations?: number
      deviation?: number
      body_ids?: string[]
    }
    try {
      message = JSON.parse(raw)
    } catch (error) {
      hooks.onError(String(error), 'parse')
      return
    }
    if (message.type === 'state' && message.state) {
      hooks.onState(message.state, message.delta?.bodies ?? [], message.applied ?? null)
    } else if (message.type === 'preview') {
      hooks.onPreview({
        preview_id: message.preview_id ?? null,
        verb: message.verb ?? '',
        bodies: message.bodies ?? [],
      })
    } else if (message.type === 'edges') {
      hooks.onEdges({
        body_id: message.body_id ?? '',
        revision: message.revision ?? 0,
        edges: message.edges ?? [],
      })
    } else if (message.type === 'file') {
      hooks.onFile({
        action: message.action ?? '',
        path: message.path ?? '',
        bytes: message.bytes ?? 0,
        operations: message.operations,
        deviation: message.deviation,
        body_ids: message.body_ids,
      })
    } else if (message.type === 'error') {
      hooks.onError(message.message ?? '(理由なし)', message.where ?? '?')
    } else if (message.type === 'warning') {
      hooks.onWarning(message.message ?? '')
    }
  }

  async function connect(): Promise<void> {
    if (disposed) return
    hooks.onStatus('connecting')
    const url = await resolveWebSocketUrl()
    const next = new WebSocket(url)
    socket = next
    next.onopen = () => hooks.onStatus('open')
    next.onmessage = (event) => handle(String(event.data))
    next.onerror = () => hooks.onError(`WebSocketのエラー (${url})`, 'socket')
    next.onclose = () => {
      hooks.onStatus('closed')
      if (disposed) return
      retry = setTimeout(() => void connect(), RECONNECT_DELAY_MS)
    }
  }

  return {
    start() {
      void connect()
    },
    runVerb(verb, args, clientOp) {
      send({ cmd: verb, ...args, client_op: clientOp })
    },
    setVisible(bodyId, visible, clientOp) {
      send({ cmd: 'set_visible', body_id: bodyId, visible, client_op: clientOp })
    },
    modifySelection(add, remove, clientOp) {
      send({ cmd: 'modify_selection', add, remove, client_op: clientOp })
    },
    commitSelection(clientOp) {
      send({ cmd: 'commit_selection', client_op: clientOp })
    },
    clearSelection(clientOp) {
      send({ cmd: 'clear_selection', client_op: clientOp })
    },
    preview(verb, args, previewId) {
      send({ cmd: 'preview', verb, args, preview_id: previewId })
    },
    getEdges(bodyId) {
      send({ cmd: 'get_edges', body_id: bodyId })
    },
    undo() {
      send({ cmd: 'undo' })
    },
    redo() {
      send({ cmd: 'redo' })
    },
    dispose() {
      disposed = true
      if (retry) clearTimeout(retry)
      socket?.close()
      socket = null
    },
  }
}
