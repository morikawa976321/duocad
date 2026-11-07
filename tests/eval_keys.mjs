/**
 * `web/src/keys.ts`(Enter・Esc を拾うハンドラの共通判定)を**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、次のどちらか。
 * - IME の変換中の Enter(候補の確定)・Esc(変換の取り消し)を、画面のコマンドの確定・中断として拾う。
 * - フォーカスが選択肢(<select>)などにある Enter を、窓のハンドラが奪ってコマンドを確定する。
 * - 入力欄・開いているダイアログの Esc でコマンドを中断する、入力欄の Delete・Backspace で選んだものを消す。
 */
import assert from 'node:assert/strict'

import { isImeKey, isTextEntry, ownsEnterKey, ownsEscapeKey } from '../web/src/keys.ts'

// IME の変換中: isComposing が真、または keyCode 229(Safari は確定の Enter で isComposing が偽になる)。
assert.equal(isImeKey({ isComposing: true, keyCode: 13 }), true)
assert.equal(isImeKey({ isComposing: false, keyCode: 229 }), true)
assert.equal(isImeKey({ keyCode: 229 }), true)
// 普通のキー。
assert.equal(isImeKey({ isComposing: false, keyCode: 13 }), false)
assert.equal(isImeKey({ isComposing: false, keyCode: 27 }), false)
assert.equal(isImeKey({}), false)

// Enter を自分で使う相手。
for (const tagName of ['INPUT', 'TEXTAREA', 'BUTTON', 'SELECT', 'A', 'SUMMARY']) {
  assert.equal(ownsEnterKey({ tagName }), true, `${tagName} は Enter を自分で使う`)
}
assert.equal(ownsEnterKey({ tagName: 'DIV', isContentEditable: true }), true, '編集可能な要素')
// 窓の Enter がコマンドの確定になる相手。
assert.equal(ownsEnterKey({ tagName: 'BODY' }), false)
assert.equal(ownsEnterKey({ tagName: 'DIV', isContentEditable: false }), false)
assert.equal(ownsEnterKey({ tagName: 'CANVAS' }), false)
assert.equal(ownsEnterKey(null), false)
// 表示のチェックボックス・スライダーにフォーカスが残っていても Enter は窓のもの。文字欄・押せる型は譲る。
for (const type of ['checkbox', 'radio', 'range', 'CHECKBOX']) {
  assert.equal(ownsEnterKey({ tagName: 'INPUT', type }), false, `INPUT ${type} は Enter を使わない`)
}
for (const type of ['text', 'number', 'button', 'submit', 'file', 'color']) {
  assert.equal(ownsEnterKey({ tagName: 'INPUT', type }), true, `INPUT ${type} は Enter を自分で使う`)
}

// 文字を打つ欄。`type` を持たない INPUT は text。
for (const target of [
  { tagName: 'INPUT' },
  { tagName: 'INPUT', type: 'text' },
  { tagName: 'INPUT', type: 'number' },
  { tagName: 'INPUT', type: 'search' },
  { tagName: 'TEXTAREA' },
  { tagName: 'SELECT' },
  { tagName: 'DIV', isContentEditable: true },
]) {
  assert.equal(isTextEntry(target), true, `${JSON.stringify(target)} は文字を打つ欄`)
  assert.equal(ownsEscapeKey(target), true, `${JSON.stringify(target)} の Esc はその欄のもの`)
}
// フォーカスが乗っているだけの相手。Esc・Delete は窓のもの。
const outside = { closest: () => null }
for (const target of [
  { tagName: 'INPUT', type: 'checkbox', ...outside },
  { tagName: 'INPUT', type: 'radio', ...outside },
  { tagName: 'BUTTON', ...outside },
  { tagName: 'BODY', ...outside },
  { tagName: 'CANVAS', ...outside },
]) {
  assert.equal(isTextEntry(target), false, `${JSON.stringify(target)} は文字を打たない`)
  assert.equal(ownsEscapeKey(target), false, `${JSON.stringify(target)} の Esc はコマンドの中断`)
}
assert.equal(isTextEntry(null), false)
assert.equal(ownsEscapeKey(null), false)
// 開いているダイアログ(設定)の中のボタン。Esc はダイアログを閉じるもので、コマンドは中断しない。
const inDialog = { tagName: 'BUTTON', closest: (selector) => (selector === 'dialog[open]' ? {} : null) }
assert.equal(ownsEscapeKey(inDialog), true)
assert.equal(isTextEntry(inDialog), false, 'ダイアログのボタンの Delete は窓のもの')

console.log('keys.ts: 全ての想定を満たした')
