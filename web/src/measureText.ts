/**
 * 測定の結果(`MeasureBetween`)を人が読む文にする。**数値の書式はここ1箇所**
 * (読み札と3Dの札が別々に丸めると、同じ測定で違う数字が並ぶ)。
 *
 * 桁は小数3桁(0.001mm / 0.001度)で固定 —— CAD の寸法表示の慣例で、単位は mm 固定。
 */
import type { MeasureBetween } from './session'

const PAIR_NAMES: Record<string, string> = {
  'point-point': '点と点',
  'point-line': '点と直線',
  'point-plane': '点と平面',
  'line-line': '直線と直線',
  'line-plane': '直線と平面',
  'plane-plane': '平面と平面',
}

function fixed3(value: number): string {
  return value.toFixed(3)
}

/** 3Dの札に出す短い文(数値と単位だけ)。 */
export function measureLabel(between: MeasureBetween): string {
  if (between.distance !== null) return `${fixed3(between.distance)} mm`
  if (between.angle_deg !== null) return `${fixed3(between.angle_deg)}°`
  return ''
}

/** 画面の読み札に出す1行(組の名前・距離か角度・点と点なら Δxyz)。 */
export function measureReadout(between: MeasureBetween): string {
  const pair = PAIR_NAMES[between.kind] ?? between.kind
  if (between.distance !== null) {
    const delta =
      between.delta !== null && between.delta.length === 3
        ? `  Δx ${fixed3(between.delta[0])}  Δy ${fixed3(between.delta[1])}  Δz ${fixed3(between.delta[2])}`
        : ''
    return `${pair}: 距離 ${fixed3(between.distance)} mm${delta}`
  }
  if (between.angle_deg !== null) {
    return `${pair}: 角度 ${fixed3(between.angle_deg)}°(平行ではない)`
  }
  return pair
}
