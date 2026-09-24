/**
 * `web/src/expression.ts` の式評価を**実際に走らせる**ための小さな口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * Node は `.ts` をそのまま読める(型注釈の除去。Node 23.6以降は既定で有効)。
 */
import assert from 'node:assert/strict'
import { evaluateExpression, isPlainNumber } from '../web/src/expression.ts'

function expressionValue(text) {
  const result = evaluateExpression(text)
  return result.ok ? result.value : null
}

/** 読めた値。読めなければ理由を添えて落ちる。 */
function value(text) {
  const result = evaluateExpression(text)
  assert.ok(result.ok, `読めるはずの式が拒まれた: ${text} -> ${result.ok ? '' : result.reason}`)
  return result.value
}

/** 拒まれた理由。読めてしまったら落ちる。 */
function reason(text) {
  const result = evaluateExpression(text)
  assert.ok(!result.ok, `拒むはずの式が通った: ${text} -> ${result.ok ? result.value : ''}`)
  return result.reason
}

function near(text, expected, tolerance = 1e-9) {
  const actual = value(text)
  assert.ok(Math.abs(actual - expected) <= tolerance, `${text} = ${actual}(期待 ${expected})`)
}

// ====================================================== 素の数値は parseFloat と同じ値でなければならない
// 実機検査も既存の欄も数字だけを打つ経路に乗っている(expression.ts の「層をまたぐ契約」)。
for (const text of ['0', '50', '0.5', '.5', '12.', '-3', '+3', '1e3', '2.5e-3', '  7  ']) {
  assert.equal(value(text), Number.parseFloat(text), `素の数値が parseFloat と食い違う: ${text}`)
}

// ====================================================== 四則・優先順位・括弧
assert.equal(value('1+2'), 3)
assert.equal(value('7-2-3'), 2, '左結合')
assert.equal(value('1+2*3'), 7, '乗除が先')
assert.equal(value('(1+2)*3'), 9, '括弧が優先順位を覆す')
assert.equal(value('100/4/5'), 5, '左結合')
assert.equal(value('((2))'), 2)

// ====================================================== 単項マイナス
assert.equal(value('-5'), -5)
assert.equal(value('3*-2'), -6, '演算子の直後の負値')
assert.equal(value('--3'), 3)
assert.equal(value('-(2+3)'), -5)

// ====================================================== べき乗
assert.equal(value('2^10'), 1024)
assert.equal(value('2^3^2'), 512, '右結合')
assert.equal(value('-2^2'), -4, '単項マイナスはべき乗より弱い(数学の慣習)')
assert.equal(value('2^-1'), 0.5, '指数側の単項マイナス')

// ====================================================== sqrt / abs
near('sqrt(2)', Math.SQRT2)
assert.equal(value('sqrt(16)'), 4)
assert.equal(value('abs(-3)'), 3)
assert.equal(value('abs(3)'), 3)

// ====================================================== 三角関数は**度**
near('sin(30)', 0.5)
near('tan(45)', 1)
near('cos(60)', 0.5)
// 90度の倍数は厳密。`Math.cos(Math.PI/2)` は 6.1e-17 で、式で作った垂直線が微小に傾く。
assert.equal(value('cos(90)'), 0, 'cos(90) は厳密に 0')
assert.equal(value('sin(180)'), 0, 'sin(180) は厳密に 0')
assert.equal(value('sin(90)'), 1)
assert.equal(value('cos(180)'), -1)
assert.equal(value('sin(-90)'), -1, '負の角も同じ象限へ畳まれる')
assert.equal(value('cos(450)'), 0, '360度を超えても畳まれる')
assert.equal(value('tan(180)'), 0)

// ====================================================== 逆三角も**度**で返る
near('asin(0.5)', 30)
near('acos(0.5)', 60)
near('atan(1)', 45)
near('atan2(1, 1)', 45)
near('atan2(10, 25)', 21.80140948635181, 1e-9)

