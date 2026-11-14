<script lang="ts">
  /**
   * ヘッダー左端の、開発版の札と安定版の更新ボタン(`duocad/channel.py`・`docs/STABLE.md`)。
   *
   * - 開発版: サーバが `channel: 'stable'` と答えたとき以外は札を出す(設定が取れない Vite の dev サーバ等も開発版扱い)。
   * - 安定版: main に新しいコミットがあるときだけ「更新」ボタンが出る(`serverRestart.svelte.ts`)。
   *   立て直し中の全面表示と読み直し後の知らせは、設定の「GPU の部品を入れる」の分もここが出す。
   */
  import { onMount } from 'svelte'
  import { fail, inform } from './Toast.svelte'
  import {
    PENDING_KEY,
    fetchJson,
    loadConfig,
    restart,
    restartServer,
    type ServerConfig,
    type ServerVersion,
  } from './serverRestart.svelte'

  type LastResult = {
    ok: boolean
    restored?: boolean
    rollback: boolean
    gpu?: boolean
    /** 起動口が起動時に起こした更新(窓を開く前に走るので、この窓は結果を待っていない)。 */
    auto?: boolean
    at?: string
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
    current: ServerVersion
    last_result?: LastResult | null
  }

  const STATUS_POLL_MS = 30 * 60 * 1000
  /** 窓が前面に戻ったときの確認を、直前の確認からこれだけ空いていなければ省く。置いた値(実測ではない)。 */
  const FOCUS_REFRESH_MIN_MS = 60 * 1000

  let config = $state<ServerConfig | null>(null)
  /** 取りに行く前に札を出すと、安定版でも一瞬「開発版」が出る。 */
  let loaded = $state(false)
  const isDev = $derived(loaded && config?.channel !== 'stable')
  let status = $state<UpdateStatus | null>(null)

  const versionText = $derived(
    config?.version?.commit ? `${config.version.commit} (${config.version.date?.slice(0, 10) ?? ''})` : '',
  )

  let lastCheckedAt = 0

  async function refreshStatus(force = false): Promise<void> {
    lastCheckedAt = Date.now()
    const next = await fetchJson<UpdateStatus>(`api/update${force ? '?force=1' : ''}`)
    if (next) status = next
  }

  function reportFinishedUpdate(result: LastResult | null | undefined): void {
    if (!result) return
    const short = (commit: string) => commit.slice(0, 7)
    if (result.gpu) {
      if (result.ok) inform('GPU の部品を入れた')
      else if (result.restored === false) fail(`GPU の部品を入れられず、元の構成にも戻せなかった: ${result.message}(ログ: ${result.log})`)
      else fail(`GPU の部品を入れられなかったので、CPU の構成に戻した: ${result.message}(ログ: ${result.log})`)
    } else if (result.ok) {
      inform(result.from === result.to ? 'アップデート済み(変更なし)' : `アップデートした(${short(result.from)} → ${short(result.to)})`)
    } else if (result.restored === false) {
      fail(`アップデートに失敗し、元の版にも戻せなかった: ${result.message}(ログ: ${result.log})`)
    } else {
      fail(`アップデートに失敗したので元の版に戻した: ${result.message}(ログ: ${result.log})`)
    }
  }

  const REPORTED_AUTO_KEY = 'duocad-reported-auto-update'

  /** 起動時の自動更新の結果は、この窓が開く前に出ている。まだ知らせていない分だけ、窓が開いたときに一度知らせる。 */
  function reportAutoUpdate(result: LastResult | null | undefined): void {
    if (!result?.auto || !result.at) return
    try {
      if (localStorage.getItem(REPORTED_AUTO_KEY) === result.at) return
      localStorage.setItem(REPORTED_AUTO_KEY, result.at)
    } catch {
      return
    }
    reportFinishedUpdate(result)
  }

  function startUpdate(): void {
    void restartServer('api/update', {
      title: 'アップデート中…',
      detail: '取り込みとビルドが終わると、自動で読み直す(数分かかることがある)',
      failPrefix: 'アップデートを始められなかった',
    }, fail)
  }

  onMount(() => {
    let timer: ReturnType<typeof setInterval> | null = null
    const onVisible = (): void => {
      if (document.visibilityState === 'visible' && Date.now() - lastCheckedAt >= FOCUS_REFRESH_MIN_MS) void refreshStatus()
    }
    void (async () => {
      config = await loadConfig()
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
      else reportAutoUpdate(status?.last_result)
      timer = setInterval(() => void refreshStatus(), STATUS_POLL_MS)
      document.addEventListener('visibilitychange', onVisible)
    })()
    return () => {
      if (timer !== null) clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
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
    disabled={restart.current !== null}
    title={`mainの新しい変更 ${status.behind}件(押すと再起動して取り込む):\n${status.commits.join('\n')}`}
  >
    アップデート {status.behind}
  </button>
{:else if status?.state === 'blocked'}
  <span class="tag" title={status.reason ?? ''}>アップデートできない</span>
{/if}

{#if restart.current && !restart.closed}
  <div class="update-overlay" role="status" aria-live="polite">
    <p>{restart.current.title}</p>
    <p class="dim">{restart.current.detail}</p>
    {#if restart.slow}
      <p class="dim">3分たっても戻らない。立て直しは続けている。戻らないときは logs/ を見るか、画面を読み直すこと</p>
      <button id="update-overlay-close" onclick={() => restart.close()}>閉じる</button>
    {/if}
  </div>
{/if}

<style>
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

  .update-overlay button {
    margin-top: 12px;
  }
</style>
