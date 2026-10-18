/**
 * `web/src/renameDraft.ts`(ブラウザの名前変更で送る名前)を**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、前後に空白のある下書きをそのまま送り、サーバが英語のエラーで断って名前が変わらない(入力欄は閉じていて打ち直しになる)。
 */
import assert from 'node:assert/strict'

import { renameTarget } from '../web/src/renameDraft.ts'

assert.equal(renameTarget('Bracket', 'Body 1'), 'Bracket')
assert.equal(renameTarget('  Bracket  ', 'Body 1'), 'Bracket', '前後の空白は落として送る')
assert.equal(renameTarget('Bracket　', 'Body 1'), 'Bracket', '全角空白(IME の確定で付く)も落とす')
assert.equal(renameTarget('Bracket\n', 'Body 1'), 'Bracket')
assert.equal(renameTarget('Left arm', 'Body 1'), 'Left arm', '中の空白は保つ')
assert.equal(renameTarget('', 'Body 1'), null, '空は送らない')
assert.equal(renameTarget('  　 ', 'Body 1'), null, '空白だけは送らない')
assert.equal(renameTarget('Body 1', 'Body 1'), null, '今と同じなら送らない')
assert.equal(renameTarget(' Body 1 ', 'Body 1'), null, '空白を落とすと今と同じなら送らない')

console.log('renameDraft.ts: 全ての想定を満たした')
