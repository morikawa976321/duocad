/**
 * 基準グリッドまわりの純関数群。**three.js を持ち込まない** —— Node からこのファイルだけを
 * 読んで検査できる(`tests/eval_grid_scale.mjs`)。
 *
 * 中身は独立した2つの話である:
 *
 * 1. **刻みと不透明度**(`TIERS` / `mmPerPixel` / `tierAlpha`)。刻みは 1/10/100mm の
 *    **絶対値で固定**で、内容の大きさでも視点でも変わらない。変わるのは段ごとの不透明度
 *    だけで、**画面上の線間隔[px]** という1つの量から1本の規則で決まる。
 * 2. **範囲**(`BASE_EXTENT` / `snap125` / `extentFor`)。**描画には使わない** ——
 *    格子はシェーダーで無限に描く。ここが決めるのは**基準平面のピック範囲**
 *    (`viewport.ts` の `basePlaneAt`)と **near/far の下限**(`updateClipPlanes`)、
 *    面外の軸線の長さである。描画を無限にしたからといってピック範囲まで無限にはしない
 *    (engine `duocad-base-planes-lose-to-solids-by-depth` /
 *    `viewport-resize-silently-changes-what-a-click-grabs` が同時に悪化する)。
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
