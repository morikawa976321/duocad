/**
 * 遮蔽の判定: その3Dの点が、立体の面(と断面の切り口)に隠れずカメラから見えているか。
 * 「見えないものは指せない」(docs/DESIGN_DECISIONS.md「見えるものはすべてさわれる」)の実体で、人のポインタにだけ掛ける
 * (AI は参照でどれでも選べる)。
 *
 * - クリック・ホバーは候補ごとに、その点を通る視線を引いて最初の面までの距離と比べる(`visible`)。候補は当たり半径の
 *   内側の数個なので、レイキャストの数は少ない。カーソルの画素1本の深度で比べないのは、立体の輪郭の外にカーソルがあると
 *   隠れた候補が通り、斜面では隣の見えている候補を落とすため。
 * - 矩形選択は数千の候補を一度に見るので、遮るものの奥行きを1回描いて読み出し、点ごとに引く(`snapshot`)。
 *
 * 許し(`slack`)は「面の上に乗っているもの」を見えている側に入れるための奥行きの幅: 面の上の辺・面に貼ったスケッチは
 * 面と同じ奥行きにあり、面の三角形と辺の折れ線はそれぞれ別に弦へ寄る(偏差ぶん)。
 */
import * as THREE from 'three'

import { DEVIATION_MIN_ABS, DEVIATION_MIN_RELATIVE } from './adaptiveResolution'
import type { ProjectedPoint, ScreenProjector } from './polylinePick'
import { DEPTH_BIAS_LAYER, DEPTH_BIAS_UNIT } from './viewportConstants'

/** 遮るもの(見えているボディのメッシュ)だけを描くための layer。`viewport.ts` がボディのメッシュに立てる。 */
export const OCCLUDER_LAYER = 5

/**
 * 許しの下限[画素]。面のテセレーションの偏差(目標 0.35 画素。`adaptiveResolution.ts` の `TOLERANCE_PIXELS`)が
 * 面と辺の両方で逆向きに出ても収まり、薄い板の裏の辺(板厚が画素数個ぶん)は通さない幅。
 */
const SLACK_PIXELS = 2
/** 偏差の下限に張り付く深いズームでの許し(下限の偏差の倍数)。面と辺が別々に弦へ寄る分。 */
const SLACK_FLOOR_DEVIATIONS = 3
/** 折れ線の偏差が分かっている的(ソリッドの辺)の許し(その偏差の倍数)。 */
const SLACK_EDGE_DEVIATIONS = 2.5

export interface OcclusionDeps {
  camera(): THREE.Camera
  renderer: THREE.WebGLRenderer
  scene: THREE.Scene
  size(): { width: number; height: number }
  /** 視線で最初に当たる面(断面の切り口を含む)までの距離。何にも当たらなければ null。 */
  surfaceDistance(ray: THREE.Ray): number | null
  /** 見えているものの外接対角[mm](偏差の下限が比例する)。 */
  contentDiagonal(): number
  /** 断面のクリップ平面(共有配列)。奥行きを描くときも切る。 */
  clipPlanes: THREE.Plane[]
}

export interface DepthSnapshot {
  /** その点が見えているか(`Occlusion.visible` と同じ意味。読み出した奥行きで引く)。 */
  visible(point: THREE.Vector3, slack: number): boolean
}

export interface Occlusion {
  visible(point: THREE.Vector3, slack: number): boolean
  /** その点の奥行きでの画面1画素の長さ[mm]。 */
  pixelAt(point: THREE.Vector3, height: number): number
  /**
   * 球が画面で中心の射影から広がりうる半径[px]の上限(候補を画面の距離で絞るのに使う)。透視では手前の点ほど大きく映るので、
   * 中心の奥行きの1画素で割ると小さく見積もる(画角90度・距離50mmの40mm立方体の角が実際の 471px に対し 353px)。
   * 手前の端の奥行き d−r で割り、中心が視線から外れている分(|xy|/d)を足す。カメラが球に近い・中に入っていれば Infinity。
   */
  screenRadius(sphere: THREE.Sphere, height: number): number
  /** その球が画面でカーソル (x, y) から `radiusPx` 以内に掛かりうるか(掛からないボディ・辺・スケッチは歩かない)。 */
  sphereNear(sphere: THREE.Sphere, x: number, y: number, radiusPx: number, projector: ScreenProjector): boolean
  /** その点に置く許し[mm]。`deviation` は的の折れ線の偏差(分かっていれば)。 */
  slack(point: THREE.Vector3, height: number, deviation?: number): number
  /** 遮るものの奥行きを今のカメラで1回描いて読み出す(描けなければ null。そのときは遮蔽を見ない)。 */
  snapshot(): DepthSnapshot | null
  dispose(): void
}

