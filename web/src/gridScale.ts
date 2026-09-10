/**
 * 基準グリッドまわりの純関数群。**three.js を持ち込まない** —— Node からこのファイルだけを
 * 読んで検査できる(`tests/eval_grid_scale.mjs`)。
 *
 * 中身は独立した2つの話である:
 *
 * 1. **刻みと不透明度**(`TIERS` / `mmPerPixel` / `tierAlpha`)。刻みは 1/10/100mm の
 *    **絶対値で固定**で、内容の大きさでも視点でも変わらない。変わるのは段ごとの不透明度
 *    だけで、**画面上の線間隔[px]** という1つの量から1本の規則で決まる。
 * 2. **ピック範囲**(`BASE_EXTENT` / `snap125` / `extentFor`)。基準平面の当たり判定
 *    (`viewport.ts` の `basePlaneAt`)と near/far の下限(`updateClipPlanes`)が使う。
 * 3. **描画範囲**(`boundingSquare` / `snapForSpan`)。内容(すべてのスケッチとソリッド)を
 *    一回り大きく包む**正方形**の内側だけに描く。
 */

// ======================================================================== 刻みと不透明度

/** 刻み[mm]。**3段固定**(細/中/太)。内容の大きさでも視点でも変わらない。 */
export const TIERS = [1, 10, 100] as const

/** 線間隔[px]がこれ未満なら不透明度0(完全に消える)。 */
export const FADE_MIN = 1.5

/** 線間隔[px]がこれ以上なら不透明度1。 */
export const FADE_FULL = 6

/**
 * カメラの見え方を投影の種類ごとに表した最小限の形。**FOVは0(完全正投影)〜90で可変**
 * (engine `duocad-fov-adjustable-from-ui`)なので、カメラ距離だけで判定すると正投影で
 * 破綻する —— 正投影には「距離」に相当する量が無い。
 */
export type CameraProjection =
  | { kind: 'persp'; fovDeg: number; distance: number }
  | { kind: 'ortho'; frustumHeight: number }

/**
 * 画面1px が何mmに相当するか。**透視・正投影の両方を1つの関数で扱う。**
 *
 * 透視は注視点までの距離で代表させる(`2 * tan(fov/2) * distance / height`)。正投影は
 * 視錐台の高さをそのまま画素で割る。**どちらも正の有限値で、fovDeg→0 でも符号は
 * 変わらない**(0へ単調に近づくだけ) —— 透視と正投影は別種の投影なので、値が一致する
 * 必要は無い。
 */
export function mmPerPixel(cam: CameraProjection, viewportHeightPx: number): number {
  const height = Math.max(1, viewportHeightPx)
  if (cam.kind === 'ortho') return Math.max(0, cam.frustumHeight) / height
  const halfAngleRad = (Math.max(0, cam.fovDeg) * Math.PI) / 360
  return (2 * Math.tan(halfAngleRad) * Math.max(0, cam.distance)) / height
}

/**
 * 線間隔[px]から不透明度。**これが3段に共通する唯一の規則**で、段ごとの特例は無い
 * (100mm段も同じ規則を通り、極端に引けば自動的に消える)。
 *
 * ON/OFFの瞬間切替にしないのは、閾値を跨ぐたびの点滅を**原理的に**無くすため
 * (現行実装がヒステリシスで対処していた問題そのもの。連続的なフェードにすれば
 * ヒステリシスは要らなくなる)。`smoothstep` なので値も傾きも連続で、常に [0, 1]。
 *
 * **フラグメントシェーダー側も同じ式を持つ**(`viewport.ts` の `fadeFromSpacingPx` の
 * GLSL版)。あちらは1画素ごとの局所的な線間隔に、こちらは注視点での代表値に適用する ——
 * 定数は両方ともここから注入するので、数値の正はこのファイル1つである。
 */
export function fadeFromSpacingPx(spacingPx: number): number {
  if (Number.isNaN(spacingPx)) return 0
  const t = Math.min(1, Math.max(0, (spacingPx - FADE_MIN) / (FADE_FULL - FADE_MIN)))
  return t * t * (3 - 2 * t)
}

/**
 * ある段(刻み `stepMm`)の不透明度。線間隔pxが `FADE_MIN` 未満で0、`FADE_FULL` 以上で1。
 *
 * `mmPerPx` が0以下(＝無限に寄っている)なら線間隔は無限大なので1。非有限な入力は
 * 0を返す(見えない側へ倒す) —— 描かない方が、壊れた値で画面を埋めるより安全である。
 */
