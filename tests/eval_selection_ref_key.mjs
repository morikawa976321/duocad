/**
 * `web/src/commands/command.ts` の `refKey` / `sameRef` を**実際に評価する**口。
 * `tests/test_selection_ref_key.py` が `node` でこれを走らせる
 * (`tests/eval_grid_scale.mjs` と同じやり方 —— ソースの文字列比較ではなく実行結果で見る)。
 *
 * ここで見るのは**コア(`duocad/selection.py` の `ref_key`)と同じ規則になっているか**。
 * クライアント側は Set 引きで重複を落とすので、鍵がコアとずれると
 * 「サーバは同じと言うのに画面では二重に残る(逆も)」が、例外を1つも出さずに起きる。
 *
 * Node は `.ts` をそのまま読める(型注釈の除去。Node 23.6以降は既定で有効)。
 * `command.ts` の import は `import type` だけなので、実行時の依存は無い。
 */
import assert from 'node:assert/strict'
import { refKey, refKind, sameRef } from '../web/src/commands/command.ts'

const face3 = { body: 'box', kind: 'face', index: 3 }
const edge3 = { body: 'box', kind: 'edge', index: 3 }
const vertex3 = { body: 'box', kind: 'vertex', index: 3 }
const bodyNull = { body: 'box', kind: 'body', index: null }
const bodyIndexed = { body: 'box', kind: 'body', index: 7 }
const bodyBare = { body: 'box', kind: 'body' }
const otherBody = { body: 'other', kind: 'body', index: null }
const region1 = { sketch: 's', index: 1 }
const region2 = { sketch: 's', index: 2 }
const whole = { sketch: 's', entity: 'e', point: null }
const wholeBare = { sketch: 's', entity: 'e' }
const pointX0 = { sketch: 's', entity: 'e', point: 'x0' }
const pointEmpty = { sketch: 's', entity: 'e', point: '' }

// ============================================================== 種別が違えば別物
assert.equal(refKind(bodyNull), 'body', 'BodyRef の種別は "body"')
assert.ok(!sameRef(face3, edge3), '面3番と辺3番は別物')
assert.ok(!sameRef(edge3, vertex3), '辺3番と頂点3番は別物')
assert.ok(!sameRef(bodyNull, face3), 'Bodyそのものと、そのBodyの面は別物')
assert.ok(!sameRef(region1, whole), '領域とスケッチ図形は別物(どちらも sketch キーを持つ)')

// ============================================================== Bodyは添字を見ない
assert.ok(sameRef(bodyNull, bodyIndexed), 'Bodyの参照は添字で比べない')
assert.ok(sameRef(bodyNull, bodyBare), '添字のキーが無くても同じBody')
assert.ok(!sameRef(bodyNull, otherBody), 'Bodyが違えば別物')

// ============================================================== 点と辺
assert.ok(!sameRef(whole, pointX0), '図形全体とその上の点は別物')
assert.ok(sameRef(whole, wholeBare), 'point の欠落と null は同じもの')
assert.ok(
  !sameRef(whole, pointEmpty),
  'null(図形全体)と空文字の点名を同じ鍵へ畳まない',
)

// ============================================================== 添字・sketch
assert.ok(!sameRef(region1, region2), '領域は添字で区別する')
assert.ok(!sameRef(region1, { sketch: 'other', index: 1 }), '領域はスケッチで区別する')
assert.ok(!sameRef(face3, { body: 'box', kind: 'face', index: 4 }), '面は添字で区別する')

// ============================================================== provenance は見ない
assert.ok(sameRef(face3, { ...face3, provenance: { op: 1 } }), 'provenance は同一性に関与しない')

// ============================================================== 鍵と sameRef が一致する
const corpus = [
  face3, edge3, vertex3,
  bodyNull, bodyIndexed, bodyBare, otherBody,
  region1, region2,
  whole, wholeBare, pointX0, pointEmpty,
]
for (const left of corpus) {
  for (const right of corpus) {
    assert.equal(
      sameRef(left, right),
      refKey(left) === refKey(right),
      `Setが落とす重複と sameRef がずれた: ${JSON.stringify(left)} / ${JSON.stringify(right)}`,
    )
  }
}

// 別の実体は別の鍵(Setに入れたときに潰れない)
const distinct = [face3, edge3, vertex3, bodyNull, otherBody, region1, region2, whole, pointX0, pointEmpty]
assert.equal(new Set(distinct.map(refKey)).size, distinct.length, '別の実体の鍵が衝突している')

console.log('OK: refKey / sameRef')
