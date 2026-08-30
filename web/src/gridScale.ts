/**
 * 基準グリッド(3平面の格子・基準平面のピック範囲・ホバー板・3軸の色付き線)の
 * 範囲[mm]と刻み[mm]を、**いま画面にある内容の大きさ**から導く純関数群。
 *
 * three.js を持ち込まない —— 受け取るのは `isEmpty()` / `min` / `max` の形だけの
 * `Box3Like` で、Node からこのファイルだけを読んで検査できる
 * (`tests/eval_grid_scale.mjs`)。`viewport.ts` 側は本物の `THREE.Box3` をそのまま
 * 渡せる(構造が一致するので余計な変換がいらない)。
 *
 * ここが決めるのは**数字**だけで、いつ広げ・いつ縮めるか(ヒステリシス)や、実際に
 * three.js のジオメトリを組み直す仕事は `viewport.ts` 側(`applyGridScale` /
 * `rebuildGrid`)が持つ。
 */

/** 空の文書のグリッド半幅[mm]。どんな内容の大きさでもこれより狭くはしない下限。 */
export const BASE_EXTENT = 100

/** グリッドの分割数(半幅 ÷ 刻み)をほぼ一定に保つための基準値。 */
export const GRID_DIVISIONS = 10

/** `extentFor` が受け取る外接直方体の最小限の形(`THREE.Box3` はこれを満たす)。 */
export interface Box3Like {
  isEmpty(): boolean
  min: { x: number; y: number; z: number }
  max: { x: number; y: number; z: number }
}

/**
 * 1-2-5 の桁上げ(目盛りが丸い数字に載る)。**呼び出し元(`extentFor`)は正の有限値だけを
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
 * 原点中心のグリッドが内容を覆う半幅[mm]。**対角ではなく原点からの最遠成分**を使う ——
 * グリッドは原点中心の正方形(立方体)なので、対角(内容自体の大きさ)ではなく「原点から
 * どれだけ離れているか」がそのまま必要な半幅になる(モデルが原点から離れているほど、
 * 内容自体は小さくても広いグリッドが要る)。
 *
 * 空の文書(`box.isEmpty()`)では `BASE_EXTENT` を返す(現状と同じ ±100mm の見え方)。
 * **`* 1.2` の余白**は「ちょうどモデルの端で格子が終わる」のを避けるためで、
 * `snap125` の丸めが大半を吸収するが、reach が1-2-5の境界すれすれのときに効く。
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

/**
 * `extentFor` が返した半幅に対する刻み[mm]。**`extentFor` は必ず 1-2-5 × 10^n を返す**
 * ので、10で割っても丸い数字のまま1桁下がるだけで済み、分割数(半幅÷刻み)は常に
 * `GRID_DIVISIONS` に一致する(モデルが大きくなっても線が密になって潰れない)。
 *
 * 例: 半幅100 → 刻み10(現状と同じ)。半幅500 → 刻み50。
 */
export function stepFor(extent: number): number {
  return extent / GRID_DIVISIONS
}
