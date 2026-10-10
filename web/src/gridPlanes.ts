/**
 * 平面グリッド(3段固定・無限)と、基準平面のピック範囲・ホバー板・面外の軸線。
 *
 * 平面frame(原点・u軸・v軸)を受け取って描く `planeGrid.ts` を最大3枚使い回す。
 * どの平面を出すかは状態が決める(`desiredGridPlanes`)。
 * ピック対象にはしない(判定は本編 `basePlaneAt` の解析交差で、範囲の半幅を `extent()` で読む)。
 * ホバー板と判定範囲は同じ `gridExtent` から作る(板は見えているのに指せない、を防ぐ)。
 */
import * as THREE from 'three'

import {
  BASE_EXTENT,
  TIERS,
  boundingSquare,
  extendUvBoxToPoint,
  extentFor,
  tierAlphas,
  type UvBounds,
  type UvBox,
} from './gridScale'
import { frameVector, planePoint } from './planeMath'
import { createPlaneGrid, type PlaneFrame, type PlaneGrid } from './planeGrid'
import { createBatch } from './sceneBatch'
import type { BasePlaneName } from './session'
import {
  AXIS_X_COLOR,
  AXIS_Y_COLOR,
  AXIS_Z_COLOR,
  BASE_PLANES,
  BASE_PLANE_AXES,
  BASE_PLANE_HOVER_OPACITY,
  DEPTH_BIAS_LAYER,
  DEPTH_BIAS_UNIT,
  GRID_ARMED_COLOR,
  GRID_COLOR,
  LAYER,
  withDepthBias,
} from './viewportConstants'

/** 本編(`viewport.ts`)から借りるもの。 */
export interface GridPlanesDeps {
  /**
   * グリッド計算専用の外接直方体(本編の `contentBoxForGrid`。`root` + `sketchRoot`)。
   * 最初の `apply()` の中で読むので、本編は `root` / `sketchRoot` を作った後で
   * `createGridPlanes` を呼ぶこと(docs/WEB_PITFALLS.md の TDZ)。
   */
  contentBox(): THREE.Box3
  /** その面のメッシュ(貼った面の範囲を測る。見つからなければ undefined)。 */
  faceMesh(bodyId: string, faceIndex: number): THREE.Mesh | undefined
  /** 画面1画素ぶんの世界座標の長さ(本編の `worldPerPixel`)。 */
  worldPerPixel(): number
  /** 今のカメラ(透視/正投影の切り替えで差し替わるので getter)。 */
  camera(): THREE.Camera
  /** ピック範囲が変わった(near/far の下限を引き直す)。 */
  onExtentChange(): void
}

/** 貼った面(`plane.kind === 'face'` のスケッチ)。 */
export interface SketchFace {
  bodyId: string
  faceIndex: number
}

export interface GridPlanes {
  /** グリッドを入れるグループ。`scene` へ足すのは呼び出し側。`root` には入れない(初回の `frameToContent` がグリッドの箱に引っ張られる)。 */
  object: THREE.Group
  /** 出す平面・面内の軸・色・面外の軸線を、いまの状態から入れ直す。 */
  apply(): void
  /** 基準3平面を指せるか(本編の `showBasePlanes`)。 */
  setArmed(on: boolean): void
  armed(): boolean
  /** スケッチ平面のframeを差し替える(モード外は null)。`face` は `plane.kind==='face'` のときだけ渡す。 */
  setSketchFrame(frame: PlaneFrame | null, face: SketchFace | null): void
  /** カーソル追従の軽量経路(スケッチ中だけ効く。`point` が null ならカーソル無し)。 */
  followCursor(point: { u: number; v: number } | null): void
  /** 段ごとの不透明度とカメラ行列を入れ直す(毎フレーム)。 */
  updateUniforms(): void
  /** 基準平面のピック範囲を内容の大きさへ合わせる(`allowShrink` の意味は実装のdocstring)。 */
  rescale(box: THREE.Box3, allowShrink: boolean): void
  /** 基準平面のピック範囲の半幅[mm]。 */
  extent(): number
  /** ホバー中の基準平面(板を1枚だけ見せる。null なら全部隠す)。 */
  setHoverPlane(name: BasePlaneName | null): void
  /** その平面のホバー板(ホバー色を読む検査フック用)。 */
  hoverMesh(name: BasePlaneName): THREE.Mesh | undefined
  /** 実機検査フック `gridTiers()` の中身。 */
  info(): {
    mmPerPixel: number
    tiers: Array<{ step: number; alpha: number }>
    planes: string[]
    axis: 'X' | 'Y' | 'Z' | null
    sketchFaceExtent: number | null
    depthBiasLayers: number[]
    planeBounds: Array<UvBounds | null>
  }
  dispose(): void
}

