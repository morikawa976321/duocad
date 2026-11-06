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
/** 全面表示に案内と [閉じる] を足すまで。置いた値(実測ではない)。 */
const RESTART_SLOW_MS = 3 * 60 * 1000

let restarting = $state<{ title: string; detail: string } | null>(null)
let slow = $state(false)
let closed = $state(false)

export const restart = {
  get current() {
    return restarting
  },
  /** 立て直しが `RESTART_SLOW_MS` を超えた(全面表示に案内と [閉じる] を出す)。 */
  get slow() {
    return slow
  },
  /** 全面表示を閉じた。立て直しそのものは続いている(`current` は残る)。 */
  get closed() {
    return closed
  },
  close(): void {
    closed = true
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

/** `duocad-config.json`(取れた結果は1回だけ取りに行く。取れなかった結果は覚えず、次の呼び出しで取り直す)。 */
export function loadConfig(): Promise<ServerConfig | null> {
  const pending = (config ??= fetchJson<ServerConfig>('duocad-config.json'))
  void pending.then((value) => {
    if (value === null && config === pending) config = null
  })
  return pending
}

/** 版の違いで読み直した先の版(同じ版へ続けて読み直さないための印)。 */
const RELOADED_FOR_KEY = 'duocad-reloaded-for-version'

/**
 * 繋ぎ直したとき、サーバが画面を読み込んだ時と別の版に入れ替わっていたら、この窓を読み直す(画面側の未送信の下書きは消える)。
 * 安定版のときだけ働く: 開発版の版(`version.commit`)は作業ツリーの HEAD で、コミットのたびに変わるので、
 * 開発サーバを立て直すたびに開いている窓が読み直されてしまう。比べるのは起動の印(`boot`)でなく版 —— `boot` は
 * 同じ版の再起動でも変わる。
 */
export async function reloadIfServerReplaced(): Promise<void> {
  const loaded = await loadConfig()
  if (loaded?.channel !== 'stable' || !loaded.version?.commit || restarting !== null) return
  const now = await fetchJson<ServerConfig>('duocad-config.json')
  const commit = now?.version?.commit
  if (!commit || commit === loaded.version.commit) return
  try {
    if (sessionStorage.getItem(RELOADED_FOR_KEY) === commit) return
    sessionStorage.setItem(RELOADED_FOR_KEY, commit)
  } catch {
    return // 印を残せないと歯止めが効かない
  }
  location.reload()
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
  slow = false
  closed = false
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
  const startedAt = Date.now()
  const deadline = startedAt + RESTART_TIMEOUT_MS
  let wentDown = false
  while (Date.now() < deadline) {
    await sleep(RESTART_POLL_MS)
    if (Date.now() - startedAt > RESTART_SLOW_MS) slow = true
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
  slow = false
  closed = false
  try {
    sessionStorage.removeItem(PENDING_KEY)
  } catch {
    // 消せなくても次の読み込みで古い結果を報告するだけ
  }
  fail('立て直しが終わらない。安定版のデータフォルダの logs/ を見ること')
}
