/**
 * ファイル操作(保存・開く・新規・改名・複製・削除・STEP/STL入出力)と `duocad/models/` の一覧の持ち主。
 *
 * **状態はコアが持つ。** ここにあるのはサーバの応答の写しだけで、編集中のファイル名も保存状態も
 * 配信(`state.current_name` / `state.saved`)を映す。応答から拾い直すと真実源が2つになる。
 *
 * 応答待ち(`pendingOp`)は App の持ち物なので、番号の発行と待ちの出し入れは `deps` で借りる。
 */
import { changesDocument } from './commands/files'
import type { DocumentInfo, FilePayload, ServerState } from './session'

export interface DocumentFilesDeps {
  runVerb(verb: string, args: Record<string, unknown>, op: number): void
  /** この画面が出した操作の番号を1つ取る。 */
  nextOp(): number
  /** その番号の応答が来るまで、次の確定を止める。 */
  hold(op: number): void
  /** その番号を待っていたなら待ちを解く。 */
  release(op: number | null): boolean
  note(message: string): void
  /** ファイル一覧のパネルを出しているか。 */
  panelOpen(): boolean
}

export interface DocumentFiles {
  /** `list_documents` の写し(`FilesPanel.svelte` が描く)。 */
  readonly list: DocumentInfo[]
  /** いま編集しているファイル名(無題なら null)。 */
  readonly currentName: string | null
  /** 直前に書いたファイル。書き出しは他に痕跡が残らない。 */
  readonly lastFile: FilePayload | null
  /** `file` の応答が届いた(ファイルを書いた / 一覧が届いた)。 */
  onFile(payload: FilePayload): void
  /** 状態配信が届いた。 */
  onState(next: ServerState): void
  /** 一覧をサーバへ取り直しに行く。 */
  refresh(): void
  /**
   * verb を1つ送る。**文書を変えるものだけ応答待ちにする**(`import_step`)。書き出しを待ちに
   * すると、`file` の応答が来るまで Enter が拒否される。
   */
  run(verb: string, args: Record<string, unknown>): void
  /**
   * **今の文書を手で書き直す**(Ctrl+S)。保存は自動で済んでいるので普段は何も変わらない ——
   * 自動保存が書けなかった(`#save-state` が出ている)ときに人が押し直すためにある。引数を
   * 送らない(書き込み先を決めるのはサーバの `Session._current_name`)。無題で何も変えて
   * いない文書は書くものが無いので何も送らない。
   */
  save(): void
  /**
   * いま編集しているファイルを改名する(issue #5)。**既存名は送っても通らない** —— サーバが
   * 拒否してトースト1枚になる(`Session.rename_document`)。ここで `list` を見た実在チェックは
   * 足さない: パス解釈の正を2箇所に持たない(`FilesPanel.nameProblem` のdocstring)。
   */
  rename(name: string): void
  /** 一覧の行から別名で複製する / ディスクから削除する(issue #17)。拒否はサーバが言う。 */
  copy(source: string, dest: string): void
  remove(name: string): void
  /** 開く。応答は `state`(`applied.change === 'document'`)で来る。**確認を挟まない。** */
  open(name: string): void
  /** 空の文書にする。応答は開くのと同じ形(`action: "new"`)。**確認を挟まない。** */
  create(): void
  exportStep(path: string): void
  exportStl(path: string): void
}

/** KB 表示。一覧の行(`FilesPanel.svelte`)とファイル応答の通知が同じ式を使う。 */
export function formatSize(bytes: number): string {
  const kb = bytes / 1024
  const maximumFractionDigits = kb >= 1 ? 0 : 1
  return `${kb.toLocaleString(undefined, { maximumFractionDigits })} KB`
}

/** 一覧の取り直しをまとめる待ち[ms]。 */
const REFRESH_DELAY_MS = 300

