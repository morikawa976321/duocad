/**
 * ゴーストの隠れ線(破線)に渡す辺の線分を、重複を除いて端点でつないだ順に並べ直す。
 *
 * 破線は線分の通し距離(`LineSegments2.computeLineDistances`)で引く。面ごとの境界線(`bodyMesh.ts` の `faceEdges`)は
 * 隣り合う2面が同じ辺を1本ずつ持ち、`EdgesGeometry` の出力は三角形の順で並ぶ。そのまま渡すと、破線の位置が線分ごとに
 * 飛んで細かい曲線が雑音になり、同じ辺が2本重なった所は互いの隙間を埋めて実線に見える。
 */

/** 同じ点とみなす座標の刻み[mm]。面ごとに測った同じ辺の端点は、この刻みより近い。 */
const POINT_QUANTUM_MM = 1e-4
/** 線分の無向キー `小さい点番号 * これ + 大きい点番号` が 2^53 を超えない点数の上限。 */
const KEY_STRIDE = 2 ** 26

/**
 * `positions` は `LineSegments` の位置配列(線分ごとに始点・終点の xyz)。返すのも同じ形で、つながった線分が
 * 続けて並び、各線分の始点は1つ前の線分の終点と同じ点になる(枝分かれ・端点で次の連なりへ移る)。
 */
export function chainSegments(positions: ArrayLike<number>): Float32Array {
  // 点の番号引きは型付き配列の開番地法で持つ(2026-10-24実測、円3000個×48本を2重に入れた28.8万本: 文字列キーの Map は
  // 約250ms、これは約55ms。投機実行は届くたびに並べ直すので、その間主スレッドが止まる)
  const maxPoints = Math.floor(positions.length / 3)
  let capacity = 16
  while (capacity < maxPoints * 2) capacity *= 2
  const slots = new Int32Array(capacity).fill(-1)
  const cells = new Float64Array(maxPoints * 3)
  const firstAt: number[] = []
  const idOf = (at: number): number => {
    const x = Math.round(positions[at] / POINT_QUANTUM_MM)
    const y = Math.round(positions[at + 1] / POINT_QUANTUM_MM)
    const z = Math.round(positions[at + 2] / POINT_QUANTUM_MM)
    let slot = (Math.imul(x | 0, 73856093) ^ Math.imul(y | 0, 19349663) ^ Math.imul(z | 0, 83492791)) & (capacity - 1)
    for (;;) {
      const id = slots[slot]
      if (id < 0) break
      if (cells[id * 3] === x && cells[id * 3 + 1] === y && cells[id * 3 + 2] === z) return id
      slot = (slot + 1) & (capacity - 1)
    }
    const id = firstAt.length
    slots[slot] = id
    cells[id * 3] = x
    cells[id * 3 + 1] = y
    cells[id * 3 + 2] = z
    firstAt.push(at)
    return id
  }

  const ends: number[] = []
  const seen = new Set<number>()
  for (let at = 0; at + 5 < positions.length; at += 6) {
    const a = idOf(at)
    const b = idOf(at + 3)
    if (a === b) continue
    const key = a < b ? a * KEY_STRIDE + b : b * KEY_STRIDE + a
    if (seen.has(key)) continue
    seen.add(key)
    ends.push(a, b)
  }
  if (firstAt.length >= KEY_STRIDE) throw new Error(`辺の点が多すぎて線分を区別できない: ${firstAt.length}`)

  const pointCount = firstAt.length
  const segmentCount = ends.length / 2
  const offsets = new Int32Array(pointCount + 1)
  for (const id of ends) offsets[id + 1] += 1
  for (let id = 0; id < pointCount; id += 1) offsets[id + 1] += offsets[id]
  const degree = (id: number): number => offsets[id + 1] - offsets[id]
  const incident = new Int32Array(ends.length)
  const filled = offsets.slice(0, pointCount)
  for (let segment = 0; segment < segmentCount; segment += 1) {
    incident[filled[ends[segment * 2]]++] = segment
    incident[filled[ends[segment * 2 + 1]]++] = segment
  }

  const used = new Uint8Array(segmentCount)
  const out = new Float32Array(segmentCount * 6)
  let write = 0
  const emit = (id: number): void => {
    const at = firstAt[id]
    out[write++] = positions[at]
    out[write++] = positions[at + 1]
    out[write++] = positions[at + 2]
  }
  const unusedAt = (id: number): number => {
    for (let k = offsets[id]; k < offsets[id + 1]; k += 1) if (used[incident[k]] === 0) return incident[k]
    return -1
  }
  const walk = (from: number, first: number): void => {
    let at = from
    let segment = first
    while (segment >= 0) {
      used[segment] = 1
      const next = ends[segment * 2] === at ? ends[segment * 2 + 1] : ends[segment * 2]
      emit(at)
      emit(next)
      at = next
      segment = degree(at) === 2 ? unusedAt(at) : -1
    }
  }
  // 端点・分岐点から歩き出し(開いた連なりを途中から割らない)、残った閉じた輪をあとで拾う
  for (let id = 0; id < pointCount; id += 1) {
    if (degree(id) === 2) continue
    for (let segment = unusedAt(id); segment >= 0; segment = unusedAt(id)) walk(id, segment)
  }
  for (let segment = 0; segment < segmentCount; segment += 1) {
    if (used[segment] === 0) walk(ends[segment * 2], segment)
  }
  return out
}
