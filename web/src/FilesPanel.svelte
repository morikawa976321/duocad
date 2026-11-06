<script lang="ts">
  /**
   * ファイルパネル — `App.svelte` の左サイドバー(`.data-panel`)の中身。`duocad/models/` の一覧を見せ、クリック1つで開く。
   * `BrowserPanel.svelte` と同じ作りで状態はコアが持つ(`documents` は `list_documents` の写し)。保存・開く・一覧は
   * `documentFiles.svelte.ts` が `session.sendCommand(...)` で直接送る(`CommandHost` は経由しない: 一覧のクリックは
   * 「対象を選ぶ」そのもので、引数収集の状態機械を通す理由が無い)。
   * ブラウザのネイティブファイルダイアログ(File System Access API)は使わない: ファイルを書くのはサーバ側のPythonで、ブラウザがハンドルを持っても噛み合わない。
   *
   * 保存は1操作ごとに自動(issue #5、本人判断 2026-09-16)。保存ボタンは無く、名前欄はいま編集しているファイルの改名(`rename_document`。
   * 無題は最初の変更で `untitled-<n>.json` が付く)。別名で複製したければ行の「複製」。上書きの口はどこにも無い:
   * 既存名への改名はサーバが拒否する(操作ログが唯一の正なので、潰したファイルは復旧できない)。
   * 開くにも「新規」(`onNew`)にも確認を挟まない(名前つきの文書は自動保存済みで失うものが無く、一覧のクリック1つで移れるのに新規だけ確認すると挙動が割れる)。
   *
   * 一覧の1行は正方形のサムネイル・ファイル名・作成日時・更新日時(本人指示 2026-10-04)。サムネイルから日時までが1つの「開く」ボタンで、
   * 右端に複製・削除が縦に並ぶ。サムネイルは行が見えたときに `onNeedThumbnail` で取りに行かせる(撮るのは `documentFiles.svelte.ts`)。
   * 作成日時を持たない既存ファイルは「—」(docs/UI.md)。
   *
   * 複製・削除(issue #17)はディスク上のファイルの整理で、いま編集している文書は変わらない。複製先は行の直下の欄に `<元の名前>-copy` を
   * 先埋めして Enter で確定(既にあればサーバが拒否)。削除は2段階(「削除」→ 行の直下に「本当に削除」。モーダルは出さない):
   * 「開くのに確認を挟まない」の根拠は「消えるのは未保存の編集だけ」で、ディスクの保存済みファイルには当たらない(本人判断 2026-09-16)。
   * 編集中のファイルも削除でき、消すと文書は閉じてトップ画面へ戻る(`Session.delete_document`)ので確認の文言でそれを言う。
   *
   * 一覧の下は書き出し(STEP・STL)だけ。文書を変えない(操作ログに載らず undo の対象でもない)ので、ここでよい。
   * STEP/SVG 読み込みは文書を変えるverbなのでここには無い(`commands/files.ts` の `IMPORT_STEP`、`commands/sketch.ts` の `IMPORT_SVG`)。
   *
   * 名前とパスの欄は拡張子を打たせない(`SuffixField.svelte`。`.json` / `.step` / `.stl` は欄の右に固定で、送るときに付ける。本人指示 2026-10-07)。
   * 書き出しのパス欄は送信後も消さず、既定は今の文書のファイル名。文書が替わったら(`generation` が進んだら)その文書の名前に入れ直す
   * (前の文書の名前が残ると、別の文書の書き出しが前のファイルを上書きする)。
   * 改名欄と複製欄は今の名前(複製は `<元の名前>-copy`)を先埋めして選んだ状態で出し、Esc で取り消す。送っても欄は消さず、
   * 断られたら打った名前が残る(改名は `currentName` が変わったら、複製は成功の応答 `lastCopied` が来たら閉じる/埋め直す)。
   */
  import { tick, untrack } from 'svelte'
  import { pathLike, unquotePath } from './commands/command'
  import { isImeKey } from './keys'
  import type { DocumentInfo } from './session'
  import SuffixField, { withSuffix, withoutSuffix } from './SuffixField.svelte'

  let {
    documents,
    currentName,
    documentOpen,
    onRename,
    onOpen,
    onNew,
    onClose,
    onCopy,
    onDelete,
    onExportStep,
    onExportStl,
    thumbnailUrl,
    onNeedThumbnail,
    lastCopied,
    generation,
  }: {
    documents: DocumentInfo[]
    /** サーバの `state.current_name` の写し(無ければ `null`)。名前は文字で出さず(ヘッダの `#current-file` と二重になる)、一覧の当該行を光らせる(`BrowserPanel.svelte` の `.sketch-row.active` と同じ形)。 */
    currentName: string | null
    /** 文書を開いているか。閉じている(トップ画面)間は、今の文書に向けた口(閉じる・改名・書き出し)を出さない。 */
    documentOpen: boolean
    /** いま編集しているファイルを改名する(無題なら「名前を付ける」)。既存名を渡してもサーバが拒否する。 */
    onRename: (name: string) => void
    onOpen: (name: string) => void
    onNew: () => void
    onClose: () => void
    onCopy: (source: string, dest: string) => void
    /** 編集中のファイルなら、消した後で文書が閉じる。 */
    onDelete: (name: string) => void
    onExportStep: (path: string) => void
    /** 粗さは固定で選べない。 */
    onExportStl: (path: string) => void
    /** 無ければ null(仮の絵を出す)。 */
    thumbnailUrl: (name: string) => string | null
    onNeedThumbnail: (name: string, version: number | null) => void
    /** 最後に成功した複製(`documentFiles.lastCopied`)。成功のたびに別のオブジェクトになる。 */
    lastCopied: { name: string } | null
    /** 文書の世代(`documentFiles.generation`)。改名では進まない。 */
    generation: number
  } = $props()

  function currentStem(): string {
    return currentName === null ? '' : withoutSuffix(currentName, '.json')
  }

  let draftName = $state('')
  $effect.pre(() => {
    draftName = currentStem()
  })

  /** `duocad/document_name.py` の `MAX_NAME_UNITS`(ファイル名の上限255から、書き込みが足す `.png.writing` の12を引いた値)の写し。 */
  const MAX_NAME_UNITS = 243

  /**
   * 打った名前(拡張子 `.json` は欄の外で付く)が使える形か。最終判定はサーバ(`Session._resolve_document_name` の `is_relative_to` 検査と
   * `duocad/document_name.py` の文字・長さの検査)で、ここは明らかに拒まれる形を早く言う入力補助。実在・既存名は見ない: `documents` は `list_documents` を最後に呼んだ時点の写しでしかなく、
   * 正はサーバの `rename_document`(拒否は `onError` -> `fail()` のトースト1枚。新しい通知経路を作らない)。
   */
  function nameProblem(text: string): string | null {
    const trimmed = text.trim()
    if (trimmed === '') return 'ファイル名を打つ(例: model)'
    if (trimmed.includes('/') || trimmed.includes('\\')) return '区切り文字は使えない(ファイル名だけ)'
    if (trimmed.includes(':')) return "':' は使えない(ドライブ指定・代替ストリームの入口になる)"
    const forbidden = [...trimmed].find((char) => '<>"|?*'.includes(char) || char.charCodeAt(0) < 0x20)
    if (forbidden !== undefined) {
      return forbidden.charCodeAt(0) < 0x20 ? '制御文字は使えない' : `'${forbidden}' は使えない(Windowsのファイル名に使えない文字)`
    }
    if (trimmed === '.' || trimmed === '..') return '使えないファイル名'
    if (withoutSuffix(trimmed, '.json') === '') return 'ファイル名を打つ(例: model)'
    if (withSuffix(trimmed, '.json').length > MAX_NAME_UNITS) {
      return `名前が長すぎる(拡張子込みで${MAX_NAME_UNITS}文字まで。保存の一時ファイルの名前が上限を超える)`
    }
    return null
  }

  const draftProblem = $derived(nameProblem(draftName))
  // 先埋めとの文字列の一致も見る: `part.json.json` の先埋め `part.json` は `withSuffix` すると別名になる。
  const draftUnchanged = $derived(
    currentName !== null && (draftName === currentStem() || withSuffix(draftName, '.json') === currentName),
  )

  function submitDraft(): void {
    if (draftProblem !== null || draftUnchanged) return
    onRename(withSuffix(draftName, '.json'))
  }

  function onDraftKey(event: KeyboardEvent): void {
    if (event.key === 'Escape' && !isImeKey(event)) {
      event.preventDefault()
      const field = event.currentTarget as HTMLInputElement
      draftName = currentStem()
      void tick().then(() => field.select())
      return
    }
    onRowEnter(event, submitDraft)
  }

  function formatStamp(epochSeconds: number | null): string {
    if (epochSeconds === null) return '—'
    const date = new Date(epochSeconds * 1000)
    const pad = (value: number) => String(value).padStart(2, '0')
    return (
      `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}`
    )
  }

  const sorted = $derived([...documents].sort((a, b) => b.mtime - a.mtime))

  /**
   * 行が一覧の見える範囲(+少し先)に入ったらサムネイルを求め、見えている間に版が上がったら求め直す(`update`)。
   * 引数は値で受ける: 関数を渡すと Svelte は中の `file.thumb` を読まないので、版が上がっても `update` が呼ばれない(svelte 5.56 のコンパイル結果で確認)。
   */
  function whenVisible(node: HTMLElement, wanted: { name: string; thumb: number | null }) {
    let visible = false
    let current = wanted
    const observer = new IntersectionObserver(
      (entries) => {
        visible = entries.some((entry) => entry.isIntersecting)
        if (visible) onNeedThumbnail(current.name, current.thumb)
      },
      { root: node.closest('.list'), rootMargin: '200px 0px' },
    )
    observer.observe(node)
    return {
      update(next: { name: string; thumb: number | null }) {
        current = next
        if (visible) onNeedThumbnail(current.name, current.thumb)
      },
      destroy() {
        observer.disconnect()
      },
    }
  }

  /** 複製の名前を打っている元の行。削除の確認中の行と同時には立たない。 */
  let copyingFrom = $state<string | null>(null)
  let copyName = $state('')
  let deletingName = $state<string | null>(null)
  /** 送って応答を待っている複製先と、送った時点の `lastCopied`。それより後に同じ名前の成功が来たら欄を閉じる。 */
  let copySent = $state<{ dest: string; before: { name: string } | null } | null>(null)

  $effect(() => {
    if (copySent !== null && lastCopied !== copySent.before && lastCopied?.name === copySent.dest) cancelRowActions()
  })

  /** 複製先の先埋め: `<元の名前>-copy`(既にあれば `-copy2` …)。手元の一覧で見るだけで、存在の正はサーバ。 */
  function suggestCopyName(source: string): string {
    const stem = withoutSuffix(source, '.json')
    const taken = new Set(documents.map((file) => file.name))
    let candidate = `${stem}-copy`
    for (let n = 2; taken.has(`${candidate}.json`); n += 1) candidate = `${stem}-copy${n}`
    return candidate
  }

  function startCopy(source: string): void {
    deletingName = null
    copySent = null
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
    const dest = withSuffix(copyName, '.json')
    copySent = { dest, before: lastCopied }
    onCopy(copyingFrom, dest)
  }

  function cancelRowActions(): void {
    copyingFrom = null
    copyName = ''
    copySent = null
    deletingName = null
  }

  function startDelete(name: string): void {
    copyingFrom = null
    copyName = ''
    copySent = null
    deletingName = name
    // 既定は「やめる」: 押し間違えの Enter で消えない。
    void tick().then(() => document.querySelector<HTMLButtonElement>('.confirm-row .files-row-cancel')?.focus())
  }

  /** 確認行のボタンで押した Esc は確認を閉じるだけにする(窓の Esc は選択と起動中のコマンドまで畳む)。 */
  function onConfirmKey(event: KeyboardEvent): void {
    if (event.key !== 'Escape' || isImeKey(event)) return
    event.preventDefault()
    event.stopPropagation()
    cancelRowActions()
  }

  function confirmDelete(): void {
    if (deletingName === null) return
    onDelete(deletingName)
    deletingName = null
  }

  /**
   * 空と、ファイル名の無いパス(`sub/` は拡張子を付けると `sub/.step` になる)だけを弾く(`nameProblem` は掛けない)。
   * 相対パスはサーバが `export/` 基準に解決する(保存の `models/` とは別の置き場)。
   */
  const pathProblem = pathLike('パス')

  function exportPathProblem(text: string, suffix: string): string | null {
    const empty = pathProblem(text)
    if (empty !== null) return empty
    const fileName = withoutSuffix(unquotePath(text), suffix).split(/[\\/]/).pop() ?? ''
    return fileName === '' ? 'ファイル名まで打つ' : null
  }

  let exportStepPath = $state('')
  /** 使えない値のまま Enter を押した。欄が空でも理由を出す。 */
  let exportStepAsked = $state(false)
  const exportStepProblem = $derived(exportPathProblem(exportStepPath, '.step'))

  function submitExportStep(): void {
    if (exportStepProblem !== null) {
      exportStepAsked = true
      return
    }
    onExportStep(withSuffix(unquotePath(exportStepPath), '.step'))
  }

  let exportStlPath = $state('')
  let exportStlAsked = $state(false)
  const exportStlProblem = $derived(exportPathProblem(exportStlPath, '.stl'))

  function submitExportStl(): void {
    if (exportStlProblem !== null) {
      exportStlAsked = true
      return
    }
    onExportStl(withSuffix(unquotePath(exportStlPath), '.stl'))
  }

  let seenGeneration: number | null = null
  let defaultStem = ''
  // 欄の既定は今の文書のファイル名。文書が替わったら入れ直し、同じ文書の中では名前が付く・変わるときも、
  // 既定のまま触っていない欄だけ追わせる(打った名前は残す)。
  $effect.pre(() => {
    const stem = currentStem()
    const changed = seenGeneration === null || generation !== seenGeneration
    untrack(() => {
      if (changed || exportStepPath === defaultStem) exportStepPath = stem
      if (changed || exportStlPath === defaultStem) exportStlPath = stem
      if (changed) {
        exportStepAsked = false
        exportStlAsked = false
      }
    })
    seenGeneration = generation
    defaultStem = stem
  })

  function onRowEnter(event: KeyboardEvent, submit: () => void): void {
    if (event.key !== 'Enter' || isImeKey(event)) return
    event.preventDefault()
    submit()
  }
