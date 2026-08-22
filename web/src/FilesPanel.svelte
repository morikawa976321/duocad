<script lang="ts">
  /**
   * ファイルパネル — **保存先が固定フォルダになったことをUIに落とす場所**
   * (2026-08-22本人決定。engine `duocad-save-location-fixed-folder`)。
   *
   * 以前の「保存」「開く」はツールバーの2つのボタンで、`CommandHost` の一般引数
   * (`text`)としてパスを1行打たせていた。パスは**サーバのCWD基準**で解決されるため、
   * 保存先は「その日サーバをどこで起動したか」で変わり、実際に保存されたファイルは
   * 1つも存在しなかった(機能はあるが使われていない状態)。
   *
   * ここでは代わりに **`duocad/models/` の中身を一覧で見せて、クリック1つで開く**。
   * `PartsPanel.svelte` / `HistoryPanel.svelte` と同じ作り —— **状態はコアが持つ**
   * (`documents` はサーバの `list_documents` が返した一覧の写しで、ここでは何も
   * 覚えない)。保存・開く・一覧はどれも `App.svelte` が `session.runVerb(...)` で
   * 直接送る(`ParametersPanel.svelte` の `onSetParameter` と同じ形。`CommandHost` は
   * 経由しない —— 一覧からのクリックは「対象を選ぶ」そのものであって、
   * 引数収集の状態機械を通す理由が無い)。
   *
   * **ブラウザのネイティブファイルダイアログ(File System Access API)は使わない**。
   * 実際にファイルを書いているのはサーバ側のPythonなので、ブラウザがファイルハンドルを
   * 持っても噛み合わない(engineの同ノート「設計上の注意」)。
   *
   * **開くのに確認ダイアログを挟まない**(README「省けるクリックは徹底的に省く」)。
   * 上書きも同様 —— 保存はそもそも「今の文書をそこへ書く」操作であり、
   * 一覧からの上書きだけ確認を挟むと**新規保存と挙動が割れる**。
   */
  import type { DocumentInfo } from './session'

  let {
    documents,
    currentName,
    onSave,
    onOpen,
  }: {
    /** `duocad/models/` の中身(サーバの `list_documents` の写し)。 */
    documents: DocumentInfo[]
    /** 直近に保存/開いたファイル名(`App.svelte` が save/open の応答から追う)。 */
    currentName: string | null
    /** ファイル名を渡して保存する(新規保存も上書きも同じ verb)。 */
    onSave: (name: string) => void
    /** ファイル名を渡して開く。 */
    onOpen: (name: string) => void
  } = $props()

  /** 打ちかけの新規保存名。送ったら空に戻す(ParametersPanelの`editing`と同じ規律)。 */
  let draftName = $state('')

  /**
   * 打った名前が使える形か(空・区切り文字・`.`/`..`)。
   *
   * **最終判定はサーバ**(`Session._resolve_document_name`)。ここでの検査は
   * 明らかに拒まれる形を早く言うためだけで、拡張子や実在チェックはしない
   * (`command.ts` の `TextArgSpec` のdocstringと同じ理由: サーバと2箇所で
   * 別々のパス解釈を持たない)。
   */
  function nameProblem(text: string): string | null {
    const trimmed = text.trim()
    if (trimmed === '') return 'ファイル名を打つ(例: model.json)'
    if (trimmed.includes('/') || trimmed.includes('\\')) return '区切り文字は使えない(ファイル名だけ)'
    if (trimmed === '.' || trimmed === '..') return '使えないファイル名'
    return null
  }

  const draftProblem = $derived(nameProblem(draftName))

  function submitDraft(): void {
    if (draftProblem !== null) return
    onSave(draftName.trim())
    draftName = ''
  }

  function formatSize(bytes: number): string {
    return `${bytes.toLocaleString()} B`
  }

  function formatTime(epochSeconds: number): string {
    return new Date(epochSeconds * 1000).toLocaleString()
  }

  const sorted = $derived([...documents].sort((a, b) => b.mtime - a.mtime))
</script>

<section class="files-panel">
  <div class="row">
    <strong>ファイル</strong>
    <span id="current-document" class="value dim">{currentName ?? '(未保存)'}</span>
  </div>

  <div class="new-row">
    <input
      id="file-name-input"
      type="text"
      autocomplete="off"
      placeholder="model.json"
      value={draftName}
      oninput={(event) => (draftName = (event.target as HTMLInputElement).value)}
      onkeydown={(event) => {
        if (event.key !== 'Enter') return
        event.preventDefault()
        submitDraft()
      }}
    />
    <button id="file-save-new" onclick={submitDraft} disabled={draftProblem !== null}>
      新規保存
    </button>
  </div>
  {#if draftName !== '' && draftProblem !== null}
    <span class="hint">{draftProblem}</span>
  {/if}

  <div class="list" id="files-list">
    {#if sorted.length === 0}
      <span class="value dim">models/ にファイルが無い</span>
    {/if}
    {#each sorted as file (file.name)}
      <div class="files-row" data-name={file.name}>
        <button class="files-open" onclick={() => onOpen(file.name)} title="クリックで開く">
          {file.name}
        </button>
        <span class="meta dim" title={formatTime(file.mtime)}>
          {file.operations ?? '?'}件 / {formatSize(file.size)}
        </span>
        <button class="files-overwrite" onclick={() => onSave(file.name)} title="今の文書で上書き">
          上書き
        </button>
      </div>
    {/each}
  </div>
</section>

<style>
  /* 高さは固定する(PartsPanel/ParametersPanelと同じ理由)。一覧は自分の中だけ
     overflow するので、増減しても `.sidebar` 全体の overflow-y:auto に吸収される
     (`.viewport` の寸法には触らない。App.svelteの`.sidebar`のdocstring参照)。 */
  .files-panel {
    display: flex;
    flex-direction: column;
    gap: 4px;
    height: 140px;
    flex: 0 0 auto;
    overflow: hidden;
    padding: 6px 8px;
    border-top: 1px solid var(--line);
  }

  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
  }

  .row .value {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .new-row {
    display: flex;
    gap: 4px;
    flex: 0 0 auto;
  }

  .new-row input {
    flex: 1 1 auto;
    min-width: 0;
  }

  .new-row button {
    flex: 0 0 auto;
    font-size: 11px;
  }

  .hint {
    font-size: 11px;
    color: var(--accent);
  }

  .list {
    flex: 1 1 auto;
    min-height: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .files-row {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .files-open {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    text-align: left;
    padding: 2px 4px;
    font-size: 12px;
  }

  .meta {
    flex: 0 0 auto;
    font-size: 11px;
  }

  .files-overwrite {
    flex: 0 0 auto;
    font-size: 11px;
    padding: 2px 4px;
  }

  .dim {
    color: var(--dim);
  }
</style>
