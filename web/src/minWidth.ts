/**
 * 形状最適化の要素サイズ・最小幅[mm]の選び方と表示。最小幅はサーバーが配る段(`min_width_steps_mm`)から選ぶだけ。
 * 表示の丸めは「表示した値がそのまま通る」向き: 下限は切り上げ、上限と保証される幅は切り捨て
 * (四捨五入の `toFixed(2)` だと 4.9998mm が「5mm」になる)。
 */
import type { OptimizeResolutionPayload, OptimizeResolutionStep } from './session'

/**
 * 要素サイズの入力 `valueMm`(`null` = 空欄 = 既定)に対して効いている段。空欄なら梯子の先頭、
 * 入力があればサーバーが返した `selected`。応答がまだその値のものでない間は `null`(古い値の段で走らせない)。
 */
export function chosenStep(
  payload: OptimizeResolutionPayload | null,
  valueMm: number | null,
): OptimizeResolutionStep | null {
  if (payload === null || payload.error) return null
  if (valueMm === null) return payload.steps?.[0] ?? null
  if (payload.requested_element_size_mm !== valueMm) return null
  return payload.selected ?? null
}

/**
 * 要素サイズの入力できる範囲の表示。上限はサーバーが実格子へ切り下げる前の値まで受ける
 * (`optimize_run._resolve_element_size`)ので、条件面からの要求と固定値の細かい方を出す。
 */
export function shownElementSizeRange(payload: OptimizeResolutionPayload): { lower: string; upper: string } | null {
  const lower = payload.min_element_size_mm
  const snapped = payload.max_element_size_mm
  if (lower === undefined || snapped === undefined) return null
  const unsnapped = Math.min(payload.from_condition_faces_mm ?? snapped, payload.max_element_size_cap_mm ?? snapped)
  const upper = Math.max(snapped, unsnapped)
  const digits = digitsFor(lower)
  return { lower: roundTo(lower, digits, 'up'), upper: roundTo(upper, digits, 'down') }
}

/**
 * 選んでいる最小幅 `valueMm` を、段 `steps` のどれかへ寄せる。前に選んだ幅以上でいちばん細い段へ寄せ
 * (より細い段へは黙って下げない)、上限を超えていれば上限の段。`null` と段が無いときは `null`。
 * 段の値は m × 要素サイズと1ulp違うことがあるので、相対誤差で「同じ段」を判定する。
 */
export function snapMinWidth(steps: readonly number[], valueMm: number | null): number | null {
  if (valueMm === null || steps.length === 0) return null
  return steps.find((step) => step >= valueMm * (1 - 1e-9)) ?? steps[steps.length - 1]
}

/** 段の m(セル数)。段の値は 1ulp 下げてあることがあるので丸めて取る。 */
export function stepCells(step: OptimizeResolutionStep, valueMm: number): number {
  return Math.round(valueMm / step.element_size_mm)
}

/** 表示の小数桁数。既定2桁、値が 0.01 前後以下なら増やす。 */
function digitsFor(value: number): number {
  return Math.max(2, 2 - Math.floor(Math.log10(value)))
}

function roundTo(value: number, digits: number, direction: 'up' | 'down'): string {
  const nearest = Number(value.toFixed(digits))
  const unit = 10 ** -digits
  if (direction === 'up' && nearest < value) return (nearest + unit).toFixed(digits)
  if (direction === 'down' && nearest > value) return (nearest - unit).toFixed(digits)
  return nearest.toFixed(digits)
}

/** 保証される幅[mm]の表示(切り捨て。表示が保証を超えないように)。 */
export function shownMinWidthMm(valueMm: number): string {
  return roundTo(valueMm, digitsFor(valueMm), 'down')
}
