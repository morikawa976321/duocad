/**
 * `web/src/resolutionTracker.ts`(可変解像度の「カメラが止まったか」)を**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる(WebGL は要らない)。
 *
 * 落ちたら、ビューポートの横幅だけが変わるリサイズ(左右パネルの開閉)で、細かく測る面の集合(focus)が採り直されない:
 * 視錐台が横に広がっても、新しく画面に入った部品は次にカメラが動くまで粗い測り(角が見える)のまま残る。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const THREE = await import(new URL('../web/node_modules/three/build/three.module.js', import.meta.url).href)
const { createResolutionTracker } = await import('../web/src/resolutionTracker.ts')

/** 立方体1つぶんの面見本(wasFine=false: 縁の余裕で粗い側へ落ちる判定を混ぜない)。 */
function face(index, x) {
  return {
    index,
    box: new THREE.Box3(new THREE.Vector3(x - 5, -5, -5), new THREE.Vector3(x + 5, 5, 5)),
    centroid: new THREE.Vector3(x, 0, 0),
    normal: new THREE.Vector3(0, -1, 0),
    planar: false,
    wasFine: false,
  }
}

function makeTracker(camera) {
  const moved = { count: 0 }
  const tracker = createResolutionTracker({
    camera: () => camera,
    worldPerPixel: () => 0.1,
    contentDiagonal: () => 800,
    // 面1は注視点の近く、面2は横へ 300mm 離れている。
    faceSamples: new Map([['a', { closed: false, slack: 0, faces: [face(1, 0), face(2, 300)] }]]),
    focusGeneration: () => 1,
    onMoved: () => (moved.count += 1),
    onApplied() {},
  })
  return { tracker, moved }
}

function lookingAtOrigin(aspect) {
  const camera = new THREE.PerspectiveCamera(45, aspect, 1, 5000)
  camera.up.set(0, 0, 1)
  camera.position.set(0, -500, 0)
  camera.lookAt(0, 0, 0)
  camera.updateMatrixWorld()
  return camera
}

// 縦に細長い窓(アスペクト 0.4)では横の面2が画面の外。細かく測るのは面1だけ。
{
  const camera = lookingAtOrigin(0.4)
  const { tracker, moved } = makeTracker(camera)
  let now = 0
  tracker.update(now)
  tracker.update((now += 1000))
  assert.deepEqual(tracker.applied().focus, { a: [1] }, '細い窓では面2が画面の外のはず')
  const movedBefore = moved.count

  // 横幅だけが広がる(カメラの位置・姿勢・ズーム・縦の画角は同じ)。面2が画面に入る。
  camera.aspect = 3
  camera.updateProjectionMatrix()
  camera.updateMatrixWorld()
  tracker.update((now += 16))
  assert.ok(moved.count > movedBefore, '横幅だけの変化を「動いた」と見ない(次のフレームから落ち着きを数え直さない)')
  tracker.update((now += 1000))
  assert.equal(tracker.applied().focus, null, '広がった窓では全面が見える(指定なし)のはず。新しく入った面が粗いまま残っている')
}

// 何も変えなければ動いたと見ない(減衰の収束判定を壊さない)。
{
  const camera = lookingAtOrigin(1.6)
  const { tracker, moved } = makeTracker(camera)
  let now = 0
  tracker.update(now)
  const settled = moved.count
  for (let i = 0; i < 5; i += 1) tracker.update((now += 16))
  assert.equal(moved.count, settled, '何も変わらないのに「動いた」と見ている')
}

console.log('resolutionTracker.ts: 全ての想定を満たした')
