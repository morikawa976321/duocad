<script lang="ts">
  /**
   * AIパネル — **アプリ内からAIを選んで対話する**唯一の口(G・2026-08-20)。
   *
   * タスクF(`duocad/agent/`)のtool-useループをWebSocket(`duocad/server.py` の `ai_*`)
   * 経由でここへ繋ぐ。中心の設計は他のパネルと同じ規律を保つこと ——
   *
   * - **`ai_*` はverbではない**。ここから送るのは対話であって、文書の変更は
   *   AIが呼ぶ既存29ツールの側から**通常のverb配信**として届く(`App.svelte` の
   *   `handleState` がいつもどおり描き直す。このパネルは何も描かない)。
   * - **APIキーはここに無い**。`status.apiKeyConfigured` の真偽だけを見て、
   *   入力欄・設定フォームのどちらにもキーを打つ場所を作らない
   *   (`duocad/agent/providers.py` の要件そのもの)。
   * - **状態はほぼ持たない** —— 唯一ここだけが持つのは打ちかけの発話と会話の時系列
   *   (`transcript`)で、これは `duocad/agent/loop.py` の会話がサーバ側にしか
   *   残らないための代わり(再読込で消えるのは「途中まで入力していたコマンド」と同じ扱い。
   *   `session.ts` の `AiTranscriptEntry` docstring)。
   * - **ツール呼び出しの表示はverb名をそのまま出す**(操作ログ・MCPツール名と同じ綴り。
   *   タスクファイルの要件)。
   */
  import type { AiProviderKind, AiStatus, AiTranscriptEntry } from './session'

  let {
    status,
    transcript,
    onSend,
    onCancel,
    onConfigure,
  }: {
    status: AiStatus | null
    transcript: AiTranscriptEntry[]
    onSend: (message: string) => void
    onCancel: () => void
    onConfigure: (provider: AiProviderKind, model?: string, baseUrl?: string) => void
  } = $props()

  let draft = $state('')
  let settingsOpen = $state(false)
  /** 設定フォームの打ちかけ(開いた時点の `status` で初期化する)。 */
  let providerDraft = $state<AiProviderKind>('anthropic')
  let modelDraft = $state('')
  let baseUrlDraft = $state('')

  const running = $derived(status?.running ?? false)
  /** `anthropic` はキーが要る。`openai_compat`(llama-server)は既定でキー不要。 */
  const blockedReason = $derived.by(() => {
    if (!status) return '接続を待っている'
    if (status.provider === 'anthropic' && !status.apiKeyConfigured) {
      return 'APIキー未設定(環境変数 ANTHROPIC_API_KEY か duocad_secrets.json に置く。画面からは入力できない)'
    }
    return null
  })

  function openSettings(): void {
    providerDraft = status?.provider ?? 'anthropic'
    modelDraft = status?.model ?? ''
    baseUrlDraft = status?.baseUrl ?? ''
    settingsOpen = true
  }

  function applySettings(): void {
    onConfigure(providerDraft, modelDraft.trim() || undefined, baseUrlDraft.trim() || undefined)
    settingsOpen = false
  }

  function send(): void {
    const text = draft.trim()
    if (!text || running || blockedReason) return
    onSend(text)
    draft = ''
  }

  /** verbと同じ綴りの名前 + 成否だけを1行に(引数・結果はJSONで畳んでおく。§長い応答対策)。 */
  function toolLine(call: AiTranscriptEntry & { kind: 'tool' }): string {
    const args = Object.keys(call.call.arguments).length ? JSON.stringify(call.call.arguments) : ''
    return `${call.call.name}${args ? ` ${args}` : ''}`
  }

  const STATUS_LABEL: Record<string, string> = {
    completed: '完了',
    max_turns: '往復の上限に達した(続けるにはもう一度どうぞ)',
    cancelled: '中断した',
    max_tokens: '出力の上限に達した(このターンのツールは実行していない。もう一度どうぞ)',
    refusal: 'AIが断った',
    provider_error: '接続に失敗した',
    stopped: '止まった(理由不明)',
  }
</script>

<section class="ai-panel">
  <div class="ai-head">
    <strong>AI</strong>
    {#if status}
      <span class="value" data-provider={status.provider}>
        {status.provider === 'anthropic' ? 'Claude' : 'llama-server'}
        {status.model ? `(${status.model})` : ''}
      </span>
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

  {#if settingsOpen}
    <div class="ai-settings">
      <label>
        プロバイダ
        <select id="ai-provider" bind:value={providerDraft}>
          <option value="anthropic">Claude(anthropic)</option>
          <option value="openai_compat">llama-server(openai_compat)</option>
        </select>
      </label>
      <label>
        モデル
        <input id="ai-model" type="text" placeholder="既定値" bind:value={modelDraft} />
      </label>
      {#if providerDraft === 'openai_compat'}
        <label>
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
</section>

<style>
  .ai-panel {
    border-top: 1px solid var(--line);
    background: var(--panel);
    padding: 6px 10px;
    /* SelectionPanelと同じ理由(コメント参照)で高さを固定する ——
       会話が伸びてもビューポートのアスペクトを動かさない。 */
    height: 220px;
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  .ai-head {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    row-gap: 4px;
    flex: 0 0 auto;
  }

  .spacer {
    flex: 1 1 auto;
  }

  .badge.running {
    font-size: 11px;
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

  .ai-settings label {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--dim);
  }

  .ai-settings input,
  .ai-settings select {
    width: 15em;
    text-align: left;
  }

  .hint {
    margin: 4px 0 0;
    font-size: 11px;
  }

  .ai-transcript {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    margin-top: 4px;
  }

  .line {
    margin: 2px 0;
    font-size: 12px;
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
    color: #e08a8a;
  }

  .line.result {
    color: var(--accent);
  }

  .line.error {
    color: #e08a8a;
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

  .dim {
    color: var(--dim);
  }
</style>
