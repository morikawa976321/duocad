/**
 * スケッチの描く4verbが、集めた点と打った寸法から形を出す純関数。`sketch.ts` から切り出してあるのは、
 * ここだけが `import type` しか持たず `node` で直接評価できるため(`tests/eval_sketch_geometry.mjs`)。
 * 打った寸法が優先で、空欄は集めた点から導く。作れない形は `null`(呼び出し側が理由を出す)。
 */
import type { Point2 } from '../session'

const DEG = 180 / Math.PI

function dimension(typed: number | undefined, fallback: number): number {
  return typed === undefined ? fallback : typed
}

/** 中心 `c` から見た点 `p` の角度(度・反時計回り・X軸から・[0,360))。 */
function angleDeg(c: Point2, p: Point2): number {
  return ((Math.atan2(p[1] - c[1], p[0] - c[0]) * DEG) % 360 + 360) % 360
}

function cross(ax: number, ay: number, bx: number, by: number): number {
  return ax * by - ay * bx
}

/** 線分の終点。角度は度・反時計回り・スケッチ平面のX軸から(`add_arc` と同じ規約)。角度だけ打てば長さはマウスまで。長さ0は作れない。 */
export function lineFromPoints(
  p0: Point2,
  p1: Point2,
  typedLength: number | undefined,
  typedAngleDeg: number | undefined,
): { p0: Point2; p1: Point2 } | null {
  const du = p1[0] - p0[0]
  const dv = p1[1] - p0[1]
  const length = dimension(typedLength, Math.hypot(du, dv))
  const angle = dimension(typedAngleDeg, Math.atan2(dv, du) * DEG)
  if (!(length > 0)) return null
  const rad = angle / DEG
  return { p0, p1: [p0[0] + length * Math.cos(rad), p0[1] + length * Math.sin(rad)] }
}

/**
 * 線分の長さ・角度(表示専用。`add_line` の `NumberArgSpec.live` から呼ぶ)。`build` が送るのは `p0`/`p1` だけで
 * `length`/`angle_deg` は畳まれて消えるため、空欄の薄字に出す値は `p0`/`p1` から作り直す。
 */
export function lineLength(p0: Point2, p1: Point2): number {
  return Math.hypot(p1[0] - p0[0], p1[1] - p0[1])
}

export function lineAngleDeg(p0: Point2, p1: Point2): number {
  return angleDeg(p0, p1)
}

/**
 * 軸平行の長方形。打った寸法も「引いた向き」へ伸ばす(符号は対角がどちら側かで決まる)。
 * `opposite === corner`(点を1つ置いて寸法だけ打つ経路)では正側。
 */
export function rectFromPoints(
  corner: Point2,
  opposite: Point2,
  typedWidth: number | undefined,
  typedHeight: number | undefined,
): { corner: Point2; width: number; height: number } | null {
  const width = dimension(typedWidth, Math.abs(opposite[0] - corner[0]))
  const height = dimension(typedHeight, Math.abs(opposite[1] - corner[1]))
  if (!(width > 0) || !(height > 0)) return null
  const x = opposite[0] < corner[0] ? corner[0] - width : corner[0]
  const y = opposite[1] < corner[1] ? corner[1] - height : corner[1]
  return { corner: [x, y], width, height }
}

export function circleFromPoints(
  center: Point2,
  rim: Point2,
  typedRadius: number | undefined,
): { center: Point2; radius: number } | null {
  const radius = dimension(typedRadius, Math.hypot(rim[0] - center[0], rim[1] - center[1]))
  if (!(radius > 0)) return null
  return { center, radius }
}

function circumcenter(a: Point2, b: Point2, c: Point2): Point2 | null {
  const d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
  if (d === 0) return null
  const sa = a[0] * a[0] + a[1] * a[1]
  const sb = b[0] * b[0] + b[1] * b[1]
  const sc = c[0] * c[0] + c[1] * c[1]
  return [
    (sa * (b[1] - c[1]) + sb * (c[1] - a[1]) + sc * (a[1] - b[1])) / d,
    (sa * (c[0] - b[0]) + sb * (a[0] - c[0]) + sc * (b[0] - a[0])) / d,
  ]
}

/**
 * 始点・終点・円弧上の点(と半径)から `add_arc` の引数を出す。
 *
 * 層をまたぐ契約: `duocad/kernel_sketch.py` の弧は `sweep = (end_deg - start_deg) % 360` で必ず反時計回りだが、
 * 幽霊(`entityKinds.ts` の `arcPoints`)は差の符号どおりに描く。そのため常に `start_deg <= end_deg` を出す
 * (時計回りの弧は始点と終点を入れ替える。`end_deg` は360を超えうるが、どちらも差しか見ない)。
 * 向きは弧の中点が `via` と同じ側かで決める(優弧/劣弧の大小比較ではちょうど半円で破れる)。
 */
export function arcFromPoints(
  p0: Point2,
  p1: Point2,
  via: Point2,
  typedRadius: number | undefined,
): { center: Point2; radius: number; start_deg: number; end_deg: number } | null {
  const du = p1[0] - p0[0]
  const dv = p1[1] - p0[1]
  const chord = Math.hypot(du, dv)
  if (chord === 0) return null
  const side = cross(du, dv, via[0] - p0[0], via[1] - p0[1])
  if (side === 0) return null
  const found = circumcenter(p0, p1, via)
  if (found === null) return null

  // 中心が `via` と同じ側にある ⇔ 優弧。半径を打ち直したときの向きの引き継ぎに使う。
  const major = Math.sign(cross(du, dv, found[0] - p0[0], found[1] - p0[1])) === Math.sign(side)
  let center = found
  let radius = Math.hypot(found[0] - p0[0], found[1] - p0[1])

  if (typedRadius !== undefined) {
    const half = chord / 2
    if (!(typedRadius >= half)) return null
    radius = typedRadius
    const offset = Math.sqrt(radius * radius - half * half)
    // p0→p1 の左向き単位法線。`via` はもう円の上に無いので、side と major だけを引き継ぐ。
    const shift = (major ? 1 : -1) * Math.sign(side) * offset
    center = [
      (p0[0] + p1[0]) / 2 + shift * (-dv / chord),
      (p0[1] + p1[1]) / 2 + shift * (du / chord),
    ]
  }

  const a0 = angleDeg(center, p0)
  const a1 = angleDeg(center, p1)
  const sweep = (a1 - a0 + 360) % 360
  const mid = (a0 + sweep / 2) / DEG
  const midPoint: Point2 = [
    center[0] + radius * Math.cos(mid),
    center[1] + radius * Math.sin(mid),
  ]
  const forward = cross(du, dv, midPoint[0] - p0[0], midPoint[1] - p0[1]) * side > 0
  return forward
    ? { center, radius, start_deg: a0, end_deg: a0 + sweep }
    : { center, radius, start_deg: a1, end_deg: a1 + (360 - sweep) }
}
