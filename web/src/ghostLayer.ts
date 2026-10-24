/**
 * ボディの幽霊(確定前の姿)。2種類を1つの層に持つ:
 * - サーバの投機実行(`showPreview`): 「別の形」。本物を隠して幽霊を出す(重ねると半透明の中に古い形が透けて読めない)。
 * - ローカル幽霊(`showBodyGhosts`): 「同じ形の別の場所」。届いている生メッシュに行列を掛けるだけでサーバへ投げない。
 *   本物は隠さない(隠すと移動前後を見比べられない)。
 * スケッチの幽霊はここに無い。
 *
 * 描き方(5案を並べて選んだ。docs/decisions.md「ゴーストの描き方」): ボディごとの `Group`(`LAYER.ghost`)に、
 * 奥行きだけ → 隠れた面の輪郭(加算) → 隠れた辺(破線) → 一番手前の面(半透明) → 見えている辺 の順で同じ形を重ねる。
 * 奥行きだけのパスが一番手前の層を決めるので、面は1層だけ均一に透け、それより奥は破線と輪郭でだけ見える。
 * 他のボディの奥・内側に入った部分も、同じ破線と輪郭で透けて見える。
 */
import * as THREE from 'three'
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js'
import { LineSegments2 } from 'three/examples/jsm/lines/LineSegments2.js'
import { LineSegmentsGeometry } from 'three/examples/jsm/lines/LineSegmentsGeometry.js'

import type { BodyMesh } from './bodyMesh'
import { chainSegments } from './ghostEdges'
import { withLineDepthBias } from './sketchLine'
import {
  DEPTH_BIAS_LAYER,
  LAYER,
  PREVIEW_COLOR,
  PREVIEW_DASH_PX,
  PREVIEW_EDGE_COLOR,
  PREVIEW_EDGE_WIDTH_PX,
  PREVIEW_FILL_OPACITY,
  PREVIEW_GAP_PX,
  PREVIEW_HIDDEN_EDGE_WIDTH_PX,
} from './viewportConstants'
import {
  bodyGhostMatrix,
  usableRotation,
  type BodyGeometryUpdate,
  type BodyGhost,
  type GeometryPayload,
} from './viewportTypes'

export interface GhostLayerDeps {
  /** 幽霊を入れる親(本物と同じ `root`。断面・視錐台の扱いを本物と揃える)。 */
  parent: THREE.Object3D
  /** 断面のクリップ平面(`sectionView` の配列そのもの。中身が差し替わるので複写しない)。 */
  clippingPlanes: THREE.Plane[]
  /**
   * 本物と同じ組み立て(`buildBodyMesh`)で形を作る。ずれるとプレビューと確定結果が違う形に見える。
   * 使うのはまとめたジオメトリと境界線の位置だけで、返したメッシュ自体はシーンに足さない。
   */
  buildShape(payload: GeometryPayload): BodyMesh
  /** 直近に届いた本物の生メッシュ(非表示・未読込なら undefined)。 */
  rawGeometry(bodyId: string): GeometryPayload | undefined
  /** 本物の表示を切り替える(`optimizeDebug` も使うので本編が持つ)。 */
  setBodyVisible(bodyId: string, visible: boolean): void
  pickChanged(): void
}

export interface GhostLayer {
  showPreview(entries: BodyGeometryUpdate[]): void
  showBodyGhosts(entries: BodyGhost[]): void
  /** 投機実行の幽霊とローカル幽霊を消して本物を戻す。 */
  clearPreview(): void
  /**
   * 幽霊の指示を掴んで全部消し、`rebuild()` で本物を作り直してから、掴んだ指示でローカル幽霊を組み直す。
   * `keepPreview` ならサーバ投機実行の幽霊は消さずに残し、作り直した本物を隠し直す。
   */
  rebuildAround(rebuild: () => void, keepPreview?: boolean): void
  /** 太線の解像度[CSS px](`applySize` が描画バッファの寸法と一緒に呼ぶ)。 */
  setLineResolution(width: number, height: number): void
  /** 画面1画素ぶんの世界長さ[mm](破線を画面上の長さで引くため、描く直前に呼ぶ)。 */
  setWorldPerPixel(worldPerPixel: number): void
  dispose(): void
}

interface GhostBody {
  group: THREE.Group
  dispose(): void
}

