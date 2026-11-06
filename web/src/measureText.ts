/** 測定の結果(`MeasureBetween`)を文にする。数値の書式はここ1箇所(読み札と3Dの札で丸めが食い違わないように)。 */
import type { MeasureBetween } from './session'

const PAIR_NAMES: Record<string, string> = {
  'point-point': '点と点',
  'point-line': '点と直線',
  'point-plane': '点と平面',
  'line-line': '直線と直線',
  'line-plane': '直線と平面',
  'plane-plane': '平面と平面',
}

export function fixed3(value: number): string {
  return value.toFixed(3)
}

export function measureLabel(between: MeasureBetween): string {
  if (between.distance !== null) return `${fixed3(between.distance)} mm`
  if (between.angle_deg !== null) return `${fixed3(between.angle_deg)}°`
  return ''
}

export function measureReadout(between: MeasureBetween): string {
  // 直線・平面は有限の辺・面ではなく、無限に延ばして測る(`duocad/measure.py`)。
  const extended = between.kind.includes('line') || between.kind.includes('plane')
  const pair = (PAIR_NAMES[between.kind] ?? between.kind) + (extended ? '(無限に延長)' : '')
  if (between.distance !== null) {
    const delta =
      between.delta !== null && between.delta.length === 3
        ? `  Δx ${fixed3(between.delta[0])} mm  Δy ${fixed3(between.delta[1])} mm  Δz ${fixed3(between.delta[2])} mm`
        : ''
    return `${pair}: 距離 ${fixed3(between.distance)} mm${delta}`
  }
  if (between.angle_deg !== null) {
    return `${pair}: 角度 ${fixed3(between.angle_deg)}°(平行ではない)`
  }
  return pair
}
