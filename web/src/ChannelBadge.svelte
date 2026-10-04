<script lang="ts">
  /**
   * ヘッダー左端の、開発版の札と安定版の更新ボタン(`duocad/channel.py`・`docs/STABLE.md`)。
   *
   * - 開発版: 常に「開発版」の札を出し、窓のタイトルにも付ける —— 安定版と並べて開いたとき、
   *   見た目が同じ2つの窓を取り違えないため。**サーバが `channel: 'stable'` と答えたときだけ札を消す**
   *   (設定が取れない Vite の dev サーバ等も開発版として扱う。付け忘れ・取り損ねで札が消える側へ倒さない)。
   * - 安定版: mainに新しいコミットがあるときだけ「更新」ボタンが出る。押すとサーバが
   *   更新スクリプトを起こして終了し、立ち直ったら画面を読み直す。
   */
  import { onMount } from 'svelte'
  import { fail, inform } from './Toast.svelte'

  type Version = { commit?: string; date?: string }
  type Config = { channel?: 'dev' | 'stable'; version?: Version; update_supported?: boolean }
  type LastResult = {
    ok: boolean
    restored?: boolean
    rollback: boolean
    from: string
    to: string
    message: string
    log: string
  }
  type UpdateStatus = {
    state: 'current' | 'available' | 'blocked' | 'unknown'
    behind: number
    commits: string[]
    reason?: string
    current: Version
    last_result?: LastResult | null
  }

  /** 更新を押してから読み直すまでの間、`sessionStorage` に立てておく印。読み直し後に結果を知らせる。 */
  const PENDING_KEY = 'duocad-update-pending'
  const STATUS_POLL_MS = 30 * 60 * 1000
  const RESTART_POLL_MS = 2000
  /** `uv sync` と `npm ci` の両方が走る更新でも収まる上限。 */
  const RESTART_TIMEOUT_MS = 15 * 60 * 1000

  let config = $state<Config | null>(null)
  /** 設定を取りに行き終えたか。取りに行く前に札を出すと、安定版でも一瞬「開発版」が光る。 */
  let loaded = $state(false)
  const isDev = $derived(loaded && config?.channel !== 'stable')
  let status = $state<UpdateStatus | null>(null)
  let updating = $state(false)

  const versionText = $derived(
    config?.version?.commit ? `${config.version.commit} (${config.version.date?.slice(0, 10) ?? ''})` : '',
  )

  async function fetchJson<T>(path: string): Promise<T | null> {
    try {
      const response = await fetch(new URL(path, location.href), { cache: 'no-store' })
      return response.ok ? ((await response.json()) as T) : null
    } catch {
      return null
    }
  }

  async function refreshStatus(force = false): Promise<void> {
    const next = await fetchJson<UpdateStatus>(`api/update${force ? '?force=1' : ''}`)
    if (next) status = next
  }

  function reportFinishedUpdate(result: LastResult | null | undefined): void {
    if (!result) return
    const short = (commit: string) => commit.slice(0, 7)
    if (result.ok) {
      inform(result.from === result.to ? '更新済み(変更なし)' : `更新した(${short(result.from)} → ${short(result.to)})`)
    } else if (result.restored === false) {
      fail(`更新に失敗し、元の版にも戻せなかった: ${result.message}(ログ: ${result.log})`)
    } else {
      fail(`更新に失敗したので元の版に戻した: ${result.message}(ログ: ${result.log})`)
    }
  }

  function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms))
  }

  async function startUpdate(): Promise<void> {
    if (updating) return
    updating = true
    let response: Response
    try {
      response = await fetch(new URL('api/update', location.href), { method: 'POST' })
    } catch (error) {
      updating = false
      fail(`更新を始められなかった: ${error}`)
      return
    }
    if (response.status !== 202) {
      updating = false
      const body = (await response.json().catch(() => null)) as { error?: string } | null
      fail(`更新を始められなかった: ${body?.error ?? response.status}`)
      return
    }
    try {
      sessionStorage.setItem(PENDING_KEY, '1')
    } catch {
      // 保存できなくても更新は進む(読み直し後の知らせが出ないだけ)
    }
    // サーバは一度落ちてから立ち直る。**落ちたのを見てから**立ち直りを待つ ——
    // 落ちる前の応答を「立ち直った」と読むと、古い画面のまま読み直してしまう。
    const before = config?.version?.commit
    const deadline = Date.now() + RESTART_TIMEOUT_MS
    let wentDown = false
    while (Date.now() < deadline) {
      await sleep(RESTART_POLL_MS)
      const next = await fetchJson<Config>('duocad-config.json')
      if (next === null) {
        wentDown = true
        continue
      }
      if (wentDown || next.version?.commit !== before) {
        location.reload()
        return
      }
    }
    updating = false
    fail('更新が終わらない。安定版のデータフォルダの logs/ を見ること')
  }

  onMount(() => {
    let timer: ReturnType<typeof setInterval> | null = null
    void (async () => {
      config = await fetchJson<Config>('duocad-config.json')
      loaded = true
      if (config?.channel !== 'stable') {
        document.title = `[開発版] ${document.title}`
        return
      }
      if (!config?.update_supported) return
      let pending = false
      try {
        pending = sessionStorage.getItem(PENDING_KEY) !== null
        sessionStorage.removeItem(PENDING_KEY)
      } catch {
        pending = false
      }
      await refreshStatus(pending)
      if (pending) reportFinishedUpdate(status?.last_result)
      timer = setInterval(() => void refreshStatus(), STATUS_POLL_MS)
    })()
    return () => {
      if (timer !== null) clearInterval(timer)
    }
  })
</script>

{#if isDev}
  <span id="channel-badge" class="channel-dev" title={`開発版のサーバに繋がっている ${versionText}`}>開発版</span>
{:else if status?.state === 'available'}
  <button
    id="update-button"
    class="tag on"
    onclick={startUpdate}
    disabled={updating}
    title={`mainの新しい変更 ${status.behind}件(押すと再起動して取り込む):\n${status.commits.join('\n')}`}
  >
    更新 {status.behind}
  </button>
{:else if status?.state === 'blocked'}
  <span class="tag" title={status.reason ?? ''}>更新できない</span>
{/if}

{#if updating}
  <div class="update-overlay" role="status" aria-live="polite">
    <p>更新中…</p>
    <p class="dim">取り込みとビルドが終わると、自動で読み直す(数分かかることがある)</p>
  </div>
{/if}

<style>
  /* 開発版の札。**琥珀(--accent =「人の手が要る」)とも赤(--bad = 失敗)とも別の色**にする ——
     どちらかに寄せると、札が状態の知らせに読まれる。 */
  .channel-dev {
    padding: 0 8px;
    border-radius: 3px;
    background: oklch(62% 0.2 330);
    color: oklch(15% 0.01 330);
    font-weight: 700;
    letter-spacing: 0.08em;
  }

  .update-overlay {
    position: fixed;
    inset: 0;
    z-index: 1000;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
    background: oklch(17% 0.006 250 / 0.88);
    color: var(--text);
  }

  .update-overlay p {
    margin: 0;
  }
</style>
