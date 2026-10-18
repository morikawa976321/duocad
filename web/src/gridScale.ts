/**
 * 基準グリッドまわりの純関数群。three.js を持ち込まない(Node からこのファイルだけを読んで
 * 検査できる。`tests/eval_grid_scale.mjs`)。
 *
 * 1. 刻みと不透明度(`TIERS` / `mmPerPixel` / `tierAlpha`)。刻みは 1/10/100mm の絶対値固定で、
 *    変わるのは段ごとの不透明度だけ(画面上の線間隔[px]から決まる)。
 * 2. ピック範囲(`BASE_EXTENT` / `snap125` / `extentFor`)。`viewport.ts` の `basePlaneAt` と
 *    `updateClipPlanes` の near/far 下限が使う。
 * 3. 描画範囲(`boundingSquare` / `snapForSpan`)。内容を一回り大きく包む正方形の内側だけに描く。
 */

// ======================================================================== 刻みと不透明度

/** 刻み[mm]。3段固定(細/中/太)。 */
export const TIERS = [1, 10, 100] as const

/** 線間隔[px]がこれ未満なら不透明度0(完全に消える)。 */
export const FADE_MIN = 1.5

/** 線間隔[px]がこれ以上なら不透明度1。 */
export const FADE_FULL = 6

/**
 * 投影の種類ごとの最小限のカメラ。FOVは0(完全正投影)〜90で可変なので、
 * 距離だけで判定すると正投影で破綻する(正投影に「距離」は無い)。
 */
export type CameraProjection =
  | { kind: 'persp'; fovDeg: number; distance: number }
  | { kind: 'ortho'; frustumHeight: number }

/**
 * 画面1px が何mmか。透視は注視点までの距離で代表させ(`2 * tan(fov/2) * distance / height`)、
 * 正投影は視錐台の高さを画素で割る。fovDeg→0 でも符号は変わらない(0へ単調に近づく)。
 */
export function mmPerPixel(cam: CameraProjection, viewportHeightPx: number): number {
  const height = Math.max(1, viewportHeightPx)
  if (cam.kind === 'ortho') return Math.max(0, cam.frustumHeight) / height
  const halfAngleRad = (Math.max(0, cam.fovDeg) * Math.PI) / 360
  return (2 * Math.tan(halfAngleRad) * Math.max(0, cam.distance)) / height
}

/**
 * 線間隔[px]から不透明度。3段に共通する唯一の規則で、段ごとの特例は無い。
 * smoothstep で値も傾きも連続・常に [0, 1] —— 閾値を跨ぐ点滅を原理的に無くす(ヒステリシス不要)。
 *
 * **フラグメントシェーダー側も同じ式を持つ**(`planeGrid.ts` の `fadeFromSpacingPx` のGLSL版)。
 * あちらは1画素ごとの局所的な線間隔に、こちらは注視点での代表値に適用する。
 * 定数は両方ともここから注入する。
 */
export function fadeFromSpacingPx(spacingPx: number): number {
  if (Number.isNaN(spacingPx)) return 0
  const t = Math.min(1, Math.max(0, (spacingPx - FADE_MIN) / (FADE_FULL - FADE_MIN)))
  return t * t * (3 - 2 * t)
}

/**
 * ある段(刻み `stepMm`)の不透明度。線間隔pxが `FADE_MIN` 未満で0、`FADE_FULL` 以上で1。
 * `mmPerPx` が0以下なら1、非有限な入力は0(見えない側へ倒す)。
 */
export function tierAlpha(stepMm: number, mmPerPx: number): number {
  if (!Number.isFinite(mmPerPx) || !Number.isFinite(stepMm)) return 0
  if (mmPerPx <= 0) return 1
  return fadeFromSpacingPx(stepMm / mmPerPx)
}

export function tierAlphas(mmPerPx: number): [number, number, number] {
  return [
    tierAlpha(TIERS[0], mmPerPx),
    tierAlpha(TIERS[1], mmPerPx),
    tierAlpha(TIERS[2], mmPerPx),
  ]
}

// ======================================================================== 範囲(ピック・クリップ)

/** 空の文書のピック範囲の半幅[mm]。どんな内容の大きさでもこれより狭くはしない下限。 */
export const BASE_EXTENT = 100

export interface Box3Like {
  isEmpty(): boolean
  min: { x: number; y: number; z: number }
  max: { x: number; y: number; z: number }
}

/**
 * 1-2-5 の桁上げ。呼び出し元(`extentFor`)は正の有限値だけを渡す前提で防御しない
 * (0以下・非有限は `NaN`/`-Infinity` が漏れる)。
 */
export function snap125(v: number): number {
  const decade = 10 ** Math.floor(Math.log10(v))
  for (const m of [1, 2, 5]) {
    if (v <= m * decade) return m * decade
  }
  return 10 * decade
}

/**
 * 原点中心の基準平面が内容を覆う半幅[mm]。基準平面は原点中心の正方形なので、
 * 対角ではなく原点からの最遠成分を使う。空の文書は `BASE_EXTENT`。
 * `* 1.2` の余白は reach が1-2-5の境界すれすれのときに端ぴったりになるのを避ける。
 */
