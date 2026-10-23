/**
 * ファイル操作(保存・開く・新規・改名・複製・削除・STEP/STL入出力)と `duocad/models/` の一覧の持ち主。
 * **状態はコアが持つ。** ここにあるのはサーバ応答の写しだけで、編集中のファイル名も保存状態も
 * 配信(`state.current_name` / `state.saved`)を映す(応答から拾い直すと真実源が2つになる)。
 * 応答待ち(`pendingOp`)は App の持ち物なので、番号の発行と待ちの出し入れは `deps` で借りる。
 */
import { changesDocument } from './commands/files'
import type { CommandRequest } from './generated/protocol'
import type { DocumentInfo, FilePayload, ServerState, Session } from './session'

export interface DocumentFilesDeps {
  sendCommand: Session['sendCommand']
  nextOp(): number
  hold(op: number): void
  release(op: number | null): boolean
  note(message: string): void
  panelOpen(): boolean
  /** PNGのbase64。見えている立体が無ければ空文字、ビューポートがまだ無ければ null(撮らない)。 */
  captureThumbnail(): string | null
  /** 繋がっていなければサムネイルは送らない(裏の要求で、送ると「送れなかった」の通知が出る)。 */
  connected(): boolean
}

/** 手元に持っているサムネイル。`url` が null なら「その版では絵が無い」と分かっている。 */
interface CachedThumbnail {
  version: number
  url: string | null
}

export interface DocumentFiles {
  readonly list: DocumentInfo[]
  /** いま編集しているファイル名(無題なら null)。 */
  readonly currentName: string | null
  onFile(payload: FilePayload): void
  onState(next: ServerState): void
  refresh(): void
  /** 文書を変えるものだけ応答待ちにする(`import_step`)。書き出しを待ちにすると `file` の応答まで Enter が拒否される。 */
  run(...request: CommandRequest): void
  /**
   * 今の文書を手で書き直す(Ctrl+S)。自動保存が書けなかった(`#save-state`)ときに人が押し直すためにある。
   * 引数は送らない(書き込み先はサーバの `Session._current_name`)。無題で未変更なら何も送らない。
   */
  save(): void
  /**
   * 改名(issue #5)。既存名は送っても通らずサーバが拒否する(`Session.rename_document`)。
   * `list` による実在チェックは足さない: パス解釈の正を2箇所に持たない(`FilesPanel.nameProblem`)。
   */
  rename(name: string): void
  /** 拒否はサーバが言う(issue #17)。 */
  copy(source: string, dest: string): void
  remove(name: string): void
  /** 応答は `state`(`applied.change === 'document'`)で来る。**確認を挟まない。** */
  open(name: string): void
  /** **確認を挟まない。** */
  create(): void
  close(): void
  exportStep(path: string): void
  exportStl(path: string): void
  thumbnailUrl(name: string): string | null
  /** 手元の絵が `version`(一覧の `thumb`)より古ければサーバへ取りに行く。何度呼んでもよい。 */
  wantThumbnail(name: string, version: number | null): void
  /**
   * サムネイルの応答待ちを全部捨てる。接続が切れたときと、サムネイルの要求がエラーで返ったとき
   * (エラーの封筒はどのファイルかを言わない)に App が呼ぶ。
   */
  dropThumbnailRequests(): void
}

const REFRESH_DELAY_MS = 300
/** 文書が変わってからサムネイルを撮るまでの待ち[ms]。操作が続く間は撮らず、手が止まったら1回撮る。 */
const CAPTURE_DELAY_MS = 500

const PNG_DATA_URL = 'data:image/png;base64,'

