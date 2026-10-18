/**
 * `web/src/viewSettings.ts`(画面設定の localStorage)を、`window.localStorage` の偽物で**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、壊れた値(`'1'`・`'True'`・空)や読み書きの例外で、原点・軸・平面の表示が黙って既定と違う側へ倒れる。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const store = new Map()
let throwing = false
globalThis.window = {
  localStorage: {
    getItem(key) {
      if (throwing) throw new Error('blocked')
      return store.has(key) ? store.get(key) : null
    },
    setItem(key, value) {
      if (throwing) throw new Error('blocked')
      store.set(key, String(value))
    },
  },
}

const { loadOriginVisible, saveOriginVisible } = await import('../web/src/viewSettings.ts')
const KEY = 'duocad.view.originVisible'

assert.equal(loadOriginVisible(), true, '何も保存していなければ表示(既定)')

saveOriginVisible(false)
assert.equal(store.get(KEY), 'false')
assert.equal(loadOriginVisible(), false, '保存した「非表示」は守る')
saveOriginVisible(true)
assert.equal(loadOriginVisible(), true)

// 壊れた値は既定(表示)へ倒す。
for (const broken of ['1', '0', 'True', 'FALSE', '', 'yes', 'null']) {
  store.set(KEY, broken)
  assert.equal(loadOriginVisible(), true, `壊れた値 ${JSON.stringify(broken)} で原点が消える`)
}

// localStorage が遮断されていても投げず、既定へ倒す。
throwing = true
assert.equal(loadOriginVisible(), true)
assert.doesNotThrow(() => saveOriginVisible(false))

console.log('viewSettings.ts: 全ての想定を満たした')
