/**
 * 測定(issue #10)—— verb ではない唯一のコマンド。文書もログも変えない。
 * 対象は共有選択集合から**ちょうど2つ**取るので Enter は要らない。送るのは `{cmd:'measure'}` だけ(対象はコアの選択)で、
 * 応答は状態配信ではなく `{type:'measure', client_op, between, ...}`(`App.svelte` の `handleMeasure`)。
 * 起動中は平らな面・直線の辺・頂点を全ボディで、点・線・領域を見えている全スケッチで指せる(`App.svelte` の `pickKinds`)。
 * 面・辺の形は `duocad/measure.py` の `_measure_item` が2項目の測定に使える形と揃える(曲面・円の辺はコアが断る)。
 * メッシュの面は残す: 指したときに測れない理由を出す(`App.svelte` の `onPick`)。
 */
import type { SelectionRef } from '../session'
import type { CommandDef } from './command'

const MEASURABLE = '頂点・直線の辺・平らな面・スケッチの点・線分・領域'

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
      kinds: ['vertex', 'edge', 'face', 'sketch_entity', 'region', 'datum'],
      min: 2,
      max: 2,
      pickVertices: true,
      shapes: { faces: ['plane', 'mesh'], edges: ['line'] },
      hint: MEASURABLE,
    },
  ],
  build(values) {
    const targets = (values.targets ?? []) as SelectionRef[]
    if (targets.length < 2) return { ok: false, reason: `測る対象(${MEASURABLE})を2つ選ぶ` }
    return { ok: true, args: {} }
  },
}
