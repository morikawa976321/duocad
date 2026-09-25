/**
 * 平面グリッド(3段固定・無限)と、基準平面のピック範囲・ホバー板・面外の軸線。
 *
 * **「ある平面frame(原点・u軸・v軸)を受け取って描くもの」**(`planeGrid.ts`)を最大3枚
 * 使い回す。XY床はその一利用者にすぎず、どの平面を出すかは状態が決める(`desiredGridPlanes`)。
 * ピック対象にはしない(判定は本編 `basePlaneAt` の解析交差が別に持ち、範囲の半幅を
 * `extent()` で読む)。**ホバー板と判定範囲は同じ `gridExtent` から作る** —— どちらも
 * `extent()` を通して読むことで、「板は見えているのに指せない(逆も)」を防ぐ。
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
   * **最初の `apply()` の中で読む**ので、本編は `root` / `sketchRoot` を作った後で
   * `createGridPlanes` を呼ぶこと(初期化の順序。docs/WEB_PITFALLS.md の TDZ)。
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
  /**
   * グリッドを入れるグループ。`scene` へ足すのは呼び出し側。**`root` には入れない** ——
   * 入れると初回の `frameToContent` がグリッドの箱に引っ張られる。
   */
  object: THREE.Group
  /** 出す平面・面内の軸・色・面外の軸線を、いまの状態から入れ直す。 */
  apply(): void
  /** 基準3平面を指せるか(本編の `showBasePlanes`)。出す平面(1枚 ⇄ 3枚)と色が変わる。 */
  setArmed(on: boolean): void
  armed(): boolean
  /**
   * スケッチ平面のframeを差し替える(モード外は null)。表示する平面がこれで決まる。
   * `face` は `plane.kind==='face'` のときだけ渡す(基準平面か任意面かの判定そのもの)。
   */
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
  // Group の `renderOrder` は three.js では groupOrder として子全体に効く。中身どうしの
  // 前後は各オブジェクトの `LAYER.*` が決める。
  gridRoot.renderOrder = -1

  /**
   * 基準平面のピック範囲の半幅[mm]。**描画範囲ではない**(格子は無限) ——
   * 本編の `basePlaneAt` のピック範囲・`updateClipPlanes` のnear/far下限・面外の軸線の長さが
   * ここを見る。`rescale` だけが動かす。
   */
  let gridExtent = BASE_EXTENT

  /** 基準3平面を指せるか(`setArmed`)。**出していない間は当たり判定を回さない**。 */
  let basePlanesOn = false

  /**
   * スケッチ中のスケッチ平面のframe(モード外は null)。**書き手は `setSketchFrame`
   * だけ**で、本編の `setActiveSketch` と `showSketches` の両方から呼ぶ —— モードに入った時点では
   * まだ描画指示が届いておらず、frameを知らないことがある。
   */
  let sketchGridFrame: PlaneFrame | null = null

  /**
   * `sketchGridFrame` が非nullの間、実際にグリッドへ渡している描く正方形(カーソル追従で
   * 広がった後の値も含む)。面スケッチで `faceUvBoxOnly` が測れなかったときは `null`
   * (無制限描画。`followCursor` のnullガード参照)。**書き手は
   * `setSketchFrame` / `desiredGridPlanes`(基準平面スケッチ側)/
   * `followCursor`(カーソル追従の軽量経路)の3箇所**
   * (`sketchGridFrame` と対で更新する)。
   */
  let sketchGridBounds: UvBounds | null = null

  /**
   * `sketchGridFrame` が非nullの間(=スケッチで実際に描画中)の「内容だけ」のUV矩形
   * (カーソル合成前・`boundingSquare` の余白/丸め前)。**カーソル追従の軽量経路
   * (`followCursor`)専用のキャッシュ** —— pointermoveのたびに
   * `contentBox`/面メッシュの頂点走査をやり直さないための分離で、書き手は
   * `desiredGridPlanes`(基準平面スケッチ側)と `setSketchFrame`(面スケッチ側)の
   * 「内容が変わったときだけ」通る箇所だけ(タスク仕様のパフォーマンス上の注意)。
   */
  let sketchContentUvBox: UvBox | null = null
  /** `sketchContentUvBox` と対で持つ、`boundingSquare` へ渡す `includeOrigin`。 */
  let sketchContentIncludeOrigin = false

  /**
   * いまのスケッチが**実ジオメトリの面(`plane.kind === 'face'`)へ貼られているか**。
   * **`basePlaneForFrame(sketchGridFrame)` のframe一致だけでは判定できない** ——
   * 「XY床にスケッチ→押し出し→その底面に再度スケッチ」のように**実ジオメトリの面でも
   * frame が数値的に基準平面と一致する**ことがあり(原点(0,0,0)・u/vが基準平面と同じ)、
   * frame一致だけで判定すると任意面のグリッド(深度バイアス層・extentの有限化・直交軸なし)が
   * 不発になる。**その面は実ソリッドのメッシュと完全同一平面**なので、本来はz-fighting対策が
   * 最も要る場面である。判定は `plane.kind` を正とし、`basePlaneForFrame` は
   * `plane.kind==='base'` のときにXY/XZ/YZのどれかを選ぶ用途にしか使わない。
   */
  let sketchGridIsFacePlane = false

  /** 実機検査フック(`gridTiers`)へ出す、いま表示している平面の名前。 */
  let gridPlanesShown: string[] = []
  /**
   * `gridPlanesShown` と対で持つ、各グリッドに適用中の深度バイアス層番号
   * (`DEPTH_BIAS_LAYER`)。**実機検査フック専用** —— 任意面のグリッドが
   * 本当に `sketchFaceGrid` 層(基準平面の `planeGrid` 層とは別)を使っているかは、
   * z-fightingの有無を静止画のスクリーンショットだけでは機械的に見分けられないため、
   * 数値そのものを検査できるようにする。
   */
  let gridDepthBiasLayers: number[] = []

  /** `gridPlanesShown` と対で持つ、各グリッドに適用中の描画範囲(実機検査フック `gridTiers`)。 */
  let gridPlaneBounds: Array<UvBounds | null> = []

  /**
   * 同時に出しうる平面は最大3枚(対象待ちの XY/XZ/YZ)。**作り直さず使い回す** ——
   * frameを差し替えるだけで別の平面になる。
   */
  const planeGrids: PlaneGrid[] = [createPlaneGrid(), createPlaneGrid(), createPlaneGrid()]
  for (const grid of planeGrids) {
    grid.object.renderOrder = LAYER.planeGrid
    // 深度バイアスは `applyGridPlanes()`(直後に必ず呼ぶ)が状態ごとに入れ直す
    // (`GridPlaneSpec.depthBiasLayer`) —— ここでは初期化しない。
    grid.setVisible(false)
    gridRoot.add(grid.object)
  }

  /** 面外の軸(1本)。面内の軸はグリッドのシェーダーが描くので、線にするのはこれだけ。 */
  const gridAxisMaterial = withDepthBias(
    new THREE.LineBasicMaterial({
      color: AXIS_Z_COLOR,
      // 不透明だが**透明パスへ入れる** —— 実体より後に描かないと、共平面の面に隠れる。
      transparent: true,
      depthWrite: false,
    }),
    DEPTH_BIAS_LAYER.gridAxisLine,
  )
  let gridAxisLine: THREE.Line | null = null
  let gridAxisShown: 'X' | 'Y' | 'Z' | null = null

  /**
   * ホバー中の基準平面に重ねる板(**同時に見えるのは多くても1枚**)。ピック範囲が変わる
   * たびに作り直す(`rebuildBasePlaneHover`)。
   *
   * 平面ごとの色分けを線でやる代わりに半透明の板を重ねて「今カーソルが指しているのは
   * この平面」を出す。**当たり判定には使わない** —— 判定は
   * 今までどおり本編 `basePlaneAt` の解析交差である。
   */
  const basePlaneHoverMeshes = new Map<BasePlaneName, THREE.Mesh>()
  /** いま板を見せている平面(本編の `hoverPlane` の写し。`setHoverPlane` だけが書く)。 */
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
   *
   * **これは「基準平面か任意面か」の判定には使わない** ——
   * 実ジオメトリの面(`plane.kind==='face'`)でも、frameが基準平面と数値的に一致することが
   * ある(「XY床にスケッチ→押し出し→その底面に再度スケッチ」が典型例。原点(0,0,0)・u/vが
   * 基準平面と同じになる)。その判定は `sketchGridIsFacePlane`(`plane.kind` を正とする)が
   * 持つ。ここが答えるのは「`plane.kind==='base'` のとき、XY/XZ/YZのどれを表示するか」だけ。
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
   * **すべてのスケッチとソリッドの外接直方体**を、その平面の u/v へ射影して求める。
   *
   * 射影するのは**箱の8隅**である。箱は軸並行なので、どの向きのframeでも8隅の射影の
   * 最小・最大が射影像の外接矩形になる(面の頂点を全部回す `faceUvBoxOnly` と違い、内容が
   * 何万頂点あっても8点で済む)。
   *
   * `boundingSquare` を呼ばず生の矩形を返すのは、カーソル追従(`extendUvBoxToPoint`)が
   * 余白・丸めより**前**に合成する必要があるため(`contentBoundsForPlane` 参照)。
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

  /**
   * 基準平面のグリッドを描く正方形。`contentUvBoxForFrame` を丸ごと包むだけの薄い覆い
   * (カーソル追従が要らない呼び出し側 —— 3平面対象待ち・スケッチ外の通常表示 —— 専用)。
   *
   * `includeOrigin: true` —— モデルが原点から遠いとき、原点が枠の外へ出ると
   * 「原点がどこか読める」というグリッド本来の役割が失われる。
   */
  function contentBoundsForPlane(frame: PlaneFrame): UvBounds {
    return boundingSquare(contentUvBoxForFrame(frame), { includeOrigin: true })
  }

  /**
   * 貼った面の「内容だけ」のUV矩形(`boundingSquare` の余白/丸め前、`setBounds` へ渡す枠の元)を、
   * スケッチのframe(u,v)で測る。
   *
   * **基準平面の `extentFor` を使い回さない** —— あれの `BASE_EXTENT`(100mm)下限は
   * 「空の文書でもこれより狭くしない」という文書スケールの前提で、実測で40×20mmの面に
   * 対して100mmを返し、面の何倍もの大きさでグリッドが描かれて背景が線で埋まる破綻を
   * 起こした(スクリーンショットで発見)。
   *
   * **原点対称の半幅ではなく実測の min/max を返す** —— 面のframeの原点は
   * 面の中心ではなく**面の隅(OCCTのUVパラメータ化の原点)のことが多く**、対称な枠だと
   * 枠が面の片側へ大きく偏る(実測: u:0-40 / v:-20-0 の面に ±50mm の枠が出た)。
   *
   * 見つからなければ `null`(=範囲で切らずに描く。面が消えた・非表示等で測れないときに
   * 装飾が消えるより、広めに描いて残る方が安全)。**面の実際の輪郭では切らない**
   * (トリム境界のマスクはステンシルが要り費用に見合わない)。
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
   * いま出す平面と、面外に線で描く軸(完了条件4の表そのもの)。
   *
   * | 状態 | 表示 |
   * |---|---|
   * | 通常(スケッチ外) | XY平面 + Z軸 |
   * | スケッチ起動中(対象待ち・`setArmed(true)`) | 3平面すべて |
   * | 基準平面へのスケッチ中 | その平面 + それに直交する軸 |
   * | 任意面へのスケッチ中 | その面のframeのグリッドのみ(直交軸なし。本人指定) |
   *
   * 対象待ちを3平面のままにするのは、基準平面も**クリックして指す**から。
   * ここでXZ/YZを隠すと
   * 指せなくなる。3枚のときは3本の軸が全部どれかの面内に出るので、面外の軸は要らない。
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
      // 実ジオメトリの面(`plane.kind==='face'`)へのスケッチ中。その面のframeへそのまま
      // 乗せる —— 原点・u/v軸はスケッチ平面のもの(完了条件4)。直交軸は出さない
      // (完了条件3・本人指定)。**frameが基準平面と数値的に一致していてもここへ来る**
      // (`sketchGridIsFacePlane` が `plane.kind` を正として判定するため —— frame一致だけでは
      // 判別できない事情は `sketchGridIsFacePlane` のdocstring参照)。貼り付けた面
      // (A群のソリッド)と完全同一平面になるので、バイアスの層番号は別枠(`sketchFaceGrid`)を使う
      // (B群・タスク指示)。
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
    // ここへ来るのは「スケッチ外(通常表示)」か「`plane.kind==='base'` のスケッチ中」だけ。
    // 後者の平面名は frame一致で解決する(基準平面のframeは常に3つのどれかと一致するので
    // `sketchPlane` は通常 null にならないが、万一の不一致は安全側の既定 'XY' へ落とす)。
    const sketchPlane = sketchGridFrame === null ? null : basePlaneForFrame(sketchGridFrame)
    const name: BasePlaneName = sketchPlane ?? 'XY'
    const axes = BASE_PLANE_AXES[name]
    // **基準平面スケッチ中(`sketchGridFrame !== null`)だけ、カーソル追従の
    // キャッシュ(`sketchContentUvBox`)を入れ直す** —— このブランチは内容が変わるたびに
    // 呼ばれる(`rescale`/`setSketchFrame` 経由)ので、ここが
    // 「内容が変わったときだけ計算する」場所そのもの(タスク仕様のパフォーマンス上の注意)。
    // 通常表示(`sketchGridFrame === null`)は追従の対象外(完了条件4)なのでキャッシュに触らない。
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

  /**
   * ホバー板をいまのピック範囲で作り直す。**本編 `basePlaneAt` の判定範囲と同じ `gridExtent`
   * から作る** —— ここを取りこぼすと「板は見えているのに指せない(逆も)」という原因の
   * 遠い壊れ方をする。
   */
  function rebuildBasePlaneHover(extent: number): void {
    basePlaneHoverBatch.clear()
    basePlaneHoverMeshes.clear()

    const hoverMaterial = withDepthBias(
      new THREE.MeshBasicMaterial({
        color: GRID_ARMED_COLOR,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: BASE_PLANE_HOVER_OPACITY,
        // 半透明どうしで真っ黒にならないように奥行きは書かない(幽霊の材質と同じ)。
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
    // 組み直した新しいメッシュへ、いまのホバー状態を反映し直す
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
      // **常に内容を一回り大きく包む正方形で切る**。
      // 基準平面は「すべてのスケッチとソリッド ∪ 原点」、任意面はその面自身の範囲が正。
      grid.setBounds(spec.bounds)
      // 任意面は貼り付けた面と同一平面になるので、バイアスの層番号を別枠にする
      // (`desiredGridPlanes` が選ぶ)。他の状態は従来どおり `planeGrid` のまま。
      grid.setDepthBias(spec.depthBiasLayer * DEPTH_BIAS_UNIT)
      grid.setVisible(true)
    })
    gridPlanesShown = wanted.planes.map((spec) => spec.name)
    gridDepthBiasLayers = wanted.planes.map((spec) => spec.depthBiasLayer)
    gridPlaneBounds = wanted.planes.map((spec) => spec.bounds)
    // 面外の軸線は**格子の枠と同じ長さ**にする(枠より長いと1本だけ飛び出して見える)。
    rebuildGridAxisLine(wanted.axis, wanted.planes[0]?.bounds?.half ?? gridExtent)
  }

  function setSketchFrame(frame: PlaneFrame | null, face: SketchFace | null): void {
    let bounds: UvBounds | null = null
    if (frame !== null && face !== null) {
      // 面スケッチの「内容だけ」のUV矩形をここでキャッシュする(カーソル追従の軽量経路
      // `followCursor` が読む)。ここは本編の `setActiveSketch`/`showSketches` からしか
      // 呼ばれない = 「内容が変わったときだけ」通る箇所(タスク仕様のパフォーマンス上の注意)。
      sketchContentUvBox = faceUvBoxOnly(face, frame)
      sketchContentIncludeOrigin = false
      // `faceUvBoxOnly` が null(測れない)なら bounds も null のまま
      // (範囲で切らずに描く。`faceUvBoxOnly` のdocstring参照)。
      bounds = sketchContentUvBox === null ? null : boundingSquare(sketchContentUvBox, { includeOrigin: false })
    }
    sketchGridFrame = frame
    sketchGridBounds = bounds
    sketchGridIsFacePlane = face !== null
    applyGridPlanes()
  }

  /**
   * カーソル追従の軽量経路(`sketchContentUvBox` のdocstring参照)。`applyGridPlanes()` は
   * 呼ばない —— `sketchGridFrame !== null` のとき `desiredGridPlanes()` は常に単一平面
   * (`planes.length===1`)を返すので、`planeGrids[0]` だけを直接書き換えれば足りる
   * (`basePlanesOn` の3平面とは同時に立たない。`App.svelte` の `wantsBasePlanes` が
   * `sketchId === null` を条件に入れている)。
   *
   * **`sketchContentUvBox === null` のときは、面スケッチか基準平面スケッチかを問わず
   * 何もしない**。面スケッチでは `faceUvBoxOnly` が「面を測れない」を表す契約上のnullで、
   * `desiredGridPlanes` はこれを `setBounds(null)`(無制限描画。`planeGrid.ts` の
   * `uBoundsHalf<=0`)として渡している。ここで `boundingSquare(null, ...)` を呼ぶと
   * `BASE_EXTENT`(100mm)の**有限**な既定へ縮んでしまい、「測れないときは無制限に描いて
   * 安全側へ倒す」という契約をpointermoveのたびに壊す。基準平面スケッチでは
   * `sketchContentUvBox===null` は「空の文書」を表し、`desiredGridPlanes` が
   * `boundingSquare(null, { includeOrigin: true })` で既に `BASE_EXTENT` の固定正方形を
   * `sketchGridBounds` に入れている——**ここで早期returnせず `extendUvBoxToPoint(null, point)`
   * へ進むと、カーソル1点だけの矩形が作られ、原点とカーソルの両方を含む可変な正方形に
   * 化けてしまう**(空スケッチなのにグリッドがカーソル位置へ追従して伸縮する見た目)。
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
   * 段ごとの不透明度とカメラ行列を毎フレーム入れ直す。**CPUの仕事はこれだけ**で、
   * ジオメトリには一切触らない(格子はシェーダーが作る)。
   *
   * 不透明度は `gridScale.ts` の `tierAlpha`(＝Nodeから検査できる純関数)が決める。
   * シェーダーはそれを上限に、1画素ごとの局所的な線間隔でさらに落とす。
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
   * **基準平面のピック範囲**の半幅を内容の大きさへ合わせる(`gridScale.ts` の `extentFor`)。
   * **格子の見た目には効かない** —— 刻みは3段固定で、格子はシェーダーが無限に描く。
   *
   * **広げるのは常に即座**(`allowShrink` に関係なく)。**縮めるのは `allowShrink` が
   * 立っているときだけ** —— 本編の `frameToContent` が実際に合わせたとき(＝`frameAll`)と、
   * 新規文書にしたとき(`resetGrid`)に限る。ふつうの編集(`refreshContentDiagonal` からの
   * 呼び出し)は常に `allowShrink: false` で、内容がしきい値付近で揺れても指せる範囲が
   * 前後に揺れることはない。
   *
   * 範囲が変わったら、ホバー板(`rebuildBasePlaneHover`)と面外の軸線の長さ
   * (`applyGridPlanes`)を組み直し、near/farの下限(`onExtentChange`)も引き直させる。
   */
  function rescale(box: THREE.Box3, allowShrink: boolean): void {
    const extent = extentFor(box)
    const changed = extent !== gridExtent && !(extent < gridExtent && !allowShrink)
    if (changed) {
      gridExtent = extent
      rebuildBasePlaneHover(gridExtent)
      onExtentChange()
    }
    // **ピック範囲が動かなくても描画の枠は入れ直す** —— 枠は内容そのもの(`contentBox`)
    // から引くので、`extentFor` の1-2-5の丸めやヒステリシスに載らない変化でも動く。
    applyGridPlanes()
  }

  rebuildBasePlaneHover(gridExtent)
  applyGridPlanes()

  return {
    object: gridRoot,
    apply: applyGridPlanes,
    setArmed(on) {
      basePlanesOn = on
      applyGridPlanes() // 出す平面(1枚 ⇄ 3枚)と色(通常 ⇄ 起動中)がこの旗で変わる
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
        // `sketchGridIsFacePlane` が判定の正(`plane.kind`ベース。frame一致だけでは見分けが
        // つかない場合がある —— その宣言のdocstring参照)。
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