export function tierAlpha(stepMm: number, mmPerPx: number): number {
  if (!Number.isFinite(mmPerPx) || !Number.isFinite(stepMm)) return 0
  if (mmPerPx <= 0) return 1
  return fadeFromSpacingPx(stepMm / mmPerPx)
}

/** 3段ぶんの不透明度をまとめて(`TIERS` と同じ並び)。 */
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

/** `extentFor` が受け取る外接直方体の最小限の形(`THREE.Box3` はこれを満たす)。 */
export interface Box3Like {
  isEmpty(): boolean
  min: { x: number; y: number; z: number }
  max: { x: number; y: number; z: number }
}

/**
 * 1-2-5 の桁上げ(範囲が丸い数字に載る)。**呼び出し元(`extentFor`)は正の有限値だけを
 * 渡す** —— ここでは防御しない(桁の対数を取るので0以下・非有限を渡すと `NaN`/`-Infinity`
 * が漏れて、呼び出し側で気づかず `BASE_EXTENT` の下限に守られたと勘違いする方が危険)。
 */
export function snap125(v: number): number {
  const decade = 10 ** Math.floor(Math.log10(v))
  for (const m of [1, 2, 5]) {
    if (v <= m * decade) return m * decade
  }
  return 10 * decade
}

/**
 * 原点中心の基準平面が内容を覆う半幅[mm]。**対角ではなく原点からの最遠成分**を使う ——
 * 基準平面は原点中心の正方形なので、対角(内容自体の大きさ)ではなく「原点からどれだけ
 * 離れているか」がそのまま必要な半幅になる。
 *
 * 空の文書(`box.isEmpty()`)では `BASE_EXTENT` を返す(±100mm)。**`* 1.2` の余白**は
 * 「ちょうどモデルの端で範囲が終わる」のを避けるためで、`snap125` の丸めが大半を吸収するが、
 * reach が1-2-5の境界すれすれのときに効く。
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
 *
 * **半幅ではなく中心と半径で持つ** —— 原点対称の半幅だと、面のframeの原点が面の隅に
 * 来る(OCCTのUVパラメータ化の原点は面の中心ではない)ときに枠が面の片側へ大きく偏る。
 * 実測: u:0-40 / v:-20-0 の面に対して ±50mm の対称な枠が出た。
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
 * 枠の縁を載せる刻み[mm]。**内容の大きさに合わせて段を選ぶ** —— 縁が段の倍数に載ると
 * 枠が太線で終わって意図的に見えるが、常に100mmに載せると40mmの面の枠が100mm角になり、
 * 「面の何倍もの大きさで描かれる」破綻に戻る。長辺の半分以下で最も粗い段を採る。
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
 * 内容のUV矩形へカーソル点を合成する。カーソル追従(グリッドがカーソル位置まで広がる仕様)の
 * 核となる1関数 —— `boundingSquare` を呼ぶ**前**にここで「内容 ∪ カーソル位置」を作ることで、
 * 余白(`BOUNDS_MARGIN_RATIO`)も目盛りスナップ(`snapForSpan`)も触らずに済む。
 *
 * **状態を持たない**。呼び出し側が「内容だけのUV矩形」と「いまのカーソル位置」を毎回渡して
 * 作り直す前提で、戻り値をどこにも保持しない —— カーソルが内容の近くへ戻れば、次に渡す点が
 * 内容の内側に収まり、自然に「内容だけ」の矩形へ戻る。
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
 * 内容を一回り大きく包む**正方形**を出す。四辺はすべて `snapForSpan` の倍数に載り、
 * かつ辺の長さが等しい(足りない軸は**段の整数個ぶん**ずつ両側へ伸ばす。半端に伸ばすと
 * 縁が倍数から外れる)。
 *
 * `includeOrigin` は基準平面用。モデルが原点から遠いときに原点が枠の外へ出ると
 * 「原点がどこか読める」というグリッド本来の役割が失われるので、**内容 ∪ 原点**を包む。
 * 面グリッドでは面自身の範囲が正なので `false` にする。
 *
 * 内容が無い(`box === null`)ときは従来どおり ±`BASE_EXTENT`(100mm)。
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