</script>

<section class="files-panel">
  <div class="row">
    <button
      id="file-new"
      onclick={onNew}
      title="空の無題の文書から始める(今の文書はファイルに残る。「未保存」の札が出ているときは先に書き直し、書けなければ切り替えない)"
    >
      新規
    </button>
    {#if documentOpen}
      <button id="file-close" onclick={onClose} title="文書を閉じてトップ画面へ戻る(ファイルに残る。「未保存」の札が出ているときは先に書き直し、書けなければ閉じない)">
        閉じる
      </button>
    {/if}
  </div>

  {#if documentOpen}
    <div class="new-row">
      <SuffixField
        id="file-name-input"
        suffix=".json"
        placeholder={currentName === null ? 'model' : withoutSuffix(currentName, '.json')}
        bind:value={draftName}
        selectOnFocus
        onkeydown={onDraftKey}
      />
      <button id="file-rename" onclick={submitDraft} disabled={draftProblem !== null || draftUnchanged}>
        {currentName === null ? '名前を付ける' : '名前を変える'}
      </button>
    </div>
    {#if draftName !== '' && draftProblem !== null}
      <span class="hint">{draftProblem}</span>
    {/if}
  {/if}

  <div class="list" id="files-list">
    {#if sorted.length === 0}
      <span class="value empty dim">models/ にファイルが無い</span>
    {/if}
    {#each sorted as file (file.name)}
      <!-- `active` は実機検査が行の印として読む。見た目は `row-active`(styles.css)。 -->
      {@const url = thumbnailUrl(file.name)}
      <div
        class="files-row"
        class:active={file.name === currentName}
        class:row-active={file.name === currentName}
        use:whenVisible={{ name: file.name, thumb: file.thumb }}
      >
        <!-- 開いている行は押せない(本人判断 2026-11-05、issue #275)。押すと開き直しになり、選択と起動中のコマンドが飛ぶ。 -->
        <button
          class="files-open"
          disabled={file.name === currentName}
          onclick={() => onOpen(file.name)}
          title={file.name === currentName ? `${file.name} を編集中` : `${file.name} を開く`}
        >
          <span class="files-thumb">
            {#if url !== null}
              <img src={url} alt="" draggable="false" />
            {:else}
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path
                  d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3zM4 7.5l8 4.5 8-4.5M12 12v9"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.2"
                  stroke-linejoin="round"
                />
              </svg>
            {/if}
          </span>
          <span class="files-text">
            <span class="files-name">{file.name}</span>
            <span class="files-date">作成 {formatStamp(file.created)}</span>
            <span class="files-date">更新 {formatStamp(file.mtime)}</span>
          </span>
        </button>
        <span class="files-actions">
          <button
            class="row-action"
            onclick={() => startCopy(file.name)}
            title="別名で複製"
            aria-label="{file.name} を別名で複製"
          >
            <svg class="row-icon" viewBox="0 0 16 16" aria-hidden="true">
              <rect x="5" y="5" width="9" height="9" fill="none" stroke="currentColor" stroke-width="1.5" />
              <path d="M3 11V3h8" fill="none" stroke="currentColor" stroke-width="1.5" />
            </svg>
          </button>
          <button
            class="row-action"
            onclick={() => startDelete(file.name)}
            title="ディスクから削除"
            aria-label="{file.name} をディスクから削除"
          >
            <svg class="row-icon" viewBox="0 0 16 16" aria-hidden="true">
              <path
                d="M2.5 4h11M6 4V2.5h4V4M4 4l.7 9.5h6.6L12 4M6.5 6.5v5M9.5 6.5v5"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linejoin="round"
              />
            </svg>
          </button>
        </span>
      </div>
      {#if deletingName === file.name}
        <div class="new-row confirm-row">
          <!-- 幅240pxの行に収まる長さにする(長いと末尾が切れて「戻せない」が読めない)。 -->
          <span class="confirm-text" title={file.name === currentName ? '開いている文書。消すと閉じて戻せない' : undefined}>
            {file.name === currentName ? '閉じて消す' : '消すと戻せない'}
          </span>
          <button
            class="files-delete-confirm"
            onclick={confirmDelete}
            onkeydown={onConfirmKey}
            title="ディスクから消す(戻せない)"
          >
            本当に削除
          </button>
          <button class="files-row-cancel" onclick={cancelRowActions} onkeydown={onConfirmKey}>やめる</button>
        </div>
      {/if}
      {#if copyingFrom === file.name}
        <div class="new-row copy-row">
          <SuffixField
            id="file-copy-name"
            suffix=".json"
            bind:value={copyName}
            selectOnFocus
            onkeydown={(event) => {
              if (event.key === 'Escape' && !isImeKey(event)) {
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

  {#if documentOpen}
    <div class="io-section">
      <div class="new-row">
        <SuffixField
          id="file-export-step-path"
          suffix=".step"
          placeholder="part"
          bind:value={exportStepPath}
          onkeydown={(event) => onRowEnter(event, submitExportStep)}
        />
        <button
          id="file-export-step"
          onclick={submitExportStep}
          disabled={exportStepProblem !== null}
          title={exportStepProblem ?? undefined}
        >
          STEP書き出し
        </button>
      </div>
      {#if (exportStepPath !== '' || exportStepAsked) && exportStepProblem !== null}
        <span class="hint">{exportStepProblem}</span>
      {/if}

      <div class="new-row">
        <SuffixField
          id="file-export-stl-path"
          suffix=".stl"
          placeholder="part"
          bind:value={exportStlPath}
          onkeydown={(event) => onRowEnter(event, submitExportStl)}
        />
        <button
          id="file-export-stl"
          onclick={submitExportStl}
          disabled={exportStlProblem !== null}
          title={exportStlProblem ?? undefined}
        >
          STL書き出し
        </button>
      </div>
      {#if (exportStlPath !== '' || exportStlAsked) && exportStlProblem !== null}
        <span class="hint">{exportStlProblem}</span>
      {/if}
    </div>
  {/if}
</section>

<style>
  /* 左サイドバーの高さを受け取り、余った分は `.list` が持つ。溢れるのは `.list` の中だけなので `.viewport` の寸法には触らない。 */
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

  .new-row button {
    flex: 0 0 auto;
  }

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
    align-items: stretch;
    gap: 2px;
  }

  .files-open {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 3px 4px;
    text-align: left;
    background: none;
  }

  button.files-open:not(:disabled):hover {
    background: var(--control-bg);
  }

  /* 押せない理由は「今開いている」ことで、行の印(`row-active`)が既に言っている。薄めると開いている文書が沈んで見える。 */
  button.files-open:disabled {
    color: inherit;
    background: none;
    cursor: default;
  }

  .files-thumb {
    flex: 0 0 auto;
    width: 67px;
    height: 67px;
    display: grid;
    place-items: center;
    background: var(--bg);
    border: 1px solid var(--line-soft);
  }

  .files-thumb img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
  }

  .files-thumb svg {
    width: 24px;
    height: 24px;
    color: var(--dim-2);
  }

  .files-text {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .files-name,
  .files-date {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .files-date {
    font-size: var(--fs-meta);
    color: var(--dim);
    font-variant-numeric: tabular-nums;
  }

  .files-actions {
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 2px;
  }

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

  .row-action {
    display: inline-flex;
    align-items: center;
    padding: 2px 3px;
  }

  .row-icon {
    display: block;
    width: 14px;
    height: 14px;
    pointer-events: none;
  }

  .files-delete-confirm {
    color: var(--accent);
    border-color: var(--accent);
  }

  .copy-row,
  .confirm-row {
    padding-left: 12px;
  }

  .confirm-row {
    align-items: center;
  }

  .confirm-text {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--fs-meta);
    color: var(--dim);
  }

  /* `.list` は伸縮する(flex:1)のでスクロールを持つ。この区切りから下は縮まない(flex:0 0 auto)。 */
  .io-section {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 0 0 auto;
    border-top: 1px solid var(--line);
    padding-top: 6px;
  }
</style>
