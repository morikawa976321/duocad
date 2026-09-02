/**
 * ビューキューブ(6面+12辺+8角=26領域)の**純関数**。three.js を import しない
 * (`tests/eval_view_cube.mjs` が Node から直接読んで検査できるようにするため —
 * `web/src/gridScale.ts` / `tests/eval_grid_scale.mjs` と同じ作法)。
 *
 * 立方体 `[-1,1]³` を各軸で3分割し(端セルの厚み `EDGE_T` ・中央セルの厚み
 * `1 - 2*EDGE_T`、ともに「全体を1とした比率」)、27セルから中心 `(0,0,0)` を除いた
 * 26セルが領域そのものになる。セルのインデックス `(i,j,k) ∈ {-1,0,1}³` は
 * そのまま「その領域をクリックしたときに見る向き」でもある —— 面(`|i|+|j|+|k|=1`)は
 * 軸方向、辺(`=2`)は2軸複合、角(`=3`)は3軸複合の視点になる。
 *
 * 描画(three.js)・入力(Pointer Lock)・第2パスのcanvas合成は `viewport.ts` の仕事。
 * ここは形と向きの計算だけを持つ。
 */

/** 端セルの厚み(全体を1とした比率)。面セルは 1 - 2*EDGE_T。 */
export const EDGE_T = 0.3

/** 27セルから中心を除いた26領域。(i,j,k) は視点方向でもある。 */
export function regionCells(): Array<[number, number, number]> {
  const cells: Array<[number, number, number]> = []
  for (const i of [-1, 0, 1] as const) {
    for (const j of [-1, 0, 1] as const) {
      for (const k of [-1, 0, 1] as const) {
        if (i === 0 && j === 0 && k === 0) continue
        cells.push([i, j, k])
      }
    }
  }
  return cells
}

/**
 * `(i,j,k)` が指す領域の種別。`|i|+|j|+|k|` が 1 なら面・2 なら辺・3 なら角。
 * `regionCells()` の外(=中心)を渡すと 0 が返る。
 */
export function regionKind(i: number, j: number, k: number): 'face' | 'edge' | 'corner' | 'center' {
  const magnitude = Math.abs(i) + Math.abs(j) + Math.abs(k)
  if (magnitude === 1) return 'face'
  if (magnitude === 2) return 'edge'
  if (magnitude === 3) return 'corner'
  return 'center'
}

/** 1軸ぶんの中心座標と長さ(半幅1の立方体上)。`idx` は -1/0/1。 */
function axisExtent(idx: number): { center: number; size: number } {
  if (idx === 0) return { center: 0, size: 2 * (1 - 2 * EDGE_T) }
  return { center: idx * (1 - EDGE_T), size: 2 * EDGE_T }
}

/**
 * セル中心の座標と一辺の長さ(半幅1の立方体上)。
 *
 * 境界は `±(1 - 2*EDGE_T)` にある(3軸とも同じ)。端セルは
 * `[idx*(1-EDGE_T) - EDGE_T, idx*(1-EDGE_T) + EDGE_T]` を占め、外側の面(`±1`)は
 * 立方体の表面にちょうど一致する。セル同士は境界を共有するだけで重ならない。
 */
export function cellBox(
  i: number,
  j: number,
  k: number,
): { center: [number, number, number]; size: [number, number, number] } {
  const x = axisExtent(i)
  const y = axisExtent(j)
  const z = axisExtent(k)
  return {
    center: [x.center, y.center, z.center],
    size: [x.size, y.size, z.size],
  }
}

/** その領域をクリックしたときのカメラ方向(正規化)。`(0,0,0)` は退避的に +Z を返す。 */
export function cellDirection(i: number, j: number, k: number): [number, number, number] {
  const length = Math.hypot(i, j, k)
  if (length < 1e-9) return [0, 0, 1]
  return [i / length, j / length, k / length]
}

/**
 * 方向 `d`(正規化済み)に対する `camera.up`。
 *
 * **既定は +Z をそのまま返す**(直交化はしない —— `camera.up` は three.js の
 * `lookAt` 側が向きから直交基底を作るための「ヒント」であり、この実装の他の場所
 * (`HOME_VIEW_OFFSET` を使う初期視点・通常の軌道回転)も既に非直交のヒントのまま
 * +Z を使い続けている。ここだけ厳密に直交化すると、その規約と食い違う)。
 *
 * **ただし `|d.z| > 0.99`(真上・真下)では +Z がほぼ `d` 自身と重なり、ヒントとして
 * 退化する**(`lookAt` の基底が不定になる) —— そこだけ **+Y(上面図)/ −Y(底面図)** に
 * 差し替える。上面図で「前(−Y)」が画面の下に来る、一般的なCADの並びになる。
 */
export function upForDirection(d: readonly [number, number, number]): [number, number, number] {
  const z = d[2]
  if (Math.abs(z) > 0.99) return z > 0 ? [0, 1, 0] : [0, -1, 0]
  return [0, 0, 1]
}
