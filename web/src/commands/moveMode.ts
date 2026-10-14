/**
 * 移動の2つのverb(`move_body` / `move_sketch_entities`)が共有するモードの読み方。
 * どちらの定義にも置かないのは、片方をもう片方から import すると分類の向きが逆になるため。
 * コア側は同じ綴りを `duocad/verbs/move_body.py` と `move_sketch.py` の両方に持つので、揃えるべきはコアとの綴り。
 */
import type { ArgValues, BuildResult, ChoiceArgSpec, NumberArgSpec } from './command'

export const MOVE_MODE_OPTIONS = [
  { value: 'translate', label: '並進' },
  { value: 'rotate', label: '回転' },
] as const

/**
 * `when` は肯定形で書く。まだ `mode` を選んでいない間は両述語が偽になり、収集は種別選択で止まる
 * (`!== 'rotate'` と書くと、選ぶ前から並進側の引数を要求し始める)。
 */
export function isTranslateMode(values: ArgValues): boolean {
  return values.mode === 'translate'
}

export function isRotateMode(values: ArgValues): boolean {
  return values.mode === 'rotate'
}

/** 回転角として使えない値なら理由を返す。360の倍数はコアが ValueError にする。 */
export function rejectAngle(value: number): string | null {
  return value % 360 === 0 ? '角度が360の倍数(何も変わらない)' : null
}

// `buttonId` / `inputId` は `prefix`(`move-body` / `sketch-move`)から作る。実機検査が叩く id(`#move-body-dx` 等)はここで決まる。
export function copyOptions(prefix: string): ChoiceArgSpec['options'] {
  return [
    { value: 'move', label: '動かす', buttonId: `${prefix}-move`, sends: false },
    { value: 'copy', label: '複製する', buttonId: `${prefix}-copy`, sends: true },
  ]
}

export function modeOptions(prefix: string): ChoiceArgSpec['options'] {
  return MOVE_MODE_OPTIONS.map((option) => ({ ...option, buttonId: `${prefix}-${option.value}` }))
}

/** 移動量の数値引数。空欄はその軸を動かさない(=0)扱いで、全軸空欄は `buildMove` が断る。 */
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

/** 角度の数値引数。既定値を置かない(0度はコアが断る値で、「空欄=回さない」が成立しない)。符号は u->v の反時計回りが正。 */
export function angleArg(prefix: string): Omit<NumberArgSpec, 'kind'> {
  return { name: 'angle', label: '角度', unit: '度', inputId: `${prefix}-angle`, reject: rejectAngle, when: isRotateMode }
}

/**
 * 移動2コマンドの `build`。`mode` は明示的に載せる(コア既定の `translate` に頼らない)。全部0はコアも弾く。
 * 対象は送らない(サーバがコアの選択から取る)。
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
