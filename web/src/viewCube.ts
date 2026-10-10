/**
 * ビューキューブ(6面+12辺+8角=26領域)の**純関数**。three.js を import しない
 * (`tests/eval_view_cube.mjs` が Node から直接読んで検査できるようにするため)。
 *
 * 立方体 `[-1,1]³` を各軸で3分割し(端セルの厚み `EDGE_T`・中央セルの厚み `1 - 2*EDGE_T`、
 * ともに全体を1とした比率)、27セルから中心を除いた26セルが領域になる。
 * セルのインデックス `(i,j,k) ∈ {-1,0,1}³` はそのままクリック時に見る向きでもある
 * (面 `|i|+|j|+|k|=1`・辺 `=2`・角 `=3`)。
 *
 * 描画・入力・第2パスは `viewCubeOverlay.ts`。ここは形と向きの計算だけ。
 */

/** 端セル(辺・角)の厚み(全体を1とした比率)。面:辺 の面積比はおよそ 2:1。 */
export const EDGE_T = 0.25

/**
 * 真上・真下とみなす境界(`|d.z|`がこれを超えたら極)。`upForDirection` の射影が退化する閾値で、
 * `viewport.ts` の `syncCameraUp` も同じ境界を見る必要があるのでここに1つだけ持つ。
 */
export const POLE_Z = 0.99

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

/** 1軸ぶんの中心座標と長さ(半幅1の立方体上)。`idx` は -1/0/1。 */
function axisExtent(idx: number): { center: number; size: number } {
  if (idx === 0) return { center: 0, size: 2 * (1 - 2 * EDGE_T) }
  return { center: idx * (1 - EDGE_T), size: 2 * EDGE_T }
}

/** セル中心の座標と一辺の長さ(半幅1の立方体上)。外側の面(`±1`)は立方体の表面に一致し、セル同士は重ならない。 */
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
 * 世界の+ZをGram-Schmidt射影で `d` に直交化する: `up = normalize(zHat - (zHat・d) d)`。
 * three.js の `lookAt` が内部で行う直交化と同一の式なので、カメラの姿勢は変わらない。
 *
 * `|d.z| > POLE_Z`(真上・真下)では射影が零ベクトルに退化するので、+Y(上面図)/ −Y(底面図)に差し替える
 * (上面図で前(−Y)が画面の下に来る一般的なCADの並び)。
 */
export function upForDirection(d: readonly [number, number, number]): [number, number, number] {
  const z = d[2]
  if (Math.abs(z) > POLE_Z) return z > 0 ? [0, 1, 0] : [0, -1, 0]
  const ux = -z * d[0]
  const uy = -z * d[1]
  const uz = 1 - z * d[2]
  const length = Math.hypot(ux, uy, uz)
  // `+ 0` で `-0` を `0` にする(`Object.is` ベースの `assert.deepEqual` strict で食い違うため)。
  return [ux / length + 0, uy / length + 0, uz / length + 0]
}
