/**
 * 干渉チェックの結果を画面に出す形へ直す純関数(issue #186。Svelte に依存しない。`tests/eval_interference.mjs` が評価する)。
 *
 * 見出しは4つ(本人判断カード4): 干渉・隙間不足・未確定は開き、接触は畳む。微小な重なり(tiny_overlap)は「接触」の中に
 * 印付きの行として入る。隙間不足には**離れている組だけ**を出す(触れている組はそれぞれの見出しの中で「隙間不足」の印)。
 */
import type { InterferencePair, InterferencePayload, InterferenceVisual } from './interferenceTypes'

export type GroupKey = 'overlap' | 'short' | 'unknown' | 'contact'

export interface PairRow {
  key: string
  pair: InterferencePair
  title: string
  detail: string
  marks: string[]
}

export interface Group {
  key: GroupKey
  heading: string
  open: boolean
  rows: PairRow[]
}

const HEADINGS: Array<{ key: GroupKey; heading: string; open: boolean }> = [
  { key: 'overlap', heading: '干渉', open: true },
  { key: 'short', heading: '隙間不足', open: true },
  { key: 'unknown', heading: '未確定', open: true },
  { key: 'contact', heading: '接触', open: false },
]

export function pairKey(pair: InterferencePair): string {
  return `${pair.a}|${pair.b}`
}

/** 有効数字4桁。大きな値も小さな値も読める短さにする。 */
export function figure(value: number): string {
  return Number(value.toPrecision(4)).toString()
}

export function groupOf(pair: InterferencePair): GroupKey | null {
  switch (pair.relation) {
    case 'overlap':
      return 'overlap'
    case 'tiny_overlap':
    case 'contact':
      return 'contact'
    case 'unknown':
      return 'unknown'
    case 'separated':
      if (pair.clearance === 'short') return 'short'
      if (pair.clearance === null) return 'unknown'
      return null // 要求ちょうど(at)・余裕(ok)・隙間を問わなかった組は一覧に出さない(件数だけ)
  }
}

function detailOf(pair: InterferencePair): string {
  switch (pair.relation) {
    case 'overlap':
    case 'tiny_overlap':
      return pair.volume === undefined ? '' : `体積 ${figure(pair.volume)} mm³`
    case 'contact':
      if (pair.contact === 'face') return `面接触${pair.area === undefined ? '' : ` ${figure(pair.area)} mm²`}`
      if (pair.contact === 'edge') return '線接触'
      if (pair.contact === 'vertex') return '点接触'
      return '接触'
    case 'separated': {
      if (pair.distance === undefined) return pair.clearance_reason ?? '距離を測れなかった'
      const prefix = pair.distance_lower_bound === true ? '距離 ≧ ' : '距離 '
      return `${prefix}${figure(pair.distance)} mm`
    }
    case 'unknown':
      return pair.reason ?? '不明'
  }
}

function marksOf(pair: InterferencePair): string[] {
  const marks: string[] = []
  if (pair.relation === 'tiny_overlap') marks.push('微小な重なり')
  if (pair.relation !== 'separated' && pair.relation !== 'unknown' && pair.clearance === 'short') marks.push('隙間不足')
  if (pair.relation === 'separated' && pair.clearance === null) marks.push(pair.clearance_reason ?? '距離を測れなかった')
  if (pair.approximate !== undefined) marks.push('近似')
  if (pair.distance_upper_bound === true) marks.push('上界')
  return marks
}

/** 見出しは常に4つ(0件でも出す: 「調べたが無かった」が画面から分かる)。行は重い順(干渉は体積の大きい順、隙間不足は近い順)。 */
export function groupPairs(pairs: readonly InterferencePair[]): Group[] {
  const groups: Group[] = HEADINGS.map((heading) => ({ ...heading, rows: [] }))
  for (const pair of pairs) {
    const key = groupOf(pair)
    if (key === null) continue
    const group = groups.find((candidate) => candidate.key === key)!
    group.rows.push({
      key: pairKey(pair),
      pair,
      title: `${pair.a_name} × ${pair.b_name}`,
      detail: detailOf(pair),
      marks: marksOf(pair),
    })
  }
  for (const group of groups) {
    group.rows.sort((left, right) => weight(group.key, right.pair) - weight(group.key, left.pair))
  }
  return groups
}

