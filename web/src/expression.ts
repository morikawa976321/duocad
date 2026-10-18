/**
 * 数値入力欄に打った式を読む(四則・単項マイナス・括弧・べき乗・sqrt・abs・三角・pi)。
 *
 * `eval` も `Function` も使わない: 文字列は人の打鍵だけとは限らない(アプリ内AIパネルが画面を触る)。
 *
 * 層をまたぐ契約: 素の数値だけ(`isPlainNumber`)なら `Number.parseFloat` と同じ値を返す
 * (数値欄の呼び出し側も `web/scripts/*.mjs` の実機検査も、その経路に乗っている)。
 *
 * 角度は度(`sin(30)` は 0.5、`atan2(10, 25)` は度で返る)。ラジアンは `sin(x*180/pi)`。
 */

export type ExpressionResult = { ok: true; value: number } | { ok: false; reason: string }

class ExpressionError extends Error {}

// ============================================================================ 度の三角関数

function turnOf(deg: number): number {
  return ((deg % 360) + 360) % 360
}

/** 90度の倍数を厳密に返す(`Math.sin(Math.PI)` は 1.2e-16 で 0 にならず、式で作った水平・垂直線が微小に傾く)。 */
function sinDeg(deg: number): number {
  const turn = turnOf(deg)
  if (turn % 90 === 0) return [0, 1, 0, -1][turn / 90]
  return Math.sin((deg * Math.PI) / 180)
}

function cosDeg(deg: number): number {
  const turn = turnOf(deg)
  if (turn % 90 === 0) return [1, 0, -1, 0][turn / 90]
  return Math.cos((deg * Math.PI) / 180)
}

/** `tan(90)` は NaN を返す(`Math.tan(Math.PI/2)` は 1.6e16 という有限のでたらめを返す)。 */
function tanDeg(deg: number): number {
  const turn = turnOf(deg)
  if (turn % 180 === 90) return Number.NaN
  if (turn % 90 === 0) return 0
  return Math.tan((deg * Math.PI) / 180)
}

function toDeg(rad: number): number {
  return (rad * 180) / Math.PI
}

interface FunctionDef {
  arity: number
  apply(args: number[]): number
}

/** `Map` で持つ —— 素のオブジェクトだと `toString` のような継承名が関数として引ける。 */
const FUNCTIONS = new Map<string, FunctionDef>([
  ['sqrt', { arity: 1, apply: (args) => Math.sqrt(args[0]) }],
  ['abs', { arity: 1, apply: (args) => Math.abs(args[0]) }],
  ['sin', { arity: 1, apply: (args) => sinDeg(args[0]) }],
  ['cos', { arity: 1, apply: (args) => cosDeg(args[0]) }],
  ['tan', { arity: 1, apply: (args) => tanDeg(args[0]) }],
  ['asin', { arity: 1, apply: (args) => toDeg(Math.asin(args[0])) }],
  ['acos', { arity: 1, apply: (args) => toDeg(Math.acos(args[0])) }],
  ['atan', { arity: 1, apply: (args) => toDeg(Math.atan(args[0])) }],
  ['atan2', { arity: 2, apply: (args) => toDeg(Math.atan2(args[0], args[1])) }],
])

const CONSTANTS = new Map<string, number>([['pi', Math.PI]])

// ============================================================================ 字句

type Token =
  | { kind: 'number'; value: number }
  | { kind: 'name'; text: string }
  | { kind: 'op'; text: '+' | '-' | '*' | '/' | '^' }
  | { kind: 'lparen' }
  | { kind: 'rparen' }
  | { kind: 'comma' }

const NUMBER_HEAD = /[0-9.]/
const NUMBER = /^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?/
const NAME = /^[A-Za-z_][A-Za-z0-9_]*/
const NAME_HEAD = /[A-Za-z_]/
const SPACE = /\s/
const OPERATORS = '+-*/^'

/** 日本語IME入力の式を読めるようにする。NFKC が全角を畳み、NFKC が触らない数学記号だけ個別に置き換える。 */
function normalize(text: string): string {
  return text
    .normalize('NFKC')
    .replace(/[−‐-―]/g, '-')
    .replace(/×/g, '*')
    .replace(/÷/g, '/')
    .trim()
}

function tokenize(source: string): Token[] {
  const tokens: Token[] = []
  let index = 0
  while (index < source.length) {
    const char = source[index]
    if (SPACE.test(char)) {
      index += 1
      continue
    }
    if (NUMBER_HEAD.test(char)) {
      const matched = NUMBER.exec(source.slice(index))
      if (matched === null) throw new ExpressionError(`数値として読めない: ${source.slice(index)}`)
      tokens.push({ kind: 'number', value: Number.parseFloat(matched[0]) })
      index += matched[0].length
      continue
    }
    if (NAME_HEAD.test(char)) {
      const matched = NAME.exec(source.slice(index)) as RegExpExecArray
      tokens.push({ kind: 'name', text: matched[0].toLowerCase() })
      index += matched[0].length
      continue
    }
    if (char === '(') tokens.push({ kind: 'lparen' })
    else if (char === ')') tokens.push({ kind: 'rparen' })
    else if (char === ',') tokens.push({ kind: 'comma' })
    else if (OPERATORS.includes(char)) tokens.push({ kind: 'op', text: char as '+' })
    else throw new ExpressionError(`使えない文字: ${char}`)
    index += 1
  }
  return tokens
}

// ============================================================================ 構文