export function extentFor(box: Box3Like): number {
  if (box.isEmpty()) return BASE_EXTENT
  const reach = Math.max(
    Math.abs(box.min.x), Math.abs(box.max.x),
    Math.abs(box.min.y), Math.abs(box.max.y),
    Math.abs(box.min.z), Math.abs(box.max.z),
  )
  return Math.max(BASE_EXTENT, snap125(reach * 1.2))
}

// ======================================================================== 描画範囲(正方形)

/**
 * 格子を描く正方形。frame上の u/v[mm] で、`centerU ± half` / `centerV ± half` が枠。
 * 中心と半径で持つ: OCCTのUVの原点は面の中心とは限らず、原点対称の半幅だと枠が面の片側へ偏る
 * (実測: u:0-40 / v:-20-0 の面に ±50mm の対称な枠が出た)。
 */
export interface UvBounds {
  centerU: number
  centerV: number
  /** 一辺の半分[mm]。 */
  half: number
}

/** 「一回り大きく」の余白(内容の長辺に対する比)。 */
export const BOUNDS_MARGIN_RATIO = 0.5

/** 内容が枠の縁ぴったりに来たときでも必ず余白が付くための下限[mm]。 */
const BOUNDS_MIN_MARGIN = 1e-6

/**
 * 枠の縁を載せる刻み[mm]。長辺の半分以下で最も粗い段を採る
 * (常に100mmだと40mmの面が100mm角で描かれる)。
 */
export function snapForSpan(span: number): number {
  if (!Number.isFinite(span) || span <= 0) return TIERS[0]
  let chosen: number = TIERS[0]
  for (const tier of TIERS) {
    if (tier <= span / 2) chosen = tier
  }
  return chosen
}

/** frame上へ射影した内容の外接矩形[mm]。測れないときは `null`。 */
export interface UvBox {
  uMin: number
  uMax: number
  vMin: number
  vMax: number
}

/**
 * 内容のUV矩形へカーソル点を合成する(カーソル追従)。`boundingSquare` の前に「内容 ∪ カーソル」を
 * 作れば、余白もスナップも触らずに済む。状態を持たない: 呼び出し側が毎回、内容だけの矩形と
 * いまのカーソル位置を渡す。
 */
export function extendUvBoxToPoint(box: UvBox | null, point: { u: number; v: number } | null): UvBox | null {
  if (point === null) return box
  if (box === null) return { uMin: point.u, uMax: point.u, vMin: point.v, vMax: point.v }
  return {
    uMin: Math.min(box.uMin, point.u),
    uMax: Math.max(box.uMax, point.u),
    vMin: Math.min(box.vMin, point.v),
    vMax: Math.max(box.vMax, point.v),
  }
}

/**
 * 内容を一回り大きく包む正方形を出す。四辺はすべて `snapForSpan` の倍数に載り、辺の長さが等しい
 * (足りない軸は段の整数個ぶんずつ両側へ伸ばす。半端に伸ばすと縁が倍数から外れる)。
 *
 * `includeOrigin` は基準平面用。モデルが原点から遠いとき原点が枠の外へ出ると「原点がどこか読める」
 * 役割が失われるので、内容 ∪ 原点を包む。面グリッドでは `false`。
 * 内容が無い(`box === null`)ときは ±`BASE_EXTENT`。
 */
export function boundingSquare(box: UvBox | null, opts: { includeOrigin: boolean }): UvBounds {
  if (box === null) return { centerU: 0, centerV: 0, half: BASE_EXTENT }
  if (![box.uMin, box.uMax, box.vMin, box.vMax].every(Number.isFinite)) {
    return { centerU: 0, centerV: 0, half: BASE_EXTENT }
  }
  const uMin = opts.includeOrigin ? Math.min(box.uMin, 0) : box.uMin
  const uMax = opts.includeOrigin ? Math.max(box.uMax, 0) : box.uMax
  const vMin = opts.includeOrigin ? Math.min(box.vMin, 0) : box.vMin
  const vMax = opts.includeOrigin ? Math.max(box.vMax, 0) : box.vMax

  const span = Math.max(uMax - uMin, vMax - vMin)
  const margin = Math.max(span * BOUNDS_MARGIN_RATIO, BOUNDS_MIN_MARGIN)
  const step = snapForSpan(span)

  const u0 = Math.floor((uMin - margin) / step) * step
  const u1 = Math.ceil((uMax + margin) / step) * step
  const v0 = Math.floor((vMin - margin) / step) * step
  const v1 = Math.ceil((vMax + margin) / step) * step

  const uSteps = Math.round((u1 - u0) / step)
  const vSteps = Math.round((v1 - v0) / step)
  const side = Math.max(uSteps, vSteps)
  const uLo = u0 - Math.floor((side - uSteps) / 2) * step
  const uHi = u1 + Math.ceil((side - uSteps) / 2) * step
  const vLo = v0 - Math.floor((side - vSteps) / 2) * step
  const vHi = v1 + Math.ceil((side - vSteps) / 2) * step

  return { centerU: (uLo + uHi) / 2, centerV: (vLo + vHi) / 2, half: (side * step) / 2 }
}
