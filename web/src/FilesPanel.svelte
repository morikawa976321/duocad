<script lang="ts">
  /**
   * ファイルパネル — **保存先が固定フォルダになったことをUIに落とす場所**。
   * `App.svelte` の**`.toolbar`(コマンドのツールバー)に `CommandHost` と並んで載る**
   * (2026-08-23。保存機能だけをサイドバーで特別扱いする必然性が無いという本人裁定で、
   * 右サイドバー→左サイドバー(データパネル)を経て、ここへ丸ごと統合した)。
   *
   * **`duocad/models/` の中身を一覧で見せて、クリック1つで開く**。`BrowserPanel.svelte`
   * と同じ作りで、**状態はコアが持つ**(`documents` はサーバの
   * `list_documents` が返した一覧の写しで、ここでは何も覚えない)。保存・開く・一覧はどれも
   * `App.svelte` が `session.runVerb(...)` で直接送る(`ParametersPanel.svelte` の
   * `onSetParameter` と同じ形。`CommandHost` は経由しない —— 一覧からのクリックは「対象を
   * 選ぶ」そのものであって、引数収集の状態機械を通す理由が無い)。
   *
   * **ブラウザのネイティブファイルダイアログ(File System Access API)は使わない**。実際に
   * ファイルを書いているのはサーバ側のPythonなので、ブラウザがファイルハンドルを持っても
   * 噛み合わない。
   *
   * **開くのに確認ダイアログを挟まない**。上書きも同様 —— 保存はそもそも「今の文書をそこへ
   * 書く」操作であり、一覧からの上書きだけ確認を挟むと**新規保存と挙動が割れる**。
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
   * 打った名前が使える形か(空・区切り文字・`.`/`..`・コロン・前後の空白・`.json` 必須)。
   *
   * **最終判定はサーバ**(`Session._resolve_document_name` の `is_relative_to` 検査)。
   * ここでの検査は明らかに拒まれる形を早く言うための入力補助でしかなく、実在チェックはしない
   * (サーバと2箇所で別々のパス解釈を持たない)。
   */
  function nameProblem(text: string): string | null {
    const trimmed = text.trim()
    if (trimmed === '') return 'ファイル名を打つ(例: model.json)'
    if (trimmed !== text) return '前後の空白は使えない'
    if (trimmed.includes('/') || trimmed.includes('\\')) return '区切り文字は使えない(ファイル名だけ)'
    if (trimmed.includes(':')) return "':' は使えない(ドライブ指定・代替ストリームの入口になる)"
    if (trimmed === '.' || trimmed === '..') return '使えないファイル名'
    if (!trimmed.toLowerCase().endsWith('.json')) return '拡張子は.json(一覧はmodels/の*.jsonだけを見る)'
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
    <span class="label">ファイル</span>
    <span id="current-document" class="value dim">{currentName ?? '(未保存)'}</span>
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
    {#if draftName !== '' && draftProblem !== null}
      <span class="hint">{draftProblem}</span>
    {/if}
  </div>

  <!-- **一覧は横スクロールの帯**(旧・縦list)。ツールバーの帯に収める都合で、
       右サイドバーにあった頃の縦長レイアウトから変えた(挙動は同じ)。 -->
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
  /* ツールバーの帯に収める横並びレイアウト(旧・右/左サイドバーの縦積みから変更)。
     `.list` だけ横スクロールにして、ファイルが増えても `.toolbar` の高さ(ひいては
     `.viewport` の寸法)には触らない。 */
  .files-panel {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding-bottom: 8px;
    margin-bottom: 8px;
    border-bottom: 1px solid var(--line);
  }

  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    /* .list(ファイル一覧)との区切り線を`.row::after`で描く土台。position:absoluteの
       疑似要素は通常フローの高さに数えられないので、`.files-panel`のgap(6px)の中に線を
       描いても`.toolbar`ひいてはビューポートの実寸(styles.css冒頭の不変条件)は動かない。
       border/paddingで区切ると、この行の高さが直接ビューポートを押し縮める。 */
    position: relative;
  }

  .row::after {
    content: '';
    position: absolute;
    left: 0;
    right: 0;
    bottom: -3px;
    height: 1px;
    background: var(--line);
  }

  .label {
    flex: 0 0 auto;
    color: var(--dim);
    font-size: 12px;
  }

  .row .value {
    flex: 0 1 auto;
    min-width: 0;
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  #file-name-input {
    flex: 0 0 180px;
    min-width: 0;
  }

  .row button {
    flex: 0 0 auto;
    font-size: 11px;
  }

  .hint {
    font-size: 11px;
    color: var(--accent);
  }

  /* 一覧はチップの横列。増えたらこの帯の中だけで横スクロールする。 */
  .list {
    display: flex;
    flex-direction: row;
    flex-wrap: nowrap;
    gap: 6px;
    overflow-x: auto;
    padding-bottom: 2px;
  }

  .files-row {
    display: flex;
    align-items: center;
    gap: 6px;
    flex: 0 0 auto;
    border: 1px solid var(--line);
    padding: 2px 6px;
    white-space: nowrap;
  }

  .files-open {
    flex: 0 0 auto;
    max-width: 160px;
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
    white-space: nowrap;
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
