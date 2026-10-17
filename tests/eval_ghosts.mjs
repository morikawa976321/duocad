/**
 * 確定前の幽霊(`preview: 'local'`)を組む純関数を**実際に評価する**口。`tests/test_ghost_agreement.py` が呼ぶ。
 *
 * 通す道は `App.svelte` の `requestPreview` → `showLocalGhost` と同じ:
 * `resolvePicksForGhost`(参照を数値へ) → `ghostSources` + `ghostCopies`(スケッチ) /
 * `bodyGhostRotation` + `bodyGhostMatrix`(ボディ)。
 *
 * 入力(stdin の JSON 配列)の1件:
 *   { verb, args, sketches, editingId, selection, edges: {bodyId: EdgesPayload},
 *     vertices: {bodyId: VerticesPayload}, probe: [[x,y,z], ...] }
 * `sketches` / `selection` / `edges` / `vertices` は**サーバが配る形そのまま**を渡す。
 *
 * 出力は入力と同じ並びの
 *   { sketch: [{kind, params}], rotation: BodyGhostRotation | null, probe: [[x,y,z], ...] | null }
 * `probe` は `move_body(mode="rotate")` の行列を掛けた点(回転を作れなければ null)。
 *
 * Node の型除去は拡張子の無い相対 import を解決しない(Vite は補う)ので、解決に失敗したときだけ
 * `.ts` を補う resolve フックを先に登録する(`eval_command_defs.mjs` と同じ)。
 */

import './ts_resolve_hook.mjs'

const source = new URL('../web/src/', import.meta.url)
const { ghostCopies } = await import(new URL('entityKinds.ts', source).href)
const { bodyGhostRotation, ghostSources, resolvePicksForGhost } = await import(new URL('ghostArgs.ts', source).href)
const { bodyGhostMatrix } = await import(new URL('viewportTypes.ts', source).href)

const chunks = []
for await (const chunk of process.stdin) chunks.push(chunk)
const cases = JSON.parse(Buffer.concat(chunks).toString('utf8'))

const results = cases.map((item) => {
  const context = {
    sketches: item.sketches ?? [],
    editingId: item.editingId ?? null,
    selection: item.selection ?? null,
    edgesOf: (bodyId) => item.edges?.[bodyId] ?? null,
    verticesOf: (bodyId) => item.vertices?.[bodyId] ?? null,
  }
  const args = resolvePicksForGhost(context, item.verb, item.args)
  if (item.verb === 'move_body') {
    const rotation = args.mode === 'rotate' ? bodyGhostRotation(args) : null
    if (rotation === null) return { sketch: [], rotation: null, probe: null }
    // three.js の行列は列優先の16要素
    const m = bodyGhostMatrix({ bodyId: 'probe', offset: [0, 0, 0], rotate: rotation }).elements
    const probe = (item.probe ?? []).map(([x, y, z]) => [
      m[0] * x + m[4] * y + m[8] * z + m[12],
      m[1] * x + m[5] * y + m[9] * z + m[13],
      m[2] * x + m[6] * y + m[10] * z + m[14],
    ])
    return { sketch: [], rotation, probe }
  }
  return {
    sketch: ghostCopies(item.verb, args, ghostSources(context, item.verb, args)),
    rotation: null,
    probe: null,
  }
})
process.stdout.write(JSON.stringify(results))
