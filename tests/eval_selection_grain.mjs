/**
 * `web/src/commands/grain.ts`(選択の粒度)と、ボディを直接ピックする口(`command.ts` の `pickedRefOf` / `pickedLabel`)を
 * **実際に評価する**口。`tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、ボディ・図形ごとに効くコマンドの起動中に、押した面・辺・点がそのまま選ばれ(光ったものと効くものが食い違う)、
 * またはボディで指したブーリアンのターゲットが「未指定」のまま確定できない。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const { foldRef, foldRefs, grainOf, hoverGrainOf, NO_GRAIN } = await import('../web/src/commands/grain.ts')
const { pickedLabel, pickedRefOf } = await import('../web/src/commands/command.ts')
const features = await import('../web/src/commands/features.ts')
const sketch = await import('../web/src/commands/sketch.ts')

const BODY = { body: true, entity: false }
const ENTITY = { body: false, entity: true }
const noPoints = () => false

// ============================================================== コマンド定義から粒度を読む
assert.deepEqual(grainOf(null), NO_GRAIN, '無起動は畳まない')
for (const command of [features.MOVE_BODY, features.MIRROR_BODY, features.SPLIT_BODY, features.BOOLEAN_BODY]) {
  assert.equal(grainOf(command).body, true, `${command.id} はボディ単位で選ぶ`)
}
assert.deepEqual(grainOf(features.DELETE_SELECTION), { body: true, entity: true }, '削除はボディも図形も丸ごと')
assert.equal(grainOf(features.PATTERN_RECTANGULAR).entity, true, '反復の `entities` は図形単位')
assert.equal(grainOf(sketch.MIRROR_SKETCH_ENTITIES).entity, true, 'スケッチの鏡映は図形単位')
assert.deepEqual(grainOf(sketch.MOVE_SKETCH_ENTITIES), NO_GRAIN, 'スケッチの移動は点だけを動かせるので点のまま')
assert.deepEqual(grainOf(features.FILLET), NO_GRAIN, 'フィレットは辺のまま')
assert.deepEqual(grainOf(features.SHELL_BODY), NO_GRAIN, 'シェルは抜く面のまま')

// ============================================================== 1つを畳む
const face = { body: 'a', kind: 'face', index: 3 }
const edge = { body: 'a', kind: 'edge', index: 5 }
const vertex = { body: 'b', kind: 'vertex', index: 1 }
const bodyA = { body: 'a', kind: 'body', index: null }
const region = { sketch: 's', index: 1 }
const endpoint = { sketch: 's', entity: 'line', point: 'x0' }
const wholeLine = { sketch: 's', entity: 'line', point: null }

assert.deepEqual(foldRef(face, BODY, noPoints), bodyA, '面はボディへ')
assert.deepEqual(foldRef(edge, BODY, noPoints), bodyA, '辺もボディへ')
assert.deepEqual(foldRef(vertex, BODY, noPoints), { body: 'b', kind: 'body', index: null }, '頂点もボディへ')
assert.equal(foldRef(face, NO_GRAIN, noPoints), face, '粒度が無ければそのまま')
assert.equal(foldRef(face, ENTITY, noPoints), face, '図形単位はソリッドの部分を畳まない')
assert.equal(foldRef(region, BODY, noPoints), region, '領域は畳まない')
assert.deepEqual(foldRef(endpoint, ENTITY, noPoints), wholeLine, '線の端点は線へ')
assert.equal(foldRef(endpoint, BODY, noPoints), endpoint, 'ボディ単位は図形の点を畳まない')
const lonePoint = { sketch: 's', entity: 'p', point: 'cx' }
assert.equal(
  foldRef(lonePoint, ENTITY, (sketchId, entityId) => sketchId === 's' && entityId === 'p'),
  lonePoint,
  '点の図形の点は畳まない(コアが `point` 無しを cx へ書き直すので、畳むと鍵が食い違う)',
)

// ============================================================== 並びを畳む
assert.deepEqual(
  foldRefs([face, edge, vertex, { body: 'a', kind: 'face', index: 4 }], BODY, noPoints),
  [bodyA, { body: 'b', kind: 'body', index: null }],
  '同じボディは1つ(最初に現れた順。コアの `require_bodies` と同じ)',
)
assert.deepEqual(
  foldRefs([endpoint, wholeLine, { sketch: 's', entity: 'line', point: 'x1' }], ENTITY, noPoints),
  [wholeLine],
  '同じ図形の点と線は1つ',
)

// ============================================================== ホバーは次のクリックで入るものに揃える
assert.deepEqual(hoverGrainOf(BODY, null), BODY, '選択を待つ間はボディ全体')
assert.deepEqual(hoverGrainOf(BODY, 'face'), NO_GRAIN, '鏡映面・切る平面の面1枚を待つ間は面1枚')
assert.deepEqual(hoverGrainOf(BODY, 'edge'), BODY, '回転軸の辺を待つ間も、面を押せばボディが入る')
assert.deepEqual(hoverGrainOf(NO_GRAIN, 'body'), BODY, 'ボディのピックはボディ全体')
assert.deepEqual(hoverGrainOf(ENTITY, 'sketchPoint'), NO_GRAIN, '回転中心の点を待つ間は点')
assert.deepEqual(hoverGrainOf(NO_GRAIN, 'sketchEntity'), ENTITY, '軸・平面に線を待つ間は線まるごと')

// ============================================================== ボディを直接ピックする
assert.deepEqual(pickedRefOf(bodyA), bodyA, 'ボディの参照はピックの値になる')
assert.equal(pickedRefOf({ body: 'a', kind: 'body', index: 2 }), null, '添字付きのボディ参照は値にしない')
assert.equal(pickedRefOf({ body: 'a', kind: 'face' }), null, '添字の無い面は値にしない')
assert.equal(pickedLabel(bodyA, 'body'), 'ボディ1つ')
assert.equal(pickedLabel(face, 'face'), '面 3', '面のピックの表示は変わらない')
