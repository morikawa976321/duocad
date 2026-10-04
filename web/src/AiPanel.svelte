<script lang="ts">
  /**
   * AIパネル — **アプリ内からAIを選んで対話する**唯一の口。
   *
   * タスクF(`duocad/agent/`)のtool-useループをWebSocket(`duocad/server.py` の `ai_*`)
   * 経由でここへ繋ぐ。中心の設計は他のパネルと同じ規律を保つこと ——
   *
   * - **`ai_*` はverbではない**。ここから送るのは対話であって、文書の変更は
   *   AIが呼ぶ既存MCPツールの側から**通常のverb配信**として届く(`App.svelte` の
   *   `handleState` がいつもどおり描き直す。このパネルは何も描かない)。
   * - **鍵を打つ場所がどこにも無い**。ClaudeをAPIキーで使う経路は廃止したので、
   *   キーという概念自体がこの画面にもサーバにも無い(Claudeへ繋ぐ道はMCP経路だけ)。
   * - **プロバイダの選択は `status.providers` で駆動する**。今は1つ(llama-server)しか
   *   無いので選択UIは出ないが、サーバの `AI_PROVIDERS` が増えれば自動で出る。
   * - **会話の時系列(`transcript`)はここが持つ状態ではない** —— **正は `aiChat.svelte.ts`**で、
   *   ここは`props`として受け取って描くだけ(パネルは何も覚えない。
   *   `duocad/agent/loop.py`の会話がサーバ側にしか残らないための代わりとして
   *   ブラウザ側に置いてある。`session.ts`の`AiTranscriptEntry`docstring参照)。
   *   **ここが自前で持つ状態**は、打ちかけの発話(`draft`)・設定フォームの
   *   開閉と打ちかけ・本体の開閉(`open`。下記)だけで、どれも見た目だけの状態(再読込で消える)。
   * - **ツール呼び出しの表示はverb名をそのまま出す**(操作ログ・MCPツール名と同じ綴り。
   *   タスクファイルの要件)。
   *
   * **`BrowserPanel.svelte` と同じ形の開閉カレット(`#ai-collapse`)を持つ**
   * (方針 3段目「使うかもしれないし使わないかも
   * しれないものは…ユーザーが表示・非表示を切り替えられるようにする」)。
   * tier3の常設パネルは全て畳める(ファイル一覧=ヘッダのトグル、
   * ブラウザ=カレット、AI設定フォーム=設定ボタン、AI本体=このカレット)。
   * **畳んでも会話が壊れない理由は上の`transcript`の所在そのもの** —— 正が親にある以上、
   * このパネルを畳んで`open`が変わっても`transcript`は影響を受けず、開き直せば続きが見える。
   * **既定は開いている**(`BrowserPanel.svelte` と同じ形。再読込をまたいだ保持はしない)。
   * 「設定」を押すと畳んだままでも設定フォームが見えるよう自動的に開く(`openSettings`)。
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
    onConfigure: (provider: AiProviderKind, baseUrl?: string) => void
  } = $props()

  let draft = $state('')
  let settingsOpen = $state(false)
  /** パネル本体(会話・入力・設定)を出しているか。**既定は開いている**(`BrowserPanel.svelte`
   * の `open` と同じ形)。畳んでもヘッダ行(プロバイダ名・中断・設定)は見えたままにする ——
   * それらは「何が繋がっているか」「応答中か」を一目で言う役目なので、畳んでも隠さない。 */
  let open = $state(true)
  /** 設定フォームの打ちかけ(開いた時点の `status` で初期化する)。 */
  let providerDraft = $state<AiProviderKind>('openai_compat')
  let baseUrlDraft = $state('')

  const running = $derived(status?.running ?? false)
  const blockedReason = $derived(status ? null : '接続を待っている')
  /** 選択UIは**2つ以上あるときだけ**出す(1つしか無い今は出さない)。 */
  const canPickProvider = $derived((status?.providers?.length ?? 0) > 1)

  /** 画面に出す名前(綴りそのままだと何と話しているか分からない)。 */
  const PROVIDER_LABEL: Record<string, string> = {
    openai_compat: 'llama-server',
  }

  function openSettings(): void {
    providerDraft = status?.provider ?? 'openai_compat'
    baseUrlDraft = status?.baseUrl ?? ''
    settingsOpen = true
    // **畳んだまま設定を開いても見えない**ので、設定を押したら本体ごと開く。
    open = true
  }

  /**
   * プロバイダを切り替えたら **エンドポイントの打ちかけを捨てる**。
   *
   * **打ちかけはプロバイダに属する** —— 欄が隠れても `baseUrlDraft` は生きているので、
   * 隠れたまま `ai_configure` に乗って「指定した覚えのないURL」で繋ぎに行く。
   * 空にすれば `undefined` になり、サーバ側の既定値が使われる。
   *
   * 今はプロバイダが1つなのでこの関数は動かない(選択UI自体が出ない)。**2つ目を足したら
   * ここが効き始める** —— `duocad/server.py` の `AI_PROVIDERS` に足すだけで選択UIが出る。
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
    timeout: '時間切れで止めた(ここまでの操作は残っている)',
    context_overflow: 'この依頼だけでAIの文脈長を超えた(依頼を分けるか、llama-server の -c を増やす)',
    stopped: '止まった(理由不明)',
  }

  /** 読む側と書く側のどちらに時間を使ったか(llama-server の `timings` を往復ぶん足した値)。
   * `timings` を返さないサーバでは空文字(行ごと出さない)。 */
  function usageLine(usage: Record<string, number> | undefined): string {
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
    <!-- **BrowserPanel.svelteと同じ形の開閉カレット**(新しい畳み方を発明しない)。 -->
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
            <!-- `bind:value` にしない —— 束縛の更新と `onchange` の順序に依存せず、
                 切り替えを1か所(`selectProvider`)で扱うため。
                 **選択肢はサーバの `ai_status.providers` が正**(ここに書き写さない)。 -->
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
        <!-- **エンドポイントは常に出す** —— 今あるプロバイダは鍵を持たないので、
             どこを指しても飛ぶのはキーの無いリクエストだけである。鍵を持つプロバイダを
             足すときは、ここと `Server._ai_configure` の両方に制限を書き戻すこと。 -->
        <label class="field-row">
          エンドポイント
          <input
            id="ai-base-url"
            type="text"
            placeholder="http://127.0.0.1:8080/v1"
            bind:value={baseUrlDraft}
          />
        </label>
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
    /* **高さを固定する。** 内容任せにすると、会話が伸びるたびにビューポートが
       リサイズされてカメラのアスペクトと射影が動く —— 直前に射影した画素を
       押すと別のものが取れる(実機検査が本物のクリックで指す前提が崩れる)。 */
    height: 220px;
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  /* **畳んだら220px固定を効かせず、ヘッダ行だけの高さにする**(`flex: 0 0 auto` は維持 ——
     縮んでよいのはビューポートだけ、という不変条件は畳んでも変わらない)。 */
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

  /* **幅は残りに合わせる**。「エンドポイント」の行が 300px のサイドバーから
     はみ出さないよう、欄側が譲る。 */
  .ai-settings input,
  .ai-settings select {
    flex: 1 1 auto;
    width: auto;
    min-width: 0;
    text-align: left;
  }

  /* 大きさは styles.css の `.hint` が持つ(--fs-meta)。 */
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
