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
 */
import type { GeometryPayload } from './viewport'

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
  measure_log: string
}

export interface Applied {
  verb: string
  seq: number
  client_op: number | null
  body_id?: string
  face_index?: number
  distance?: number
  after_face_index?: number | null
  server_ms?: number
  undone?: string
  redone?: string
  visible?: boolean
  changed_bodies?: string[]
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
}

export interface Session {
  start(): void
  extrude(bodyId: string, faceIndex: number, distance: number, clientOp: number): void
  setVisible(bodyId: string, visible: boolean, clientOp: number): void
  undo(): void
  redo(): void
  /** 計測1行を送る(追記するのはサーバ。区間のms換算もサーバが行う)。 */
  record(record: Record<string, unknown>): void
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
    }
    try {
      message = JSON.parse(raw)
    } catch (error) {
      hooks.onError(String(error), 'parse')
      return
    }
    if (message.type === 'state' && message.state) {
      hooks.onState(message.state, message.delta?.bodies ?? [], message.applied ?? null)
    } else if (message.type === 'error') {
      hooks.onError(message.message ?? '(理由なし)', message.where ?? '?')
    } else if (message.type === 'warning') {
      hooks.onWarning(message.message ?? '')
    }
    // "recorded" は確認だけなので何もしない(計測の正はサーバのjsonl)
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
    undo() {
      send({ cmd: 'undo' })
    },
    redo() {
      send({ cmd: 'redo' })
    },
    record(record) {
      send({ cmd: 'measure_record', record })
    },
    dispose() {
      disposed = true
      if (retry) clearTimeout(retry)
      socket?.close()
      socket = null
    },
  }
}
