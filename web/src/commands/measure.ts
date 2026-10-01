/**
 * 測定(issue #10)—— **verb ではない**唯一のコマンド。文書もログも変えない。
 *
 * verb→target の枠(`CommandHost`)にそのまま乗る: 起動 → 対象を2つ指す → その場で測る。
 * how が無く対象は**ちょうど2つ**なので Enter は要らない(`needsEnter` の「下限＝上限」)。
 * 対象は共有選択集合から取る(`SelectionArgSpec`)ので、AIが `propose_selection` で2つ
 * 置いたものをそのまま測れるし、AI側は同じ `measure` ツールで同じ答えを読める。
 *
 * 送るのは `{cmd:'measure'}` だけ(対象はコアの選択にある)。応答は状態配信ではなく
 * `{type:'measure', client_op, between, ...}`(`App.svelte` の `handleMeasure`)。
 *
 * **辺・頂点を指せるのは対象ボディ1つぶん**(`App.svelte` の `edgeTargetBody`。フィレットの
 * 辺と同じ規律)。面は常に全ボディを指せるので、別のボディの面との距離は測れる。
 */
import type { SelectionRef } from '../session'
import type { CommandDef } from './command'

export const MEASURE: CommandDef<'measure'> = {
  id: 'measure',
  label: '測定',
  buttonId: 'measure',
  preview: 'none',
  args: [
    {
      kind: 'selection',
      name: 'targets',
      label: '測る対象',
      shapeKind: 'vertex',
      kinds: ['vertex', 'edge', 'face'],
      min: 2,
      max: 2,
      pickVertices: true,
    },
  ],
  build(values) {
    const targets = (values.targets ?? []) as SelectionRef[]
    if (targets.length < 2) return { ok: false, reason: '測る対象(頂点・直線の辺・平らな面)を2つ選ぶ' }
    return { ok: true, args: {} }
  },
}
