<script lang="ts">
  /**
   * ファイルパネル — **保存先が固定フォルダになったことをUIに落とす場所**。
   * `App.svelte` の**左サイドバー(`.data-panel`)の中身はこれ1枚だけ**である
   * (Fusion 360のデータパネルに対応する位置)。
   *
   * **`duocad/models/` の中身を一覧で見せて、クリック1つで開く**。`BrowserPanel.svelte`
   * と同じ作りで、**状態はコアが持つ**(`documents` はサーバの
   * `list_documents` が返した一覧の写しで、ここでは何も覚えない)。保存・開く・一覧はどれも
   * `App.svelte` が `session.runVerb(...)` で直接送る(`CommandHost` は経由しない ——
   * 一覧からのクリックは「対象を選ぶ」そのものであって、引数収集の状態機械を通す理由が無い)。
   *
   * **ブラウザのネイティブファイルダイアログ(File System Access API)は使わない**。実際に
   * ファイルを書いているのはサーバ側のPythonなので、ブラウザがファイルハンドルを持っても
   * 噛み合わない。
   *
   * **保存は1操作ごとに自動で走る**(issue #5、本人判断 2026-09-16)。このパネルに保存ボタンは
   * 無く、名前欄は**いま編集しているファイルの改名**(`rename_document`)である —— 無題の
   * 文書は最初の変更で `untitled-<n>.json` が自動で付くので、人が決めるのは名前だけでよい。
   * 別名で複製したければ一覧の行の「複製」(下記)。**上書きの口はどこにも無い**: A を
   * 編集中に B の名前へ改名しようとすればサーバが拒否する(操作ログが唯一の正である以上、
   * 潰したファイルは復旧できない)。
   *
   * **開くのに確認ダイアログを挟まない**。**「新規」も同じ**(`onNew`) —— 一覧の
   * クリック1つで別の文書へ移れるのに、空の文書へ移るときだけ確認を挟むと挙動が割れる。
   * 名前つきの文書は自動保存されていて失うものが無く、無題の文書も最初の変更で名前が付く。
   *
   * **一覧の行からは、開く・別名で複製・削除ができる**(issue #17)。複製と削除はディスク上の
   * ファイルの整理であって、いま編集している文書は変わらない(`open_document` とは別の操作)。
   * 複製先の名前は行内の欄に `<元の名前>-copy.json` を先埋めして Enter で確定する(打ち直せる。
   * 既にあればサーバが拒否する)。**削除は行内の2段階**(「削除」→ 同じ行が「本当に削除」に
   * 変わる。モーダルは出さない) —— 「開くのに確認を挟まない」の根拠は「消えるのは未保存の
   * 編集だから」であり、ディスクの保存済みファイルには当たらない(本人判断 2026-09-16)。
   * 編集中のファイルの削除ボタンは**押せなくしない** —— 押せばサーバが理由つきで断る
   * (押せない札より理由を言う)。
   *
   * **一覧の下に置くのは書き出しの2つだけ**(STEP書き出し・STL書き出し)。どちらも
   * 保存/開くと同じ形で `App.svelte` が `session.runVerb(...)` を直接呼ぶ
   * (`CommandHost` を経由しない)。**文書を変えないから**ここでよい ——
   * 操作ログに載らず、undoの対象でもなく、モデルは1ミリも動かない。
   *
   * **STEP読み込み(`import_step`)はここには無い**(理由は `commands/files.ts` の
   * `IMPORT_STEP` のdocstring) —— あれだけは文書を変えるverbで、ファイルの出し入れでは
   * なくモデリングの1手である。**SVG読み込み(`import_svg`)も同じ理由でここには無い**
   * (`commands/sketch.ts` の `IMPORT_SVG` を見よ)。
   *
   * **送信後もパス欄は消さない**(ファイル名欄=`draftName`とは逆の規律)。STEP/STLのパスは
   * 同じ値へ書き出し直す・同じファイルを読み直すことが多く、毎回打ち直させると手間になる
   * ——「名前を変える」が消すのは1回きりの新しい名前だからで、性質が違う。
   */
  import { tick } from 'svelte'
  import { pathLike } from './commands/command'
  import type { DocumentInfo } from './session'

  let {
    documents,
    currentName,
    locked,
    onRename,
    onOpen,
    onNew,
    onCopy,
    onDelete,
    onExportStep,
    onExportStl,
  }: {
    /** `duocad/models/` の中身(サーバの `list_documents` の写し)。 */
    documents: DocumentInfo[]
    /**
     * **いま編集しているファイル名**(サーバの `state.current_name` の写し。無ければ `null`)。
     *
     * ヘッダの `#current-file` と同じ値で、ヘッダの保存ボタンが書く先そのものである。
     * **このパネルは名前を文字で出さない**(同じ文字列をヘッダと2度出さないため)。
     * 代わりに**一覧の中の当該行を光らせる**:
     * 「どれを開いているか」は一覧を見るときの問いなので、答えは一覧の中にあるべきで、
     * `BrowserPanel.svelte` が編集中のスケッチを `.sketch-row.active` で示すのと同じ形。
     */
    currentName: string | null
    /**
     * **文書を差し替える口(開く・新規)を止める**(形状最適化の実行中)。走っている最適化は
     * 開始時の文書へ結果を足すので、差し替えると結果が別の文書へ入る(サーバも断る)。
     * 改名・書き出しは文書を変えないので止めない。
     */
    locked: boolean
    /**
     * **いま編集しているファイルを改名する**(名前欄。無題なら「名前を付ける」)。
     *
     * **既存のファイル名を渡してもサーバが拒否する** —— 上書きの口はどこにも無い
     * (冒頭のdocstring参照)。
     */
    onRename: (name: string) => void
    /** ファイル名を渡して開く。 */
    onOpen: (name: string) => void
    /** 空の文書にする(引数なし。ファイルはまだ1つも決まっていない)。 */
    onNew: () => void
    /** `source` を `dest` へ別名で複製する(ディスクのJSONを写すだけ。文書は変わらない)。 */
    onCopy: (source: string, dest: string) => void
    /** ディスクから削除する(編集中のファイルはサーバが拒否する)。 */
    onDelete: (name: string) => void
    /** 書き出し先のパスを渡してSTEPへ書き出す(文書は変えない)。 */
    onExportStep: (path: string) => void
    /** 書き出し先のパスを渡してSTLへ書き出す(文書は変えない)。**粗さは固定**で選べない。 */
    onExportStl: (path: string) => void
  } = $props()

  /** 打ちかけの新しい名前(改名)。送ったら空に戻す(パネルは打ちかけ以外の状態を覚えない)。 */
  let draftName = $state('')

  /**
   * 打った名前が使える形か(空・区切り文字・`.`/`..`・コロン・前後の空白・`.json` 必須)。
   *
   * **最終判定はサーバ**(`Session._resolve_document_name` の `is_relative_to` 検査)。
   * ここでの検査は明らかに拒まれる形を早く言うための入力補助でしかなく、実在チェックはしない
   * (サーバと2箇所で別々のパス解釈を持たない)。
   *
   * **既存名かどうかもここでは見ない**。`documents` は手元にあるが、
   * それは `list_documents` を最後に呼んだ時点の写しでしかなく、「その名前にできるか」の正は
   * サーバ側の `rename_document` にある。打った名前が既にあればサーバが拒否し、
   * `onError` -> `fail()` でトースト1枚になる —— **新しい通知経路を作らない**。
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
    onRename(draftName.trim())
    draftName = ''
  }

  function formatSize(bytes: number): string {
    const kb = bytes / 1024
    const maximumFractionDigits = kb >= 1 ? 0 : 1
    return `${kb.toLocaleString(undefined, { maximumFractionDigits })} KB`
  }

  function formatTime(epochSeconds: number): string {
    return new Date(epochSeconds * 1000).toLocaleString()
  }

  const sorted = $derived([...documents].sort((a, b) => b.mtime - a.mtime))

  // ---------------------------------------------------------------- 複製・削除(行内)
  /** 複製の名前を打っている元の行(無ければ null)。削除の確認中の行と同時には立たない。 */
  let copyingFrom = $state<string | null>(null)
  let copyName = $state('')
  /** 「本当に削除」を出している行(無ければ null)。 */
  let deletingName = $state<string | null>(null)

  /**
   * 複製先の先埋め: `<元の名前>-copy.json`。既にあれば `-copy2` `-copy3` …
   * (手元の一覧で見るだけ。**存在の正はサーバ**で、打ち直して衝突すれば拒否される)。
   */
  function suggestCopyName(source: string): string {
    const stem = source.toLowerCase().endsWith('.json') ? source.slice(0, -5) : source
    const taken = new Set(documents.map((file) => file.name))
    let candidate = `${stem}-copy.json`
    for (let n = 2; taken.has(candidate); n += 1) candidate = `${stem}-copy${n}.json`
    return candidate
  }

  function startCopy(source: string): void {
    deletingName = null
    copyingFrom = source
    copyName = suggestCopyName(source)
    // 欄はこの後のDOM更新で現れる(Svelteの更新は非同期)。
    void tick().then(() => {
      const field = document.querySelector<HTMLInputElement>('#file-copy-name')
      field?.focus()
      field?.select()
    })
  }

  const copyProblem = $derived(nameProblem(copyName))

  function submitCopy(): void {
    if (copyingFrom === null || copyProblem !== null) return
    onCopy(copyingFrom, copyName.trim())
    copyingFrom = null
    copyName = ''
  }

  function cancelRowActions(): void {
    copyingFrom = null
    copyName = ''
    deletingName = null
  }

  function startDelete(name: string): void {
    copyingFrom = null
    copyName = ''
    deletingName = name
  }

  function confirmDelete(): void {
    if (deletingName === null) return
    onDelete(deletingName)
    deletingName = null
  }

  // ---------------------------------------------------------------- STEP/STL(入出力)
  /**
   * 空だけを弾く(**ファイル名の検査`nameProblem`は掛けない**)。STEP/STLは`models/`配下に
   * 閉じない任意パスで、`commands/files.ts`のdocstringが定めたとおり解釈はサーバに任せる。
   */
  const pathProblem = pathLike('パス')

  let exportStepPath = $state('')
  const exportStepProblem = $derived(pathProblem(exportStepPath))

  function submitExportStep(): void {
    if (exportStepProblem !== null) return
    onExportStep(exportStepPath.trim())
  }

  let exportStlPath = $state('')
  const exportStlProblem = $derived(pathProblem(exportStlPath))

  function submitExportStl(): void {
    if (exportStlProblem !== null) return
    onExportStl(exportStlPath.trim())
  }

  /** Enterで、その行のボタンと同じことをする。 */
  function onRowEnter(event: KeyboardEvent, submit: () => void): void {
    if (event.key !== 'Enter') return
    event.preventDefault()
    submit()
  }
