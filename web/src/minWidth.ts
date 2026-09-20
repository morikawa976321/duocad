/**
 * 形状最適化の最小幅[mm]の判定と表示。
 *
 * 判定はサーバーの `optimize_run._resolve_min_width` と同じ比較(丸めない値どうし)で行い、
 * 表示だけを丸める。**丸める向きは「表示した値がそのまま通る」側**にする —— 下限は切り上げ、上限は
 * 切り捨て、保証される幅は切り捨て(`toFixed(2)` の四捨五入だと、要素サイズが 2.4501mm のとき
 * 「下限 4.90mm」と表示されたまま 4.90mm が下限割れになる)。
 */
import type { OptimizeResolutionStep } from './session'

export type MinWidthVerdict =
  | { kind: 'none' }
  | { kind: 'below' }
  | { kind: 'above' }
  | { kind: 'ok'; guaranteedMm: number }

/** 小数2桁へ丸める。`up` は切り上げ(値以上)、`down` は切り捨て(値以下)。 */
function roundTo2(value: number, direction: 'up' | 'down'): string {
  const nearest = Number(value.toFixed(2))
  if (direction === 'up' && nearest < value) return (nearest + 0.01).toFixed(2)
  if (direction === 'down' && nearest > value) return (nearest - 0.01).toFixed(2)
  return nearest.toFixed(2)
}

/** 下限の表示。この値を入力すれば下限割れにならない。 */
export function shownFloorMm(step: OptimizeResolutionStep): string {
  return roundTo2(step.min_width_floor_mm, 'up')
}

/** 上限の表示。この値を入力すれば上限超えにならない。 */
export function shownCeilingMm(step: OptimizeResolutionStep): string {
  return roundTo2(step.min_width_ceiling_mm, 'down')
}

/** 入力値(`null` は指定なし)の判定。指定なしは常に有効(制約なし)。 */
export function judgeMinWidth(valueMm: number | null, step: OptimizeResolutionStep | null): MinWidthVerdict {
  if (valueMm === null || step === null) return { kind: 'none' }
  if (valueMm < step.min_width_floor_mm) return { kind: 'below' }
  if (valueMm > step.min_width_ceiling_mm) return { kind: 'above' }
  // サーバーは `m = ceil(値 / 要素サイズ)` を上限のセル数で頭打ちにする(`_resolve_min_width`)。
  const guaranteed = Math.min(Math.ceil(valueMm / step.element_size_mm) * step.element_size_mm, step.min_width_ceiling_mm)
  return { kind: 'ok', guaranteedMm: guaranteed }
}

/** 無効な入力に添える説明(パネルの赤字とトーストで同じ文を使う)。 */
export function minWidthProblem(verdict: MinWidthVerdict, step: OptimizeResolutionStep): string | null {
  if (verdict.kind === 'below') return `最小幅は下限 ${shownFloorMm(step)}mm(選んだ要素サイズの2倍)を割っている`
  if (verdict.kind === 'above') {
    return `最小幅は上限 ${shownCeilingMm(step)}mm(格子の最短の辺の長さ。これより太い立方は入らない)を超えている`
  }
  return null
}

/** 実際に保証される幅の表示(切り捨て。表示が保証を超えないように)。 */
export function shownGuaranteedMm(verdict: MinWidthVerdict): string | null {
  return verdict.kind === 'ok' ? roundTo2(verdict.guaranteedMm, 'down') : null
}
