/** `web/src/aiToolLine.ts` の評価口(`tests/test_ts_evaluators.py` が走らせる)。ツール行をコマンドのラベルと引数の見出しで書き、対応の無い verb は内部名のままにする。 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const { toolLine, toolRawLine } = await import(new URL('../web/src/aiToolLine.ts', import.meta.url).href)
const { EXTRUDE_FACES, MOVE_BODY, FILLET } = await import(new URL('../web/src/commands/features.ts', import.meta.url).href)

const defs = new Map([EXTRUDE_FACES, MOVE_BODY, FILLET].map((def) => [def.id, def]))
const commandOf = (verb) => defs.get(verb)
const call = (name, args) => ({ name, arguments: args, result: {}, ok: true })

// 数値は見出しと値、選択肢は見出しと選択肢のラベル。括弧書きの補足は行に入れない
assert.equal(toolLine(call('extrude', { distance: 10, mode: 'join' }), commandOf), '押し出す(距離 10・結果 結合)')

// サーバへ出す値(`copy: true`)から選択肢のラベルを引く
assert.equal(
  toolLine(call('move_body', { copy: true, mode: 'translate', dx: 5, dy: 0, dz: 0 }), commandOf),
  '動かす(元のボディ 残して複製・動かし方 並進・X 5)',
)

// 引数は3つまで。選ぶ対象(選択・ピック)は入れない
assert.equal(toolLine(call('fillet', { edges: ['e'], radius: 2 }), commandOf), 'フィレット(半径 2)')

// 引数が1つも載らないときはラベルだけ
assert.equal(toolLine(call('extrude', { faces: ['f'] }), commandOf), '押し出す')

// 対応するコマンドの無い verb は内部名と JSON のまま
assert.equal(toolLine(call('set_visible', { body_id: 'b1', visible: false }), commandOf), 'set_visible {"body_id":"b1","visible":false}')
assert.equal(toolRawLine(call('undo', {})), 'undo')
assert.equal(toolRawLine(call('extrude', { distance: 10 })), 'extrude {"distance":10}')
