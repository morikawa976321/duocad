/**
 * ゴーストの描き方(`ghostLayer.ts`)の契約と、破線用の辺の並べ直し(`ghostEdges.ts`)を検査する。
 * 落ちたら: パスの順・透明物の列・断面のクリップ・隠した本物の戻し、または破線の連続が崩れている。
 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'
const THREE = await import('../web/node_modules/three/build/three.module.js')
const { chainSegments } = await import('../web/src/ghostEdges.ts')
const { createGhostLayer } = await import('../web/src/ghostLayer.ts')
const { buildBodyMesh } = await import('../web/src/bodyMesh.ts')
const { LAYER } = await import('../web/src/viewportConstants.ts')
const { bodyGhostMatrix } = await import('../web/src/viewportTypes.ts')

// ------------------------------------------------------------ chainSegments
const key = (...xyz) => xyz.map((v) => (Math.round(v * 1000) / 1000 + 0).toFixed(3)).join(',') // `+ 0` で -0 を 0 に揃える
function segmentsOf(array) {
  const out = []
  for (let at = 0; at < array.length; at += 6) out.push([key(array[at], array[at + 1], array[at + 2]), key(array[at + 3], array[at + 4], array[at + 5])])
  return out
}
const undirected = (segments) => new Set(segments.map(([a, b]) => (a < b ? `${a}|${b}` : `${b}|${a}`)))

// 円を24本の線分にし、順番と向きをばらばらにし、半分を2回入れる(隣り合う面が同じ辺を1本ずつ持つのと同じ)
const ring = []
for (let i = 0; i < 24; i += 1) {
  const a = (i / 24) * Math.PI * 2, b = ((i + 1) / 24) * Math.PI * 2
  ring.push([Math.cos(a) * 5, Math.sin(a) * 5, 1, Math.cos(b) * 5, Math.sin(b) * 5, 1])
}
let seed = 7
const random = () => ((seed = (seed * 16807) % 2147483647) / 2147483647)
const shuffled = [...ring, ...ring.slice(0, 12)]
  .map((segment) => (random() < 0.5 ? segment : [...segment.slice(3), ...segment.slice(0, 3)]))
  .sort(() => random() - 0.5)
const chained = segmentsOf(chainSegments(shuffled.flat()))
assert.equal(chained.length, 24, '重複した辺が1本にならない')
assert.deepEqual(undirected(chained), undirected(segmentsOf(ring.flat())), '辺の集合が変わった')
for (let i = 1; i < chained.length; i += 1) {
  assert.equal(chained[i][0], chained[i - 1][1], `閉じた輪の ${i} 本目が前の線分の終点から始まらない(破線が飛ぶ)`)
}

// T字(分岐点)と、測り方で最下位桁だけずれた同じ点
const tee = [
  [0, 0, 0, 10, 0, 0],
  [10, 0, 0, 20, 0, 0],
  [10, 0, 0, 10, 10, 0],
  [20, 0, 0, 10.00000001, 0, 0],
]
const teeChained = segmentsOf(chainSegments(tee.flat()))
assert.equal(teeChained.length, 3, '最下位桁だけずれた同じ辺が別の辺として残った')
assert.deepEqual(undirected(teeChained), undirected(segmentsOf(tee.slice(0, 3).flat())))

// ------------------------------------------------------------ createGhostLayer
/** 1辺 `size` の立方体を面6つの生メッシュにする(面ごとに頂点を持つ。サーバの `kernel.tessellate` と同じ形)。 */
function cube(size) {
  const s = size / 2
  const quads = [
    [[-s, -s, -s], [s, -s, -s], [s, s, -s], [-s, s, -s]],
    [[-s, -s, s], [s, -s, s], [s, s, s], [-s, s, s]],
    [[-s, -s, -s], [s, -s, -s], [s, -s, s], [-s, -s, s]],
    [[-s, s, -s], [s, s, -s], [s, s, s], [-s, s, s]],
    [[-s, -s, -s], [-s, s, -s], [-s, s, s], [-s, -s, s]],
    [[s, -s, -s], [s, s, -s], [s, s, s], [s, -s, s]],
  ]
  return {
    deviation: 0.1,
    face_count: 6,
    bbox: { min: [-s, -s, -s], max: [s, s, s], center: [0, 0, 0], diagonal: size * Math.sqrt(3) },
    faces: quads.map((quad, i) => ({
      index: i + 1, kind: 'plane', area: size * size, centroid: [0, 0, 0], normal: [0, 0, 1],
      positions: new Float32Array(quad.flat()), indices: new Uint32Array([0, 1, 2, 0, 2, 3]),
    })),
    untriangulated: [],
  }
}

