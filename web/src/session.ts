/**
 * コアとの接続 — コマンドは下り、イベントは上り(実装計画書 §2)。
 *
 * ここが持つのは「WebSocketの接続」と「途中まで入力していたコマンド」だけで、
 * 文書・操作ログ・undo履歴・**可視性**はコア側にある
 * (§2「アプリ層が落ちて失われてよいのは、途中まで入力していたコマンドだけ」)。
 *
 * **例外はM1由来の「面の選択」**: 実装計画書§3.5は選択集合もコア側と定めているが、
 * 選択(どの面を狙っているか)はブラウザ側(`App.svelte` の `selected`)が正で、コアは
 * 選択そのものを知らない。移すのはM4の債務。**可視性は違う** — M3.5で
 * `duocad/verbs/visibility.py` の `set_visible` verbがコアに入ったので、表示・非表示の
 * 状態はここを経由してコアだけが持つ(このファイルはON/OFFを覚えず、コマンドを送るだけ)。
 *
 * **M3.5(2026-08-11)で `state` は部品ごとの配列になった**。`state.bodies` は軽量
 * (geometryを含まない)で、形状は `delta.bodies`(版が変わったBodyだけ)で届く
 * (`duocad/server.py` のモジュールdocstring参照)。
 *
 * **B(2026-08-13)で「M1由来の例外」が消えた** —— 選択はコア側(`state.selection`)が正に
 * なり、ここは `modify_selection` / `commit_selection` / `clear_selection` を送るだけになった。
 * 上の「例外はM1由来の面の選択」はもう無い(ブラウザは選択を1つも覚えない)。
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
}

export interface Session {
  start(): void
  extrude(bodyId: string, faceIndex: number, distance: number, clientOp: number): void
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
  undo(): void
  redo(): void
  dispose(): void
}

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
    extrude(bodyId, faceIndex, distance, clientOp) {
      send({
        cmd: 'extrude_face',
        body_id: bodyId,
        face_index: faceIndex,
        distance,
        client_op: clientOp,
      })
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