</script>

<section class="files-panel">
  <!-- **見出しも編集中のファイル名も置かない** —— 「ファイル」の語はヘッダの開閉ボタン
       (`#data-panel-toggle`)が既に言っている。編集中のファイルは**文字ではなく
       一覧の行の強調**(下の `.files-row.active`)で示す(冒頭のdocstring参照)。 -->
  <div class="row">
    <!-- **新規は一覧の外にある** —— 開くのが「一覧のどれかを指す」操作なのに対し、こちらは
         指す先が無い(空の文書にする)。名前を打つ欄より前に置いて、ファイル名の入力と
         「新規保存」の組から離してある。 -->
    <button
      id="file-new"
      onclick={onNew}
      disabled={locked}
      title={locked
        ? '形状最適化の実行中は文書を差し替えられない'
        : '今の文書を捨てて空から始める(保存していない編集は消える)'}
    >
      新規
    </button>
  </div>

  <!-- 名前欄 = **いま編集しているファイルの改名**(冒頭のdocstring)。無題なら「名前を付ける」。
       置き場所の欄は空のまま、いまの名前を薄く見せる。 -->
  <div class="new-row">
    <input
      id="file-name-input"
      type="text"
      autocomplete="off"
      placeholder={currentName ?? 'model.json'}
      value={draftName}
      oninput={(event) => (draftName = (event.target as HTMLInputElement).value)}
      onkeydown={(event) => onRowEnter(event, submitDraft)}
    />
    <button id="file-rename" onclick={submitDraft} disabled={draftProblem !== null}>
      {currentName === null ? '名前を付ける' : '名前を変える'}
    </button>
  </div>
  {#if draftName !== '' && draftProblem !== null}
    <span class="hint">{draftProblem}</span>
  {/if}

  <div class="list" id="files-list">
    {#if sorted.length === 0}
      <span class="value empty dim">models/ にファイルが無い</span>
    {/if}
    {#each sorted as file (file.name)}
      <!-- **編集中の1行を光らせる**(`BrowserPanel` の `.sketch-row.active` と同じ形)。 -->
      <div class="files-row" class:active={file.name === currentName} data-name={file.name}>
        <button
          class="files-open"
          onclick={() => onOpen(file.name)}
          disabled={locked}
          title={locked ? '形状最適化の実行中は文書を差し替えられない' : 'クリックで開く'}
        >
          {file.name}
        </button>
        <span class="meta dim" title={formatTime(file.mtime)}>
          {file.operations ?? '?'}件 / {formatSize(file.size)}
        </span>
        <!-- **行に「上書き」ボタンは無い**(冒頭のdocstring)。上書きの口はどこにも無く、
             保存は自動で走る。 -->
        {#if deletingName === file.name}
          <!-- 削除の2段目。同じ行の中で聞く(モーダルは出さない。冒頭のdocstring)。 -->
          <button class="files-delete-confirm" onclick={confirmDelete} title="ディスクから消す(戻せない)">
            本当に削除
          </button>
          <button class="files-row-cancel" onclick={cancelRowActions}>やめる</button>
        {:else}
          <button class="files-copy row-action" onclick={() => startCopy(file.name)} title="別名で複製">
            複製
          </button>
          <button class="files-delete row-action" onclick={() => startDelete(file.name)} title="ディスクから削除">
            削除
          </button>
        {/if}
      </div>
      {#if copyingFrom === file.name}
        <!-- 複製先の名前。先埋めしてあるので Enter だけで通る(打ち直してもよい)。 -->
        <div class="new-row copy-row" data-copy-of={file.name}>
          <input
            id="file-copy-name"
            type="text"
            autocomplete="off"
            spellcheck="false"
            value={copyName}
            oninput={(event) => (copyName = (event.target as HTMLInputElement).value)}
            onkeydown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault()
                cancelRowActions()
                return
              }
              onRowEnter(event, submitCopy)
            }}
          />
          <button id="file-copy-confirm" onclick={submitCopy} disabled={copyProblem !== null}>複製</button>
          <button class="files-row-cancel" onclick={cancelRowActions}>やめる</button>
        </div>
        {#if copyName !== '' && copyProblem !== null}
          <span class="hint">{copyProblem}</span>
        {/if}
      {/if}
    {/each}
  </div>

  <!-- STEP/STLの**書き出し**。一覧の下に区切りを置いて積む(読み込みは含まない ——
       冒頭のdocstring)。「入力欄+ボタン」の見た目は上の新規保存の行と揃えてある。 -->
  <div class="io-section">
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
      <button id="file-export-stl" onclick={submitExportStl} disabled={exportStlProblem !== null}>
        STL書き出し
      </button>
    </div>
    {#if exportStlPath !== '' && exportStlProblem !== null}
      <span class="hint">{exportStlProblem}</span>
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

  .row button {
    flex: 0 0 auto;
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
  }

  /* 大きさは styles.css の `.hint` が持つ(--fs-meta)。ここは色だけ —— 打った値が
     通らない理由なので、他の添え物と違って琥珀で言う。 */
  .hint {
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

  /* いま開いているファイル(`BrowserPanel` の `.sketch-row.active` と同じ見せ方)。 */
  .files-row.active {
    background: var(--accent-soft);
  }

  /* **一覧の行はボタンの面を持たない**。押せるものは面で見せる(styles.css の
     `button`)のが既定だが、それは「行の中に置かれた操作子」の話で、**行そのものが
     押せる**ときに面を敷くと、ファイルが20個あれば面が20枚並ぶ。ブラウザ側の
     `.sketch-name` と同じ「素のまま、hoverでだけ光る」形に揃える。 */
  .files-open {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    text-align: left;
    padding: 2px 4px;
    background: none;
  }

  .files-open:hover {
    background: var(--control-bg);
    border-color: transparent;
  }

  /* 行内の操作子(複製・削除・やめる)。面は持たず、大きさは添え物(`--fs-meta`)に揃える。 */
  .row-action,
  .files-row-cancel,
  .files-delete-confirm {
    flex: 0 0 auto;
    padding: 1px 4px;
    font-size: var(--fs-meta);
    background: none;
    color: var(--dim);
    border: 1px solid transparent;
  }

  .row-action:hover,
  .files-row-cancel:hover {
    color: var(--text);
  }

  /* 2段目の「本当に削除」だけは目立たせる(戻せない操作なので)。 */
  .files-delete-confirm {
    color: var(--accent);
    border-color: var(--accent);
  }

  .copy-row {
    padding-left: 12px;
  }

  .meta {
    flex: 0 0 auto;
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