const parent = new THREE.Group()
const clippingPlanes = []
const visibility = []
const raw = new Map([['b', cube(10)]])
const bodyMaterial = new THREE.MeshLambertMaterial()
const layer = createGhostLayer({
  parent,
  clippingPlanes,
  buildShape: (payload) => buildBodyMesh('ghost', payload, { smooth: bodyMaterial, flat: bodyMaterial }, new THREE.LineBasicMaterial(), 0x9aa6b2),
  rawGeometry: (bodyId) => raw.get(bodyId),
  setBodyVisible: (bodyId, visible) => visibility.push([bodyId, visible]),
  pickChanged() {},
})

layer.showPreview([{ bodyId: 'a', geometry: cube(10) }])
assert.equal(parent.children.length, 1)
const group = parent.children[0]
assert.ok(group.isGroup)
assert.equal(group.renderOrder, LAYER.ghost, 'ゴーストの描く順がボディごとの Group(groupOrder)に付いていない')
const passes = [...group.children].sort((a, b) => a.renderOrder - b.renderOrder)
assert.equal(passes.length, 5)
assert.equal(new Set(passes.map((pass) => pass.renderOrder)).size, 5, '同じ順番のパスがあると描く順が奥行きの並べ替えで入れ替わる')
const [depth, rim, hidden, fill, visible] = passes.map((pass) => pass.material)
for (const material of [depth, rim, hidden, fill, visible]) {
  assert.equal(material.transparent, true, '不透明物の列に入ったパスがある(奥行きだけのパスが半透明のグリッド・領域を消す)')
  assert.equal(material.clippingPlanes, clippingPlanes, '断面で切れないパスがある')
}
assert.equal(depth.colorWrite, false)
assert.equal(depth.depthWrite, true)
assert.equal(rim.depthFunc, THREE.GreaterDepth)
assert.equal(rim.blending, THREE.AdditiveBlending)
assert.equal(hidden.depthFunc, THREE.GreaterDepth)
assert.equal(hidden.dashed, true)
assert.ok(passes[2].geometry.getAttribute('instanceDistanceStart'), '隠れた辺に線の通し距離が無い(破線にならない)')
assert.equal(fill.depthWrite, false)
assert.equal(visible.depthFunc, THREE.LessEqualDepth)
// 立方体の辺は12本(面ごとの境界線は24本で届く)
assert.equal(passes[4].geometry.getAttribute('instanceStart').count, 12)
assert.deepEqual(visibility, [['a', false]])

layer.setWorldPerPixel(0.25)
assert.equal(hidden.dashScale, 4)

layer.clearPreview()
assert.equal(parent.children.length, 0)
assert.deepEqual(visibility.at(-1), ['a', true])

// 三角形の無い形でも、隠した本物は戻る
visibility.length = 0
layer.showPreview([{ bodyId: 'empty', geometry: { ...cube(10), faces: [] } }])
assert.equal(parent.children.length, 0)
layer.clearPreview()
assert.deepEqual(visibility, [['empty', false], ['empty', true]])

// ローカル幽霊: 本物は隠さず、変換をグループに掛け、無関係な再配信でも組み直す
visibility.length = 0
const entry = { bodyId: 'b', offset: [5, 0, 0], rotate: null }
layer.showBodyGhosts([entry])
assert.equal(parent.children.length, 1)
assert.equal(parent.children[0].matrixAutoUpdate, false)
assert.ok(parent.children[0].matrix.equals(bodyGhostMatrix(entry)))
assert.deepEqual(visibility, [])
layer.rebuildAround(() => raw.set('b', cube(12)))
assert.equal(parent.children.length, 1, '再配信でローカル幽霊が戻らない')
const rebuilt = [...parent.children[0].children].sort((a, b) => a.renderOrder - b.renderOrder)
rebuilt[0].geometry.computeBoundingBox()
assert.equal(rebuilt[0].geometry.boundingBox.max.x, 6, '届いたばかりの形で組み直していない')

layer.dispose()
assert.equal(parent.children.length, 0)
