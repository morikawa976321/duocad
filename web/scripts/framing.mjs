/**
 * 比較スクショの距離決め(`shoot-topopt.mjs` が使う。純関数なので `tests/eval_shoot_framing.mjs` が評価する)。
 */

const cross = (a, b) => [
  a[1] * b[2] - a[2] * b[1],
  a[2] * b[0] - a[0] * b[2],
  a[0] * b[1] - a[1] * b[0],
]

/**
 * その視線方向で**全部の面が画面に収まる**距離を1つ決める。
 * 外接球ではなく実際の投影から決める —— 外接球だと細長い題材が画面の3分の1にしか映らない。
 *
 * @param {number[]} dir 注視点からカメラへの向き(零ベクトル・非有限は断る)
 * @param {number[]} centre 注視点
 * @param {Float32Array[]} allVerts 面ごとの頂点(x,y,z の並び)
 * @param {number} fov 縦の画角[度]
 * @param {number} aspect 幅 / 高さ
 */
export function distanceFor(dir, centre, allVerts, fov, aspect) {
  const n = Math.hypot(...dir)
  if (!Number.isFinite(n) || n === 0) throw new Error(`視線の向きが使えない: ${dir.join(',')}`)
  const d = dir.map((v) => v / n)
  const up = [0, 0, 1]
  // three.js の `Matrix4.lookAt` と同じ手順で画面の軸を決める(z = 注視点からカメラへの向き、x = up × z、y = z × x)。
  // 退化(x が厳密に零ベクトル)のときだけ z を up と平行でない向きへ 1e-4 ずらして取り直す。**しきい値を 1e-9 などに
  // しない**: 厳密には零でない 1e-10 の外積を three.js は正規化して使う(右が -X になる)ので、別の向きを選ぶと
  // 距離の計算と実際の画面の軸が食い違い、被写体が画面から欠ける。
  let z = d.slice()
  let x = cross(up, z)
  if (x[0] * x[0] + x[1] * x[1] + x[2] * x[2] === 0) {
    if (Math.abs(up[2]) === 1) z[0] += 0.0001
    else z[2] += 0.0001
    const zn = Math.hypot(...z)
    z = z.map((v) => v / zn)
    x = cross(up, z)
  }
  const xn = Math.hypot(...x)
  const right = x.map((v) => v / xn)
  const upv = cross(z, right)
  const tanV = Math.tan((fov * Math.PI) / 180 / 2)
  const tanH = tanV * aspect
  let dist = 0
  for (const verts of allVerts) {
    for (let i = 0; i < verts.length; i += 3) {
      const p = [verts[i] - centre[0], verts[i + 1] - centre[1], verts[i + 2] - centre[2]]
      const along = p[0] * z[0] + p[1] * z[1] + p[2] * z[2]
      const x = Math.abs(p[0] * right[0] + p[1] * right[1] + p[2] * right[2])
      const y = Math.abs(p[0] * upv[0] + p[1] * upv[1] + p[2] * upv[2])
      dist = Math.max(dist, along + x / tanH, along + y / tanV)
    }
  }
  return dist * 1.06
}
