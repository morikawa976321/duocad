/**
 * 面の境界線と折れ目の線分。three.js の `EdgesGeometry` と同じ規則で、同じ線分を同じ順に返す
 * (`tests/eval_edge_segments.mjs` が突き合わせる)。添字は3の倍数個で、頂点の範囲内にあること(余りの添字は捨てる)。
 *
 * `EdgesGeometry` は頂点を丸めた座標の文字列で突き合わせるので、ズームで測り直した QR1 の面を受けたとき、
 * 受信1回 138 ms のうち 111 ms がここだった(実測)。丸めた座標を数のまま突き合わせる。
 */

/** `EdgesGeometry` の `precisionPoints = 4`。 */
const PRECISION = 10 ** 4

/** 丸めた座標が同じ頂点に同じ番号を振る(`EdgesGeometry` の頂点の文字列が同じ ⇔ 番号が同じ)。 */
function mergeRounded(positions: Float32Array, vertexCount: number): { ids: Int32Array; count: number } {
  const rx = new Float64Array(vertexCount)
  const ry = new Float64Array(vertexCount)
  const rz = new Float64Array(vertexCount)
  let size = 1
  while (size < vertexCount * 2) size *= 2
  const mask = size - 1
  const table = new Int32Array(size).fill(-1)
  const ids = new Int32Array(vertexCount)
  // 文字列にすると -0 と 0 は同じ "0"、NaN どうしも同じになる
  const same = (a: number, b: number) => a === b || (a !== a && b !== b)
  let count = 0
  for (let vertex = 0; vertex < vertexCount; vertex += 1) {
    const x = Math.round(positions[vertex * 3] * PRECISION)
    const y = Math.round(positions[vertex * 3 + 1] * PRECISION)
    const z = Math.round(positions[vertex * 3 + 2] * PRECISION)
    rx[vertex] = x
    ry[vertex] = y
    rz[vertex] = z
    let slot = (Math.imul(x | 0, 73856093) ^ Math.imul(y | 0, 19349663) ^ Math.imul(z | 0, 83492791)) & mask
    for (;;) {
      const other = table[slot]
      if (other === -1) {
        table[slot] = vertex
        ids[vertex] = count
        count += 1
        break
      }
      if (same(rx[other], x) && same(ry[other], y) && same(rz[other], z)) {
        ids[vertex] = ids[other]
        break
      }
      slot = (slot + 1) & mask
    }
  }
  return { ids, count }
}

/**
 * 隣の三角形と `thresholdDegrees` を超えて折れる辺と、相手の無い辺の線分(1本につき端点2つの xyz)。
 * 相手は逆向きの辺だけで、同じ向きに2度現れた辺は1本目だけを残す(`EdgesGeometry` と同じ)。
 */
export function edgeSegments(positions: Float32Array, indices: ArrayLike<number>, thresholdDegrees: number): Float32Array {
  const thresholdDot = Math.cos((Math.PI / 180) * thresholdDegrees)
  const { ids, count } = mergeRounded(positions, positions.length / 3)
  const triangleCount = Math.floor(indices.length / 3)
  const open = new Map<number, number>() // 向きのある辺(番号の組) → 控えの番号。控えは足した順に番号を振る
  const from = new Int32Array(triangleCount * 3)
  const to = new Int32Array(triangleCount * 3)
  const normals = new Float64Array(triangleCount * 9)
  const alive = new Uint8Array(triangleCount * 3)
  let entries = 0
  const out = new Float32Array(triangleCount * 3 * 6)
  let outAt = 0
  const push = (vertex: number) => {
    out[outAt] = positions[vertex * 3]
    out[outAt + 1] = positions[vertex * 3 + 1]
    out[outAt + 2] = positions[vertex * 3 + 2]
    outAt += 3
  }
  const corner = [0, 0, 0]
  const merged = [0, 0, 0]
  for (let triangle = 0; triangle < triangleCount; triangle += 1) {
    const a = indices[triangle * 3]
    const b = indices[triangle * 3 + 1]
    const c = indices[triangle * 3 + 2]
    const ax = positions[a * 3], ay = positions[a * 3 + 1], az = positions[a * 3 + 2]
    const bx = positions[b * 3], by = positions[b * 3 + 1], bz = positions[b * 3 + 2]
    const cx = positions[c * 3], cy = positions[c * 3 + 1], cz = positions[c * 3 + 2]
    // `Triangle.getNormal` と同じ式・同じ順(折れ目の判定が境目で食い違わないように)
    const tx = cx - bx, ty = cy - by, tz = cz - bz
    const ux = ax - bx, uy = ay - by, uz = az - bz
    let nx = ty * uz - tz * uy
    let ny = tz * ux - tx * uz
    let nz = tx * uy - ty * ux
    const lengthSq = nx * nx + ny * ny + nz * nz
    if (lengthSq > 0) {
      const scale = 1 / Math.sqrt(lengthSq)
      nx *= scale
      ny *= scale
      nz *= scale
    } else {
      nx = 0
      ny = 0
      nz = 0
    }
    corner[0] = a
    corner[1] = b
    corner[2] = c
    merged[0] = ids[a]
    merged[1] = ids[b]
    merged[2] = ids[c]
    if (merged[0] === merged[1] || merged[1] === merged[2] || merged[2] === merged[0]) continue
    for (let j = 0; j < 3; j += 1) {
      const next = (j + 1) % 3
      const reverse = open.get(merged[next] * count + merged[j])
      if (reverse !== undefined && alive[reverse] === 1) {
        const dot = nx * normals[reverse * 3] + ny * normals[reverse * 3 + 1] + nz * normals[reverse * 3 + 2]
        if (dot <= thresholdDot) {
          push(corner[j])
          push(corner[next])
        }
        alive[reverse] = 0
        continue
      }
      const key = merged[j] * count + merged[next]
      if (open.has(key)) continue
      open.set(key, entries)
      from[entries] = corner[j]
      to[entries] = corner[next]
      normals[entries * 3] = nx
      normals[entries * 3 + 1] = ny
      normals[entries * 3 + 2] = nz
      alive[entries] = 1
      entries += 1
    }
  }
  for (let entry = 0; entry < entries; entry += 1) {
    if (alive[entry] === 0) continue
    push(from[entry])
    push(to[entry])
  }
  return out.slice(0, outAt)
}
