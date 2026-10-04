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
  /**
   * いまの文書のサムネイルを撮る(PNGのbase64。`Viewport.captureThumbnail`)。見えている立体が
   * 無ければ空文字、ビューポートがまだ無ければ null(撮らない)。
   */
  captureThumbnail(): string | null
  /**
   * サーバに繋がっているか。サムネイルは人が出していない裏の要求なので、繋がっていなければ
   * 送らない(送ると「繋がっていないので送れなかった」の通知が出る)。
   */
  connected(): boolean
}

/** 手元に持っているサムネイル。`url` が null なら「その版では絵が無い」と分かっている。 */
interface CachedThumbnail {
  version: number
  url: string | null
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
  /** そのファイルのサムネイルの `data:` URL(手元に無ければ null)。 */
  thumbnailUrl(name: string): string | null
  /**
   * 一覧の行が見えた。手元の絵が `version`(一覧の `thumb`)より古ければサーバへ取りに行く
   * (何度呼んでもよい)。
   */
  wantThumbnail(name: string, version: number | null): void
  /**
   * サムネイルの応答待ちを全部捨てる。**接続が切れたとき**と、**サムネイルの要求が
   * エラーで返ったとき**(エラーの封筒はどのファイルかを言わない)に App が呼ぶ。
   */
  dropThumbnailRequests(): void
}

/** 一覧の取り直しをまとめる待ち[ms]。 */
const REFRESH_DELAY_MS = 300
/**
 * 文書が変わってからサムネイルを撮るまでの待ち[ms]。続けて操作している間は撮らず、
 * 手が止まったら1回撮る(撮るたびにPNGを1枚送るので、1操作ごとには撮らない)。
 */
const CAPTURE_DELAY_MS = 1500

const PNG_DATA_URL = 'data:image/png;base64,'

