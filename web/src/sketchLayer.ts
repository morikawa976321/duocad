import * as THREE from 'three'

import { type ViewResolution } from './adaptiveResolution'
import { entityHandles, entityHasIndependentPoints, entityPoints, sketchIntersections } from './entityKinds'
import { planePoint } from './planeMath'
import { createBatch, type SceneBatch } from './sceneBatch'
import { buildSketchLine, createSketchLineMaterial, withLineDepthBias, type SketchFatLine } from './sketchLine'
import { variantCache } from './viewportMaterials'
import type { SketchDraw, SketchEntityHit } from './viewportTypes'
import {
  DEPTH_BIAS_LAYER, HOVER_LIGHTEN, LAYER, REGION_COLOR, REGION_SELECTED_COLOR, SKETCH_COLOR,
  SKETCH_GHOST_COLOR, SKETCH_GHOST_WARN_COLOR, SKETCH_HANDLE_COLOR, SKETCH_HANDLE_PIXELS,
  SKETCH_SELECTED_COLOR, lighten, withDepthBias,
} from './viewportConstants'

interface OwnedSketchScene {
  /** 作ったときの鍵(`sketchBuildKey`)。同じ鍵の指示が来たら作り直さず、選択の印だけ付け直す。 */
  builtKey: string
  frame: SketchDraw['frame']
  plane: SketchDraw['plane']
  /** シーンに足したもの(除去と破棄のため。ピック用の配列は別に持つ)。 */
  batch: SceneBatch
  regionMeshes: THREE.Mesh[]
  /** 図形の (u,v) の折れ線と点の図形の位置(視点合わせの中心を出すのに使う)。 */
  points: Array<[number, number]>
  /** 図形1本ごとの折れ線の点数(実機検査の `sketchPolylineCounts` が読む)。 */
  polylineCounts: number[]
  /** 編集中のスケッチとして作ったか(`SketchDraw.editing`)。 */
  editing: boolean
  /**
   * 点のマーカー(編集中のスケッチだけ。1点1オブジェクト)。当たった点からコアの参照へ戻すため `userData` に `entityId` と
   * `point`(xキー)を持つ(まとめて1つの `Points` にすると戻れない)。
   */
  handleMeshes: THREE.Points[]
  /**
   * 編集していないスケッチの点(ピックの候補だけ。オブジェクトは作らない: 取り込んだ SVG はベジエの制御点が数千あり、
   * 全スケッチに1点1オブジェクトを置くと毎フレームの走査が重い)。描くのは選択中(`markSelected`)とホバー中(`markHovered`)だけ。
   */
  pickPoints: Array<{ point: THREE.Vector3; entityId: string; key: string }>
  /** 選択中の点(`entityId|xキー`)。 */
  selectedPointKeys: Set<string>
  markSelected: THREE.Points | null
  markHovered: THREE.Points | null
  /** 図形の折れ線(文書の図形だけ。`userData.entityId` を持つ)。辺のピック対象。 */
  handleLines: SketchFatLine[]
  /** 文書の図形(`handles`)。スナップ座標の解決に使う。 */
  handles: SketchDraw['handles']
  /**
   * 直線区間(line/rectangleの辺)どうしの交点(`sketchIntersections`。曲線は対象外)。
   * `sketchCornerAt` が本物の点・`visualCorners` と同じ優先度で拾うために持つ。
   */
  intersections: ReturnType<typeof sketchIntersections>
  /** 線・点・領域の外接球(世界座標。画面でカーソルから遠いスケッチを歩かない)。 */
  sphere: THREE.Sphere
}

/** ピック・視点合わせ・検査フックが借りる参照。資源の差し替えと破棄はレイヤーだけが行う。 */
export type SketchScene = Readonly<Omit<OwnedSketchScene, 'batch' | 'builtKey' | 'selectedPointKeys'>>
type RegionHover = { sketchId: string; index: number } | null

