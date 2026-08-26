<script lang="ts">
  /**
   * ファイルパネル — **保存先が固定フォルダになったことをUIに落とす場所**。
   * `App.svelte` の**左サイドバー(`.data-panel`)の中身はこれ1枚だけ**である
   * (Fusion 360のデータパネルに対応する位置。2026-08-23にいったんツールバーへ統合したが、
   * 2026-08-26に本人が左サイドバーへの復元を決定した)。
   *
   * **`duocad/models/` の中身を一覧で見せて、クリック1つで開く**。`BrowserPanel.svelte`
   * と同じ作りで、**状態はコアが持つ**(`documents` はサーバの
   * `list_documents` が返した一覧の写しで、ここでは何も覚えない)。保存・開く・一覧はどれも
   * `App.svelte` が `session.runVerb(...)` で直接送る(`SketchPanel.svelte` の
   * `onSetParameter` と同じ形。`CommandHost` は経由しない —— 一覧からのクリックは「対象を
   * 選ぶ」そのものであって、引数収集の状態機械を通す理由が無い)。
   *
   * **ブラウザのネイティブファイルダイアログ(File System Access API)は使わない**。実際に
   * ファイルを書いているのはサーバ側のPythonなので、ブラウザがファイルハンドルを持っても
   * 噛み合わない。
   *
   * **開くのに確認ダイアログを挟まない**。上書きも同様 —— 保存はそもそも「今の文書をそこへ
   * 書く」操作であり、一覧からの上書きだけ確認を挟むと**新規保存と挙動が割れる**。
   * **「新規」も同じ**(`onNew`) —— 一覧のクリック1つで別の文書へ移れるのに、空の文書へ
   * 移るときだけ確認を挟むと挙動が割れる。どちらも保存していない編集を捨てる点は同じである。
   */
  import type { DocumentInfo } from './session'

  let {
    documents,
    currentName,
    onSave,
    onOpen,
    onNew,
  }: {
    /** `duocad/models/` の中身(サーバの `list_documents` の写し)。 */
    documents: DocumentInfo[]
    /** 直近に保存/開いたファイル名(`App.svelte` が save/open の応答から追う)。 */
    currentName: string | null
    /** ファイル名を渡して保存する(新規保存も上書きも同じ verb)。 */
    onSave: (name: string) => void
    /** ファイル名を渡して開く。 */
    onOpen: (name: string) => void
    /** 空の文書にする(引数なし。ファイルはまだ1つも決まっていない)。 */
    onNew: () => void
  } = $props()

  /** 打ちかけの新規保存名。送ったら空に戻す(SketchPanelの`editing`と同じ規律)。 */
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
    <!-- **新規は一覧の外にある** —— 開くのが「一覧のどれかを指す」操作なのに対し、こちらは
         指す先が無い(空の文書にする)。名前を打つ欄より前に置いて、ファイル名の入力と
         「新規保存」の組から離してある。 -->
    <button id="file-new" onclick={onNew} title="今の文書を捨てて空から始める(保存していない編集は消える)">
      新規
    </button>
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
  /* 左サイドバーの高さを丸ごと受け取り、余った分は `.list` が持つ
     (自分の高さを固定していた頃の 140px は、右サイドバーで他のパネルと
     縦に並んでいたときの都合であって、専用の枠に移った今は要らない)。
     溢れるのは `.list` の中だけなので、ファイルが増えても `.viewport` の寸法には触らない。 */
  .files-panel {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 1 1 auto;
    min-height: 0;
    overflow: hidden;
    padding: 6px 8px;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
  }

  .label {
    flex: 0 0 auto;
    color: var(--dim);
    font-size: 12px;
  }

  .row .value {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .row button {
    flex: 0 0 auto;
    font-size: 11px;
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
