/** 落ちたら、ドロップの種類・モード・作業状態に対する取り込みの可否か断りが崩れている。 */
import assert from 'node:assert/strict'
import { decideDroppedFiles } from '../web/src/fileDrop.ts'

const idle = { connected: true, documentOpen: true, mode: 'idle', waiting: false, commandActive: false }
const decide = (names, state = {}) => decideDroppedFiles(names.map((name) => ({ name })), { ...idle, ...state })
for (const name of ['part.step', 'part.STEP', '日本語.STP', 'a..b.step']) {
  assert.deepEqual(decide([name]), { verb: 'import_step' })
}
for (const name of ['drawing.svg', 'drawing.SVG']) {
  assert.deepEqual(decide([name], { mode: 'sketch' }), { verb: 'import_svg' })
  assert.deepEqual(decide([name]), { message: 'SVG はスケッチの中でしか取り込めない(スケッチを開いてから落とす)' })
  assert.deepEqual(decide([name], { mode: 'sketch-pending' }), { message: 'SVG はスケッチの中でしか取り込めない(スケッチを開いてから落とす)' })
}
for (const mode of ['sketch', 'sketch-pending']) {
  assert.deepEqual(decide(['part.stp'], { mode }), { message: 'STEP はスケッチの中では取り込めない(スケッチを閉じてから落とす)' })
}
const filesOnly = { message: '落とせるのは STEP(.step・.stp)か SVG(.svg)の1ファイルだけ(フォルダは取り込めない)' }
for (const names of [[], ['part.exe'], ['noextension'], ['part.step.exe'], ['a.step', 'b.svg']]) {
  assert.deepEqual(decide(names), filesOnly)
}
assert.deepEqual(decideDroppedFiles([{ name: 'folder.step', directory: true }], idle), filesOnly)
for (const [state, message] of [
  [{ connected: false }, 'サーバに繋がっていないのでファイルを取り込めない'],
  [{ documentOpen: false }, '文書を開いていないのでファイルを取り込めない'],
  [{ waiting: true }, '処理中なのでファイルを取り込めない(終わってから落とす)'],
  [{ commandActive: true }, 'コマンドの途中なのでファイルを取り込めない(確定か中断をしてから落とす)'],
  [{ mode: 'optimize' }, 'このモードではファイルを取り込めない(通常モードに戻ってから落とす)'],
  [{ mode: 'other' }, 'このモードではファイルを取り込めない(通常モードに戻ってから落とす)'],
]) {
  assert.deepEqual(decide(['part.step'], state), { message })
  assert.deepEqual(decide(['drawing.svg'], { mode: 'sketch', ...state }), { message })
}
console.log('ok')
