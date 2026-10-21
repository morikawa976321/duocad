/**
 * 干渉チェックの結果の持ち主(issue #186。verb ではない。文書もログも変えない)。
 *
 * - 隙間の欄は、文書を開いて最初の起動は 0、同じ文書を開いている間は直前の値、文書を切り替えたら 0(本人判断カード3)。
 * - 結果の寿命: 起動のたびに `client_op` を振り、その番号の返事だけを描く(続けて起動したときの古い応答を捨てる)。検査の間に文書の版が変わった・
 *   接続が切れた後に届いた結果も描かない。表示中の結果は文書の版が変わったら消す(古い赤い立体を残さない)。
 *   **選択と可視性が変わっても結果は消さない**(対象は開始時にサーバが確定している)。
 * - 行を選ぶとその組の重なりの立体・接触面・最短距離の線と札を出す。何も選んでいなければ重なりの立体だけを全部出す(カード1)。
 */
import { parseClearance } from './commands/interference'
import { acceptResult, groupPairs, visualFor, type Group } from './interferenceGroups'
import type { InterferencePayload } from './interferenceTypes'
import type { Viewport } from './viewport'

export interface Interference {
  readonly open: boolean
  readonly running: boolean
  clearanceText: string
  readonly result: InterferencePayload | null
  readonly groups: Group[]
  readonly selectedKey: string | null
  /** 結果を捨てたときなどの一言(パネルに出す)。 */
  readonly notice: string
  /** 起動する(パネルを開き、隙間の欄の値で検査を頼む)。 */
  run(): void
  onResult(payload: InterferencePayload): void
  /** サーバが検査の要求を断った(対象が足りない等)。自分の起動の失敗なら実行中を解く。 */
  onError(clientOp: number | null | undefined): void
  select(key: string | null): void
  close(): void
}

export function createInterference(deps: {
  send: (clearance: number, clientOp: number) => void
  nextOp: () => number
  /** 世代.適用回数.ログ本数(サーバの `document_revision` と同じ作り)。文書が無ければ null。 */
  revision: () => string | null
  /** 文書の世代(開く・新規・閉じるで進む)。 */
  generation: () => number | null
  connected: () => boolean
  /** 今のモード(`'idle' | 'sketch-pending' | 'sketch' | 'optimize'`)。変わったら結果を畳む。 */
  modeKind: () => string
  viewport: () => Viewport | null
  fail: (message: string) => void
}): Interference {
  let open = $state(false)
  let running = $state(false)
  let clearanceText = $state('0')
  let result = $state.raw<InterferencePayload | null>(null)
  let selectedKey = $state<string | null>(null)
  let notice = $state('')
  let currentOp: number | null = null
  let seenGeneration: number | null | undefined

  function drop(): void {
    result = null
    selectedKey = null
  }

  // 文書を切り替えたら、隙間の欄は 0 に戻し、パネルも結果も畳む。
  $effect(() => {
    const generation = deps.generation()
    if (seenGeneration !== undefined && generation !== seenGeneration) {
      clearanceText = '0'
      open = false
      running = false
      currentOp = null
      notice = ''
      drop()
    }
    seenGeneration = generation
  })

  // 表示中の結果は、文書の版が変わったら消す。
  $effect(() => {
    const revision = deps.revision()
    if (result !== null && result.document_revision !== revision) {
      drop()
      notice = '文書が変わったので結果を消した。もう一度検査する'
    }
  })

  // 繋がりが切れたら、返事を待たない。検査の途中で切れたら、前の結果(と赤い立体)は今回の検査の結果ではないので消して知らせる。
  $effect(() => {
    if (!deps.connected()) {
      if (running) {
        drop()
        notice = '検査の途中で接続が切れた。繋がり直したらもう一度検査する'
      }
      running = false
      currentOp = null
    }
  })

  // モードが変わったら畳む(スケッチ・形状最適化へ移ると文書の版は変わらないので、版の見張りでは消えない)。検査中なら返事も捨てる。
  let seenMode: string | undefined
  $effect(() => {
    const mode = deps.modeKind()
    if (seenMode !== undefined && mode !== seenMode) {
      open = false
      running = false
      currentOp = null
      notice = ''
      drop()
    }
    seenMode = mode
  })

  // ビューポートの層。結果と選んだ行だけで決まる。
  $effect(() => {
    // 先に読む(`?.` の短絡で依存を失わない)。
    const visual = visualFor(result, selectedKey)
    deps.viewport()?.showInterference(visual)
  })

  const groups = $derived(result === null ? [] : groupPairs(result.pairs ?? []))

  return {
    get open() {
      return open
    },
    get running() {
      return running
    },
    get clearanceText() {
      return clearanceText
    },
    set clearanceText(text: string) {
      clearanceText = text
    },
    get result() {
      return result
    },
    get groups() {
      return groups
    },
    get selectedKey() {
      return selectedKey
    },
    get notice() {
      return notice
    },
    run() {
      const parsed = parseClearance(clearanceText)
      if (!parsed.ok) {
        open = true
        deps.fail(parsed.reason)
        return
      }
      if (!deps.connected()) {
        deps.fail('サーバに繋がっていないので干渉チェックできない')
        return
      }
      open = true
      running = true
      notice = ''
      currentOp = deps.nextOp()
      deps.send(parsed.value, currentOp)
    },
    onResult(payload) {
      const verdict = acceptResult(payload, { currentOp, revision: deps.revision(), connected: deps.connected() })
      if (verdict === 'stale_request' || verdict === 'disconnected') return
      running = false
      currentOp = null
      if (verdict === 'stale_revision') {
        notice = '検査の間に文書が変わったので結果を捨てた。もう一度検査する'
        return
      }
      if (payload.error !== undefined) {
        drop()
        deps.fail(`干渉チェックに失敗した: ${payload.error}`)
        return
      }
      result = payload
      selectedKey = null
    },
    onError(clientOp) {
      if (clientOp === null || clientOp === undefined || clientOp !== currentOp) return
      running = false
      currentOp = null
    },
    select(key) {
      selectedKey = key === selectedKey ? null : key
    },
    close() {
      open = false
      running = false
      currentOp = null
      notice = ''
      drop()
    },
  }
}