// ====================================================== pi
near('pi', Math.PI)
near('2*pi*10', 2 * Math.PI * 10)
near('sin(pi)', Math.sin((Math.PI * Math.PI) / 180), 1e-12) // pi は度として渡る(ラジアンではない)

// ====================================================== 大文字・全角(日本語IMEを入れたまま打った式)
assert.equal(value('SQRT(4)'), 2, '関数名は大文字小文字を問わない')
near('PI', Math.PI)
assert.equal(value('１＋２'), 3, '全角の数字と演算子')
assert.equal(value('（２＋３）×４'), 20, '全角の括弧と×')
assert.equal(value('10÷4'), 2.5)
assert.equal(value('10−4'), 6, 'U+2212 のマイナス記号')

// ====================================================== 拒む側(黙って数を作らない)
assert.equal(reason(''), '式が空')
assert.equal(reason('   '), '式が空')
assert.ok(reason('50mm').includes('余分'), 'parseFloat と違い、末尾のゴミを黙って捨てない')
assert.ok(reason('3 4').includes('余分'), 'parseFloat は 3 を返すが、ここは拒む')
assert.ok(reason('2(3)').includes('余分'), '暗黙の乗算は認めない(曖昧なので)')
assert.equal(reason('(1+2'), '括弧が閉じていない')
assert.equal(reason('1+'), '式が途中で終わっている')
assert.equal(reason('*3'), 'ここに数値が要る')
assert.equal(reason('.'), '数値として読めない: .')
assert.ok(reason('1..2').includes('余分'))
assert.ok(reason('abc').includes('知らない名前'))
assert.ok(reason('sin').includes('sin(...)'), '関数を括弧なしで書いたときは書き方を出す')
assert.ok(reason('pi(2)').includes('関数ではない'))
assert.ok(reason('sqrt(1,2)').includes('引数は1個'))
assert.ok(reason('sqrt()').includes('引数は1個'))
assert.ok(reason('atan2(1)').includes('引数は2個'))
assert.ok(reason('1@2').includes('使えない文字'))

// **値が数にならないものは通さない**(`Number.isFinite` を呼び出し側に任せない)。
assert.ok(reason('1/0').includes('値が数にならない'))
assert.ok(reason('sqrt(-1)').includes('値が数にならない'))
assert.ok(reason('asin(2)').includes('値が数にならない'))
assert.ok(reason('tan(90)').includes('値が数にならない'), 'Math.tan は 1.6e16 という有限のでたらめを返す')
assert.ok(reason('10^400').includes('値が数にならない'))

// **深い入れ子で投げてはならない**。括弧だけが再帰するので、貼り付けた式が
// `RangeError` を投げると、これを呼ぶ `$derived` ごと画面が落ちる。
assert.equal(reason('('.repeat(5000) + '1' + ')'.repeat(5000)), '式が深すぎる')
// 左結合の並びは反復なので、長さでは枯れない。
assert.equal(value(Array(5000).fill('1').join('+')), 5000)

// **継承した名前が関数として引けてはならない**(表を `Map` で持つ理由)。
for (const name of ['toString', 'constructor', 'hasOwnProperty', 'valueOf']) {
  assert.ok(!evaluateExpression(`${name}(1)`).ok, `継承名が関数として通った: ${name}`)
  assert.ok(!evaluateExpression(name).ok, `継承名が定数として通った: ${name}`)
}

// ====================================================== isPlainNumber(評価結果を添えるかの判定)
for (const text of ['50', '-3', '0.5', '.5', ' 50 ', '1e3', '１２']) {
  assert.ok(isPlainNumber(text), `素の数値のはず: ${text}`)
}
for (const text of ['', '5*2', 'sqrt(4)', '50mm', '(5)', '1+', '- 3']) {
  assert.ok(!isPlainNumber(text), `式のはず: ${text}`)
}

// ====================================================== expressionValue(理由の要らない口)
assert.equal(expressionValue('2*3'), 6)
assert.equal(expressionValue('50mm'), null)

console.log('expression.ts: 全ての想定を満たした')