export interface SketchLayer {
  readonly object: THREE.Group
  get(sketchId: string): SketchScene | undefined
  entries(): IterableIterator<[string, SketchScene]>
  values(): IterableIterator<SketchScene>
  /** 削除・構築があったら true。同じ版の選択変更では false。 */
  update(entries: SketchDraw[], resolution: ViewResolution): boolean
  redraw(resolution: ViewResolution): boolean
  setLineResolution(width: number, height: number): void
  applyRegionMaterials(hover: RegionHover): void
  applySketchHoverMaterials(hover: SketchEntityHit | null): void
  showGhosts(sketchId: string, entities: SketchDraw['entities'], resolution: ViewResolution, tone?: 'normal' | 'warn'): void
  clearGhost(): void
  rebuilds(): number
  dispose(): void
}

export function createSketchLayer(sketchRoot = new THREE.Group()): SketchLayer {
  const sketches = new Map<string, OwnedSketchScene>()
  let ghost: SceneBatch | null = null
  let ghostSketchId: string | null = null
  const sketchLineResolution = new THREE.Vector2(1, 1)
  const sketchMaterial = createSketchLineMaterial(SKETCH_COLOR, sketchLineResolution)
  const sketchGhostMaterial = createSketchLineMaterial(SKETCH_GHOST_COLOR, sketchLineResolution)
  const sketchGhostWarnMaterial = createSketchLineMaterial(SKETCH_GHOST_WARN_COLOR, sketchLineResolution)
  // 点の図形の幽霊(折れ線を持たないので取っ手と同じ大きさの印で描く)
  const ghostPointMaterialOf = (color: number) =>
    new THREE.PointsMaterial({ color, size: SKETCH_HANDLE_PIXELS * 2, sizeAttenuation: false, depthTest: false })
  const sketchGhostPointMaterial = ghostPointMaterialOf(SKETCH_GHOST_COLOR)
  const sketchGhostWarnPointMaterial = ghostPointMaterialOf(SKETCH_GHOST_WARN_COLOR)

  /**
   * スケッチの線・点の材質は (選択状態 × ホバー × 描き方) のキャッシュから取る(`faceMaterialFor` と同じ形)。
   * 描き方: 編集中のスケッチ(作図のため)と選択中の図形(AI が選んだ裏のものも人に見える)は立体に透かす(`'xray'`)。
   * それ以外は立体の裏に回ると隠れる(`'depth'`。docs/DESIGN_DECISIONS.md「見えるものはすべてさわれる」。
   * 指せるかの判定 `occlusion.ts` と揃う)。
   */
  type SketchMaterialState = 'plain' | 'selected'
  type SketchDepth = 'xray' | 'depth'
  type SketchLineMaterial = ReturnType<typeof createSketchLineMaterial>
  const sketchStateKey = (state: SketchMaterialState, hovered: boolean): string => `${state}:${hovered ? 1 : 0}`
  // `plain` の非ホバーの透かしは既存の `sketchMaterial` を種にする(インスタンスを分裂させない)。
  const sketchLineMaterials = variantCache(
    (state: SketchMaterialState, hovered: boolean, depth: SketchDepth) => `${sketchStateKey(state, hovered)}:${depth}`,
    (state, hovered, depth) => {
      const base = state === 'selected' ? SKETCH_SELECTED_COLOR : SKETCH_COLOR
      const material = createSketchLineMaterial(hovered ? lighten(base, HOVER_LIGHTEN) : base, sketchLineResolution)
      return depth === 'depth' ? withLineDepthBias(material, DEPTH_BIAS_LAYER.sketch) : material
    },
    [['plain:0:xray', sketchMaterial]],
  )
  function sketchLineMaterialFor(state: SketchMaterialState, hovered: boolean, depth: SketchDepth = 'xray'): SketchLineMaterial {
    return sketchLineMaterials.get(state, hovered, depth)
  }

  // 深度を書かない(頂点マーカーと同じ。線の上に必ず出す)。
  const sketchHandleMaterials = variantCache(sketchStateKey, (state, hovered) => {
    const base = state === 'selected' ? SKETCH_SELECTED_COLOR : SKETCH_HANDLE_COLOR
    return new THREE.PointsMaterial({
      color: hovered ? lighten(base, HOVER_LIGHTEN) : base,
      size: SKETCH_HANDLE_PIXELS * 2,
      sizeAttenuation: false,
      depthTest: false,
    })
  })
  function sketchHandleMaterialFor(state: SketchMaterialState, hovered: boolean): THREE.PointsMaterial {
    return sketchHandleMaterials.get(state, hovered)
  }
  // 編集していないスケッチの点の図形: 取っ手と同じ見た目で、立体の裏に回ると隠れる(線と同じ深度バイアス)
  const sketchPointEntityMaterial = withDepthBias(
    new THREE.PointsMaterial({
      color: SKETCH_HANDLE_COLOR,
      size: SKETCH_HANDLE_PIXELS * 2,
      sizeAttenuation: false,
      depthWrite: false,
    }),
    DEPTH_BIAS_LAYER.sketch,
  )
  /**
   * 領域の材質は (選択 × ホバー) のキャッシュから取る。不透明度は選択で変わる(非選択 0.35 / 選択 0.55)。
   * 選択中は立体に透かし、それ以外は立体の裏に回ると隠れる(線と同じ。押し出した元の領域は押し出した面と
   * 厳密に同一平面なので、深度バイアスで手前へ寄せる)。
   */
  const regionMaterials = variantCache(
    (selected: boolean, hovered: boolean) => `${selected ? 1 : 0}:${hovered ? 1 : 0}`,
    (selected, hovered) => {
      const base = selected ? REGION_SELECTED_COLOR : REGION_COLOR
      const material = new THREE.MeshBasicMaterial({
        color: hovered ? lighten(base, HOVER_LIGHTEN) : base,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: selected ? 0.55 : 0.35,
        depthTest: !selected,
        depthWrite: false,
      })
      return selected ? material : withDepthBias(material, DEPTH_BIAS_LAYER.sketch)
    },
  )

  /** スケッチ平面上の (u,v) を3Dの点へ(コアの `kernel_sketch._to_pnt` と同じ規則)。 */
  function toWorld(frame: SketchDraw['frame'], u: number, v: number): THREE.Vector3 {
    return planePoint(frame.origin, frame.u, frame.v, u, v)
  }

  function buildEntityLine(
    frame: SketchDraw['frame'],
    entity: { kind: string; params: Record<string, number> },
    material: SketchLineMaterial,
    resolution: ViewResolution,
  ): { object: SketchFatLine; geometry: THREE.BufferGeometry; points: Array<[number, number]> } | null {
    // 今の画面の粗さで折る(`resolutionTracker.applied()`)
    const points = entityPoints(entity.kind, entity.params, resolution)
    if (points.length < 2) return null
    const worldPoints = points.map(([u, v]) => toWorld(frame, u, v))
    const built = buildSketchLine(worldPoints, material)
    if (!built) return null
    built.object.userData.polylineWorld = worldPoints
    return { object: built.object, geometry: built.geometry, points }
  }

  function clearSketch(sketchId: string): void {
    const existing = sketches.get(sketchId)
    if (!existing) return
    existing.batch.clear()
    existing.markSelected = replaceMark(existing, existing.markSelected, new Set(), sketchHandleMaterialFor('selected', false))
    existing.markHovered = replaceMark(existing, existing.markHovered, new Set(), sketchHandleMaterialFor('plain', true))
    sketches.delete(sketchId)
  }

  /** 直近に受け取った描画指示。解像度が変わったとき折り直すために控える。 */
  let lastSketchDraws: SketchDraw[] = []

  /**
   * スケッチを作り直すかの鍵: コアの版(描くものが変わると上がる)と、編集中か(描き方と点のマーカーが変わる)。
   * 選択はここに入れない(作り直さずに `restyleSketch` で塗り直す)。
   */
  function sketchBuildKey(entry: SketchDraw): string {
    return `${entry.revision}|${entry.editing ? 'edit' : ''}`
  }

  /** 作り直さずに選択の印(`userData.selected` と点の見え方)だけを付け直す。材質は呼び手が塗る。 */
  function restyleSketch(scene: OwnedSketchScene, entry: SketchDraw): void {
    const selected = new Set(entry.selectedRegions)
    for (const mesh of scene.regionMeshes) mesh.userData.selected = selected.has(mesh.userData.regionIndex as number)
    const pickedWhole = new Set(entry.selectedHandles.filter((item) => item.point === null).map((item) => item.entityId))
    const pickedPoints = new Set(
      entry.selectedHandles.filter((item) => item.point !== null).map((item) => `${item.entityId}|${item.point}`),
    )
    for (const line of scene.handleLines) line.userData.selected = pickedWhole.has(line.userData.entityId as string)
    for (const mesh of scene.handleMeshes) {
      const entityId = mesh.userData.entityId as string
      mesh.userData.selected = pickedWhole.has(entityId) || pickedPoints.has(`${entityId}|${mesh.userData.point as string}`)
    }
    scene.selectedPointKeys = pickedPoints
    if (!scene.editing) {
      scene.markSelected = replaceMark(scene, scene.markSelected, pickedPoints, sketchHandleMaterialFor('selected', false))
    }
  }

  /**
   * 編集していないスケッチの点の印を差し替える(`keys` は `entityId|xキー`)。空なら消す。印は立体に透かす
   * (選択中の点は裏でも見え、ホバー中の点は見えている点にしか乗らない)。
   */
  function replaceMark(
    scene: OwnedSketchScene,
    previous: THREE.Points | null,
    keys: ReadonlySet<string>,
    material: THREE.PointsMaterial,
  ): THREE.Points | null {
    if (previous !== null) {
      sketchRoot.remove(previous)
      previous.geometry.dispose()
    }
    const positions: number[] = []
    for (const item of scene.pickPoints) {
      if (keys.has(`${item.entityId}|${item.key}`)) positions.push(item.point.x, item.point.y, item.point.z)
    }
    if (positions.length === 0) return null
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(positions), 3))
    geometry.computeBoundingSphere()
    const mark = new THREE.Points(geometry, material)
    mark.renderOrder = LAYER.sketchPoint
    sketchRoot.add(mark)
    return mark
  }

  /** 実機検査の数え上げ(`sketchRebuilds`): スケッチを作り直した回数。選択だけの配信で増えないことを見る。 */
  let sketchRebuildCount = 0

  /** `force` は版が同じでも作り直す(画面の粗さが変わって折り直すとき)。 */
  function update(entries: SketchDraw[], resolution: ViewResolution, force = false): boolean {
    lastSketchDraws = entries
    const keep = new Set(entries.map((entry) => entry.sketchId))
    // 消した・隠したスケッチも変化に数える(ホバーの印・内容の大きさ・視錐台を引き直す)。
    let rebuilt = false
    for (const sketchId of [...sketches.keys()]) {
      if (keep.has(sketchId)) continue
      clearSketch(sketchId)
      if (ghostSketchId === sketchId) clearGhost()
      rebuilt = true
    }
    for (const entry of entries) {
      const existing = sketches.get(entry.sketchId)
      if (!force && existing !== undefined && existing.builtKey === sketchBuildKey(entry)) {
        restyleSketch(existing, entry)
        continue
      }
      rebuilt = true
      sketchRebuildCount += 1
      clearSketch(entry.sketchId)
      const batch = createBatch(sketchRoot)
      const regionMeshes: THREE.Mesh[] = []
      const points: Array<[number, number]> = []
      const polylineCounts: number[] = []
      const handleMeshes: THREE.Points[] = []
      const handleLines: SketchFatLine[] = []
      // 選ばれている図形・点の索き(`entityId` -> 選ばれた点のxキーの集合。図形全体は `null`)。
      const pickedWhole = new Set(
        entry.selectedHandles.filter((item) => item.point === null).map((item) => item.entityId),
      )
      const pickedPoints = new Set(
        entry.selectedHandles
          .filter((item) => item.point !== null)
          .map((item) => `${item.entityId}|${item.point}`),
      )

      // 領域を先に置く(線が領域の上に見えるように。奥行きは書かないので順序で決まる)
      const selected = new Set(entry.selectedRegions)
      for (const region of entry.regions) {
        if (region.indices.length === 0) continue
        const geometry = new THREE.BufferGeometry()
        geometry.setAttribute(
          'position',
          new THREE.BufferAttribute(new Float32Array(region.positions), 3),
        )
        geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(region.indices), 1))
        geometry.computeBoundingSphere()
        const mesh = new THREE.Mesh(geometry, regionMaterials.get(selected.has(region.index), false))
        mesh.userData.sketchId = entry.sketchId
        mesh.userData.regionIndex = region.index
        // 選択かどうかを印として残す(材質から読み戻すとホバー色が付いた瞬間に選択が分からなくなる)
        mesh.userData.selected = selected.has(region.index)
        regionMeshes.push(batch.add(mesh, geometry))
      }

      const editing = entry.editing
      // 旧形式の反復の複製は `entities` にしか無い(持つときだけ描く。`SketchDraw.livePatterns`)
      if (entry.livePatterns) {
        const material = editing ? sketchMaterial : sketchLineMaterialFor('plain', false, 'depth')
        for (const entity of entry.entities) {
          const built = buildEntityLine(entry.frame, entity, material, resolution)
          if (!built) continue
          batch.add(built.object, built.geometry)
          points.push(...built.points)
          polylineCounts.push(built.points.length)
        }
      }

      // 文書の図形(`handles`)。ここに置くのは「指せるもの」(旧形式の反復の複製は `entity_id` を持たず指せない)。
      const pickPoints: OwnedSketchScene['pickPoints'] = []
      const pointEntityPositions: number[] = []
      for (const handle of entry.handles) {
        const selected = pickedWhole.has(handle.entityId)
        const built = buildEntityLine(
          entry.frame,
          handle,
          sketchLineMaterialFor(selected ? 'selected' : 'plain', false, editing || selected ? 'xray' : 'depth'),
          resolution,
        )
        if (built) {
          built.object.renderOrder = LAYER.sketchLine
          built.object.userData.entityId = handle.entityId
          // 選択かどうかの印(領域の `userData.selected` と同じ理由)
          built.object.userData.selected = selected
          handleLines.push(batch.add(built.object, built.geometry))
          if (!entry.livePatterns) {
            points.push(...built.points)
            polylineCounts.push(built.points.length)
          }
        }
        // 点の図形は折れ線を持たないので、視点合わせの範囲へ位置そのものを入れる
        if (handle.kind === 'point') points.push([handle.params.cx, handle.params.cy])
        if (!editing) {
          // 点は普段描かない(ホバー中・選択中だけ `replaceMark`)。点の図形だけは編集していないスケッチでも常に描く
          // (本人決定 2026-10-18。docs/DESIGN_DECISIONS.md)。
          for (const point of entityHandles(handle.kind, handle.params)) {
            if (!Number.isFinite(point.u) || !Number.isFinite(point.v)) continue
            const world = toWorld(entry.frame, point.u, point.v)
            pickPoints.push({ point: world, entityId: handle.entityId, key: point.key })
            if (handle.kind === 'point') pointEntityPositions.push(world.x, world.y, world.z)
          }
          continue
        }
        // 点が互いに独立して動く種別だけ常時表示する(entityHasIndependentPoints)。
        // rectangle/circle/arc は点が1個で、その点だけ動かすのと図形全体を動かすのが
        // move_sketch_entities 内で同じ処理になるため、ホバー/選択のときだけ見せる
        // (ピック可能な座標は変えない。`pickPointsAt` は mesh.visible を見ない)。
        // 点の図形は点の印のほかに描くものが無いので常に見せる。
        const showPointsAtRest = handle.kind === 'point' || entityHasIndependentPoints(handle.kind)
        for (const point of entityHandles(handle.kind, handle.params)) {
          if (!Number.isFinite(point.u) || !Number.isFinite(point.v)) continue
          const world = toWorld(entry.frame, point.u, point.v)
          const geometry = new THREE.BufferGeometry()
          geometry.setAttribute(
            'position',
            new THREE.BufferAttribute(new Float32Array([world.x, world.y, world.z]), 3),
          )
          geometry.computeBoundingSphere()
          const picked = selected || pickedPoints.has(`${handle.entityId}|${point.key}`)
          const mesh = new THREE.Points(
            geometry,
            sketchHandleMaterialFor(picked ? 'selected' : 'plain', false),
          )
          mesh.renderOrder = LAYER.sketchPoint // 深度を書かないので、描く順序で線より手前に出す
          mesh.visible = showPointsAtRest || picked
          mesh.userData.entityId = handle.entityId
          mesh.userData.point = point.key
          mesh.userData.selected = picked
          mesh.userData.showAtRest = showPointsAtRest
          handleMeshes.push(batch.add(mesh, geometry))
        }
      }
      if (pointEntityPositions.length > 0) {
        const geometry = new THREE.BufferGeometry()
        geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pointEntityPositions), 3))
        geometry.computeBoundingSphere()
        const mesh = new THREE.Points(geometry, sketchPointEntityMaterial)
        mesh.renderOrder = LAYER.sketchPoint
        batch.add(mesh, geometry)
      }
      const bounds = new THREE.Box3()
      for (const line of handleLines) {
        for (const point of line.userData.polylineWorld as THREE.Vector3[]) bounds.expandByPoint(point)
      }
      for (const mesh of regionMeshes) bounds.expandByObject(mesh)
      for (const item of pickPoints) bounds.expandByPoint(item.point)
      const scene: OwnedSketchScene = {
        builtKey: sketchBuildKey(entry),
        frame: entry.frame,
        plane: entry.plane,
        batch,
        regionMeshes,
        points,
        polylineCounts,
        editing,
        handleMeshes,
        pickPoints,
        selectedPointKeys: pickedPoints,
        markSelected: null,
        markHovered: null,
        handleLines,
        handles: entry.handles,
        intersections: sketchIntersections(entry.handles),
        sphere: bounds.isEmpty() ? new THREE.Sphere(new THREE.Vector3(), -1) : bounds.getBoundingSphere(new THREE.Sphere()),
      }
      if (!editing) scene.markSelected = replaceMark(scene, null, pickedPoints, sketchHandleMaterialFor('selected', false))
      sketches.set(entry.sketchId, scene)
    }
    return rebuilt
  }

  /** 領域の材質を決める。選択とホバーの両方を見る唯一の場所。 */
  function applyRegionMaterials(hoverRegion: RegionHover): void {
    for (const [sketchId, entry] of sketches) {
      for (const mesh of entry.regionMeshes) {
        const regionIndex = mesh.userData.regionIndex as number
        const isHit = mesh.userData.selected === true
        const isHovered =
          hoverRegion !== null &&
          hoverRegion.sketchId === sketchId &&
          hoverRegion.index === regionIndex
        mesh.material = regionMaterials.get(isHit, isHovered)
      }
    }
  }

  /**
   * スケッチの線・点の材質を決める。選択とホバーの両方を見る唯一の場所。`handleLines`/`handleMeshes` を
   * 歩いて `.material` を差し替えるだけ。点は必ず辺の上に乗るので、点がホバーされているときは
   * 線を光らせない(`hoverSketchEntity.point === null` を線側の条件に入れて排他にする)。
   */
  function applySketchHoverMaterials(hoverSketchEntity: SketchEntityHit | null): void {
    for (const [sketchId, entry] of sketches) {
      for (const line of entry.handleLines) {
        const entityId = line.userData.entityId as string
        const selected = line.userData.selected === true
        const hovered =
          hoverSketchEntity !== null &&
          hoverSketchEntity.sketchId === sketchId &&
          hoverSketchEntity.entityId === entityId &&
          hoverSketchEntity.point === null
        line.material = sketchLineMaterialFor(
          selected ? 'selected' : 'plain',
          hovered,
          entry.editing || selected ? 'xray' : 'depth',
        )
      }
      if (!entry.editing) {
        const hoveredPoint =
          hoverSketchEntity !== null && hoverSketchEntity.sketchId === sketchId && hoverSketchEntity.point !== null
            ? new Set([`${hoverSketchEntity.entityId}|${hoverSketchEntity.point}`])
            : new Set<string>()
        if (hoveredPoint.size > 0 || entry.markHovered !== null) {
          const selected = [...hoveredPoint].some((key) => entry.selectedPointKeys.has(key))
          entry.markHovered = replaceMark(
            entry,
            entry.markHovered,
            hoveredPoint,
            sketchHandleMaterialFor(selected ? 'selected' : 'plain', true),
          )
        }
      }
      for (const mesh of entry.handleMeshes) {
        const entityId = mesh.userData.entityId as string
        const point = mesh.userData.point as string
        const selected = mesh.userData.selected === true
        const showAtRest = mesh.userData.showAtRest === true
        const hovered =
          hoverSketchEntity !== null &&
          hoverSketchEntity.sketchId === sketchId &&
          hoverSketchEntity.entityId === entityId &&
          hoverSketchEntity.point === point
        mesh.material = sketchHandleMaterialFor(selected ? 'selected' : 'plain', hovered)
        mesh.visible = showAtRest || selected || hovered
      }
    }
  }

  function clearGhost(): void {
    if (!ghost) return
    ghost.clear()
    ghost = null
    ghostSketchId = null
  }

  function showSketchGhosts(
    sketchId: string,
    entities: Array<{ kind: string; params: Record<string, number> }>,
    resolution: ViewResolution,
    tone: 'normal' | 'warn' = 'normal',
  ): void {
    clearGhost()
    const target = sketches.get(sketchId)
    if (entities.length === 0 || !target) return
    const batch = createBatch(sketchRoot)
    const material = tone === 'warn' ? sketchGhostWarnMaterial : sketchGhostMaterial
    const pointPositions: number[] = []
    for (const entity of entities) {
      if (entity.kind === 'point') {
        const world = toWorld(target.frame, entity.params.cx, entity.params.cy)
        pointPositions.push(world.x, world.y, world.z)
        continue
      }
      const built = buildEntityLine(target.frame, entity, material, resolution)
      if (!built) continue
      batch.add(built.object, built.geometry)
    }
    if (pointPositions.length > 0) {
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pointPositions), 3))
      geometry.computeBoundingSphere()
      const mesh = new THREE.Points(geometry, tone === 'warn' ? sketchGhostWarnPointMaterial : sketchGhostPointMaterial)
      mesh.renderOrder = LAYER.sketchPoint
      batch.add(mesh, geometry)
    }
    if (batch.objects.length === 0) return
    ghost = batch
    ghostSketchId = sketchId
  }

  return {
    object: sketchRoot,
    get: (sketchId) => sketches.get(sketchId),
    entries: () => sketches.entries(),
    values: () => sketches.values(),
    update,
    redraw: (resolution) => lastSketchDraws.length > 0 && update(lastSketchDraws, resolution, true),
    setLineResolution: (width, height) => sketchLineResolution.set(width, height),
    applyRegionMaterials,
    applySketchHoverMaterials,
    showGhosts: showSketchGhosts,
    clearGhost,
    rebuilds: () => sketchRebuildCount,
    dispose() {
      clearGhost()
      for (const sketchId of [...sketches.keys()]) clearSketch(sketchId)
      lastSketchDraws = []
      sketchGhostMaterial.dispose()
      sketchGhostWarnMaterial.dispose()
      sketchGhostPointMaterial.dispose()
      sketchGhostWarnPointMaterial.dispose()
      sketchPointEntityMaterial.dispose()
      sketchLineMaterials.dispose()
      sketchHandleMaterials.dispose()
      regionMaterials.dispose()
      sketchRoot.removeFromParent()
    },
  }
}