export function createDocumentFiles(deps: DocumentFilesDeps): DocumentFiles {
  let list = $state<DocumentInfo[]>([])
  let currentName = $state<string | null>(null)
  let lastOp: number | null = null
  // list_documents は全ファイルのログを読むので、配信のたびには呼ばず少し待ってまとめ、
  // パネルを出しているときだけ取りに行く(畳んでいる間は「古い」印だけ立てる)。
  let refreshTimer: ReturnType<typeof setTimeout> | null = null
  let stale = false
  let seenAppliedCount: number | null = null
  let seenLogLength: number | null = null

  const thumbnails = $state<Record<string, CachedThumbnail>>({})
  /**
   * 取りに行って応答待ちのファイル名(同じ行が何度見えても1回だけ取る)。接続が切れたら空にする
   * (`dropThumbnailRequests`): 応答はもう来ないので、残すと再接続後もその行を取りに行かない。
   */
  const fetching = new Set<string>()
  /** 送ったが応答をまだ受けていない絵。同名を続けて送りうるので、名前ごとに送信順の列で持つ(応答は送った順に返る)。 */
  const uploading = new Map<string, string[]>()
  let captureTimer: ReturnType<typeof setTimeout> | null = null
  /** 繋がっていなくて撮れなかった。次の状態配信(再接続の直後に必ず来る)で撮り直す。 */
  let captureMissed = false

  function scheduleCapture(): void {
    if (captureTimer !== null) clearTimeout(captureTimer)
    captureTimer = setTimeout(captureNow, CAPTURE_DELAY_MS)
  }

  /**
   * 撮るのは今の画面の文書で、名前もいま編集中のもの(同じ状態配信から来るので食い違わない)。
   * 無題でまだ書かれていない文書は預け先が無いので撮らない。
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
    deps.sendCommand('save_thumbnail', { name: currentName, png }, deps.nextOp())
  }

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
    deps.sendCommand('list_documents', {}, deps.nextOp())
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

  $effect(() => {
    if (deps.panelOpen() && stale) {
      stale = false
      refresh()
    }
  })

  function run(...request: CommandRequest): void {
    const op = deps.nextOp()
    lastOp = op
    if (changesDocument(request[0])) deps.hold(op)
    deps.sendCommand(...[...request, op])
  }

  return {
    get list() {
      return list
    },
    get currentName() {
      return currentName
    },
    onFile(payload) {
      // **`list_documents` の応答は通知を出さずここで抜ける**(保存/開くのたびに裏で走る)。
      if (payload.action === 'list') {
        list = payload.files ?? []
        return
      }
      // **サムネイルの応答も同じ理由で抜ける。** 人が出したファイル操作の待ちを裏の応答で解かないよう、`release` にも触らない。
      if (payload.action === 'save_thumbnail' || payload.action === 'thumbnail') {
        onThumbnail(payload)
        return
      }
      // **応答が `file` で返ったなら、その操作の待ちはここで解く。** 「文書を変えるか」の判定はブラウザと
      // サーバで別々に持つので、片方だけ直すと状態配信を待ち続けて確定が永久に拒否される。番号が一致するときだけ解く。
      deps.release(lastOp)
      // `body_ids` が載るのは export_step / export_stl だけ。非表示で除外した個数は0件なら言わない。
      const exported = payload.body_ids !== undefined ? `${payload.body_ids.length}個` : ''
      const skippedCount = payload.skipped_hidden?.length ?? 0
      const skipped = skippedCount > 0 ? `※非表示の${skippedCount}個は除外` : ''
      const detail = [exported, skipped].filter((part) => part !== '').join(' ')
      deps.note(`${payload.action}: ${payload.path}${detail !== '' ? `(${detail})` : ''}`)
      followFileChange(payload)
      if (['save', 'rename', 'copy', 'delete'].includes(payload.action)) refresh()
    },
    onState(next) {
      currentName = next.current_name
      // **文書が変わったら一覧を取り直す**(issue #5)。verb と開く/新規は `applied_count` を、undo/redo は
      // `log.length` を動かす。**`applied_count` は undo/redo で動かない**ので両方を見る。サムネイルも同じ時機に撮る
      // (画面を開いた直後の1通目も撮る: 撮ったことの無い既存文書に、開いただけで絵が付く)。
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
      if (name === currentName && captureTimer !== null) {
        clearTimeout(captureTimer)
        captureTimer = null
      }
      run('delete_document', { name })
    },
    open(name) {
      flushCapture()
      deps.sendCommand('open_document', { name }, deps.nextOp())
    },
    create() {
      flushCapture()
      deps.sendCommand('new_document', {}, deps.nextOp())
    },
    close() {
      flushCapture()
      deps.sendCommand('close_document', {}, deps.nextOp())
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
      deps.sendCommand('get_thumbnail', { name }, deps.nextOp())
    },
    dropThumbnailRequests() {
      fetching.clear()
      uploading.clear()
    },
  }
}