export function createGridPlanes(deps: GridPlanesDeps): GridPlanes {
  const { contentBox, faceMesh, worldPerPixel, camera, onExtentChange } = deps

  const gridRoot = new THREE.Group()
  /** 基準平面のホバー板(除去と破棄の束。ピックは `basePlaneHoverMeshes` から)。 */
  const basePlaneHoverBatch = createBatch(gridRoot)
  // Group の `renderOrder` は groupOrder として子全体に効く。中身どうしの前後は `LAYER.*` が決める。
  gridRoot.renderOrder = -1

  /**
   * 基準平面のピック範囲の半幅[mm]。描画範囲ではない(格子は無限)。
   * 本編の `basePlaneAt`・`updateClipPlanes` のnear/far下限・面外の軸線の長さが見る。`rescale` だけが動かす。
   */
  let gridExtent = BASE_EXTENT

  /** 基準3平面を指せるか(`setArmed`)。出していない間は当たり判定を回さない。 */
  let basePlanesOn = false

  /**
   * スケッチ中のスケッチ平面のframe(モード外は null)。書き手は `setSketchFrame` だけで、
   * 本編の `setActiveSketch` と `showSketches` の両方から呼ぶ(モードに入った時点ではframeを知らないことがある)。
   */
  let sketchGridFrame: PlaneFrame | null = null

  /**
   * `sketchGridFrame` が非nullの間、グリッドへ渡している描く正方形(カーソル追従で広がった後の値を含む)。
   * 面スケッチで `faceUvBoxOnly` が測れなかったときは `null`(無制限描画)。
   * 書き手は `setSketchFrame` / `desiredGridPlanes` / `followCursor` の3箇所(`sketchGridFrame` と対で更新)。
   */
  let sketchGridBounds: UvBounds | null = null

  /**
   * スケッチ描画中の「内容だけ」のUV矩形(カーソル合成前・`boundingSquare` の余白/丸め前)。
   * `followCursor` が pointermove ごとに頂点走査をやり直さないためのキャッシュ。
   * 書き手は `desiredGridPlanes`(基準平面側)と `setSketchFrame`(面側)の内容が変わったときだけ。
   */
  let sketchContentUvBox: UvBox | null = null
  /** `sketchContentUvBox` と対で持つ、`boundingSquare` へ渡す `includeOrigin`。 */
  let sketchContentIncludeOrigin = false

  /**
   * いまのスケッチが実ジオメトリの面(`plane.kind === 'face'`)へ貼られているか。
   * frame一致(`basePlaneForFrame`)では判定できない: 「XY床にスケッチ→押し出し→その底面に再度スケッチ」
   * のように実ジオメトリの面でもframeが基準平面と数値的に一致し、任意面のグリッド(深度バイアス層・
   * extentの有限化・直交軸なし)が不発になる。その面は実ソリッドと同一平面でz-fighting対策が最も要る。
   */
  let sketchGridIsFacePlane = false

  /** 実機検査フック(`gridTiers`)へ出す、いま表示している平面の名前。 */
  let gridPlanesShown: string[] = []
  /** `gridPlanesShown` と対の、各グリッドの深度バイアス層番号(実機検査フック専用。z-fightingは静止画では機械判定できないため数値を検査する)。 */
  let gridDepthBiasLayers: number[] = []

  /** `gridPlanesShown` と対で持つ、各グリッドに適用中の描画範囲(実機検査フック `gridTiers`)。 */
  let gridPlaneBounds: Array<UvBounds | null> = []

  /** 同時に出しうる平面は最大3枚(対象待ちの XY/XZ/YZ)。作り直さず、frameを差し替えて使い回す。 */
  const planeGrids: PlaneGrid[] = [createPlaneGrid(), createPlaneGrid(), createPlaneGrid()]
  for (const grid of planeGrids) {
    grid.object.renderOrder = LAYER.planeGrid
    // 深度バイアスは直後の `applyGridPlanes()` が入れ直す。
    grid.setVisible(false)
    gridRoot.add(grid.object)
  }

  /** 面外の軸(1本)。面内の軸はグリッドのシェーダーが描くので、線にするのはこれだけ。 */
  const gridAxisMaterial = withDepthBias(
    new THREE.LineBasicMaterial({
      color: AXIS_Z_COLOR,
      // 不透明だが透明パスへ入れる(実体より後に描かないと共平面の面に隠れる)。
      transparent: true,
      depthWrite: false,
    }),
    DEPTH_BIAS_LAYER.gridAxisLine,
  )
  let gridAxisLine: THREE.Line | null = null
  let gridAxisShown: 'X' | 'Y' | 'Z' | null = null

  /** ホバー中の基準平面に重ねる半透明の板(同時に見えるのは多くても1枚)。ピック範囲が変わるたびに作り直す(`rebuildBasePlaneHover`)。当たり判定には使わない。 */
  const basePlaneHoverMeshes = new Map<BasePlaneName, THREE.Mesh>()
  /** いま板を見せている平面(`setHoverPlane` だけが書く)。 */
  let hoverPlane: BasePlaneName | null = null

  function basePlaneSpec(name: BasePlaneName): (typeof BASE_PLANES)[number] {
    const found = BASE_PLANES.find((spec) => spec.name === name)
    if (found === undefined) throw new Error(`基準平面が表に無い: ${name}`)
    return found
  }

  function baseFrame(name: BasePlaneName): PlaneFrame {
    const spec = basePlaneSpec(name)
    return { origin: [0, 0, 0], u: spec.u, v: spec.v }
  }

  /**
   * frameがどの基準平面のものか(u/vの一致で見る)。基準平面でなければ null。
   * 「基準平面か任意面か」の判定には使わない(`sketchGridIsFacePlane` 参照)。
   * `plane.kind==='base'` のときにXY/XZ/YZのどれを表示するかだけを答える。
   */
  function basePlaneForFrame(frame: PlaneFrame): BasePlaneName | null {
    const same = (a: readonly number[], b: readonly number[]): boolean =>
      Math.abs(a[0] - b[0]) < 1e-6 && Math.abs(a[1] - b[1]) < 1e-6 && Math.abs(a[2] - b[2]) < 1e-6
    if (!same(frame.origin, [0, 0, 0])) return null
    for (const spec of BASE_PLANES) {
      if (same(frame.u, spec.u) && same(frame.v, spec.v)) return spec.name
    }
    return null
  }

  /**
   * 基準平面グリッドの「内容だけ」のUV矩形(`boundingSquare` の余白/丸め前)。
   * すべてのスケッチとソリッドの外接直方体の8隅をu/vへ射影した外接矩形(頂点を全部回さずに済む)。
   * `boundingSquare` を呼ばないのは、カーソル追従(`extendUvBoxToPoint`)が余白・丸めの前に合成するため。
   */
  function contentUvBoxForFrame(frame: PlaneFrame): UvBox | null {
    const box = contentBox()
    if (box.isEmpty()) return null
    const origin = frameVector(frame.origin)
    const axisU = frameVector(frame.u).normalize()
    const axisV = frameVector(frame.v).normalize()
    const corner = new THREE.Vector3()
    let uv: UvBox | null = null
    for (let i = 0; i < 8; i++) {
      corner
        .set(
          i & 1 ? box.max.x : box.min.x,
          i & 2 ? box.max.y : box.min.y,
          i & 4 ? box.max.z : box.min.z,
        )
        .sub(origin)
      uv = extendUvBoxToPoint(uv, { u: corner.dot(axisU), v: corner.dot(axisV) })
    }
    return uv
  }

  /** 基準平面のグリッドを描く正方形(カーソル追従が要らない3平面対象待ち・スケッチ外の通常表示用)。 */
  function contentBoundsForPlane(frame: PlaneFrame): UvBounds {
    return boundingSquare(contentUvBoxForFrame(frame), { includeOrigin: true })
  }

  /**
   * 貼った面の「内容だけ」のUV矩形(`boundingSquare` の余白/丸め前)を、スケッチのframe(u,v)で測る。
   *
   * 基準平面の `extentFor` は使い回さない: `BASE_EXTENT`(100mm)下限のせいで、実測40×20mmの面に
   * 100mmを返し、背景が線で埋まった。
   * 原点対称の半幅ではなく実測の min/max を返す: 面のframe原点は面の隅(OCCTのUVパラメータ化の原点)
   * のことが多く、対称な枠だと片側へ偏る(実測: u:0-40 / v:-20-0 の面に ±50mm の枠が出た)。
   *
   * 見つからなければ `null`(範囲で切らずに描く)。面の実際の輪郭では切らない(ステンシルが要り費用に見合わない)。
   */
  function faceUvBoxOnly(face: SketchFace, frame: PlaneFrame): UvBox | null {
    const mesh = faceMesh(face.bodyId, face.faceIndex)
    if (!mesh) return null
    const position = mesh.geometry.getAttribute('position') as THREE.BufferAttribute
    if (!position || position.count === 0) return null
    const origin = frameVector(frame.origin)
    const axisU = frameVector(frame.u).normalize()
    const axisV = frameVector(frame.v).normalize()
    const rel = new THREE.Vector3()
    let uv: UvBox | null = null
    for (let i = 0; i < position.count; i++) {
      rel.set(position.getX(i), position.getY(i), position.getZ(i)).sub(origin)
      uv = extendUvBoxToPoint(uv, { u: rel.dot(axisU), v: rel.dot(axisV) })
    }
    if (uv === null) return null
    if (!(uv.uMax - uv.uMin > 0) && !(uv.vMax - uv.vMin > 0)) return null
    return uv
  }

  interface GridPlaneSpec {
    /** 実機検査フック(`gridTiers`)へ出す名前。 */
    name: string
    frame: PlaneFrame
    /** 面内の軸の色。`null` の側は描かない(同じ世界軸を2枚が二重に描かないため)。 */
    uColor: number | null
    vColor: number | null
    /** 描く正方形(frame上の中心と半径[mm])。`null` は範囲が測れなかったときだけ。 */
    bounds: UvBounds | null
    /** 深度バイアスの層番号(`DEPTH_BIAS_LAYER`)。 */
    depthBiasLayer: number
  }

  /**
   * いま出す平面と、面外に線で描く軸。
   *
   * | 状態 | 表示 |
   * |---|---|
   * | 通常(スケッチ外) | XY平面 + Z軸 |
   * | スケッチ起動中(対象待ち・`setArmed(true)`) | 3平面すべて |
   * | 基準平面へのスケッチ中 | その平面 + それに直交する軸 |
   * | 任意面へのスケッチ中 | その面のframeのグリッドのみ(直交軸なし) |
   *
   * 対象待ちは基準平面もクリックして指すので3平面のまま(3本の軸が全部どれかの面内に出るので面外の軸は要らない)。
   */
  function desiredGridPlanes(): {
    planes: GridPlaneSpec[]
    axis: (typeof BASE_PLANE_AXES)[BasePlaneName]['outOfPlane'] | null
  } {
    if (basePlanesOn) {
      return {
        planes: [
          { name: 'XY', frame: baseFrame('XY'), uColor: AXIS_X_COLOR, vColor: AXIS_Y_COLOR, bounds: contentBoundsForPlane(baseFrame('XY')), depthBiasLayer: DEPTH_BIAS_LAYER.planeGrid },
          { name: 'XZ', frame: baseFrame('XZ'), uColor: null, vColor: AXIS_Z_COLOR, bounds: contentBoundsForPlane(baseFrame('XZ')), depthBiasLayer: DEPTH_BIAS_LAYER.planeGrid },
          { name: 'YZ', frame: baseFrame('YZ'), uColor: null, vColor: null, bounds: contentBoundsForPlane(baseFrame('YZ')), depthBiasLayer: DEPTH_BIAS_LAYER.planeGrid },
        ],
        axis: null,
      }
    }
    if (sketchGridFrame !== null && sketchGridIsFacePlane) {
      // 実ジオメトリの面へのスケッチ中。frameが基準平面と数値的に一致していてもここへ来る
      // (`sketchGridIsFacePlane` 参照)。貼った面と同一平面なので深度バイアスは別枠(`sketchFaceGrid`)。
      return {
        planes: [
          {
            name: 'sketch-face',
            frame: sketchGridFrame,
            uColor: null,
            vColor: null,
            bounds: sketchGridBounds,
            depthBiasLayer: DEPTH_BIAS_LAYER.sketchFaceGrid,
          },
        ],
        axis: null,
      }
    }
    // ここへ来るのはスケッチ外か `plane.kind==='base'` のスケッチ中だけ。万一frameが不一致なら 'XY' へ落とす。
    const sketchPlane = sketchGridFrame === null ? null : basePlaneForFrame(sketchGridFrame)
    const name: BasePlaneName = sketchPlane ?? 'XY'
    const axes = BASE_PLANE_AXES[name]
    // 基準平面スケッチ中だけ `sketchContentUvBox` を入れ直す(内容が変わるたびに
    // `rescale`/`setSketchFrame` 経由で通る)。通常表示は追従の対象外なので触らない。
    let bounds: UvBounds
    if (sketchGridFrame !== null) {
      sketchContentUvBox = contentUvBoxForFrame(baseFrame(name))
      sketchContentIncludeOrigin = true
      bounds = boundingSquare(sketchContentUvBox, { includeOrigin: true })
      sketchGridBounds = bounds
    } else {
      bounds = contentBoundsForPlane(baseFrame(name))
    }
    return {
      planes: [
        {
          name,
          frame: baseFrame(name),
          uColor: axes.uColor,
          vColor: axes.vColor,
          bounds,
          depthBiasLayer: DEPTH_BIAS_LAYER.planeGrid,
        },
      ],
      axis: axes.outOfPlane,
    }
  }

  function rebuildGridAxisLine(
    axis: (typeof BASE_PLANE_AXES)[BasePlaneName]['outOfPlane'] | null,
    halfLength: number,
  ): void {
    if (gridAxisLine !== null) {
      gridRoot.remove(gridAxisLine)
      gridAxisLine.geometry.dispose()
      gridAxisLine = null
    }
    gridAxisShown = axis === null ? null : axis.name
    if (axis === null) return
    gridAxisMaterial.color.setHex(axis.color)
    const geometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-axis.dir[0], -axis.dir[1], -axis.dir[2]).multiplyScalar(halfLength),
      new THREE.Vector3(axis.dir[0], axis.dir[1], axis.dir[2]).multiplyScalar(halfLength),
    ])
    const line = new THREE.Line(geometry, gridAxisMaterial)
    line.renderOrder = LAYER.gridAxisLine
    line.raycast = () => {}
    gridRoot.add(line)
    gridAxisLine = line
  }

  /** ホバー板をいまのピック範囲で作り直す。`basePlaneAt` の判定範囲と同じ `gridExtent` から作ること(板は見えているのに指せない、を防ぐ)。 */
  function rebuildBasePlaneHover(extent: number): void {
    basePlaneHoverBatch.clear()
    basePlaneHoverMeshes.clear()

    const hoverMaterial = withDepthBias(
      new THREE.MeshBasicMaterial({
        color: GRID_ARMED_COLOR,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: BASE_PLANE_HOVER_OPACITY,
        // 半透明どうしで真っ黒にならないよう奥行きは書かない。
        depthWrite: false,
      }),
      DEPTH_BIAS_LAYER.basePlaneHover,
    )
    basePlaneHoverBatch.own(hoverMaterial)
    for (const spec of BASE_PLANES) {
      const corners = ([[-1, -1], [1, -1], [1, 1], [-1, 1]] as const).map(([su, sv]) =>
        planePoint(null, spec.u, spec.v, su * extent, sv * extent),
      )
      const geometry = new THREE.BufferGeometry().setFromPoints([
        corners[0], corners[1], corners[2],
        corners[0], corners[2], corners[3],
      ])
      const mesh = new THREE.Mesh(geometry, hoverMaterial)
      mesh.renderOrder = LAYER.basePlaneHover
      mesh.visible = false
      basePlaneHoverBatch.add(mesh, geometry)
      basePlaneHoverMeshes.set(spec.name, mesh)
    }
    applyBasePlaneHover()
  }

  function applyBasePlaneHover(): void {
    for (const [name, mesh] of basePlaneHoverMeshes) mesh.visible = hoverPlane === name
  }

  function applyGridPlanes(): void {
    const wanted = desiredGridPlanes()
    const color = basePlanesOn ? GRID_ARMED_COLOR : GRID_COLOR
    planeGrids.forEach((grid, index) => {
      const spec: GridPlaneSpec | undefined = wanted.planes[index]
      if (spec === undefined) {
        grid.setVisible(false)
        return
      }
      grid.setFrame(spec.frame)
      grid.setAxisColors(spec.uColor, spec.vColor)
      grid.setColor(color)
      grid.setBounds(spec.bounds)
      grid.setDepthBias(spec.depthBiasLayer * DEPTH_BIAS_UNIT)
      grid.setVisible(true)
    })
    gridPlanesShown = wanted.planes.map((spec) => spec.name)
    gridDepthBiasLayers = wanted.planes.map((spec) => spec.depthBiasLayer)
    gridPlaneBounds = wanted.planes.map((spec) => spec.bounds)
    // 面外の軸線は格子の枠と同じ長さにする。
    rebuildGridAxisLine(wanted.axis, wanted.planes[0]?.bounds?.half ?? gridExtent)
  }

  function setSketchFrame(frame: PlaneFrame | null, face: SketchFace | null): void {
    let bounds: UvBounds | null = null
    if (frame !== null && face !== null) {
      // `followCursor` が読むキャッシュ。ここは内容が変わったときだけ通る。
      sketchContentUvBox = faceUvBoxOnly(face, frame)
      sketchContentIncludeOrigin = false
      // 測れない(null)なら bounds も null のまま(範囲で切らずに描く)。
      bounds = sketchContentUvBox === null ? null : boundingSquare(sketchContentUvBox, { includeOrigin: false })
    }
    sketchGridFrame = frame
    sketchGridBounds = bounds
    sketchGridIsFacePlane = face !== null
    applyGridPlanes()
  }

  /**
   * カーソル追従の軽量経路。`applyGridPlanes()` は呼ばず `planeGrids[0]` だけを書き換える
   * (`sketchGridFrame !== null` のとき単一平面で、`basePlanesOn` の3平面とは同時に立たない。
   * `App.svelte` の `wantsBasePlanes` が `sketchId === null` を条件に入れている)。
   *
   * `sketchContentUvBox === null` のときは何もしない。面スケッチでは「面を測れない」の契約上のnullで、
   * `boundingSquare(null, ...)` を呼ぶと `BASE_EXTENT`(100mm)の有限な既定へ縮み、無制限描画の契約が
   * pointermoveのたびに壊れる。基準平面スケッチでは「空の文書」で、`extendUvBoxToPoint(null, point)` へ
   * 進むと原点とカーソルを含む可変な正方形になり、空スケッチなのにグリッドがカーソルに追従して伸縮する。
   */
  function followCursor(point: { u: number; v: number } | null): void {
    if (sketchGridFrame === null) return
    if (sketchContentUvBox === null) return
    const merged = extendUvBoxToPoint(sketchContentUvBox, point)
    const bounds = boundingSquare(merged, { includeOrigin: sketchContentIncludeOrigin })
    sketchGridBounds = bounds
    gridPlaneBounds = [bounds]
    planeGrids[0].setBounds(bounds)
  }

  /**
   * 段ごとの不透明度とカメラ行列を毎フレーム入れ直す(ジオメトリには触らない。格子はシェーダーが作る)。
   * 不透明度は `gridScale.ts` の `tierAlpha` が決め、シェーダーはそれを上限に局所的な線間隔でさらに落とす。
   */
  let gridMmPerPixel = 0
  let gridTierAlphaValues: [number, number, number] = [0, 0, 0]
  const gridViewProjection = new THREE.Matrix4()
  const gridInverseViewProjection = new THREE.Matrix4()

  function updateUniforms(): void {
    const current = camera()
    gridMmPerPixel = worldPerPixel()
    gridTierAlphaValues = tierAlphas(gridMmPerPixel)
    current.updateMatrixWorld()
    gridViewProjection.multiplyMatrices(current.projectionMatrix, current.matrixWorldInverse)
    gridInverseViewProjection.copy(gridViewProjection).invert()
    for (const grid of planeGrids) {
      grid.setTierAlphas(gridTierAlphaValues)
      grid.setCamera(gridViewProjection, gridInverseViewProjection)
    }
  }

  /**
   * 基準平面のピック範囲の半幅を内容の大きさへ合わせる(`gridScale.ts` の `extentFor`)。格子の見た目には効かない。
   *
   * 広げるのは常に即座。縮めるのは `allowShrink` のときだけ(`frameAll` と `resetGrid`)。
   * ふつうの編集(`refreshContentDiagonal`)は `false` で、内容がしきい値付近で揺れても指せる範囲は揺れない。
   * 範囲が変わったらホバー板・面外の軸線を組み直し、`onExtentChange` を呼ぶ。
   */
  function rescale(box: THREE.Box3, allowShrink: boolean): void {
    const extent = extentFor(box)
    const changed = extent !== gridExtent && !(extent < gridExtent && !allowShrink)
    if (changed) {
      gridExtent = extent
      rebuildBasePlaneHover(gridExtent)
      onExtentChange()
    }
    // ピック範囲が動かなくても枠は入れ直す(枠は `contentBox` から直接引くので、`extentFor` の丸めに載らない)。
    applyGridPlanes()
  }

  rebuildBasePlaneHover(gridExtent)
  applyGridPlanes()

  return {
    object: gridRoot,
    apply: applyGridPlanes,
    setArmed(on) {
      basePlanesOn = on
      applyGridPlanes()
    },
    armed: () => basePlanesOn,
    setSketchFrame,
    followCursor,
    updateUniforms,
    rescale,
    extent: () => gridExtent,
    setHoverPlane(name) {
      hoverPlane = name
      applyBasePlaneHover()
    },
    hoverMesh: (name) => basePlaneHoverMeshes.get(name),
    info() {
      return {
        mmPerPixel: gridMmPerPixel,
        tiers: TIERS.map((step, index) => ({ step, alpha: gridTierAlphaValues[index] })),
        planes: [...gridPlanesShown],
        axis: gridAxisShown,
        sketchFaceExtent:
          sketchGridFrame !== null && sketchGridIsFacePlane ? (sketchGridBounds?.half ?? null) : null,
        depthBiasLayers: [...gridDepthBiasLayers],
        planeBounds: gridPlaneBounds.map((bounds) => (bounds === null ? null : { ...bounds })),
      }
    },
    dispose() {
      basePlaneHoverBatch.clear()
      for (const grid of planeGrids) grid.dispose()
      gridAxisLine?.geometry.dispose()
      gridAxisMaterial.dispose()
      gridRoot.clear()
    },
  }
}
