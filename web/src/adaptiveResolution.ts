/**
 * カメラの見え方から目標のテセレーション精度を決める変換(可変解像度)のTS側実装。
 * スケッチの円・円弧はここで折れ線になり、ソリッドと辺はバックエンド(OCCT)がテセレーションする。
 * 共有できるのは「画面の許容誤差 → 目標偏差」の変換だけ。
 *
 * Python側の対: `duocad/resolution.py`。定数の一致は `tests/test_adaptive_resolution.py` が検査する
 * (片方だけ変えると、同じ画面でスケッチの線とソリッドの辺の滑らかさが食い違うが、エラーは出ない)。
 * **定数は `export const NAME = <数値リテラル>` の形で書く**(検査がその形で読む)。
 *
 * **角度はすべてラジアン**(OCCTの角度偏差パラメータの単位に揃える)。
 */

/** 既定の線形偏差[mm](`duocad/kernel.py` の `DEFAULT_DEVIATION`)。 */
export const DEFAULT_DEVIATION = 0.2
/** 既定の角度偏差[ラジアン](`duocad/kernel.py` の `DEFAULT_ANGULAR_DEFLECTION`)。 */
export const DEFAULT_ANGULAR_DEFLECTION = 0.35

/** 画面上で許す弦と弧の乖離[画素]。**この1つだけが「どれくらい滑らかか」を決める。** */
export const TOLERANCE_PIXELS = 0.35

/** 目標偏差[mm]の下限(絶対)。 */
export const DEVIATION_MIN_ABS = 1.0e-3
/** 目標偏差[mm]の上限(絶対)。 */
export const DEVIATION_MAX_ABS = 5.0
/** 目標偏差の下限(モデルの外接対角に対する比)。 */
export const DEVIATION_MIN_RELATIVE = 1.0e-5
/** 目標偏差の上限(同・比)。 */
export const DEVIATION_MAX_RELATIVE = 2.0e-2

/** 角度偏差[ラジアン]の下限。 */
export const ANGULAR_MIN = 0.02
/** 角度偏差[ラジアン]の上限(OCCT自身の既定値と同じ)。 */
export const ANGULAR_MAX = 0.5

/**
 * 折れ線1本の分割数の下限。引ききっても線分1本にはしない(円弧が弦に潰れる)。
 * 円1周は `ANGULAR_MAX` から13分割が下限なので、ここが効くのは開きの小さい円弧だけ。
 */
export const MIN_CURVE_SEGMENTS = 4
/** 折れ線1本の分割数の上限。寄りきったときに1本の曲線でGPUを埋めないための蓋。 */
export const MAX_CURVE_SEGMENTS = 512

/** 目標精度の組。**線形と角度の両方**を満たすまで分割する(OCCT側と同じ規約)。 */
export interface ViewResolution {
  /** 線形偏差[mm](弦と弧の最大乖離)。 */
  deviation: number
  /** 角度偏差[**ラジアン**]。 */
  angularDeflection: number
}

/** カメラがまだ分からないときの既定。 */
export const DEFAULT_RESOLUTION: ViewResolution = {
  deviation: DEFAULT_DEVIATION,
  angularDeflection: DEFAULT_ANGULAR_DEFLECTION,
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value))
}

/**
 * 目標偏差[mm]を安全な範囲へ丸める。`sizeHint` はモデルの外接対角[mm]
 * (0以下・非有限なら比による上下限は使わない)。
 */
export function clampDeviation(deviation: number, sizeHint = 0): number {
  if (!Number.isFinite(deviation) || deviation <= 0) return DEFAULT_DEVIATION
  const size = Number.isFinite(sizeHint) && sizeHint > 0 ? sizeHint : 0
  const low = Math.max(DEVIATION_MIN_ABS, size * DEVIATION_MIN_RELATIVE)
  // **上限は下限より小さくならない**(極端に小さなモデルでは比の上限が絶対の下限を割る)。
  const high = Math.max(
    low,
    size > 0 ? Math.min(DEVIATION_MAX_ABS, size * DEVIATION_MAX_RELATIVE) : DEVIATION_MAX_ABS,
  )
  return clamp(deviation, low, high)
}