/** `color` を sRGB のまま vec3 にする(`ghostRimMaterial` は色空間の変換を通さない)。 */
function srgbVector(hex: number): THREE.Vector3 {
  return new THREE.Vector3(((hex >> 16) & 255) / 255, ((hex >> 8) & 255) / 255, (hex & 255) / 255)
}

/**
 * 隠れた面のうち、視線とほぼ平行な所(円筒の側面の縁など、辺にならない輪郭)だけを加算で光らせる。
 * 加算なので重なりの順に依らない。出力を色空間の変換に通さないのは意図的: 比較で見た強さ(sRGB の値をそのまま足す)
 * に合わせてあり、変換すると弱い光がガンマで持ち上がって広い面までぼんやり光る。
 */
function ghostRimMaterial(clippingPlanes: THREE.Plane[]): THREE.ShaderMaterial {
  return new THREE.ShaderMaterial({
    uniforms: {
      color: { value: srgbVector(PREVIEW_COLOR) },
      strength: { value: 0.85 },
      sharpness: { value: 5 },
    },
    vertexShader: /* glsl */ `
      #include <common>
      #include <clipping_planes_pars_vertex>
      varying vec3 vViewNormal;
      varying vec3 vViewOffset;
      void main() {
        #include <begin_vertex>
        #include <project_vertex>
        #include <clipping_planes_vertex>
        vViewNormal = normalize(normalMatrix * normal);
        vViewOffset = -mvPosition.xyz;
      }`,
    fragmentShader: /* glsl */ `
      #include <common>
      #include <clipping_planes_pars_fragment>
      uniform vec3 color;
      uniform float strength;
      uniform float sharpness;
      varying vec3 vViewNormal;
      varying vec3 vViewOffset;
      void main() {
        #include <clipping_planes_fragment>
        vec3 toEye = isOrthographic ? vec3(0.0, 0.0, 1.0) : normalize(vViewOffset);
        float facing = abs(dot(normalize(vViewNormal), toEye));
        gl_FragColor = vec4(color * strength * pow(1.0 - facing, sharpness), 1.0);
      }`,
    clipping: true,
    clippingPlanes,
    side: THREE.DoubleSide,
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthFunc: THREE.GreaterDepth,
    depthWrite: false,
    // 奥行きだけのパス(奥へ寄せてある)と重なった本物のボディより確実に手前へ寄せる: 同じ奥行きの面が「隠れている」と判定されて斑に光らない
    polygonOffset: true,
    polygonOffsetFactor: -1,
    polygonOffsetUnits: -2,
  })
}

function createGhostMaterials(clippingPlanes: THREE.Plane[]) {
  const resolution = new THREE.Vector2(1, 1)
  // 全パスを透明物の列に入れる(`transparent: true`)。不透明物の列に置くと、奥行きだけのパスが半透明のグリッドや
  // スケッチの領域より先に奥行きを書き、ゴーストの奥にあるそれらが透けずに消える。
  const depth = new THREE.MeshBasicMaterial({
    colorWrite: false,
    side: THREE.DoubleSide,
    transparent: true,
    clippingPlanes,
    polygonOffset: true,
    polygonOffsetFactor: 1,
    polygonOffsetUnits: 1,
  })
  // 奥行きだけのパスより1単位手前: 同じ面だけが `<=` を通り、自分の奥の層は落ちる
  const fill = new THREE.MeshLambertMaterial({
    color: PREVIEW_COLOR,
    side: THREE.DoubleSide,
    transparent: true,
    opacity: PREVIEW_FILL_OPACITY,
    depthWrite: false,
    clippingPlanes,
    polygonOffset: true,
    polygonOffsetFactor: 1,
    polygonOffsetUnits: 0,
  })
  const rim = ghostRimMaterial(clippingPlanes)
  const visibleEdge = withLineDepthBias(
    new LineMaterial({
      color: PREVIEW_EDGE_COLOR,
      linewidth: PREVIEW_EDGE_WIDTH_PX,
      resolution,
      transparent: true,
      clippingPlanes,
    }),
    DEPTH_BIAS_LAYER.ghostEdge,
  )
  const hiddenEdge = withLineDepthBias(
    new LineMaterial({
      color: PREVIEW_COLOR,
      linewidth: PREVIEW_HIDDEN_EDGE_WIDTH_PX,
      resolution,
      dashed: true,
      dashSize: PREVIEW_DASH_PX,
      gapSize: PREVIEW_GAP_PX,
      transparent: true,
      opacity: 0.85,
      clippingPlanes,
    }),
    DEPTH_BIAS_LAYER.ghostEdge,
  )
  hiddenEdge.depthFunc = THREE.GreaterDepth
  return {
    depth,
    fill,
    rim,
    visibleEdge,
    hiddenEdge,
    resolution,
    dispose() {
      for (const material of [depth, fill, rim, visibleEdge, hiddenEdge]) material.dispose()
    },
  }
}

