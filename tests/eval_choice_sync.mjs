/**
 * `web/src/commands/command.ts` の `syncedChoice`(配信から選択肢の欄を決める)を**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 題材は回転体の軸の3択(「線分を選ぶ」は `sends: null` で、`axis_line` から戻す)。落ちたら、再読み込みで
 * 「線分を選ぶ」が戻らない(ピックの欄が隠れたまま確定できない)か、人がまだ送っていない x 軸への切り替えが
 * 古い `axis_line` に上書きされ、古い線分の軸で確定される。
 */
import assert from 'node:assert/strict'
import { syncedChoice } from '../web/src/commands/command.ts'

const AXIS = {
  kind: 'choice',
  name: 'axis',
  label: '回転軸',
  options: [
    { value: 'x', label: 'x軸', buttonId: 'x' },
    { value: 'y', label: 'y軸', buttonId: 'y' },
    { value: 'line', label: '線分を選ぶ', buttonId: 'line', sends: null, impliedBy: 'axis_line' },
  ],
}
const LINE = { sketch: 's', entity: 'e', point: null }

// 再読み込み: 画面は何も送っていない。配信の axis_line から「線分を選ぶ」を戻す。
assert.deepEqual(syncedChoice(AXIS, { axis_line: LINE }, {}, undefined), { value: 'line' })

// AI が角度だけ直した: 配信の axis_line は前と同じでも、欄が「線分」なら残す。
assert.deepEqual(syncedChoice(AXIS, { axis_line: LINE, angle: 90 }, { axis_line: LINE }, 'line'), { value: 'line' })

// 人が x へ切り替えてまだ送っていない間に、AI が角度だけ直した配信が来た: 古い axis_line で「線分」へ戻さない。
// (欄は「選んでいない」へ戻る —— 送っていない値より配信が勝つのは他の選択肢と同じ。古い線分で確定はされない)
assert.deepEqual(syncedChoice(AXIS, { axis_line: LINE, angle: 90 }, { axis_line: LINE }, 'x'), { value: undefined })

// AI が軸を x から線分へ替えた(axis を外し axis_line を足した): 「線分」へ移る。
assert.deepEqual(syncedChoice(AXIS, { axis_line: LINE }, { axis: 'x' }, 'x'), { value: 'line' })

// 配信に axis があればそれが勝つ。
assert.deepEqual(syncedChoice(AXIS, { axis: 'y', axis_line: LINE }, { axis_line: LINE }, 'line'), { value: 'y' })

// 対応する選択肢の無い値は欄に触らない。
assert.deepEqual(syncedChoice(AXIS, { axis: 'z' }, {}, 'x'), { unmapped: true })

// 「線分を選ぶ」をまだ指していない(axis_line が無い)ときは、配信に無いことを「選んでいない」と読まない。
assert.deepEqual(syncedChoice(AXIS, { angle: 90 }, {}, 'line'), { value: 'line' })
