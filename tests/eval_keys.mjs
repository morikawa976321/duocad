/**
 * `web/src/keys.ts`(Enter・Esc を拾うハンドラの共通判定)を**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、次のどちらか。
 * - IME の変換中の Enter(候補の確定)・Esc(変換の取り消し)を、画面のコマンドの確定・中断として拾う。
 * - フォーカスが選択肢(<select>)などにある Enter を、窓のハンドラが奪ってコマンドを確定する。
 */
import assert from 'node:assert/strict'

import { isImeKey, ownsEnterKey } from '../web/src/keys.ts'

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

console.log('keys.ts: 全ての想定を満たした')
