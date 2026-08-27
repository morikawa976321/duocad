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
   *
   * **STEP読み込み・STEP書き出し・STL書き出しも一覧の下に置く**(2026-08-27に本人が決定。
   * それまでツールバーの `CommandHost` に残っていた)。この3つも保存/開くと同じ形で
   * `App.svelte` が `session.runVerb(...)` を直接呼ぶ(`CommandHost` を経由しない)。
   * **SVG読み込み(`import_svg`)はここには無い**(2026-08-28) —— 「開いているスケッチへ
   * 取り込む」形になり、スケッチモードのコマンド(`commands/sketch.ts` の `IMPORT_SVG`)へ
   * 移った。`commands/files.ts` の `CommandDef` はもう1つも無い。
   *
   * **送信後もパス欄は消さない**(ファイル名欄=`draftName`とは逆の規律)。STEP/STLのパスは
   * 同じ値へ書き出し直す・同じファイルを読み直すことが多く、毎回打ち直させると手間になる
   * ——「新規保存」が消すのは1回きりの新しい名前だからで、性質が違う。
   */
  import type { DocumentInfo } from './session'

  let {
    documents,
    currentName,
    onSave,
    onOpen,
    onNew,
    onImportStep,
    onExportStep,
    onExportStl,
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
    /** STEPのパスを渡して読み込む(文書を変える。編集可能なボディが増える)。 */
    onImportStep: (path: string) => void
    /** 書き出し先のパスを渡してSTEPへ書き出す(文書は変えない)。 */
    onExportStep: (path: string) => void
    /** 書き出し先のパスと偏差(mm)を渡してSTLへ書き出す(文書は変えない)。 */
    onExportStl: (path: string, deviation: number) => void
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

  // ---------------------------------------------------------------- STEP/STL(入出力)
  /**
   * 空だけを弾く(**ファイル名の検査`nameProblem`は掛けない**)。STEP/STLは`models/`配下に
   * 閉じない任意パスで、`commands/files.ts`のdocstringが定めたとおり解釈はサーバに任せる。
   */
  function pathProblem(text: string): string | null {
    return text.trim() === '' ? 'パスを打つ' : null
  }

  let importPath = $state('')
  const importProblem = $derived(pathProblem(importPath))

  function submitImportStep(): void {
    if (importProblem !== null) return
    onImportStep(importPath.trim())
  }

  let exportStepPath = $state('')
  const exportStepProblem = $derived(pathProblem(exportStepPath))

  function submitExportStep(): void {
    if (exportStepProblem !== null) return
    onExportStep(exportStepPath.trim())
  }

  let exportStlPath = $state('')
  let deviationText = $state('')
  const exportStlPathProblem = $derived(pathProblem(exportStlPath))

  /**
   * 偏差(mm)。**既定値を埋めない**。0以下(空欄含む)は無効 —— 用途で桁が変わる値なので、
   * 黙って既定値で出すと粗いままプリンタへ送られる(`commands/files.ts`の`EXPORT_STL`
   * docstring参照)。
   */
  const deviationProblem = $derived(
    Number.parseFloat(deviationText.trim()) > 0 ? null : '偏差は0より大きい値',
  )
  const exportStlProblem = $derived(exportStlPathProblem ?? deviationProblem)

  function submitExportStl(): void {
    if (exportStlProblem !== null) return
    onExportStl(exportStlPath.trim(), Number.parseFloat(deviationText.trim()))
  }

  /** Enterで、その行のボタンと同じことをする(`#file-name-input`と同じ規律)。 */
  function onRowEnter(event: KeyboardEvent, submit: () => void): void {
    if (event.key !== 'Enter') return
    event.preventDefault()
    submit()
  }
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

  <!-- STEP/STLの入出力。一覧の下に区切りを置いて積む(2026-08-27に本人がツールバーから
       ここへ寄せると決定)。「入力欄+ボタン」の見た目は上の新規保存の行と揃えてある。 -->
  <div class="io-section">
    <div class="new-row">
      <input
        id="file-import-path"
        type="text"
        autocomplete="off"
        spellcheck="false"
        placeholder="part.step"
        value={importPath}
        oninput={(event) => (importPath = (event.target as HTMLInputElement).value)}
        onkeydown={(event) => onRowEnter(event, submitImportStep)}
      />
      <button id="file-import-step" onclick={submitImportStep} disabled={importProblem !== null}>
        STEP読み込み
      </button>
    </div>
    {#if importPath !== '' && importProblem !== null}
      <span class="hint">{importProblem}</span>
    {/if}

    <div class="new-row">
      <input
        id="file-export-step-path"
        type="text"
        autocomplete="off"
        spellcheck="false"
        placeholder="out.step"
        value={exportStepPath}
        oninput={(event) => (exportStepPath = (event.target as HTMLInputElement).value)}
        onkeydown={(event) => onRowEnter(event, submitExportStep)}
      />
      <button id="file-export-step" onclick={submitExportStep} disabled={exportStepProblem !== null}>
        STEP書き出し
      </button>
    </div>
    {#if exportStepPath !== '' && exportStepProblem !== null}
      <span class="hint">{exportStepProblem}</span>
    {/if}

    <div class="new-row">
      <input
        id="file-export-stl-path"
        type="text"
        autocomplete="off"
        spellcheck="false"
        placeholder="out.stl"
        value={exportStlPath}
        oninput={(event) => (exportStlPath = (event.target as HTMLInputElement).value)}
        onkeydown={(event) => onRowEnter(event, submitExportStl)}
      />
      <input
        id="file-deviation"
        class="deviation"
        type="text"
        inputmode="decimal"
        autocomplete="off"
        placeholder="mm"
        value={deviationText}
        oninput={(event) => (deviationText = (event.target as HTMLInputElement).value)}
        onkeydown={(event) => onRowEnter(event, submitExportStl)}
      />
      <button id="file-export-stl" onclick={submitExportStl} disabled={exportStlProblem !== null}>
        STL書き出し
      </button>
    </div>
    {#if exportStlPath !== '' && exportStlPathProblem !== null}
      <span class="hint">{exportStlPathProblem}</span>
    {/if}
    {#if deviationText !== '' && deviationProblem !== null}
      <span class="hint">{deviationProblem}</span>
    {/if}
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

  .new-row input.deviation {
    flex: 0 0 56px;
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

  /* `.list`は伸縮する(flex:1)のでスクロールを持つ。この区切りから下は
     `.new-row`(ファイル名の行)と同じく縮まない(flex:0 0 auto)。 */
  .io-section {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 0 0 auto;
    border-top: 1px solid var(--line);
    padding-top: 6px;
  }
</style>
