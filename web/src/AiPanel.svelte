<script lang="ts">
  /**
   * AIパネル — アプリ内からAIを選んで対話する口(`duocad/agent/` の tool-use ループを `duocad/server.py` の `ai_*` 経由で繋ぐ)。
   * - `ai_*` は verb ではない。文書の変更はAIが呼ぶ既存MCPツール側から通常の verb 配信として届く(`App.svelte` の `handleState` が描き直す。このパネルは何も描かない)。
   * - 鍵はここでは打たない(`SettingsDialog.svelte`)。ここが見るのは置いてあるかの真偽(`apiKeyConfigured`)だけ。
   * - プロバイダの選択は `status.providers` で駆動する(2つ以上のときだけ選択UIを出す)。
   * - 会話の時系列(`transcript`)の正は `aiChat.svelte.ts`。サーバ側の会話(`duocad/agent/loop.py`)が残らないための代わりにブラウザ側が持つ
   *   (`session.ts` の `AiTranscriptEntry` 参照)。ここが持つのは見た目だけの状態(`draft`・設定フォームの開閉と打ちかけ・`open`)で、再読込で消える。
   * - ツール呼び出しの表示は verb 名をそのまま出す(操作ログ・MCPツール名と同じ綴り)。
   *
   * `BrowserPanel.svelte` と同じ形の開閉カレット(`#ai-collapse`)を持つ。畳んでも `transcript` の正は親にあるので会話は壊れない。
   * 既定は開いている(再読込をまたいで保持しない)。「設定」を押すと畳んだままでも設定フォームが見えるよう自動的に開く(`openSettings`)。
   */
  import type { AiProviderKind, AiStatus, AiThinking, AiTranscriptEntry } from './session'

  let {
    status,
    transcript,
    onSend,
    onCancel,
    onConfigure,
    onSetThinking,
  }: {
    status: AiStatus | null
    transcript: AiTranscriptEntry[]
    onSend: (message: string) => void
    onCancel: () => void
    onConfigure: (provider: AiProviderKind, baseUrl?: string) => void
    onSetThinking: (thinking: AiThinking) => void
  } = $props()

  let draft = $state('')
  let settingsOpen = $state(false)
  /** パネル本体(会話・入力・設定)を出しているか。畳んでもヘッダ行(プロバイダ名・中断・設定)は「何が繋がっているか」「応答中か」を言う役目なので見せたままにする。 */
  let open = $state(true)
  let providerDraft = $state<AiProviderKind>('openai_compat')
  let baseUrlDraft = $state('')

  const running = $derived(status?.running ?? false)
  const blockedReason = $derived(status ? null : '接続を待っている')
  const canPickProvider = $derived((status?.providers?.length ?? 0) > 1)

  const PROVIDER_LABEL: Record<string, string> = {
    openai_compat: 'llama-server',
    anthropic: 'Claude',
  }

  /** 思考の設定は llama-server にだけ送る(Claude の思考の深さは別の口)。 */
  const canPickThinking = $derived(status?.provider === 'openai_compat' && (status?.thinkingChoices.length ?? 0) > 0)

  const THINKING_LABEL: Record<AiThinking, string> = {
    off: '思考なし',
    low: '思考 low',
    medium: '思考 medium',
    server: 'サーバ任せ',
  }

  /** 送り先を選べるのは鍵を持たないプロバイダだけ(Claudeは固定。`Server._ai_configure` が拒否する)。 */
  const canPickEndpoint = $derived(providerDraft !== 'anthropic')
  const keyMissing = $derived(status?.provider === 'anthropic' && !status.apiKeyConfigured)

  function openSettings(): void {
    providerDraft = status?.provider ?? 'openai_compat'
    baseUrlDraft = status?.baseUrl ?? ''
    settingsOpen = true
    open = true
  }

  /**
   * プロバイダを切り替えたらエンドポイントの打ちかけを捨てる。欄が隠れても `baseUrlDraft` は生きていて `ai_configure` に乗り、
   * 指定した覚えのないURLで繋ぎに行く。空にすれば `undefined` でサーバ側の既定値が使われる
   * (Claudeへ切り替えて打ちかけが残ると、サーバが `base_url` を拒否して切り替え自体が失敗する)。
   */
  function selectProvider(next: AiProviderKind): void {
    if (next === providerDraft) return
    providerDraft = next
    baseUrlDraft = ''
  }

  function applySettings(): void {
    onConfigure(providerDraft, baseUrlDraft.trim() || undefined)
    settingsOpen = false
  }

  function send(): void {
    const text = draft.trim()
    if (!text || running || blockedReason) return
    onSend(text)
    draft = ''
  }

  /** 引数・結果はJSONで畳んでおく(§長い応答対策)。 */
  function toolLine(call: AiTranscriptEntry & { kind: 'tool' }): string {
    const args = Object.keys(call.call.arguments).length ? JSON.stringify(call.call.arguments) : ''
    return `${call.call.name}${args ? ` ${args}` : ''}`
  }

  const STATUS_LABEL: Record<string, string> = {
    completed: '完了',
    empty_reply: 'AIが結論を出さずに止まった(何もしていない。言い方を変えてもう一度どうぞ)',
    max_turns: '往復の上限に達した(続けるにはもう一度どうぞ)',
    cancelled: '中断した',
    max_tokens: '出力の上限に達した(このターンのツールは実行していない。もう一度どうぞ)',
    refusal: 'AIが断った',
    provider_error: '接続に失敗した',
    timeout: '時間切れで止めた(ここまでの操作は残っている)',
    context_overflow: 'この依頼だけでAIの文脈長を超えた(依頼を分けるか、llama-server の -c を増やす)',
    stopped: '止まった(理由不明)',
  }

  /** llama-server の `timings` を往復ぶん足した値。Claudeは時間ではなくトークン数(課金の単位)を出す。どちらも返さないサーバでは空文字(行ごと出さない)。 */
  function usageLine(usage: Record<string, number> | undefined): string {
    if (usage?.input_tokens !== undefined && usage.output_tokens !== undefined) {
      return (
        `入力 ${usage.input_tokens}(キャッシュ読み ${usage.cache_read_input_tokens ?? 0}` +
        `・書き ${usage.cache_creation_input_tokens ?? 0})／ 出力 ${usage.output_tokens} トークン`
      )
    }
    if (usage?.prompt_n === undefined || usage.predicted_n === undefined) return ''
    const seconds = (ms: number | undefined): string => ((ms ?? 0) / 1000).toFixed(1)
    return (
      `読み ${usage.prompt_n}(再利用 ${usage.cache_n ?? 0})トークン ${seconds(usage.prompt_ms)}秒` +
      ` ／ 書き ${usage.predicted_n} トークン ${seconds(usage.predicted_ms)}秒`
    )
  }
