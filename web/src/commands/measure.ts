/**
 * 測定(issue #10)—— verb ではない唯一のコマンド。文書もログも変えない。
 * 対象は共有選択集合から**ちょうど2つ**取るので Enter は要らない。送るのは `{cmd:'measure'}` だけ(対象はコアの選択)で、
 * 応答は状態配信ではなく `{type:'measure', client_op, between, ...}`(`App.svelte` の `handleMeasure`)。
 * 辺・頂点を指せるのは対象ボディ1つぶん(`App.svelte` の `edgeTargetBody`)。面は全ボディを指せる。
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
