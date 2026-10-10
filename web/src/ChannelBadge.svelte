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

  let config = $state<ServerConfig | null>(null)
  /** 取りに行く前に札を出すと、安定版でも一瞬「開発版」が出る。 */
  let loaded = $state(false)
  const isDev = $derived(loaded && config?.channel !== 'stable')
  let status = $state<UpdateStatus | null>(null)

  const versionText = $derived(
    config?.version?.commit ? `${config.version.commit} (${config.version.date?.slice(0, 10) ?? ''})` : '',
  )

  async function refreshStatus(force = false): Promise<void> {
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
      inform(result.from === result.to ? '更新済み(変更なし)' : `更新した(${short(result.from)} → ${short(result.to)})`)
    } else if (result.restored === false) {
      fail(`更新に失敗し、元の版にも戻せなかった: ${result.message}(ログ: ${result.log})`)
    } else {
      fail(`更新に失敗したので元の版に戻した: ${result.message}(ログ: ${result.log})`)
    }
  }

  function startUpdate(): void {
    void restartServer('api/update', {
      title: '更新中…',
      detail: '取り込みとビルドが終わると、自動で読み直す(数分かかることがある)',
      failPrefix: '更新を始められなかった',
    }, fail)
  }

  onMount(() => {
    let timer: ReturnType<typeof setInterval> | null = null
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
    disabled={restart.current !== null}
    title={`mainの新しい変更 ${status.behind}件(押すと再起動して取り込む):\n${status.commits.join('\n')}`}
  >
    更新 {status.behind}
  </button>
{:else if status?.state === 'blocked'}
  <span class="tag" title={status.reason ?? ''}>更新できない</span>
{/if}

{#if restart.current}
  <div class="update-overlay" role="status" aria-live="polite">
    <p>{restart.current.title}</p>
    <p class="dim">{restart.current.detail}</p>
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
</style>