interface Cursor {
  tokens: Token[]
  index: number
}

function peek(cursor: Cursor): Token | null {
  return cursor.tokens[cursor.index] ?? null
}

function isOp(token: Token | null, ...texts: string[]): boolean {
  return token !== null && token.kind === 'op' && texts.includes(token.text)
}

function parseSum(cursor: Cursor): number {
  let value = parseProduct(cursor)
  for (let token = peek(cursor); isOp(token, '+', '-'); token = peek(cursor)) {
    cursor.index += 1
    const right = parseProduct(cursor)
    value = isOp(token, '+') ? value + right : value - right
  }
  return value
}

function parseProduct(cursor: Cursor): number {
  let value = parseUnary(cursor)
  for (let token = peek(cursor); isOp(token, '*', '/'); token = peek(cursor)) {
    cursor.index += 1
    const right = parseUnary(cursor)
    value = isOp(token, '*') ? value * right : value / right
  }
  return value
}

/** `-2^2` が `-(2^2)` になる並び(単項マイナスはべき乗より弱い、数学の慣習どおり)。 */
function parseUnary(cursor: Cursor): number {
  const token = peek(cursor)
  if (isOp(token, '-')) {
    cursor.index += 1
    return -parseUnary(cursor)
  }
  if (isOp(token, '+')) {
    cursor.index += 1
    return parseUnary(cursor)
  }
  return parsePower(cursor)
}

/** `^` は右結合(`2^3^2` は `2^(3^2)`)。指数側に単項マイナスが来られる(`2^-1`)。 */
function parsePower(cursor: Cursor): number {
  const base = parseAtom(cursor)
  if (!isOp(peek(cursor), '^')) return base
  cursor.index += 1
  return base ** parseUnary(cursor)
}

function parseAtom(cursor: Cursor): number {
  const token = peek(cursor)
  if (token === null) throw new ExpressionError('式が途中で終わっている')
  if (token.kind === 'number') {
    cursor.index += 1
    return token.value
  }
  if (token.kind === 'lparen') {
    cursor.index += 1
    const value = parseSum(cursor)
    expectClose(cursor)
    return value
  }
  if (token.kind === 'name') {
    cursor.index += 1
    if (peek(cursor)?.kind === 'lparen') return parseCall(cursor, token.text)
    const constant = CONSTANTS.get(token.text)
    if (constant !== undefined) return constant
    if (FUNCTIONS.has(token.text)) {
      throw new ExpressionError(`${token.text} は ${token.text}(...) と書く`)
    }
    throw new ExpressionError(`知らない名前: ${token.text}`)
  }
  throw new ExpressionError('ここに数値が要る')
}

function parseCall(cursor: Cursor, name: string): number {
  const definition = FUNCTIONS.get(name)
  if (definition === undefined) {
    throw new ExpressionError(CONSTANTS.has(name) ? `${name} は関数ではない` : `知らない関数: ${name}`)
  }
  cursor.index += 1
  const args: number[] = []
  if (peek(cursor)?.kind !== 'rparen') {
    args.push(parseSum(cursor))
    while (peek(cursor)?.kind === 'comma') {
      cursor.index += 1
      args.push(parseSum(cursor))
    }
  }
  expectClose(cursor)
  if (args.length !== definition.arity) {
    throw new ExpressionError(`${name}() の引数は${definition.arity}個`)
  }
  return definition.apply(args)
}

function expectClose(cursor: Cursor): void {
  if (peek(cursor)?.kind !== 'rparen') throw new ExpressionError('括弧が閉じていない')
  cursor.index += 1
}

// ============================================================================ 口

const PLAIN_NUMBER = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/

export function isPlainNumber(text: string): boolean {
  return PLAIN_NUMBER.test(normalize(text))
}

/** 打った文字列を数にする。末尾のゴミは捨てず理由を返して拒む(`parseFloat('50mm')` は 50 を返す)。 */
export function evaluateExpression(text: string): ExpressionResult {
  const source = normalize(text)
  if (source === '') return { ok: false, reason: '式が空' }
  try {
    const cursor: Cursor = { tokens: tokenize(source), index: 0 }
    const value = parseSum(cursor)
    if (cursor.index !== cursor.tokens.length) throw new ExpressionError('余分な文字がある')
    if (!Number.isFinite(value)) throw new ExpressionError('値が数にならない(0除算・定義域外)')
    return { ok: true, value }
  } catch (error) {
    if (error instanceof ExpressionError) return { ok: false, reason: error.message }
    // 括弧の入れ子だけが再帰する。深く貼り付けられた `RangeError` を投げたままだと、呼ぶ `$derived` ごと画面が落ちる。
    if (error instanceof RangeError) return { ok: false, reason: '式が深すぎる' }
    throw error
  }
}

/**
 * 打ってある欄のうち、読めない式があれば最初の1つの理由(`ラベル: 理由`。無ければ null。空欄は読まない)。
 * 読めない式は値にならないので、点から寸法を導く欄(`fromPoints`)では空欄と見分けがつかず、打ち間違いが黙って捨てられる。
 * 確定する側がこれを見て止める。
 */
export function firstUnreadableExpression(fields: ReadonlyArray<{ label: string; raw: string }>): string | null {
  for (const { label, raw } of fields) {
    if (raw === '') continue
    const evaluated = evaluateExpression(raw)
    if (!evaluated.ok) return `${label}: ${evaluated.reason}`
  }
  return null
}