/** 本物の境界線を破線用に並べ直したもの(`chainSegments` はボディ全体を歩くので、同じ生メッシュでは使い回す)。 */
const chainedEdges = new WeakMap<GeometryPayload, Float32Array>()

export function createGhostLayer(deps: GhostLayerDeps): GhostLayer {
  const { parent, clippingPlanes, buildShape, rawGeometry, setBodyVisible, pickChanged } = deps
  const materials = createGhostMaterials(clippingPlanes)

  /** 幽霊を1ボディぶん組んで `parent` へ足す(`matrix` はローカル幽霊の変換)。三角形が1つも無ければ null。 */
  function buildGhost(payload: GeometryPayload, matrix: THREE.Matrix4 | null): GhostBody | null {
    const shape = buildShape(payload)
    if (shape.faces.length === 0) {
      shape.dispose()
      return null
    }
    const group = new THREE.Group()
    group.renderOrder = LAYER.ghost
    if (matrix !== null) {
      group.matrixAutoUpdate = false // 位置ではなく行列を直に置くので自動再計算を切る
      group.matrix.copy(matrix)
      group.matrixWorldNeedsUpdate = true
    }
    const geometry = shape.mesh.geometry
    const pass = (object: THREE.Object3D, order: number): void => {
      object.renderOrder = order
      object.raycast = () => {} // 幽霊は指せない
      group.add(object)
    }
    pass(new THREE.Mesh(geometry, materials.depth), 0)
    pass(new THREE.Mesh(geometry, materials.rim), 1)
    let lineGeometry: LineSegmentsGeometry | null = null
    const boundary = shape.lines?.geometry.getAttribute('position')
    if (boundary !== undefined) {
      let chained = chainedEdges.get(payload)
      if (chained === undefined) {
        chained = chainSegments(boundary.array)
        chainedEdges.set(payload, chained)
      }
      lineGeometry = new LineSegmentsGeometry().setPositions(chained)
      const hidden = new LineSegments2(lineGeometry, materials.hiddenEdge)
      hidden.computeLineDistances()
      pass(hidden, 2)
    }
    pass(new THREE.Mesh(geometry, materials.fill), 3)
    if (lineGeometry !== null) pass(new LineSegments2(lineGeometry, materials.visibleEdge), 4)
    parent.add(group)
    return {
      group,
      dispose() {
        parent.remove(group)
        shape.dispose()
        lineGeometry?.dispose()
      },
    }
  }

  /** body_id -> 投機実行の幽霊。三角形の無い形(null)でもキーは残す: `clearPreview` が隠した本物を戻す一覧を兼ねる。 */
  const previewMeshes = new Map<string, GhostBody | null>()

  function showPreview(entries: BodyGeometryUpdate[]): void {
    pickChanged() // 本物のBodyを隠すので、同じ画素が別のもの(または何も)を指す
    clearPreview()
    for (const entry of entries) {
      if (!entry.geometry) continue
      setBodyVisible(entry.bodyId, false)
      previewMeshes.set(entry.bodyId, buildGhost(entry.geometry, null))
    }
  }

  /** body_id -> ローカル幽霊(変換後の姿)。`previewMeshes` とは別のMap(あちらは本物を隠す)。 */
  const bodyGhosts = new Map<string, GhostBody>()
  /**
   * いま出しているローカル幽霊の指示そのもの。**`rebuildAround` が届いた形で組み直すための
   * 入力**(消したり形を変えたりすると製品の挙動が変わる)。
   * 差し替え(`bodyGhostInfo = []`)で空にすること(`clearBodyGhosts` の契約)。
   */
  let bodyGhostInfo: BodyGhost[] = []

  function showBodyGhosts(entries: BodyGhost[]): void {
    clearBodyGhosts()
    for (const entry of entries) {
      // 使えない回転(軸の向きが長さ0・値が非有限)は幽霊ごと落とす。平行移動だけ掛けると、元の位置に重なった嘘の幽霊が出る。
      if (entry.rotate && !usableRotation(entry.rotate)) continue
      // 届いている生メッシュをそのまま使う(サーバへ投げない)。非表示・未読込のボディは黙って落ちる。
      const payload = rawGeometry(entry.bodyId)
      if (!payload) continue
      // 変換はグループに掛ける(面ごとに座標を書き換えない)。
      const ghost = buildGhost(payload, bodyGhostMatrix(entry))
      if (ghost === null) continue
      bodyGhosts.set(entry.bodyId, ghost)
      bodyGhostInfo.push({
        bodyId: entry.bodyId,
        offset: [...entry.offset],
        rotate: entry.rotate
          ? {
              point: [...entry.rotate.point] as [number, number, number],
              dir: [...entry.rotate.dir] as [number, number, number],
              angleDeg: entry.rotate.angleDeg,
            }
          : null,
      })
    }
  }

  function clearBodyGhosts(): void {
    // 必ず新しい配列へ差し替える(中身を空にしない): `rebuildAround` は消す前に参照を掴んで消した後に組み直すので、
    // 空にすると掴んだ側も空になり、型検査にも実行時エラーにも出ないまま「無関係な再配信で幽霊が戻らない」が再発する。
    bodyGhostInfo = []
    if (bodyGhosts.size === 0) return
    for (const ghost of bodyGhosts.values()) ghost.dispose()
    bodyGhosts.clear()
  }

  function clearPreview(): void {
    // ローカル幽霊も一緒に消す(`update`/`prune`/`App.svelte` の中断が何も足さずに正しく消える)。
    clearBodyGhosts()
    pickChanged() // 隠していた本物が戻る(`showPreview` の対)
    if (previewMeshes.size === 0) return
    for (const [bodyId, ghost] of previewMeshes) {
      ghost?.dispose()
      setBodyVisible(bodyId, true)
    }
    previewMeshes.clear()
  }

  function rebuildAround(rebuild: () => void, keepPreview = false): void {
    // ローカル幽霊の指示だけ控える(`clearPreview()` は `bodyGhostInfo` を新しい配列へ差し替えるので巻き添えにならない)。
    const ghosts = bodyGhostInfo
    if (keepPreview && previewMeshes.size > 0) {
      // 形の変わらない再配信(粗さだけ)ではサーバの幽霊を残し、作り直した本物を隠し直す(`showPreview` と同じ見え方)。
      clearBodyGhosts()
      rebuild()
      for (const bodyId of previewMeshes.keys()) setBodyVisible(bodyId, false)
      pickChanged()
    } else {
      clearPreview()
      rebuild()
    }
    // ローカル幽霊は消して終わりにせず、届いたばかりの形で組み直す。消すだけだと形の変わらない再配信でも落ちたまま戻らない:
    // カメラを動かすと可変解像度の `focus` が変わり、幽霊が乗っているボディ自身が測り直されて届く
    // (2026-09-06実測: 右ドラッグ1回でそのボディの版が 2→3)。呼び直す側の `CommandHost` は引数が変わっていないと見て再発火しない(`previewedKey`)。
    if (ghosts.length > 0) showBodyGhosts(ghosts)
  }

  return {
    showPreview,
    showBodyGhosts,
    clearPreview,
    rebuildAround,
    setLineResolution(width, height) {
      materials.resolution.set(width, height)
    },
    setWorldPerPixel(worldPerPixel) {
      // 線の通し距離[mm]を画素へ直す(`dashSize`/`gapSize` は画素で持つ)
      materials.hiddenEdge.dashScale = 1 / worldPerPixel
    },
    dispose() {
      // 本物の表示とピックは触らない(viewport ごと捨てる途中なので戻す先が無い)
      for (const ghost of [...bodyGhosts.values(), ...previewMeshes.values()]) ghost?.dispose()
      bodyGhosts.clear()
      previewMeshes.clear()
      bodyGhostInfo = []
      materials.dispose()
    },
  }
}
