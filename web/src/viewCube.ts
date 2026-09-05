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
 * 描画(three.js)・入力(Pointer Lock)・第2パスのcanvas合成は `viewCubeOverlay.ts` の仕事。
 * ここは形と向きの計算だけを持つ。
 */

/**
 * 端セル(辺・角)の厚み(全体を1とした比率)。面セルは `1 - 2*EDGE_T`。
 *
 * 0.3 は3等分にほぼ等しく、面と辺の主従が付かなかった。**0.25 にして面セルを
 * 一辺の40%から50%へ広げた**(端セルは30%→25%。面:辺 = 2:1)。2026-09-03本人指示。
 */
export const EDGE_T = 0.25

/**
 * 真上・真下とみなす境界(`|d.z|`がこれを超えたら極)。`upForDirection`の射影が退化する
 * 側の閾値そのものだが、`viewport.ts`の`syncCameraUp`(極を離れたら`camera.up`を
 * WORLD_UPへ戻す判定)も**同じ境界**を見る必要があるので、ここに1つだけ持ってexportする——
 * 2箇所に0.99を書き写すと、片方だけ動かした日に「極のすぐ外側なのに戻らない/戻り過ぎる」
 * 帯ができる。
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
 * **世界の+ZをGram-Schmidt射影で `d` に直交化して返す**: `up = normalize(zHat - (zHat・d) d)`。
 * three.js の `camera.lookAt` / `Matrix4.lookAt` が「upヒント」から基底を作るときに内部で
 * 行っている直交化と同一の式なので、**このモジュールが+Zを厳密な直交ベクトルとして返しても
 * lookAt側の見え方(カメラの姿勢)は変わらない** —— 従来「非直交のヒントのままでよい」としていた
 * 判断を撤回し、`upForDirection` の出力そのものを直交ベクトルにする(2026-09-03 QA指摘で修正)。
 *
 * `d` が水平(`z===0`)なら射影は何もせず結果はそのまま `(0,0,1)`(既定+Zの記述と矛盾しない)。
 *
 * **ただし `|d.z| > 0.99`(真上・真下)では射影が退化する**(`zHat - (zHat・d) d` がほぼ零ベクトルに
 * なり正規化できない) —— そこだけ **+Y(上面図)/ −Y(底面図)** に差し替える。上面図で
 * 「前(−Y)」が画面の下に来る、一般的なCADの並びになる。
 */
export function upForDirection(d: readonly [number, number, number]): [number, number, number] {
  const z = d[2]
  if (Math.abs(z) > POLE_Z) return z > 0 ? [0, 1, 0] : [0, -1, 0]
  // zHat=(0,0,1) なので zHat・d = z。射影 zHat - z*d を正規化する。
  const ux = -z * d[0]
  const uy = -z * d[1]
  const uz = 1 - z * d[2]
  const length = Math.hypot(ux, uy, uz)
  // `-0` を避ける(`+ 0` はIEEE754で `-0 -> 0` にだけ効き、他の値は変えない) —— 
  // 水平方向では ux/uy が厳密に0のはずなのに符号だけ `-0` になり、`Object.is` ベースの
  // 等値検査(`assert.deepEqual` の strict版)で `0` と食い違って見える(2026-09-03実測)。
  return [ux / length + 0, uy / length + 0, uz / length + 0]
}
