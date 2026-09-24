/**
 * 形状最適化の最小幅[mm]の判定と表示。
 *
 * 判定はサーバーの `optimize_run._resolve_min_width` と同じ比較(丸めない値どうし)で行い、
 * 表示だけを丸める。**丸める向きは「表示した値がそのまま通る」側**にする —— 下限は切り上げ、上限は
 * 切り捨て、保証される幅は切り捨て(四捨五入の `toFixed(2)` だと、要素サイズが 2.4501mm のとき
 * 「下限 4.90mm」と表示されたまま 4.90mm が下限割れになる)。
 */
import type { OptimizeResolutionStep } from './session'

export type MinWidthVerdict =
  | { kind: 'none' }
  | { kind: 'below' }
  | { kind: 'above' }
  | {
      kind: 'ok'
      /** 保証される段の番号 m(サーバーの `ceil(値 / 要素サイズ)` を上限で頭打ちにしたもの)。 */
      cells: number
      guaranteedMm: number
      /** 入力が段のあいだに落ちて次の段へ切り上がったときの、要求に対する増分の割合(0.46 = +46%)。段ちょうどなら `null`。 */
      roundedUpBy: number | null
      /** 切り上がった段(`cells`)の丸めない値(押すと段ちょうどの入力になる)。 */
      stepMm: number
      /** ひとつ下の段(`cells - 1`)の値。`cells` が下限の段(2)なら `null`。 */
      lowerStepMm: number | null
    }

/**
 * 表示の小数桁数。既定は2桁だが、値が 0.01 に近い・下回る小さい部品では、下限と上限が同じ桁で
 * 区別できるように有効数字3桁分まで増やす。
 */
function digitsFor(value: number): number {
  return Math.max(2, 2 - Math.floor(Math.log10(value)))
}

/** `digits` 桁へ丸める。`up` は切り上げ(値以上)、`down` は切り捨て(値以下)。 */
function roundTo(value: number, digits: number, direction: 'up' | 'down'): string {
  const nearest = Number(value.toFixed(digits))
  const unit = 10 ** -digits
  if (direction === 'up' && nearest < value) return (nearest + unit).toFixed(digits)
  if (direction === 'down' && nearest > value) return (nearest - unit).toFixed(digits)
  return nearest.toFixed(digits)
}

/** 下限の表示。この値を入力すれば下限割れにならない。 */
export function shownFloorMm(step: OptimizeResolutionStep): string {
  return roundTo(step.min_width_floor_mm, digitsFor(step.min_width_floor_mm), 'up')
}

/** 上限の表示。この値を入力すれば上限超えにならない(桁数は下限に揃える)。 */
export function shownCeilingMm(step: OptimizeResolutionStep): string {
  return roundTo(step.min_width_ceiling_mm, digitsFor(step.min_width_floor_mm), 'down')
}

/** 入力値(`null` は指定なし)の判定。指定なしはサーバーが既定(要素サイズの2倍と1mmの大きい方)を
 *  使うので、クライアントの下限・上限チェックの対象外として常に有効を返す。 */
export function judgeMinWidth(valueMm: number | null, step: OptimizeResolutionStep | null): MinWidthVerdict {
  if (valueMm === null || step === null) return { kind: 'none' }
  if (valueMm < step.min_width_floor_mm) return { kind: 'below' }
  if (valueMm > step.min_width_ceiling_mm) return { kind: 'above' }
  // サーバーは `m = ceil(値 / 要素サイズ)` を上限のセル数で頭打ちにする(`_resolve_min_width`)。
  const steps = step.min_width_steps_mm
  const cells = Math.min(Math.ceil(valueMm / step.element_size_mm), steps.length + 1)
  const guaranteed = Math.min(cells * step.element_size_mm, step.min_width_ceiling_mm)
  // 段ちょうどのボタン値は m × 要素サイズと1ulp違うことがあるので、相対誤差で「段のあいだ」を判定する。
  const excess = guaranteed / valueMm - 1
  return {
    kind: 'ok',
    cells,
    guaranteedMm: guaranteed,
    roundedUpBy: excess > 1e-9 ? excess : null,
    stepMm: steps[cells - 2],
    lowerStepMm: cells > 2 ? steps[cells - 3] : null,
  }
}

/** 無効な入力に添える説明(パネルの赤字とトーストで同じ文を使う)。 */
export function minWidthProblem(verdict: MinWidthVerdict, step: OptimizeResolutionStep): string | null {
  if (verdict.kind === 'below') return `最小幅は下限 ${shownFloorMm(step)}mm(選んだ要素サイズの2倍)を割っている`
  if (verdict.kind === 'above') {
    return `最小幅は上限 ${shownCeilingMm(step)}mm(格子の最短の辺の長さ。これより太い立方は入らない)を超えている`
  }
  return null
}

/** 保証される幅[mm]の表示(切り捨て。表示が保証を超えないように)。実行後の通知もこれを使う。 */
export function shownMinWidthMm(valueMm: number): string {
  return roundTo(valueMm, digitsFor(valueMm), 'down')
}

/** 実際に保証される幅の表示(`shownMinWidthMm`)。 */
export function shownGuaranteedMm(verdict: MinWidthVerdict): string | null {
  return verdict.kind === 'ok' ? shownMinWidthMm(verdict.guaranteedMm) : null
}

/** 段のボタンの表示。入力値ではない(m=2 の段を丸めて打つと、下限割れか m=3 になる。ボタンは丸めない値を入れる)。 */
export function shownStepMm(valueMm: number): string {
  return valueMm.toFixed(Math.min(digitsFor(valueMm) + 1, 100)) // toFixed の桁数の上限
}

/** 段のあいだに落ちた入力への説明(切り上げ先を、実行を押す前に見せる)。段ちょうどなら `null`。 */
export function roundUpNotice(verdict: MinWidthVerdict, valueMm: number | null): string | null {
  if (verdict.kind !== 'ok' || verdict.roundedUpBy === null || valueMm === null) return null
  return (
    `入力 ${valueMm}mm は段のあいだにあり、m=${verdict.cells} へ切り上がって ` +
    `${shownGuaranteedMm(verdict)}mm が保証される(要求より ${(verdict.roundedUpBy * 100).toFixed(1)}% 太い部品が出る)`
  )
}
