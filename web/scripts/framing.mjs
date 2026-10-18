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
  const f = d.map((v) => -v)
  const up = [0, 0, 1]
  let right = cross(f, up)
  const length = Math.hypot(...right)
  // 真上・真下(Z軸と平行)では外積が零ベクトルになる。three.js の `lookAt` が選ぶ向き(画面の右 = +Y)に揃える。
  right = length < 1e-9 ? [0, 1, 0] : right.map((v) => v / length)
  const upv = cross(right, f)
  const tanV = Math.tan((fov * Math.PI) / 180 / 2)
  const tanH = tanV * aspect
  let dist = 0
  for (const verts of allVerts) {
    for (let i = 0; i < verts.length; i += 3) {
      const p = [verts[i] - centre[0], verts[i + 1] - centre[1], verts[i + 2] - centre[2]]
      const along = p[0] * d[0] + p[1] * d[1] + p[2] * d[2]
      const x = Math.abs(p[0] * right[0] + p[1] * right[1] + p[2] * right[2])
      const y = Math.abs(p[0] * upv[0] + p[1] * upv[1] + p[2] * upv[2])
      dist = Math.max(dist, along + x / tanH, along + y / tanV)
    }
  }
  return dist * 1.06
}
