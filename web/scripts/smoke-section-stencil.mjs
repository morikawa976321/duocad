/** 断面の重なりを実際の WebGL 画素で比較する。バックエンド・制作データは使わない。 */
import assert from 'node:assert/strict'
import { createServer as createHttpServer } from 'node:http'
import { fileURLToPath } from 'node:url'
import { createServer } from 'vite'
import { launchBrowser } from './browser.mjs'

const harness = `
import * as THREE from 'three'
import { createSectionView } from '/src/sectionView.ts'

export function run() {
  const renderer = new THREE.WebGLRenderer({ antialias: false, stencil: true })
  renderer.setSize(128, 128)
  renderer.localClippingEnabled = true
  const target = new THREE.WebGLRenderTarget(128, 128, { depthBuffer: true, stencilBuffer: true })
  renderer.setRenderTarget(target)
  const pixels = new Uint8Array(128 * 128 * 4)
  const scenarios = []
  function check(condition, message) { if (!condition) throw new Error(message) }
  function fixture(plane, inverted, perspective) {
    const axis = { YZ: 0, XZ: 1, XY: 2 }[plane]
    const u = axis === 0 ? 1 : 0
    const v = axis === 2 ? 1 : 2
    const scene = new THREE.Scene()
    scene.add(new THREE.AmbientLight(0xffffff, 3))
    const camera = perspective ? new THREE.PerspectiveCamera(45, 1, 0.1, 5000)
      : new THREE.OrthographicCamera(-10, 10, 10, -10, 0.1, 5000)
    const section = createSectionView({ camera: () => camera, project: () => null, rayAt: () => null,
      onDrag() {}, pickChanged() {} })
    const spec = { on: true, plane, inverted, position: 0 }
    section.setState(spec)
    // homeSide は X/Z が正、Y が負。切り取られる側から見る。
    const sign = (axis === 1 ? -1 : 1) * (inverted ? -1 : 1)
    camera.position.setComponent(axis, 24 * sign)
    camera.up.set(0, 0, 0).setComponent(v, 1)
    camera.lookAt(0, 0, 0)
    camera.updateMatrixWorld()
    scene.add(section.object)
    const bodies = new Map()
    const content = new THREE.Box3()
    const material = new THREE.MeshBasicMaterial({ color: 0xff0000, clippingPlanes: section.clipPlanes })
    function add(id, x = 0) {
      const geometry = new THREE.BoxGeometry(6, 6, 6)
      const shift = new THREE.Vector3().setComponent(u, x)
      geometry.translate(shift.x, shift.y, shift.z)
      const body = new THREE.Mesh(geometry, material)
      scene.add(body)
      bodies.set(id, body)
      section.setBody(id, [geometry])
      content.union(new THREE.Box3().setFromObject(body))
      section.setContent(content)
    }
    function sample() {
      renderer.render(scene, camera)
      renderer.readRenderTargetPixels(target, 0, 0, 128, 128, pixels)
      return [[-1, -1], [1, -1], [7, -1]].map(([x, y]) => {
        const p = new THREE.Vector3().setComponent(u, x).setComponent(v, y).project(camera)
        const ix = Math.floor((p.x + 1) * 64), iy = Math.floor((p.y + 1) * 64)
        return Array.from(pixels.slice((iy * 128 + ix) * 4, (iy * 128 + ix) * 4 + 4))
      })
    }
    function visible(id, shown) {
      bodies.get(id).visible = shown
      section.setBodyVisible(id, shown)
    }
    function dispose() {
      section.dispose()
      for (const body of bodies.values()) body.geometry.dispose()
      material.dispose()
    }
    return { section, spec, add, sample, visible, bodies, scene, axis, dispose }
  }
  function same(a, b, message) { check(JSON.stringify(a) === JSON.stringify(b), message + ': ' + JSON.stringify({ a, b })) }
  for (const plane of ['XY', 'XZ', 'YZ']) for (const inverted of [false, true]) for (const perspective of [false, true]) {
    const f = fixture(plane, inverted, perspective)
    f.add('0')
    const baseline = f.sample()
    check(baseline[0][1] > 0 && baseline[0][2] > 0, '対照の切り口が塗られていない')
    check(baseline[2][0] === 0 && baseline[2][1] === 0 && baseline[2][2] === 0, '対照の外側が塗られている: ' + JSON.stringify(baseline))
    const label = plane + '/' + inverted + '/' + perspective
    for (let i = 1; i < 25; i++) {
      // 1体目と9体目だけが重なる。8ビットを使い切った際の打ち消しを検出する。
      f.add(String(i), i === 8 ? 0 : 100 + i * 10)
      if ([8, 9, 15, 16, 24].includes(i)) same(f.sample(), baseline, label + '/bodies=' + (i + 1))
    }
    f.visible('0', false)
    same(f.sample(), baseline, label + '/hide-first')
    f.visible('8', false)
    check(f.sample()[0].slice(0, 3).every((value) => value === 0), label + '/hide-both')
    f.visible('0', true)
    f.section.setState({ ...f.spec, on: false })
    f.section.setState(f.spec)
    same(f.sample(), baseline, label + '/toggle')
    f.section.setBody('0', [f.bodies.get('0').geometry])
    same(f.sample(), baseline, label + '/replace')
    f.section.removeBody('8')
    same(f.sample(), baseline, label + '/remove')
    f.dispose()
    scenarios.push(label)
  }
  // 板が深度検査に落ちた画素に偶奇が残ると、後続の空のボディが余分な切り口を描く。
  const f = fixture('XY', false, false)
  f.add('0')
  const occluder = new THREE.Mesh(new THREE.BoxGeometry(4, 4, 2), new THREE.MeshBasicMaterial({ color: 0x0000ff }))
  occluder.position.set(0, 0, 6)
  f.scene.add(occluder)
  const probeGeometry = new THREE.PlaneGeometry(1000, 1000)
  const probeMaterial = new THREE.MeshBasicMaterial({ color: 0x00ff00, depthTest: false,
    depthWrite: false, stencilWrite: true, stencilFunc: THREE.NotEqualStencilFunc, stencilRef: 0 })
  const probe = new THREE.Mesh(probeGeometry, probeMaterial)
  probe.renderOrder = 100
  f.scene.add(probe)
  const covered = f.sample()
  check(covered[0][0] === 0 && covered[0][1] === 0 && covered[0][2] === 255, '遮蔽された画素にステンシルが残っている')
  f.add('far', 100)
  same(f.sample(), covered, 'occluded cap clears its stencil')
  const gl = renderer.getContext()
  check(gl.getError() === gl.NO_ERROR, 'WebGL error')
  f.dispose()
  probeGeometry.dispose()
  probeMaterial.dispose()
  occluder.geometry.dispose()
  occluder.material.dispose()
  target.dispose()
  renderer.dispose()
  return { scenarios, occlusion: true }
}
`

const vite = await createServer({
  root: fileURLToPath(new URL('..', import.meta.url)),
  configFile: false,
  appType: 'custom',
  logLevel: 'error',
  server: { middlewareMode: true, hmr: false },
  plugins: [{
    name: 'section-stencil-regression',
    resolveId(id) { if (id === '/__section_stencil__.js') return id },
    load(id) { if (id === '/__section_stencil__.js') return harness },
  }],
})
const server = createHttpServer((request, response) => {
  if (request.url === '/') {
    response.setHeader('Content-Type', 'text/html')
    response.end('<!doctype html><title>Section stencil regression</title>')
  } else vite.middlewares(request, response)
})
let browser
try {
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  browser = await launchBrowser()
  const page = await browser.newPage()
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  await page.goto('http://127.0.0.1:' + server.address().port)
  const result = await page.evaluate(async () => (await import('/__section_stencil__.js')).run())
  assert.equal(result.scenarios.length, 12)
  assert.deepEqual(errors, [])
  console.log('section stencil: 12 plane/direction/camera combinations, 9–25 bodies, lifecycle and occlusion passed')
} finally {
  await browser?.close()
  await vite.close()
  await new Promise((resolve) => server.close(resolve))
}
