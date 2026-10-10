/**
 * 安定版のサーバを立て直す操作(`api/update`)の画面側。サーバは更新スクリプトを起こして一度落ち、
 * 立ち直る(`duocad/channel.py`)。立ち直ったら画面を読み直す。待っている間の全面表示は `ChannelBadge` が `restart.current` で出す。
 */
export type ServerVersion = { commit?: string; date?: string }
export type ServerConfig = {
  channel?: 'dev' | 'stable'
  version?: ServerVersion
  /** サーバのプロセスの起動ごとに変わる印(`duocad/channel.py` の `BOOT_ID`)。 */
  boot?: string
  update_supported?: boolean
}

/** 読み直し後に結果を知らせるための `sessionStorage` の印。 */
export const PENDING_KEY = 'duocad-update-pending'
const RESTART_POLL_MS = 2000
/** `uv sync`(GPU の部品は約 2GB)と `npm ci` の両方が走っても収まる上限。 */
const RESTART_TIMEOUT_MS = 15 * 60 * 1000

let restarting = $state<{ title: string; detail: string } | null>(null)

export const restart = {
  get current() {
    return restarting
  },
}

export async function fetchJson<T>(path: string): Promise<T | null> {
  try {
    const response = await fetch(new URL(path, location.href), { cache: 'no-store' })
    return response.ok ? ((await response.json()) as T) : null
  } catch {
    return null
  }
}

let config: Promise<ServerConfig | null> | null = null

/** `duocad-config.json`(1回だけ取りに行く)。 */
export function loadConfig(): Promise<ServerConfig | null> {
  config ??= fetchJson<ServerConfig>('duocad-config.json')
  return config
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** `path` へ POST して立て直しを始め、立ち直ったら読み直す。 */
export async function restartServer(
  path: string,
  shown: { title: string; detail: string; failPrefix: string },
  fail: (message: string) => void,
): Promise<void> {
  if (restarting) return
  restarting = { title: shown.title, detail: shown.detail }
  // 比べる元は押した時点の印。取れなければ始めない(落ちる前の応答を「立ち直った」と読みうる)。
  const now = await fetchJson<ServerConfig>('duocad-config.json')
  if (now === null) {
    restarting = null
    fail(`${shown.failPrefix}: サーバの状態を取れなかった`)
    return
  }
  const before = now.boot
  let response: Response
  try {
    response = await fetch(new URL(path, location.href), { method: 'POST' })
  } catch (error) {
    restarting = null
    fail(`${shown.failPrefix}: ${error}`)
    return
  }
  if (response.status !== 202) {
    restarting = null
    const body = (await response.json().catch(() => null)) as { error?: string } | null
    fail(`${shown.failPrefix}: ${body?.error ?? response.status}`)
    return
  }
  try {
    sessionStorage.setItem(PENDING_KEY, '1')
  } catch {
    // 保存できなくても立て直しは進む
  }
  // 起動の印(`boot`)が変わったら立ち直った。落ちている間を見なくても判定できる。
  // 印を持たない古いサーバからの更新だけは、落ちたのを見てから立ち直りを待つ。
  const deadline = Date.now() + RESTART_TIMEOUT_MS
  let wentDown = false
  while (Date.now() < deadline) {
    await sleep(RESTART_POLL_MS)
    const next = await fetchJson<ServerConfig>('duocad-config.json')
    if (next === null) {
      wentDown = true
      continue
    }
    if (before !== undefined ? next.boot !== before : wentDown) {
      location.reload()
      return
    }
  }
  restarting = null
  fail('立て直しが終わらない。安定版のデータフォルダの logs/ を見ること')
}
