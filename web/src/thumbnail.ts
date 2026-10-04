/**
 * ファイル一覧のサムネイルを撮る(128px四方のPNG。背景は透明)。
 *
 * **画面のシーンは描かない。** 形状(`BufferGeometry`)だけを借りて専用の小さなシーンに並べ、
 * 材質・光・カメラはここで持つ —— 選択の強調・ホバー・断面のクリップ・グリッドやスケッチの
 * 重ね表示が写り込まず、撮ったときの視点にも左右されない(いつ撮っても同じ向きの絵になる)。
 *
 * 呼び手(`viewport.ts`)は見えている立体のメッシュを渡すだけで、撮る時機は
 * `documentFiles.svelte.ts` が決める。
 */
import * as THREE from 'three'
import { BASE_COLOR, EDGE_COLOR, HOME_VIEW_OFFSET } from './viewportConstants'

/** 書き出すPNGの一辺[px]。一覧では56px四方に出す(高DPIで2倍まで粗くならない大きさ)。 */
export const THUMBNAIL_SIZE = 128
/** 縮小前に描く倍率。描画先はMSAAを持たないので、大きく描いて縮めて縁のギザギザを消す。 */
const SUPERSAMPLE = 2
/** 立体のまわりに残す余白(外接の一辺に対する割合)。 */
const MARGIN = 0.08

export interface Thumbnailer {
  /**
   * 渡したオブジェクト(面の `Mesh` と境界線の `LineSegments`)を撮り、PNGのbase64を返す
   * (`data:` の接頭辞は付けない)。**撮るものが無ければ空文字**(サーバは空文字で消す)。
   */
  capture(objects: readonly THREE.Object3D[]): string
  dispose(): void
}

