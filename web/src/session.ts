/**
 * コアとの接続 — コマンドは下り、イベントは上り(実装計画書 §2)。
 *
 * ここが持つのは「WebSocketの接続」と「途中まで入力していたコマンド」だけで、
 * 文書・操作ログ・undo履歴はコア側にある
 * (§2「アプリ層が落ちて失われてよいのは、途中まで入力していたコマンドだけ」)。
 *
 * **例外はM1の「選択」**: 実装計画書§3.5は選択集合もコア側と定めているが、M1では
 * ブラウザ側(`App.svelte` の `selected`)が正で、コアは選択を知らない。移すのはM4の債務。
 */
import type { GeometryPayload } from './viewport'

export interface ServerState {
  units: string
  body_id: string
  body_name: string
  face_count: number
  edge_count: number
  faces_by_type: Record<string, number>
  volume: number
  geometry: GeometryPayload
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
  face_index?: number
  distance?: number
  after_face_index?: number | null
  server_ms?: number
  undone?: string
  redone?: string
}

export type ConnectionStatus = 'connecting' | 'open' | 'closed'

export interface SessionHooks {
  onState(state: ServerState, applied: Applied | null): void
  onError(message: string, where: string): void
  onWarning(message: string): void
  onStatus(status: ConnectionStatus): void
}

export interface Session {
  start(): void
  extrude(faceIndex: number, distance: number, clientOp: number): void
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
      hooks.onState(message.state, message.applied ?? null)
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
    extrude(faceIndex, distance, clientOp) {
      send({ cmd: 'extrude_face', face_index: faceIndex, distance, client_op: clientOp })
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
