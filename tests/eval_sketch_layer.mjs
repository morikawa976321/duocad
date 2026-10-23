/** 描画・ピック参照の寿命と、選択だけの配信で描画資源を作り直さない契約を検査する。 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'
const THREE = await import('../web/node_modules/three/build/three.module.js')
const { createSketchLayer } = await import('../web/src/sketchLayer.ts')
const { DEFAULT_RESOLUTION } = await import('../web/src/adaptiveResolution.ts')

const layer = createSketchLayer()
const parent = new THREE.Scene()
parent.add(layer.object)
const frame = { origin: [0, 0, 7], u: [1, 0, 0], v: [0, 1, 0], normal: [0, 0, 1] }
const handles = [
  { entityId: 'rect', kind: 'rectangle', params: { x: 0, y: 0, width: 20, height: 10, angle: 0 } },
  { entityId: 'circle', kind: 'circle', params: { cx: 30, cy: 0, diameter: 10 } },
  { entityId: 'point', kind: 'point', params: { cx: 5.3, cy: 4.2 } },
  { entityId: 'line-a', kind: 'line', params: { x0: 40, y0: 0, x1: 50, y1: 10 } },
  { entityId: 'line-b', kind: 'line', params: { x0: 40, y0: 10, x1: 50, y1: 0 } },
]
const draw = (sketchId = 'a', editing = false) => ({
  sketchId, revision: 1, frame, plane: { kind: 'base', name: 'XY' }, editing, livePatterns: false,
  handles, entities: handles, selectedRegions: [], selectedHandles: [],
  regions: [{ index: 0, positions: [0, 0, 7, 20, 0, 7, 20, 10, 7, 0, 10, 7], indices: [0, 1, 2, 0, 2, 3] }],
})
const geometries = new Set(), materials = new Set()
const disposedGeometry = new Map(), disposedMaterial = new Map()
const originalGeometryDispatch = THREE.BufferGeometry.prototype.dispatchEvent
const originalMaterialDispatch = THREE.Material.prototype.dispatchEvent
THREE.BufferGeometry.prototype.dispatchEvent = function (event) {
  if (event.type === 'dispose') disposedGeometry.set(this, (disposedGeometry.get(this) ?? 0) + 1)
  return originalGeometryDispatch.call(this, event)
}
THREE.Material.prototype.dispatchEvent = function (event) {
  if (event.type === 'dispose') disposedMaterial.set(this, (disposedMaterial.get(this) ?? 0) + 1)
  return originalMaterialDispatch.call(this, event)
}
function observe() {
  for (const child of layer.object.children) {
    geometries.add(child.geometry)
    materials.add(child.material)
    assert.equal(disposedGeometry.has(child.geometry), false, '破棄済みの形がシーンに残っている')
    assert.equal(disposedMaterial.has(child.material), false, '破棄済みの材質がシーンに残っている')
  }
}
function update(entries) {
  const rebuilt = layer.update(entries, DEFAULT_RESOLUTION)
  layer.applyRegionMaterials(null)
  layer.applySketchHoverMaterials(null)
  observe()
  return rebuilt
}
function empty() {
  assert.equal(layer.object.children.length, 0, '描画オブジェクトが残っている')
  assert.equal([...layer.entries()].length, 0, 'ピック対象が残っている')
  assert.equal(layer.get('a'), undefined)
  assert.equal(layer.redraw(DEFAULT_RESOLUTION), false, '消した指示で再描画される')
}
const cleared = []
try {
  const entry = draw()
  assert.equal(update([entry]), true)
  const scene = layer.get('a')
  assert.equal(scene.handleLines.length, 4)
  assert.equal(scene.pickPoints.length > 0, true)
  assert.equal(scene.sphere.radius > 0, true)
  assert.equal(scene.intersections.some((p) => p.u === 45 && p.v === 5), true)
  const originalObjects = [...layer.object.children]
  const beforeSelection = layer.rebuilds()
  const selected = { ...entry, selectedRegions: [0], selectedHandles: [{ entityId: 'circle', point: null }, { entityId: 'point', point: 'cx' }] }
  for (let i = 0; i < 10; i++) {
    assert.equal(update([i % 2 ? entry : selected]), false)
    assert.equal(layer.get('a'), scene)
    for (const object of originalObjects) assert.equal(layer.object.children.includes(object), true)
    assert.equal(scene.regionMeshes[0].material.depthTest, i % 2 === 1)
    assert.equal(scene.handleLines.find((line) => line.userData.entityId === 'circle').material.depthTest, i % 2 === 1)
  }
  const selectionRebuilds = layer.rebuilds() - beforeSelection
  assert.equal(selectionRebuilds, 0)
  update([selected])
  layer.applySketchHoverMaterials({ sketchId: 'a', entityId: 'point', point: 'cx' })
  layer.applyRegionMaterials({ sketchId: 'a', index: 0 })
  observe()
  assert.ok(scene.markSelected)
  assert.ok(scene.markHovered)
  const coarse = scene.polylineCounts[1]
  assert.equal(layer.redraw({ ...DEFAULT_RESOLUTION, deviation: 0.001, angularDeflection: 0.03 }), true)
  observe()
  const fine = layer.get('a').polylineCounts[1]
  assert.ok(fine > coarse)
  assert.equal(disposedGeometry.get(scene.handleLines[0].geometry), 1)
  assert.equal(update([{ ...entry, revision: 2 }]), true)
  assert.equal(update([{ ...entry, revision: 2, editing: true }]), true)
  const editing = layer.get('a')
  assert.ok(editing.handleMeshes.length > 0)
  assert.equal(editing.pickPoints.length, 0)
  assert.ok(editing.handleLines.every((line) => !line.material.depthTest))
  for (const tone of ['normal', 'warn']) {
    layer.showGhosts('a', [{ kind: 'point', params: { cx: 8, cy: 9 } }, handles[0]], DEFAULT_RESOLUTION, tone)
    observe()
  }
  const current = layer.get('a')
  const ghostObjects = layer.object.children.filter((child) => !current.regionMeshes.includes(child) && !current.handleMeshes.includes(child) && !current.handleLines.includes(child))
  assert.equal(ghostObjects.length, 2)
  assert.equal(update([{ ...entry, revision: 2, editing: true }]), false)
  assert.equal(layer.redraw(DEFAULT_RESOLUTION), true)
  observe()
  for (const object of ghostObjects) assert.equal(layer.object.children.includes(object), true, '解像度の更新で幽霊が消える')
  assert.equal(update([]), true)
  empty()
  cleared.push({ reason: 'delete', children: layer.object.children.length, picks: [...layer.values()].length })
  for (let i = 0; i < 3; i++) {
    update([draw('a'), draw('b', true)])
    layer.showGhosts('a', [handles[0]], DEFAULT_RESOLUTION)
    observe()
    update([draw('b', true)])
    assert.equal(layer.get('a'), undefined)
    assert.ok(layer.object.children.every((child) => layer.get('b').handleLines.includes(child) || layer.get('b').handleMeshes.includes(child) || layer.get('b').regionMeshes.includes(child)))
    update([])
    empty()
    cleared.push({ reason: 'hide/' + i, children: 0, picks: 0 })
    update([draw('new-document-' + i)])
    layer.showGhosts('new-document-' + i, [handles[2]], DEFAULT_RESOLUTION)
    observe()
    update([])
    empty()
    cleared.push({ reason: 'document/' + i, children: 0, picks: 0 })
  }
  update([{ ...draw(), livePatterns: true }])
  layer.showGhosts('a', handles, DEFAULT_RESOLUTION)
  observe()
  layer.dispose()
  empty()
  assert.equal(parent.children.length, 0)
  for (const geometry of geometries) assert.equal(disposedGeometry.get(geometry), 1)
  for (const material of materials) assert.equal(disposedMaterial.get(material), 1)
  assert.ok([...disposedGeometry.values()].every((count) => count === 1))
  assert.ok([...disposedMaterial.values()].every((count) => count === 1))
  console.log(JSON.stringify({ selectionRebuilds, circlePoints: [coarse, fine], cleared, disposal: {
    geometry: disposedGeometry.size, material: disposedMaterial.size, children: layer.object.children.length, picks: [...layer.values()].length,
  } }))
} finally {
  THREE.BufferGeometry.prototype.dispatchEvent = originalGeometryDispatch
  THREE.Material.prototype.dispatchEvent = originalMaterialDispatch
}