export function createThumbnailer(renderer: THREE.WebGLRenderer): Thumbnailer {
  const pixels = THUMBNAIL_SIZE * SUPERSAMPLE
  // **sRGBの描画先にする**。three は描画先へは線形のまま書くので、素の8bitの描画先から
  // 読むと画面より暗い絵になる。sRGBの描画先ならGPUが書き込み時に変換し、読み出しは
  // 変換後の値を返す。
  const target = new THREE.WebGLRenderTarget(pixels, pixels, { colorSpace: THREE.SRGBColorSpace })
  const scene = new THREE.Scene()
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 10)
  // 光は画面(`viewport.ts`)と同じ組: 視点に付けた平行光 + 天地の半球光。
  // 平行光の向きは「光の位置 → `target`」で決まる。`target` を立体の中心に置き、光の位置は
  // 立体の大きさに合わせて離す(寸法の桁が違っても同じ向きから当たるように)。
  const headlight = new THREE.DirectionalLight(0xffffff, 1.6)
  const aim = new THREE.Object3D()
  headlight.target = aim
  camera.add(headlight)
  scene.add(camera)
  scene.add(aim)
  scene.add(new THREE.HemisphereLight(0xdfe7f2, 0x191c21, 1.0))

  // 面は境界線より奥へ押す(同じ深さで描くと線が面に負けて途切れる)。
  const faceMaterial = new THREE.MeshLambertMaterial({
    color: BASE_COLOR,
    side: THREE.DoubleSide,
    polygonOffset: true,
    polygonOffsetFactor: 1,
    polygonOffsetUnits: 1,
  })
  const flatFaceMaterial = faceMaterial.clone()
  flatFaceMaterial.flatShading = true
  const edgeMaterial = new THREE.LineBasicMaterial({ color: EDGE_COLOR })

  const readback = new Uint8Array(pixels * pixels * 4)
  const large = document.createElement('canvas')
  large.width = pixels
  large.height = pixels
  const small = document.createElement('canvas')
  small.width = THUMBNAIL_SIZE
  small.height = THUMBNAIL_SIZE

  /** カメラを `box` が正方形の中にちょうど収まる位置へ置く(向きは画面の初期視点と同じ)。 */
  function frame(box: THREE.Box3): void {
    const center = box.getCenter(new THREE.Vector3())
    const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 1e-6)
    const direction = HOME_VIEW_OFFSET.clone().normalize()
    camera.position.copy(center).addScaledVector(direction, radius * 2)
    camera.lookAt(center)
    headlight.position.set(0.4, 0.7, 1).multiplyScalar(radius * 2)
    aim.position.copy(center)
    camera.updateMatrixWorld(true)
    aim.updateMatrixWorld(true)
    // 外接球ではなく箱の8隅を視点の座標へ移して測る(細長い部品が小さく写らないように)。
    const view = camera.matrixWorldInverse
    const corner = new THREE.Vector3()
    let minX = Infinity
    let maxX = -Infinity
    let minY = Infinity
    let maxY = -Infinity
    let minZ = Infinity
    let maxZ = -Infinity
    for (let i = 0; i < 8; i += 1) {
      corner
        .set(i & 1 ? box.max.x : box.min.x, i & 2 ? box.max.y : box.min.y, i & 4 ? box.max.z : box.min.z)
        .applyMatrix4(view)
      minX = Math.min(minX, corner.x)
      maxX = Math.max(maxX, corner.x)
      minY = Math.min(minY, corner.y)
      maxY = Math.max(maxY, corner.y)
      minZ = Math.min(minZ, corner.z)
      maxZ = Math.max(maxZ, corner.z)
    }
    const half = (Math.max(maxX - minX, maxY - minY) / 2) * (1 + MARGIN * 2)
    const midX = (minX + maxX) / 2
    const midY = (minY + maxY) / 2
    camera.left = midX - half
    camera.right = midX + half
    camera.top = midY + half
    camera.bottom = midY - half
    // 視点の座標では奥が -z。前後に少し余らせる。
    const depth = Math.max(maxZ - minZ, 1e-6)
    camera.near = Math.max(-maxZ - depth * 0.1, 1e-6)
    camera.far = -minZ + depth * 0.1
    camera.updateProjectionMatrix()
  }

  function capture(objects: readonly THREE.Object3D[]): string {
    const box = new THREE.Box3()
    const stand: THREE.Object3D[] = []
    for (const object of objects) {
      object.updateWorldMatrix(true, false)
      let copy: THREE.Object3D | null = null
      if (object instanceof THREE.Mesh) {
        box.expandByObject(object)
        copy = new THREE.Mesh(object.geometry, object.userData.flatShaded ? flatFaceMaterial : faceMaterial)
      } else if (object instanceof THREE.LineSegments) {
        copy = new THREE.LineSegments(object.geometry, edgeMaterial)
      }
      if (copy === null) continue
      copy.matrixAutoUpdate = false
      copy.matrix.copy(object.matrixWorld)
      stand.push(copy)
    }
    if (box.isEmpty()) return ''
    for (const copy of stand) scene.add(copy)
    frame(box)

    const previousTarget = renderer.getRenderTarget()
    const previousClear = renderer.getClearColor(new THREE.Color())
    const previousAlpha = renderer.getClearAlpha()
    try {
      renderer.setRenderTarget(target)
      renderer.setClearColor(0x000000, 0)
      renderer.clear()
      renderer.render(scene, camera)
      renderer.readRenderTargetPixels(target, 0, 0, pixels, pixels, readback)
    } finally {
      renderer.setRenderTarget(previousTarget)
      renderer.setClearColor(previousClear, previousAlpha)
      for (const copy of stand) scene.remove(copy)
    }

    // 読み出しは下の行から。2Dキャンバスへは上下を返して置く。
    const image = new ImageData(pixels, pixels)
    const row = pixels * 4
    for (let y = 0; y < pixels; y += 1) {
      image.data.set(readback.subarray((pixels - 1 - y) * row, (pixels - y) * row), y * row)
    }
    large.getContext('2d')!.putImageData(image, 0, 0)
    const context = small.getContext('2d')!
    context.clearRect(0, 0, THUMBNAIL_SIZE, THUMBNAIL_SIZE)
    context.imageSmoothingEnabled = true
    context.imageSmoothingQuality = 'high'
    context.drawImage(large, 0, 0, THUMBNAIL_SIZE, THUMBNAIL_SIZE)
    return small.toDataURL('image/png').slice('data:image/png;base64,'.length)
  }

  return {
    capture,
    dispose() {
      target.dispose()
      faceMaterial.dispose()
      flatFaceMaterial.dispose()
      edgeMaterial.dispose()
    },
  }
}
