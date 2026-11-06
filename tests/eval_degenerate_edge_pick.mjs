import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'

const { createSolidFeatures } = await import('../web/src/solidFeatures.ts')
const THREE = await import(new URL('../web/node_modules/three/build/three.module.js', import.meta.url).href)
const { createProjector } = await import('../web/src/polylinePick.ts')
const features = createSolidFeatures({ clipPlanes: [], invalidate() {}, hiddenByPreview: () => false, sphereNear: () => true })
features.setEdges('body', { revision: 1, deviation: 0.2, edges: [
  { index: 1, kind: 'circle', length: 0, closed: true, degenerate: true, points: [0, 0, 0, 0, 0, 0] },
  { index: 2, kind: 'line', length: 1, closed: false, degenerate: false, points: [0, 0, 0, 1, 0, 0] },
] })
const indexes = []
features.forEachEdge((ref) => indexes.push(ref.edgeIndex))
assert.deepEqual(indexes, [2])
const camera = new THREE.PerspectiveCamera(50, 1, 0.1, 100)
camera.position.z = 10
camera.updateMatrixWorld(true)
const projector = createProjector(camera, 800, 800)
assert.deepEqual(features.edgeCandidates(400, 400, 6, projector, null).map((hit) => hit.ref.edgeIndex), [2])
features.dispose()
