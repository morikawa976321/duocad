/** 視点の控えの比べ方(`web/src/viewSnapshot.ts`)が、止まったカメラの揺れを同じと見て、画素の動く変化を違うと見る。 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'

const THREE = await import(new URL('../web/node_modules/three/build/three.module.js', import.meta.url).href)
const { probePoints, sameView, snapshotView } = await import('../web/src/viewSnapshot.ts')

const WIDTH = 944
const HEIGHT = 688

/** `viewport.ts` の `viewSnapshot` と同じ取り方(見本点の届く範囲は見えている広さの半分、透視はカメラまでの半分まで)。 */
function worldToClip(camera) {
  camera.updateMatrixWorld()
  return new THREE.Matrix4().multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse).elements
}
function snapshot(camera, target, width = WIDTH, height = HEIGHT) {
  const distance = camera.position.distanceTo(target)
  let reach
  if (camera.isPerspectiveCamera) {
    const perPixel = (2 * Math.tan((camera.fov * Math.PI) / 360) * distance) / height
    reach = Math.min((perPixel * Math.min(width, height)) / 2, distance / 2)
  } else {
    reach = (((camera.top - camera.bottom) / camera.zoom / height) * Math.min(width, height)) / 2
  }
  return snapshotView(worldToClip(camera), probePoints(target.toArray(), camera.matrixWorld.elements, reach), width, height)
}
const same = (kept, camera, width = WIDTH, height = HEIGHT) => sameView(kept, worldToClip(camera), width, height)

function perspective() {
  const camera = new THREE.PerspectiveCamera(30, WIDTH / HEIGHT, 1, 2000)
  camera.position.set(140.932268, -95.932268, 77.559361)
  camera.up.set(0, 0, 1)
  camera.lookAt(20, 15, 5)
  camera.updateProjectionMatrix()
  return { camera, target: new THREE.Vector3(20, 15, 5) }
}
function topView(halfHeight) {
  const aspect = WIDTH / HEIGHT
  const camera = new THREE.OrthographicCamera(-halfHeight * aspect, halfHeight * aspect, halfHeight, -halfHeight, 0.1, 1e7)
  camera.position.set(0, 0, 500)
  camera.up.set(0, 1, 0)
  camera.lookAt(0, 0, 0)
  camera.updateProjectionMatrix()
  return { camera, target: new THREE.Vector3(0, 0, 0) }
}

// 止まったカメラの揺れ(スケッチモード): 姿勢の成分が 0 をはさんで ±1e-8 揺れる。小数6桁の文字列では -0.000000 と 0.000000 に分かれた
for (const make of [perspective, () => topView(50)]) {
  const { camera, target } = make()
  const kept = snapshot(camera, target)
  camera.quaternion.x += 1e-8
  camera.quaternion.y -= 1e-8
  camera.position.x += 1e-8
  assert.ok(same(kept, camera), '揺れを視点の変化と見た')
  assert.ok(same(kept, camera, WIDTH, HEIGHT))
}

// 正投影で上下 ±100 m を映しているときの 5% のズーム(行列の成分の差は 5e-7 しかない)
{
  const { camera, target } = topView(100000)
  const kept = snapshot(camera, target)
  camera.zoom = 1.0526
  camera.updateProjectionMatrix()
  assert.ok(!same(kept, camera), '広い正投影のズームを同じ視点と見た')
}

// 1 px のパンは違う。0.01 px のパンは同じ
{
  const { camera, target } = topView(50)
  const kept = snapshot(camera, target)
  const pixel = 100 / HEIGHT
  camera.position.x += pixel
  assert.ok(!same(kept, camera), '1 px のパンを同じ視点と見た')
  camera.position.x -= pixel * 0.99
  assert.ok(same(kept, camera), '0.01 px のずれを違う視点と見た')
}

// 透視: 注視点まわりの 0.1° の回転・寄り・画面の寸法の変化は違う
{
  const { camera, target } = perspective()
  const kept = snapshot(camera, target)
  camera.position.sub(target).applyAxisAngle(new THREE.Vector3(0, 0, 1), (0.1 * Math.PI) / 180).add(target)
  camera.lookAt(target)
  assert.ok(!same(kept, camera), '0.1° の回転を同じ視点と見た')
}
{
  const { camera, target } = perspective()
  const kept = snapshot(camera, target)
  camera.position.lerp(target, 0.02)
  assert.ok(!same(kept, camera), '2% 寄ったのを同じ視点と見た')
}
{
  const { camera, target } = perspective()
  const kept = snapshot(camera, target)
  assert.ok(!same(kept, camera, WIDTH, HEIGHT - 1), '画面の寸法の変化を同じ視点と見た')
}

// 注視点の面の写り方を保ったまま寄って画角を広げる(奥行きの写り方だけが変わる)のも違う
{
  const { camera, target } = perspective()
  const kept = snapshot(camera, target)
  const distance = camera.position.distanceTo(target)
  const nearer = distance * 0.8
  camera.fov = (2 * Math.atan((Math.tan((camera.fov * Math.PI) / 360) * distance) / nearer) * 180) / Math.PI
  camera.position.copy(target).add(camera.position.clone().sub(target).setLength(nearer))
  camera.updateProjectionMatrix()
  assert.ok(!same(kept, camera), '寄りながら画角を広げたのを同じ視点と見た')
}

// 控えは写し(カメラを動かしても控えの画素は変わらない)。カメラの後ろへ回った見本点は同じにならない
{
  const { camera, target } = perspective()
  const kept = snapshot(camera, target)
  camera.position.set(-140, 95, 77)
  camera.lookAt(-300, 200, 100)
  assert.ok(!same(kept, camera))
}

console.log('ok')