export function createDocumentFiles(deps: DocumentFilesDeps): DocumentFiles {
  let lastFile = $state<FilePayload | null>(null)
  let list = $state<DocumentInfo[]>([])
  let currentName = $state<string | null>(null)
  /** 直前に出したファイル操作の番号(応答が `file` で返ったときの待ちを解く)。 */
  let lastOp: number | null = null
  // 自動保存のたびに一覧の操作数・サイズが変わり、無題の文書は最初の変更で新しい行が要る。
  // ただし `list_documents` は全ファイルのログを読むので、配信のたびに素で呼ばず**少し待って
  // まとめ、パネルを出しているときだけ**取りに行く(畳んでいる間は「古い」印だけ立てる)。
  let refreshTimer: ReturnType<typeof setTimeout> | null = null
  let stale = false
  let seenAppliedCount: number | null = null
  let seenLogLength: number | null = null

  function refresh(): void {
    deps.runVerb('list_documents', {}, deps.nextOp())
  }

  function scheduleRefresh(): void {
    if (!deps.panelOpen()) {
      stale = true
      return
    }
    if (refreshTimer !== null) clearTimeout(refreshTimer)
    refreshTimer = setTimeout(() => {
      refreshTimer = null
      refresh()
    }, REFRESH_DELAY_MS)
  }

  // パネルを畳んでいる間に文書が変わっていたら、開いたときに1回取り直す。
  $effect(() => {
    if (deps.panelOpen() && stale) {
      stale = false
      refresh()
    }
  })

  function run(verb: string, args: Record<string, unknown>): void {
    const op = deps.nextOp()
    lastOp = op
    if (changesDocument(verb)) deps.hold(op)
    deps.runVerb(verb, args, op)
  }

  return {
    get list() {
      return list
    },
    get currentName() {
      return currentName
    },
    get lastFile() {
      return lastFile
    },
    onFile(payload) {
      // **`list_documents` の応答はここで抜ける。** 一覧の取得は保存/開くのたびに黙って裏で
      // 走るので、他のファイル操作と同じ通知を出すと、取り直すたびに無意味な行が積まれる。
      if (payload.action === 'list') {
        list = payload.files ?? []
        return
      }
      lastFile = payload
      // **応答が `file` で返ったなら、その操作の待ちはここで解く。** 「文書を変えるか」の判定は
      // ブラウザとサーバで別々に持っているので、片方だけ直したときに状態配信を待ち続けて
      // 確定が永久に拒否される経路ができる。出した番号と一致するときだけ解く(取りすぎない)。
      deps.release(lastOp)
      // **どこへ何バイト書いたかを必ず画面へ出す** —— 書き出しはブラウザの中に痕跡が残らない
      // 操作なので、通知が唯一の確認手段になる。
      const size = formatSize(payload.bytes)
      const extra = payload.operations !== undefined ? ` / 操作 ${payload.operations}件` : ''
      // `body_ids` が載るのは export_step / export_stl だけ。非表示で除外した個数は、0件なら
      // 言わない —— 「隠したつもりが無いのに減っている」を見えない驚きにしないための行。
      const exported = payload.body_ids !== undefined ? ` / ${payload.body_ids.length}個` : ''
      const skippedCount = payload.skipped_hidden?.length ?? 0
      const skipped = skippedCount > 0 ? ` ※非表示の${skippedCount}個は除外` : ''
      deps.note(`${payload.action}: ${payload.path}(${size}${extra}${exported}${skipped})`)
      // 上書きで更新日時・サイズ・操作数が変わり、改名では行の名前が、複製では行そのものが増え、
      // 削除では行が消える。
      if (['save', 'rename', 'copy', 'delete'].includes(payload.action)) refresh()
    },
    onState(next) {
      currentName = next.current_name
      // **文書が変わったら一覧を取り直す**(自動保存の結果を一覧へ映す。issue #5)。verb と
      // 開く/新規は `applied_count` を、undo/redo は操作数(`log.length`)を動かす ——
      // **`applied_count` は undo/redo で動かない**ので両方を見る。経路(WS/MCP/最適化)を
      // 問わずここ1箇所で拾える。
      if (
        seenAppliedCount !== null &&
        (next.applied_count !== seenAppliedCount || next.log.length !== seenLogLength)
      ) {
        scheduleRefresh()
      }
      seenAppliedCount = next.applied_count
      seenLogLength = next.log.length
    },
    refresh,
    run,
    save() {
      if (currentName === null) return
      run('save_document', {})
    },
    rename(name) {
      run('rename_document', { name })
    },
    copy(source, dest) {
      run('copy_document', { source, dest })
    },
    remove(name) {
      run('delete_document', { name })
    },
    open(name) {
      deps.runVerb('open_document', { name }, deps.nextOp())
    },
    create() {
      deps.runVerb('new_document', {}, deps.nextOp())
    },
    exportStep(path) {
      run('export_step', { path })
    },
    exportStl(path) {
      run('export_stl', { path })
    },
  }
}
