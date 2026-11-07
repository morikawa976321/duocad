/**
 * `web/src/selectionEdits.ts`(送って返っていない選択の変更を、配信済みの選択へ重ねる)を評価する。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、応答を待たずに続けた操作が古い選択に対して差分を組む: 上限つきの引数(ねじ・測定)への連続クリックが上限を超える、
 * 中断の全解除が送られず押した分が残る、またはコアの `modify_selection` / `toggle_selection` と効き方が食い違う。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const { clearEdit, expectedItems, modifyEdit, selectionDiff, toggleEdit } = await import('../web/src/selectionEdits.ts')
const { evictionsForClick } = await import('../web/src/commands/command.ts')
const features = await import('../web/src/commands/features.ts')
const measure = await import('../web/src/commands/measure.ts')

const face = (index) => ({ body: 'b1', kind: 'face', index })
const cylinder = () => 'cylinder_full'

// 返っていない変更が無ければ、配信済みの並びそのもの
const delivered = [face(1)]
assert.equal(expectedItems(delivered, []), delivered)
// 効き方が分からない変更(null)は飛ばす
assert.deepEqual(expectedItems(delivered, [null, toggleEdit(face(2))]), [face(1), face(2)])

// トグル: 無ければ末尾に足し、入っていれば外す
assert.deepEqual(toggleEdit(face(2))([face(1)]), [face(1), face(2)])
assert.deepEqual(toggleEdit(face(1))([face(1), face(2)]), [face(2)])

// 変更: 入っている参照の位置は動かさない。同じ参照を2回足しても重ならない。両方にあれば外す側が勝つ。無い参照を外すのは空振り
assert.deepEqual(modifyEdit([face(1), face(3), face(3)], [])([face(1), face(2)]), [face(1), face(2), face(3)])
assert.deepEqual(modifyEdit([face(3)], [face(3), face(9)])([face(1)]), [face(1)])
assert.deepEqual(modifyEdit([face(2)], [face(1)])([face(1)]), [face(2)])

// 全解除の後に送った変更は、空の選択に重なる
assert.deepEqual(expectedItems([face(1), face(2)], [clearEdit, toggleEdit(face(1))]), [face(1)])

// ねじ(面1枚まで): 応答を待たずに3枚続けて押しても、毎回その時点の見込みの1枚を外すので、最後の1枚だけが残る
{
  const inFlight = []
  for (const index of [1, 2, 3]) {
    const current = expectedItems([], inFlight)
    const evicted = evictionsForClick(features.THREAD, {}, face(index), current, cylinder)
    inFlight.push(evicted.length > 0 ? modifyEdit([face(index)], evicted) : toggleEdit(face(index)))
  }
  assert.deepEqual(expectedItems([], inFlight), [face(3)])
}

// 測定(2つまで): 応答を待たずに4つ続けて押すと、最後の2つが残る
{
  const inFlight = []
  for (const index of [1, 2, 3, 4]) {
    const current = expectedItems([], inFlight)
    const evicted = evictionsForClick(measure.MEASURE, {}, face(index), current)
    inFlight.push(evicted.length > 0 ? modifyEdit([face(index)], evicted) : toggleEdit(face(index)))
  }
  assert.deepEqual(expectedItems([], inFlight), [face(3), face(4)])
}
// 構えたコマンドの終わりが届いたときの全解除: 終わった時点の選択を外し、終わりより後に送った変更は残す。
// 差分は「いまの見込み」から「空の選択に、返っていない変更だけが効いたもの」へ。
{
  // Esc(全解除を送る)の直後に、唯一選ばれていた面を押した。終わりが届いた時点で返っていないのは、全解除とその後のトグル
  const afterEnd = [clearEdit, toggleEdit(face(1))]
  const kept = expectedItems([], afterEnd)
  assert.deepEqual(kept, [face(1)])
  assert.deepEqual(selectionDiff(expectedItems([face(1)], afterEnd), kept), { add: [], remove: [] }, '押した面を外さない')
  // 確定のあとに別の面を押した(全解除は送っていない): 終わった時点の選択だけを外す
  const clicked = [toggleEdit(face(3))]
  assert.deepEqual(selectionDiff(expectedItems([face(1), face(2)], clicked), expectedItems([], clicked)), {
    add: [],
    remove: [face(1), face(2)],
  })
  // 確定のあとに、選ばれていた面を押した(トグルは外す向きで送られている): 押した面が選ばれた状態にする
  const reclicked = [toggleEdit(face(1))]
  assert.deepEqual(selectionDiff(expectedItems([face(1), face(2)], reclicked), expectedItems([], reclicked)), {
    add: [face(1)],
    remove: [face(2)],
  })
}
console.log('eval_selection_edits: ok')