export function createOcclusion(deps: OcclusionDeps): Occlusion {
  const raycaster = new THREE.Raycaster()
  const ndc = new THREE.Vector2()
  const scratch = new THREE.Vector3()

  function rayTo(point: THREE.Vector3): THREE.Ray | null {
    const camera = deps.camera()
    scratch.copy(point).project(camera)
    if (!Number.isFinite(scratch.x) || !Number.isFinite(scratch.y) || scratch.z < -1 || scratch.z > 1) return null
    ndc.set(scratch.x, scratch.y)
    raycaster.setFromCamera(ndc, camera)
    return raycaster.ray
  }

  function visible(point: THREE.Vector3, slack: number): boolean {
    const ray = rayTo(point)
    if (ray === null) return false
    const surface = deps.surfaceDistance(ray)
    return surface === null || ray.origin.distanceTo(point) <= surface + slack
  }

  /** その点の奥行きでの画面1画素の長さ[mm]。 */
  function pixelAt(point: THREE.Vector3, height: number): number {
    const camera = deps.camera()
    if (camera instanceof THREE.PerspectiveCamera) {
      const depth = Math.max(-scratch.copy(point).applyMatrix4(camera.matrixWorldInverse).z, 1e-9)
      return (2 * depth * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2)) / camera.zoom / Math.max(height, 1)
    }
    if (camera instanceof THREE.OrthographicCamera) {
      return (camera.top - camera.bottom) / camera.zoom / Math.max(height, 1)
    }
    return 0
  }

  function screenRadius(sphere: THREE.Sphere, height: number): number {
    const camera = deps.camera()
    if (sphere.radius < 0) return 0
    if (camera instanceof THREE.OrthographicCamera) {
      return (sphere.radius * camera.zoom * Math.max(height, 1)) / (camera.top - camera.bottom)
    }
    if (!(camera instanceof THREE.PerspectiveCamera)) return Infinity
    const view = scratch.copy(sphere.center).applyMatrix4(camera.matrixWorldInverse)
    const depth = -view.z
    const nearest = depth - sphere.radius
    if (nearest <= camera.near) return Infinity
    const pixelsPerMm = (camera.zoom * Math.max(height, 1)) / (2 * nearest * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2))
    return sphere.radius * pixelsPerMm * (1 + Math.hypot(view.x, view.y) / depth)
  }

  const projectedCenter: ProjectedPoint = { x: 0, y: 0, w: 1 }

  /** `screenRadius` と同じ見積もりを、射影1回(と、その射影の定数)から出す。辺1本ごとに呼ぶのでホバーの時間に効く。 */
  function sphereNear(sphere: THREE.Sphere, x: number, y: number, radiusPx: number, projector: ScreenProjector): boolean {
    if (sphere.radius < 0) return false
    if (!projector.project(sphere.center, projectedCenter)) return true // 中心がカメラの裏でも一部は見えうる
    let reach = sphere.radius * projector.focalPx
    if (!projector.orthographic) {
      const nearest = projectedCenter.w - sphere.radius
      if (nearest <= projector.near) return true
      // 中心が視線から外れている分(|xy|/d)は、画面の中心からの画素を focalPx で割ったもの
      const offAxis =
        Math.hypot(projectedCenter.x - projector.width / 2, projectedCenter.y - projector.height / 2) / projector.focalPx
      reach = (reach / nearest) * (1 + offAxis)
    }
    reach += radiusPx
    const dx = projectedCenter.x - x
    const dy = projectedCenter.y - y
    return dx * dx + dy * dy <= reach * reach
  }

  function slack(point: THREE.Vector3, height: number, deviation = 0): number {
    const floor = Math.max(DEVIATION_MIN_ABS, deps.contentDiagonal() * DEVIATION_MIN_RELATIVE)
    // 編集していないスケッチを手前へ寄せて描く量(画面の高さに対する比)より狭いと、寄せて見えているものが指せない
    const biasPixels = DEPTH_BIAS_LAYER.sketch * DEPTH_BIAS_UNIT * height
    const pixels = Math.max(SLACK_PIXELS, biasPixels + 1)
    return Math.max(pixels * pixelAt(point, height), SLACK_FLOOR_DEVIATIONS * floor, SLACK_EDGE_DEVIATIONS * deviation)
  }

  // ------------------------------------------------------------ 奥行きの読み出し(矩形選択)
  // 視点からの奥行きを near..far で 0..1 に正規化し、RGBA8 の4バイトへ分けて書く(浮動小数の描画先は環境を選ぶ)。
  // 両面を描く: 断面で切った中は切り口の蓋の代わりに裏の面が遮る(蓋より奥なので、蓋と裏の面の間にあるものは
  // 見えている側に数える。矩形選択の「一部でも見えていれば入れる」の甘い側に倒れるだけ)。
  const depthMaterial = new THREE.ShaderMaterial({
    side: THREE.DoubleSide,
    clipping: true,
    uniforms: { uNear: { value: 0.1 }, uFar: { value: 1000 } },
    vertexShader: /* glsl */ `
      #include <clipping_planes_pars_vertex>
      varying float vViewDepth;
      void main() {
        vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
        vViewDepth = -mvPosition.z;
        gl_Position = projectionMatrix * mvPosition;
        #include <clipping_planes_vertex>
      }
    `,
    fragmentShader: /* glsl */ `
      #include <clipping_planes_pars_fragment>
      uniform float uNear;
      uniform float uFar;
      varying float vViewDepth;
      void main() {
        #include <clipping_planes_fragment>
        float v = clamp((vViewDepth - uNear) / (uFar - uNear), 0.0, 0.999999);
        vec4 enc = fract(vec4(1.0, 255.0, 65025.0, 16581375.0) * v);
        enc -= enc.yzww * vec4(1.0 / 255.0, 1.0 / 255.0, 1.0 / 255.0, 0.0);
        gl_FragColor = enc;
      }
    `,
  })
  depthMaterial.clippingPlanes = deps.clipPlanes
  let target: THREE.WebGLRenderTarget | null = null

  function snapshot(): DepthSnapshot | null {
    const camera = deps.camera()
    const { width, height } = deps.size()
    const w = Math.max(1, Math.floor(width))
    const h = Math.max(1, Math.floor(height))
    if (!(camera instanceof THREE.PerspectiveCamera || camera instanceof THREE.OrthographicCamera)) return null
    if (target === null || target.width !== w || target.height !== h) {
      target?.dispose()
      target = new THREE.WebGLRenderTarget(w, h, { depthBuffer: true })
    }
    const near = camera.near
    const far = camera.far
    depthMaterial.uniforms.uNear.value = near
    depthMaterial.uniforms.uFar.value = far

    const { renderer, scene } = deps
    const previousTarget = renderer.getRenderTarget()
    const previousClear = renderer.getClearColor(new THREE.Color())
    const previousAlpha = renderer.getClearAlpha()
    const previousMask = camera.layers.mask
    const previousOverride = scene.overrideMaterial
    const previousBackground = scene.background
    const buffer = new Uint8Array(w * h * 4)
    try {
      camera.layers.set(OCCLUDER_LAYER)
      scene.overrideMaterial = depthMaterial
      scene.background = null
      renderer.setRenderTarget(target)
      renderer.setClearColor(0xffffff, 1)
      renderer.clear(true, true, false)
      renderer.render(scene, camera)
      renderer.readRenderTargetPixels(target, 0, 0, w, h, buffer)
    } catch {
      return null
    } finally {
      camera.layers.mask = previousMask
      scene.overrideMaterial = previousOverride
      scene.background = previousBackground
      renderer.setRenderTarget(previousTarget)
      renderer.setClearColor(previousClear, previousAlpha)
    }

    const viewPoint = new THREE.Vector3()
    const projected = new THREE.Vector3()
    /** 画素 (ix, iy)(左上原点)の面の奥行き。面が無ければ Infinity。 */
    function depthAt(ix: number, iy: number): number {
      if (ix < 0 || iy < 0 || ix >= w || iy >= h) return Infinity
      const at = ((h - 1 - iy) * w + ix) * 4
      const v =
        buffer[at] / 255 +
        buffer[at + 1] / 255 / 255 +
        buffer[at + 2] / 255 / 65025 +
        buffer[at + 3] / 255 / 16581375
      if (v >= 0.99999) return Infinity
      return near + v * (far - near)
    }
    return {
      visible(point, slackMm) {
        projected.copy(point).project(camera)
        if (projected.z < -1 || projected.z > 1) return false
        const ix = Math.floor(((projected.x + 1) / 2) * w)
        const iy = Math.floor(((1 - projected.y) / 2) * h)
        const depth = -viewPoint.copy(point).applyMatrix4(camera.matrixWorldInverse).z
        // その画素と上下左右のうち一番奥の面で比べる(輪郭の上の点が隣の画素の面に隠れたことにしない)
        const surface = Math.max(
          depthAt(ix, iy),
          depthAt(ix - 1, iy),
          depthAt(ix + 1, iy),
          depthAt(ix, iy - 1),
          depthAt(ix, iy + 1),
        )
        return depth <= surface + slackMm
      },
    }
  }

  return {
    visible,
    pixelAt,
    screenRadius,
    sphereNear,
    slack,
    snapshot,
    dispose() {
      target?.dispose()
      depthMaterial.dispose()
    },
  }
}