/**
 * 線形偏差[mm]に見合う角度偏差[ラジアン]。`deviation === DEFAULT_DEVIATION` で
 * `DEFAULT_ANGULAR_DEFLECTION` にちょうど戻る(既定値の意味を変えない)。
 */
export function angularForDeviation(deviation: number): number {
  if (!Number.isFinite(deviation) || deviation <= 0) return DEFAULT_ANGULAR_DEFLECTION
  return clamp(
    DEFAULT_ANGULAR_DEFLECTION * Math.sqrt(deviation / DEFAULT_DEVIATION),
    ANGULAR_MIN,
    ANGULAR_MAX,
  )
}

/**
 * **変換式の唯一の入口**。`worldPerPixel` は画面1画素ぶんの世界長さ[mm](注視点の面)。
 * 非有限・0以下なら既定値の組を返す。
 */
export function viewResolution(worldPerPixel: number, sizeHint = 0): ViewResolution {
  if (!Number.isFinite(worldPerPixel) || worldPerPixel <= 0) return DEFAULT_RESOLUTION
  const deviation = clampDeviation(TOLERANCE_PIXELS * worldPerPixel, sizeHint)
  return { deviation, angularDeflection: angularForDeviation(deviation) }
}

/**
 * 半径 `radius`[mm]・開き `sweep`[ラジアン]の円弧を折れ線にするときの分割数。
 * 線形偏差と角度偏差の両方を満たす(OCCTの `GCPnts_TangentialDeflection` と同じ規約。
 * 揃えないと同じ画面でスケッチの円だけが辺より粗く/細かく見える)。
 * 線形側は `theta = 2*acos(1 - e/r)` が上限。
 */
export function arcSegments(radius: number, sweep: number, resolution: ViewResolution): number {
  const span = Math.abs(sweep)
  if (!Number.isFinite(radius) || !Number.isFinite(span) || radius <= 0 || span <= 0) {
    return MIN_CURVE_SEGMENTS
  }
  // 半径より偏差の方が大きければ、線形側は何も要求しない(角度側だけが効く)。
  const ratio = resolution.deviation / radius
  const linear = ratio >= 1 ? Math.PI : 2 * Math.acos(clamp(1 - ratio, -1, 1))
  const step = Math.min(linear, resolution.angularDeflection)
  if (!(step > 0)) return MAX_CURVE_SEGMENTS
  return clamp(Math.ceil(span / step), MIN_CURVE_SEGMENTS, MAX_CURVE_SEGMENTS)
}

/**
 * 3次ベジエを折れ線にするときの分割数。均等分割の最大誤差 `max|B''| / (8 n^2)`
 * (`max|B''| <= 6*max(|P0-2P1+P2|, |P1-2P2+P3|)`)から `n` を逆に解く。
 * 角度偏差は使わない(ベジエには半径が無く、角度の意味が円弧と揃わない)。
 */
export function bezierSegments(
  p0: readonly [number, number],
  p1: readonly [number, number],
  p2: readonly [number, number],
  p3: readonly [number, number],
  resolution: ViewResolution,
): number {
  const ax = p0[0] - 2 * p1[0] + p2[0]
  const ay = p0[1] - 2 * p1[1] + p2[1]
  const bx = p1[0] - 2 * p2[0] + p3[0]
  const by = p1[1] - 2 * p2[1] + p3[1]
  const second = 6 * Math.sqrt(Math.max(ax * ax + ay * ay, bx * bx + by * by))
  if (!Number.isFinite(second) || second <= 0) return MIN_CURVE_SEGMENTS
  const count = Math.ceil(Math.sqrt(second / (8 * resolution.deviation)))
  return clamp(count, MIN_CURVE_SEGMENTS, MAX_CURVE_SEGMENTS)
}