function weight(key: GroupKey, pair: InterferencePair): number {
  if (key === 'overlap') return pair.volume ?? 0
  if (key === 'short') return -(pair.distance ?? 0)
  if (key === 'contact') return pair.relation === 'contact' ? 1 : 0 // 微小な重なりは接触の後ろ
  return 0
}

/** 結果の要約(パネルの上段)。 */
export function summaryLines(payload: InterferencePayload): string[] {
  const lines: string[] = []
  const target = payload.target
  if (target !== undefined) {
    const mode =
      target.mode === 'visible_all'
        ? '見えているボディ全部'
        : target.mode === 'one_vs_rest'
          ? `${target.selected[0] ?? '選んだ1つ'} 対 ほかの見えているボディ`
          : '選んだボディどうし'
    const hidden = target.excluded_hidden > 0 ? `・隠した ${target.excluded_hidden} 個は除く` : ''
    lines.push(`${mode}の ${target.pairs} 組を調べた${hidden}`)
  }
  const counts = payload.counts
  if (counts !== undefined && payload.clearance !== undefined && payload.clearance > 0) {
    lines.push(`隙間 ${figure(payload.clearance)} mm: 不足 ${counts.short} / 要求どおり ${counts.at} / 余裕 ${counts.ok}`)
  }
  if (payload.complete === false) {
    lines.push(`時間の上限で打ち切った(未着手 ${payload.not_started ?? 0} 組)。結果は途中まで`)
  }
  return lines
}

/** 返事を描いてよいか。古い応答(別の起動の番号)・版の違う結果(検査の間に文書が変わった)・切れた接続の後の結果は描かない。 */
export type Verdict = 'draw' | 'stale_request' | 'stale_revision' | 'disconnected'

export function acceptResult(
  payload: Pick<InterferencePayload, 'client_op' | 'document_revision'>,
  context: { currentOp: number | null; revision: string | null; connected: boolean },
): Verdict {
  if (!context.connected) return 'disconnected'
  if (context.currentOp === null || payload.client_op !== context.currentOp) return 'stale_request'
  if (payload.document_revision !== undefined && payload.document_revision !== context.revision) return 'stale_revision'
  return 'draw'
}

/** ビューポートの層に渡すもの。行を選んでいればその組(重なりの立体・接触面・最短距離の線と札)、選んでいなければ重なりの立体だけを全部。 */
export function visualFor(payload: InterferencePayload | null, selectedKey: string | null): InterferenceVisual | null {
  if (payload === null) return null
  const pairs = payload.pairs ?? []
  const visual: InterferenceVisual = { solids: [], faces: [], edges: [], points: [], distance: null }
  const picked = selectedKey === null ? null : pairs.find((pair) => pairKey(pair) === selectedKey) ?? null
  if (picked === null) {
    for (const pair of pairs) if (pair.render?.solid !== undefined) visual.solids.push(pair.render.solid)
    return visual
  }
  const render = picked.render
  if (render?.solid !== undefined) visual.solids.push(render.solid)
  if (render?.faces !== undefined) visual.faces.push(render.faces)
  if (render?.edges !== undefined) visual.edges.push(...render.edges)
  if (render?.points !== undefined) visual.points.push(...render.points)
  if (picked.points !== undefined && picked.points.length === 2 && picked.distance !== undefined) {
    visual.distance = { from: picked.points[0], to: picked.points[1], label: `${figure(picked.distance)} mm` }
  }
  return visual
}