</script>

<section class="ai-panel" class:collapsed={!open}>
  <div class="ai-head">
    <button
      id="ai-collapse"
      class="caret"
      onclick={() => (open = !open)}
      title={open ? '畳む' : '開く'}
      aria-expanded={open}
    >
      {open ? '▾' : '▸'}
    </button>
    <strong>AI</strong>
    {#if status}
      <span class="value" data-provider={status.provider}>
        {PROVIDER_LABEL[status.provider] ?? status.provider}
      </span>
      {#if status.provider === 'anthropic' && status.model}
        <span class="dim" data-ai-model>{status.model}</span>
      {/if}
      {#if canPickThinking}
        <!-- 適用ボタンを待たずに送る(会話は作り直されず、次の往復から効く。`Server._ai_set_thinking`)。値の正は `ai_status.thinking`。 -->
        <select
          id="ai-thinking"
          title="llama-server へ送る思考の設定"
          value={status.thinking}
          disabled={running}
          onchange={(event) => onSetThinking(event.currentTarget.value as AiThinking)}
        >
          {#each status.thinkingChoices as choice (choice)}
            <option value={choice}>{THINKING_LABEL[choice] ?? choice}</option>
          {/each}
        </select>
      {/if}
      {#if running}
        <span class="badge running">応答中</span>
      {/if}
    {/if}
    <span class="spacer"></span>
    {#if running}
      <button id="ai-cancel" onclick={onCancel}>中断</button>
    {/if}
    <button id="ai-settings-toggle" onclick={() => (settingsOpen ? (settingsOpen = false) : openSettings())}>
      設定
    </button>
  </div>

  {#if open}
    {#if settingsOpen}
      <div class="ai-settings">
        {#if canPickProvider}
          <label class="field-row">
            プロバイダ
            <!-- `bind:value` にしない(束縛の更新と `onchange` の順序に依存せず、切り替えを `selectProvider` 1か所で扱う)。
                 選択肢の正はサーバの `ai_status.providers`。 -->
            <select
              id="ai-provider"
              value={providerDraft}
              onchange={(event) => selectProvider(event.currentTarget.value as AiProviderKind)}
            >
              {#each status?.providers ?? [] as kind (kind)}
                <option value={kind}>{PROVIDER_LABEL[kind] ?? kind}({kind})</option>
              {/each}
            </select>
          </label>
        {/if}
        <!-- エンドポイントは鍵を持たないプロバイダにだけ出す(Claudeの送り先は固定。`Server._ai_configure`)。 -->
        {#if canPickEndpoint}
          <label class="field-row">
            エンドポイント
            <input
              id="ai-base-url"
              type="text"
              placeholder="http://127.0.0.1:8080/v1"
              bind:value={baseUrlDraft}
            />
          </label>
        {/if}
        <button id="ai-settings-apply" onclick={applySettings} disabled={running}>適用</button>
      </div>
    {/if}

    {#if blockedReason}
      <p class="hint dim" data-ai-blocked>{blockedReason}</p>
    {/if}
    {#if keyMissing}
      <p class="hint dim" data-ai-key-missing>
        APIキーが未設定(ヘッダーの「設定」から置ける)
      </p>
    {/if}

    <div class="ai-transcript" id="ai-transcript">
      {#each transcript as entry, index (index)}
        {#if entry.kind === 'user'}
          <p class="line user">{entry.text}</p>
        {:else if entry.kind === 'tool'}
          <p class="line tool" class:tool-fail={!entry.call.ok} data-tool={entry.call.name}>
            {entry.call.ok ? '✓' : '✗'} {toolLine(entry)}
          </p>
        {:else if entry.kind === 'result'}
          <p class="line result" data-status={entry.result.status}>
            {entry.result.text || STATUS_LABEL[entry.result.status] || entry.result.status}
          </p>
          {#if usageLine(entry.result.usage)}
            <p class="hint dim" data-ai-usage>{usageLine(entry.result.usage)}</p>
          {/if}
        {:else}
          <p class="line error">{entry.message}</p>
        {/if}
      {/each}
      {#if transcript.length === 0}
        <p class="hint dim">AIに話しかける(例: 「ボディの一覧を教えて」)</p>
      {/if}
    </div>

    <div class="ai-input">
      <input
        id="ai-message"
        type="text"
        placeholder={blockedReason ?? '話しかける…'}
        disabled={running || !!blockedReason}
        bind:value={draft}
        onkeydown={(event) => {
          if (event.key !== 'Enter') return
          event.preventDefault()
          send()
        }}
      />
      <button id="ai-send" onclick={send} disabled={running || !draft.trim() || !!blockedReason}>
        送信
      </button>
    </div>
  {/if}
</section>

<style>
  .ai-panel {
    border-top: 1px solid var(--line);
    background: var(--panel);
    padding: 6px 10px;
    /* 高さを固定する: 内容任せだと会話が伸びるたびにビューポートがリサイズされ、カメラのアスペクトと射影が動く
       (直前に射影した画素を押すと別のものが取れる)。 */
    height: 220px;
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  /* 畳んだら220px固定を効かせずヘッダ行だけの高さにする(`flex: 0 0 auto` は維持: 縮んでよいのはビューポートだけ)。 */
  .ai-panel.collapsed {
    height: auto;
  }

  .ai-head {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    row-gap: 4px;
    flex: 0 0 auto;
  }

  .badge.running {
    font-size: var(--fs-meta);
    padding: 1px 6px;
    border-radius: 8px;
    background: var(--accent);
    color: #1a1204;
  }

  .ai-settings {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 6px 0;
    border-bottom: 1px solid var(--line-soft);
    flex: 0 0 auto;
  }

  .ai-settings input,
  .ai-settings select {
    flex: 1 1 auto;
    width: auto;
    min-width: 0;
    text-align: left;
  }

  .hint {
    margin: 4px 0 0;
  }

  .ai-transcript {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    margin-top: 4px;
  }

  .line {
    margin: 2px 0;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .line.user {
    color: var(--text);
  }

  .line.user::before {
    content: '> ';
    color: var(--dim);
  }

  .line.tool {
    color: var(--dim);
  }

  .line.tool-fail {
    color: var(--bad);
  }

  .line.result {
    color: var(--accent);
  }

  .line.error {
    color: var(--bad);
  }

  .ai-input {
    display: flex;
    gap: 6px;
    margin-top: 4px;
    flex: 0 0 auto;
  }

  .ai-input input {
    flex: 1 1 auto;
    width: auto;
    text-align: left;
  }
</style>
