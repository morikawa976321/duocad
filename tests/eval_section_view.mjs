/** 断面の対象の差し替え・非表示・再開で描画資源と表示状態が食い違わない。 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'

const THREE = await import(new URL('../web/node_modules/three/build/three.module.js', import.meta.url).href)
const { createSectionView } = await import('../web/src/sectionView.ts')
const section = createSectionView({ pickChanged() {} })
const geometry = new THREE.BoxGeometry(10, 10, 10)
const content = new THREE.Box3(new THREE.Vector3(-5, -5, -5), new THREE.Vector3(5, 5, 5))
const state = { on: true, plane: 'XY', position: 0, inverted: false }
for (let i = 0; i < 25; i++) section.setBody(String(i), [geometry])
section.setContent(content)
section.setState(state)

function meshes() {
  const result = []
  section.object.traverse((object) => {
    if (object.isMesh && object.material.stencilWrite) result.push(object)
  })
  return result.sort((a, b) => a.renderOrder - b.renderOrder)
}

assert.equal(meshes().length, 50)
for (let i = 0; i < 50; i += 2) {
  const [stencil, cap] = meshes().slice(i, i + 2)
  assert.equal(stencil.material.colorWrite, false)
  assert.equal(cap.material.stencilFunc, THREE.NotEqualStencilFunc)
  assert.equal(cap.material.stencilZFail, THREE.ZeroStencilOp)
  assert.equal(cap.material.stencilZPass, THREE.ZeroStencilOp)
  assert.ok(stencil.renderOrder < cap.renderOrder)
  if (i + 2 < 50) assert.ok(cap.renderOrder < meshes()[i + 2].renderOrder)
}
const firstPair = meshes().slice(0, 2)
section.setBodyVisible('0', false)
assert.ok(firstPair.every((mesh) => !mesh.visible))
section.setContent(new THREE.Box3())
section.setBodyVisible('0', true)
assert.equal(firstPair[1].visible, false)
section.setContent(content)
assert.equal(firstPair[1].visible, true)

let disposed = 0
firstPair[0].geometry.addEventListener('dispose', () => disposed++)
section.setBody('0', [geometry])
assert.equal(disposed, 1)
assert.equal(meshes().length, 50)
section.removeBody('8')
assert.equal(meshes().length, 48)
section.setState({ ...state, on: false })
assert.equal(meshes().length, 0)
assert.equal(section.clipPlanes.length, 0)
section.setBodyVisible('0', false)
section.setState(state)
assert.equal(meshes().length, 48)
assert.equal(meshes().filter((mesh) => mesh.visible).length, 46)
section.dispose()
assert.equal(meshes().length, 0)
geometry.dispose()
console.log('sectionView.ts: lifecycle passed')