export function createDocumentFiles(deps: DocumentFilesDeps): DocumentFiles {
  let lastFile = $state<FilePayload | null>(null)
  let list = $state<DocumentInfo[]>([])
  let currentName = $state<string | null>(null)
  /** 直前に出したファイル操作の番号(応答が `file` で返ったときの待ちを解く)。 */
  let lastOp: number | null = null
  // 自動保存のたびに一覧の更新日時(並び順)が変わり、無題の文書は最初の変更で新しい行が要る。
  // ただし `list_documents` は全ファイルのログを読むので、配信のたびに素で呼ばず**少し待って
  // まとめ、パネルを出しているときだけ**取りに行く(畳んでいる間は「古い」印だけ立てる)。
  let refreshTimer: ReturnType<typeof setTimeout> | null = null
  let stale = false
  let seenAppliedCount: number | null = null
  let seenLogLength: number | null = null

  // ---------------------------------------------------------------- サムネイル
  const thumbnails = $state<Record<string, CachedThumbnail>>({})
  /**
   * 取りに行って応答を待っているファイル名(同じ行が何度見えても1回だけ取る)。
   * **接続が切れたら空にする**(`dropThumbnailRequests`) —— 応答はもう来ないので、残すと
   * 再接続後もその行を二度と取りに行かない。
   */
  const fetching = new Set<string>()
  /**
   * 送ったが応答をまだ受けていない絵(応答の版と組にして手元へ置く。取り直さないため)。
   * 同じ名前で続けて送ることがあるので、名前ごとに送った順の列で持つ(応答は同じ接続を
   * 送った順に返る)。
   */
  const uploading = new Map<string, string[]>()
  let captureTimer: ReturnType<typeof setTimeout> | null = null
  /** 繋がっていなくて撮れなかった。次の状態配信(再接続の直後に必ず来る)で撮り直す。 */
  let captureMissed = false

  function scheduleCapture(): void {
    if (captureTimer !== null) clearTimeout(captureTimer)
    captureTimer = setTimeout(captureNow, CAPTURE_DELAY_MS)
  }

  /**
   * いまの文書を撮って預ける。**撮るのは今の画面の文書で、名前もいま編集中のもの**
   * (どちらも同じ状態配信から来ているので食い違わない)。無題でまだ書かれていない文書は
   * 預け先が無いので撮らない。
   */
  function captureNow(): void {
    if (captureTimer !== null) clearTimeout(captureTimer)
    captureTimer = null
    if (currentName === null) return
    if (!deps.connected()) {
      captureMissed = true
      return
    }
    const png = deps.captureThumbnail()
    if (png === null) return
    const queue = uploading.get(currentName) ?? []
    queue.push(png)
    uploading.set(currentName, queue)
    deps.runVerb('save_thumbnail', { name: currentName, png }, deps.nextOp())
  }

  /** 撮る予定が残っていれば今撮る(開く・新規で文書が差し替わる前に呼ぶ)。 */
  function flushCapture(): void {
    if (captureTimer !== null) captureNow()
  }

  function onThumbnail(payload: FilePayload): void {
    const name = payload.name
    if (name === undefined) return
    if (payload.action === 'save_thumbnail') {
      const queue = uploading.get(name)
      const png = queue?.shift()
      if (queue !== undefined && queue.length === 0) uploading.delete(name)
      if (!payload.saved) return
      // 消した(空文字を送った)ときは版が無い。手元の版は今にして、一覧に残っている
      // 古い版で取りに行かせない。
      thumbnails[name] = {
        version: payload.thumb ?? Date.now() / 1000,
        url: payload.thumb != null && png ? PNG_DATA_URL + png : null,
      }
      return
    }
    fetching.delete(name)
    thumbnails[name] = {
      version: payload.thumb ?? Date.now() / 1000,
      url: payload.png ? PNG_DATA_URL + payload.png : null,
    }
  }

  /** 改名・複製・削除を手元の絵へも映す(サーバの付随ファイルと同じ動き)。 */
  function followFileChange(payload: FilePayload): void {
    if (payload.name === undefined) return
    if (payload.action === 'rename' && payload.previous) {
      const moved = thumbnails[payload.previous]
      delete thumbnails[payload.previous]
      if (moved) thumbnails[payload.name] = moved
      else delete thumbnails[payload.name]
    } else if (payload.action === 'copy' && payload.source) {
      const copied = thumbnails[payload.source]
      if (copied) thumbnails[payload.name] = copied
      else delete thumbnails[payload.name]
    } else if (payload.action === 'delete') {
      delete thumbnails[payload.name]
    }
  }

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
      // **サムネイルの応答も同じ理由で抜ける**(撮るたび・行が見えるたびに裏で走る)。
      // 待ちの解除(`release`)にも触らない —— 人が出したファイル操作の待ちを、裏の応答で
      // 解いてしまわないように。
      if (payload.action === 'save_thumbnail' || payload.action === 'thumbnail') {
        onThumbnail(payload)
        return
      }
      lastFile = payload
      // **応答が `file` で返ったなら、その操作の待ちはここで解く。** 「文書を変えるか」の判定は
      // ブラウザとサーバで別々に持っているので、片方だけ直したときに状態配信を待ち続けて
      // 確定が永久に拒否される経路ができる。出した番号と一致するときだけ解く(取りすぎない)。
      deps.release(lastOp)
      // **どこへ書いたかを必ず画面へ出す** —— 書き出しはブラウザの中に痕跡が残らない
      // 操作なので、通知が唯一の確認手段になる。
      // `body_ids` が載るのは export_step / export_stl だけ。非表示で除外した個数は、0件なら
      // 言わない —— 「隠したつもりが無いのに減っている」を見えない驚きにしないための行。
      const exported = payload.body_ids !== undefined ? `${payload.body_ids.length}個` : ''
      const skippedCount = payload.skipped_hidden?.length ?? 0
      const skipped = skippedCount > 0 ? `※非表示の${skippedCount}個は除外` : ''
      const detail = [exported, skipped].filter((part) => part !== '').join(' ')
      deps.note(`${payload.action}: ${payload.path}${detail !== '' ? `(${detail})` : ''}`)
      // 上書きで更新日時(並び順)が変わり、改名では行の名前が、複製では行そのものが増え、
      // 削除では行が消える。
      followFileChange(payload)
      if (['save', 'rename', 'copy', 'delete'].includes(payload.action)) refresh()
    },
    onState(next) {
      currentName = next.current_name
      // **文書が変わったら一覧を取り直す**(自動保存の結果を一覧へ映す。issue #5)。verb と
      // 開く/新規は `applied_count` を、undo/redo は操作数(`log.length`)を動かす ——
      // **`applied_count` は undo/redo で動かない**ので両方を見る。経路(WS/MCP/最適化)を
      // 問わずここ1箇所で拾える。サムネイルも同じ時機に撮る(画面を開いた直後の1通目も
      // 撮る —— 撮ったことの無い既存の文書に、開いただけで絵が付くように)。
      const firstState = seenAppliedCount === null
      if (
        !firstState &&
        (next.applied_count !== seenAppliedCount || next.log.length !== seenLogLength)
      ) {
        scheduleRefresh()
        scheduleCapture()
      } else if (firstState || captureMissed) {
        scheduleCapture()
      }
      captureMissed = false
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
      flushCapture()
      deps.runVerb('open_document', { name }, deps.nextOp())
    },
    create() {
      flushCapture()
      deps.runVerb('new_document', {}, deps.nextOp())
    },
    exportStep(path) {
      run('export_step', { path })
    },
    exportStl(path) {
      run('export_stl', { path })
    },
    thumbnailUrl(name) {
      return thumbnails[name]?.url ?? null
    },
    wantThumbnail(name, version) {
      if (version === null || fetching.has(name) || !deps.connected()) return
      const cached = thumbnails[name]
      if (cached !== undefined && cached.version >= version) return
      fetching.add(name)
      deps.runVerb('get_thumbnail', { name }, deps.nextOp())
    },
    dropThumbnailRequests() {
      fetching.clear()
      uploading.clear()
    },
  }
}
