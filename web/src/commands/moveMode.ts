/**
 * 移動の2つのverb(`move_body` / `move_sketch_entities`)が共有する**モードの読み方**。
 *
 * **どちらのコマンド定義にも置かない**(`features.ts` はソリッド・`sketch.ts` はスケッチの
 * 定義であって、片方をもう片方からimportすると分類の向きが逆になる)。綴りを2箇所に持つと、
 * 片方だけ直したときに**そのverbだけ黙って引数が揃わなくなる**。
 *
 * コア側は同じ綴りを `duocad/verbs/move_body.py` と `move_sketch.py` の両方に持っている
 * (verb同士がimportし合わない方針)。**TS側は1箇所**でよく、揃えるべきはコアとの綴りである。
 */
import type { ArgValues, BuildResult, ChoiceArgSpec, NumberArgSpec } from './command'

/** 「並進」「回転」の2択(`ChoiceArgSpec.options`。`buttonId` は使う側が付ける)。 */
export const MOVE_MODE_OPTIONS = [
  { value: 'translate', label: '並進' },
  { value: 'rotate', label: '回転' },
] as const

/**
 * **肯定形で書いてある**(`ArgSpecCommon.when` のdocstring) —— まだ `mode` を選んでいない
 * 間はどちらの述語も偽になり、収集は「種別を選ぶ」で止まる。`!== 'rotate'` のように
 * 否定で書くと、選ぶ前から並進側の引数を要求し始める。
 */
export function isTranslateMode(values: ArgValues): boolean {
  return values.mode === 'translate'
}

export function isRotateMode(values: ArgValues): boolean {
  return values.mode === 'rotate'
}

/**
 * 回転角として使えない値なら理由を返す。**360の倍数はコアが ValueError にする**
 * (0度・360度は移動なら何も変えず、複製なら元と完全に重なる)。
 */
export function rejectAngle(value: number): string | null {
  return value % 360 === 0 ? '角度が360の倍数(何も変わらない)' : null
}

// --------------------------------------------------------------------------------------
// 2つのコマンド定義が書き写していた部品。`buttonId` / `inputId` は `prefix`(`move-body` / `sketch-move`)
// から作る —— 実機検査が叩く id(`#move-body-dx`・`#sketch-move-rotate` 等)はここで決まる。
// --------------------------------------------------------------------------------------
/** 「動かす / 複製する」の2択(`${prefix}-move` / `${prefix}-copy`)。 */
export function copyOptions(prefix: string): ChoiceArgSpec['options'] {
  return [
    { value: 'move', label: '動かす', buttonId: `${prefix}-move` },
    { value: 'copy', label: '複製する', buttonId: `${prefix}-copy` },
  ]
}

/** 種別(並進 / 回転)の選択肢に `buttonId`(`${prefix}-translate` / `${prefix}-rotate`)を付けたもの。 */
export function modeOptions(prefix: string): ChoiceArgSpec['options'] {
  return MOVE_MODE_OPTIONS.map((option) => ({ ...option, buttonId: `${prefix}-${option.value}` }))
}

/**
 * 移動量の数値引数(`kind: 'number'` は宣言側が書く)。空欄はその軸を動かさない(=0)扱いで、
 * 全軸とも空欄(=全部0)は `buildMove` が別途断る(`NumberArgSpec.default` の docstring)。
 */
export function offsetArg(
  axis: 'dx' | 'dy' | 'dz',
  prefix: string,
  reject: (value: number) => string | null,
): Omit<NumberArgSpec, 'kind'> {
  return {
    name: axis,
    label: axis[1].toUpperCase(),
    unit: 'mm',
    inputId: `${prefix}-${axis}`,
    reject,
    default: 0,
    when: isTranslateMode,
  }
}

/**
 * 角度の数値引数。**既定値を置かない**(dx/dy/dz と違い「空欄=回さない」が成立しない ——
 * 0度はコアが断る値であって、無指定の代わりにはならない)。符号は u->v の反時計回りが正。
 */
export function angleArg(prefix: string): Omit<NumberArgSpec, 'kind'> {
  return { name: 'angle', label: '角度', unit: '度', inputId: `${prefix}-angle`, reject: rejectAngle, when: isRotateMode }
}

/**
 * 移動2コマンドの `build`: 対象が空なら断る → 回転なら角度を検査して `rotateArgs`(軸・中心の参照を載せる)→
 * 並進なら数値を検査して全部0を断る。`copy` は両方の末尾に載せる。
 *
 * **`mode` を明示的に載せる** —— コア側の既定も `translate` だが、モードを人に選ばせた結果を送らないと、
 * 画面の「並進」ボタンが何も意味しないことになる。**全部0はコアも弾く**(移動なら何も起きず、複製なら元と
 * 完全に重なる)が、ここで先に断るのは理由を人の近くで出すため。対象そのものは送らない(サーバがコアの
 * 選択から取る)。
 */
export function buildMove(
  values: ArgValues,
  spec: {
    missingTarget: (values: ArgValues) => string | null
    axes: ReadonlyArray<'dx' | 'dy' | 'dz'>
    zeroReason: string
    rotateArgs: (angle: number, values: ArgValues) => BuildResult
  },
): BuildResult {
  const missing = spec.missingTarget(values)
  if (missing !== null) return { ok: false, reason: missing }
  const copy = values.copy === 'copy'
  if (values.mode === 'rotate') {
    const angle = values.angle
    if (typeof angle !== 'number') return { ok: false, reason: '角度が数値でない' }
    const reason = rejectAngle(angle)
    if (reason !== null) return { ok: false, reason }
    const built = spec.rotateArgs(angle, values)
    return built.ok ? { ok: true, args: { ...built.args, copy } } : built
  }
  const offsets = spec.axes.map((axis) => values[axis])
  if (!offsets.every((value): value is number => typeof value === 'number')) {
    return { ok: false, reason: '移動量が数値でない' }
  }
  if (offsets.every((value) => value === 0)) return { ok: false, reason: spec.zeroReason }
  const args: Record<string, unknown> = { mode: 'translate' }
  spec.axes.forEach((axis, at) => {
    args[axis] = offsets[at]
  })
  return { ok: true, args: { ...args, copy } }
}
