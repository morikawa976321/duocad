/**
 * ソリッドの辺と頂点: 見えているボディ全部の折れ線・座標を持ち、画面で当たるものを返し、ホバー中と選択中のものだけ描く。
 *
 * 辺・頂点はサーバから取り寄せる(`DocumentView.edges` / `vertices`。状態の配信には入らない)ので、届いたボディの分だけ
 * 指せる。画面はカーソルの近くのボディの分を頼む(`viewport.ts` の `onPickDataWanted`)。
 *
 * **普段は描かない**: 見た目の辺はボディの境界線(`bodyMesh.ts` の `EdgesGeometry`)が受け持つ。全ボディの辺を
 * 深度検査なしで描くと裏の辺まで全部透けて見える。描くのはホバー中・選択中の辺と頂点だけで、それは深度検査をしない
 * (選んだものは裏に回っても見える。AI が選んだ裏のものを人が確かめられる)。
 */
import * as THREE from 'three'

import { closestOnPolyline, screenHitOfPoint, type PickPolyline, type ScreenHit, type ScreenProjector } from './polylinePick'
import type { EdgeGeometry, VertexGeometry } from './session'
import { HIGHLIGHT_COLOR, HOVER_LIGHTEN, LAYER, PICK_EDGE_COLOR, VERTEX_PIXELS, lighten } from './viewportConstants'
import { variantCache } from './viewportMaterials'
import type { EdgeRef, VertexRef } from './viewportTypes'

/** 1ボディぶんの辺(`EdgesPayload` の中身)。 */
export interface BodyEdges {
  revision: number
  /** 折れ線の弦の偏差[mm](遮蔽の許しに使う)。 */
  deviation: number
  edges: EdgeGeometry[]
}

/** 1ボディぶんの頂点(`VerticesPayload` の中身)。 */
export interface BodyVertices {
  revision: number
  vertices: VertexGeometry[]
}

export interface EdgeCandidate extends ScreenHit {
  ref: EdgeRef
  /** その辺の折れ線の偏差[mm]。 */
  deviation: number
}

export interface VertexCandidate extends ScreenHit {
  ref: VertexRef
}

export interface SolidFeaturesDeps {
  /** 断面のクリップ平面(共有配列)。描く辺・頂点も切る。 */
  clipPlanes: THREE.Plane[]
  /** その点の奥行きでの画面1画素の長さ[mm](画面の外接円で候補を絞る)。 */
  pixelAt(point: THREE.Vector3): number
  /** 描くものが変わった(描き直しを頼む)。 */
  invalidate(): void
}

export interface SolidFeatures {
  /** 描く辺・頂点のグループ。`scene` へ足すのは呼び出し側。 */
  object: THREE.Group
  /** そのボディの辺を差し替える(null で捨てる)。同じものを渡されたら何もしない。差し替えたら true。 */
  setEdges(bodyId: string, data: BodyEdges | null): boolean
  setVertices(bodyId: string, data: BodyVertices | null): boolean
  hasEdges(bodyId: string): boolean
  hasVertices(bodyId: string): boolean
  /** 当たり半径の内側の辺(距離順ではない)。`bodies` を渡せばそのボディだけ。 */
  edgeCandidates(
    x: number,
    y: number,
    radiusPx: number,
    projector: ScreenProjector,
    clip: ((point: THREE.Vector3) => number) | null,
  ): EdgeCandidate[]
  vertexCandidates(
    x: number,
    y: number,
    radiusPx: number,
    projector: ScreenProjector,
    keep: ((point: THREE.Vector3) => boolean) | null,
  ): VertexCandidate[]
  /** 辺・頂点のホバーを差し替える。変わったら true。 */
  setHover(edge: EdgeRef | null, vertex: VertexRef | null): boolean
  highlightEdges(refs: EdgeRef[]): void
  highlightVertices(refs: VertexRef[]): void
  /** 辺を連なりの単位で扱うか(フィレット・面取りの起動中)。 */
  groupChains(on: boolean): void
  /** 辺の連なり(連なりで扱わない間はその1本だけ。まだ分からなければ null)。 */
  chainOf(ref: EdgeRef): number[] | null
  setChains(bodyId: string, chains: number[][]): void
  onChainWanted(listener: (ref: EdgeRef) => void): void
  /** そのボディの辺1本ずつ(矩形選択)。 */
  forEachEdge(visit: (ref: EdgeRef, points: Float32Array, deviation: number) => void): void
  forEachVertex(visit: (ref: VertexRef, point: THREE.Vector3) => void): void
  /** 辺の折れ線の道のりの `fraction` の位置(0=始点 1=終点)。無ければ null。 */
  edgePoint(ref: EdgeRef, fraction: number): THREE.Vector3 | null
  vertexPoint(ref: VertexRef): THREE.Vector3 | null
  /** その辺・頂点を今描いている色(16進。描いていなければ null)。実機検査フック専用。 */
  drawnColor(ref: EdgeRef | VertexRef): number | null
  dispose(): void
}

interface EdgeEntry {
  index: number
  points: Float32Array
  sphere: THREE.Sphere
}

interface BodyEdgeEntry {
  source: BodyEdges
  edges: EdgeEntry[]
  sphere: THREE.Sphere
}

interface BodyVertexEntry {
  source: BodyVertices
  vertices: Array<{ index: number; point: THREE.Vector3 }>
  sphere: THREE.Sphere
}

function sphereOf(points: ArrayLike<number>): THREE.Sphere {
  const box = new THREE.Box3()
  const scratch = new THREE.Vector3()
  for (let at = 0; at + 2 < points.length; at += 3) box.expandByPoint(scratch.set(points[at], points[at + 1], points[at + 2]))
  return box.isEmpty() ? new THREE.Sphere(new THREE.Vector3(), -1) : box.getBoundingSphere(new THREE.Sphere())
}

function float32Polyline(points: Float32Array): PickPolyline {
  return {
    count: Math.floor(points.length / 3),
    at(index, out) {
      out.set(points[index * 3], points[index * 3 + 1], points[index * 3 + 2])
    },
  }
}

const edgeKey = (bodyId: string, edgeIndex: number) => `${bodyId}|${edgeIndex}`

export function createSolidFeatures(deps: SolidFeaturesDeps): SolidFeatures {
  const object = new THREE.Group()
  const edgeData = new Map<string, BodyEdgeEntry>()
  const vertexData = new Map<string, BodyVertexEntry>()

  // ------------------------------------------------------------ 当たり
  const projectedCenter = { x: 0, y: 0, w: 1 }

  /** その球が画面でカーソルから `radiusPx` 以内に掛かりうるか(掛からないものは折れ線を歩かない)。 */
  function sphereNear(sphere: THREE.Sphere, x: number, y: number, radiusPx: number, projector: ScreenProjector): boolean {
    if (sphere.radius < 0) return false
    if (!projector.project(sphere.center, projectedCenter)) return true // 中心がカメラの裏でも一部は見えうる
    const reach = sphere.radius / Math.max(deps.pixelAt(sphere.center), 1e-12) + radiusPx
    const dx = projectedCenter.x - x
    const dy = projectedCenter.y - y
    return dx * dx + dy * dy <= reach * reach
  }

  function edgeCandidates(
    x: number,
    y: number,
    radiusPx: number,
    projector: ScreenProjector,
    clip: ((point: THREE.Vector3) => number) | null,
  ): EdgeCandidate[] {
    const limit = radiusPx * radiusPx
    const found: EdgeCandidate[] = []
    for (const [bodyId, entry] of edgeData) {
      if (!sphereNear(entry.sphere, x, y, radiusPx, projector)) continue
      for (const edge of entry.edges) {
        if (!sphereNear(edge.sphere, x, y, radiusPx, projector)) continue
        const hit = closestOnPolyline(x, y, float32Polyline(edge.points), projector, clip)
        if (hit === null || hit.distSq > limit) continue
        found.push({ ...hit, ref: { bodyId, edgeIndex: edge.index }, deviation: entry.source.deviation })
      }
    }
    return found
  }

  function vertexCandidates(
    x: number,
    y: number,
    radiusPx: number,
    projector: ScreenProjector,
    keep: ((point: THREE.Vector3) => boolean) | null,
  ): VertexCandidate[] {
    const limit = radiusPx * radiusPx
    const found: VertexCandidate[] = []
    for (const [bodyId, entry] of vertexData) {
      if (!sphereNear(entry.sphere, x, y, radiusPx, projector)) continue
      for (const vertex of entry.vertices) {
        if (keep !== null && !keep(vertex.point)) continue
        const hit = screenHitOfPoint(x, y, vertex.point, projector)
        if (hit === null || hit.distSq > limit) continue
        found.push({ ...hit, ref: { bodyId, vertexIndex: vertex.index } })
      }
    }
    return found
  }

  // ------------------------------------------------------------ 描く(ホバー中・選択中だけ)
  type MarkState = 'plain' | 'selected'
  const edgeMaterials = variantCache(
    (state: MarkState, hovered: boolean) => `${state}:${hovered ? 1 : 0}`,
    (state, hovered) => {
      const base = state === 'selected' ? HIGHLIGHT_COLOR : PICK_EDGE_COLOR
      return new THREE.LineBasicMaterial({
        color: hovered ? lighten(base, HOVER_LIGHTEN) : base,
        depthTest: false,
        clippingPlanes: deps.clipPlanes,
      })
    },
  )
  const vertexMaterials = variantCache(
    (state: MarkState, hovered: boolean) => `${state}:${hovered ? 1 : 0}`,
    (state, hovered) => {
      const base = state === 'selected' ? HIGHLIGHT_COLOR : PICK_EDGE_COLOR
      return new THREE.PointsMaterial({
        color: hovered ? lighten(base, HOVER_LIGHTEN) : base,
        size: VERTEX_PIXELS * 2,
        sizeAttenuation: false,
        depthTest: false,
        clippingPlanes: deps.clipPlanes,
      })
    },
  )

  /** 描いたものを外して捨てる(ジオメトリだけ。材質は共有のキャッシュ)。 */
  const batch = {
    add<T extends THREE.Line | THREE.Points>(drawn: T): T {
      object.add(drawn)
      return drawn
    },
    remove(drawn: THREE.Object3D): void {
      object.remove(drawn)
      if (drawn instanceof THREE.Line || drawn instanceof THREE.Points) drawn.geometry.dispose()
    },
    clear(): void {
      for (const drawn of [...object.children]) this.remove(drawn)
      drawnEdges.clear()
      drawnVertices.clear()
    },
  }
  /** 描いている辺・頂点(鍵 -> 線)。欲しい集合が変わるたびに足し引きする。 */
  const drawnEdges = new Map<string, THREE.Line>()
  const drawnVertices = new Map<string, THREE.Points>()

  let hoverEdge: EdgeRef | null = null
  let hoverVertex: VertexRef | null = null
  let selectedEdges: EdgeRef[] = []
  let selectedVertices: VertexRef[] = []
  let chainsWanted = false
  const chainsOf = new Map<string, Map<number, number[]>>()
  let chainWanted: ((ref: EdgeRef) => void) | null = null

  function chainOf(ref: EdgeRef): number[] | null {
    if (!chainsWanted) return [ref.edgeIndex]
    return chainsOf.get(ref.bodyId)?.get(ref.edgeIndex) ?? null
  }

  function edgeEntry(ref: EdgeRef): EdgeEntry | undefined {
    return edgeData.get(ref.bodyId)?.edges.find((edge) => edge.index === ref.edgeIndex)
  }

  function redraw(): void {
    const hovered = new Set<string>()
    if (hoverEdge !== null) {
      const chain = chainOf(hoverEdge)
      // 連なりがまだ分からない間はその1本だけ光らせ、取り寄せを頼む(届くと `setChains` が塗り直す)
      if (chain === null) chainWanted?.(hoverEdge)
      for (const index of chain ?? [hoverEdge.edgeIndex]) hovered.add(edgeKey(hoverEdge.bodyId, index))
    }
    const selected = new Set(selectedEdges.map((ref) => edgeKey(ref.bodyId, ref.edgeIndex)))
    const wanted = new Set([...hovered, ...selected])
    for (const [key, line] of drawnEdges) {
      if (wanted.has(key)) continue
      batch.remove(line)
      drawnEdges.delete(key)
    }
    for (const key of wanted) {
      let line = drawnEdges.get(key)
      if (line === undefined) {
        const [bodyId, index] = key.split('|')
        const edge = edgeEntry({ bodyId, edgeIndex: Number(index) })
        // 2点(=6要素)未満は線にならない
        if (edge === undefined || edge.points.length < 6) continue
        const geometry = new THREE.BufferGeometry()
        geometry.setAttribute('position', new THREE.BufferAttribute(edge.points, 3))
        geometry.computeBoundingSphere()
        line = new THREE.Line(geometry, edgeMaterials.get('plain', false))
        line.renderOrder = LAYER.edge // 深度を書かないぶん、描く順序で面より手前に出す
        drawnEdges.set(key, batch.add(line))
      }
      line.material = edgeMaterials.get(selected.has(key) ? 'selected' : 'plain', hovered.has(key))
    }

    const vertexKey = (ref: VertexRef) => `${ref.bodyId}|${ref.vertexIndex}`
    const hoveredVertex = hoverVertex === null ? '' : vertexKey(hoverVertex)
    const selectedVertexKeys = new Set(selectedVertices.map(vertexKey))
    const wantedVertices = new Set([...selectedVertexKeys, ...(hoveredVertex === '' ? [] : [hoveredVertex])])
    for (const [key, points] of drawnVertices) {
      if (wantedVertices.has(key)) continue
      batch.remove(points)
      drawnVertices.delete(key)
    }
    for (const key of wantedVertices) {
      let points = drawnVertices.get(key)
      if (points === undefined) {
        const [bodyId, index] = key.split('|')
        const point = vertexPoint({ bodyId, vertexIndex: Number(index) })
        if (point === null) continue
        const geometry = new THREE.BufferGeometry()
        geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array([point.x, point.y, point.z]), 3))
        geometry.computeBoundingSphere()
        points = new THREE.Points(geometry, vertexMaterials.get('plain', false))
        points.renderOrder = LAYER.region // 深度を書かないので、描く順序で辺より手前に出す
        drawnVertices.set(key, batch.add(points))
      }
      points.material = vertexMaterials.get(selectedVertexKeys.has(key) ? 'selected' : 'plain', key === hoveredVertex)
    }
    deps.invalidate()
  }

  function vertexPoint(ref: VertexRef): THREE.Vector3 | null {
    return vertexData.get(ref.bodyId)?.vertices.find((vertex) => vertex.index === ref.vertexIndex)?.point ?? null
  }

  function dropDrawn(bodyId: string): void {
    for (const map of [drawnEdges, drawnVertices] as Array<Map<string, THREE.Object3D>>) {
      for (const [key, drawn] of map) {
        if (!key.startsWith(`${bodyId}|`)) continue
        batch.remove(drawn)
        map.delete(key)
      }
    }
  }

  function setEdges(bodyId: string, data: BodyEdges | null): boolean {
    const known = edgeData.get(bodyId)
    if ((known?.source ?? null) === data) return false
    if (data === null) edgeData.delete(bodyId)
    else {
      const edges = data.edges.map((edge) => {
        const points = edge.points instanceof Float32Array ? edge.points : new Float32Array(edge.points)
        return { index: edge.index, points, sphere: sphereOf(points) }
      })
      const all: number[] = []
      for (const edge of edges) {
        if (edge.sphere.radius < 0) continue
        const { center, radius } = edge.sphere
        all.push(center.x - radius, center.y, center.z, center.x + radius, center.y, center.z)
        all.push(center.x, center.y - radius, center.z, center.x, center.y + radius, center.z)
        all.push(center.x, center.y, center.z - radius, center.x, center.y, center.z + radius)
      }
      edgeData.set(bodyId, { source: data, edges, sphere: sphereOf(all) })
    }
    // 形(精度)が変わったので連なりと描いた線を捨てる(連なりは版つきで届き直す)
    chainsOf.delete(bodyId)
    dropDrawn(bodyId)
    redraw()
    return true
  }

  function setVertices(bodyId: string, data: BodyVertices | null): boolean {
    const known = vertexData.get(bodyId)
    if ((known?.source ?? null) === data) return false
    if (data === null) vertexData.delete(bodyId)
    else {
      const vertices = data.vertices
        .filter((vertex) => vertex.point.length >= 3)
        .map((vertex) => ({ index: vertex.index, point: new THREE.Vector3(vertex.point[0], vertex.point[1], vertex.point[2]) }))
      const flat: number[] = []
      for (const vertex of vertices) flat.push(vertex.point.x, vertex.point.y, vertex.point.z)
      vertexData.set(bodyId, { source: data, vertices, sphere: sphereOf(flat) })
    }
    dropDrawn(bodyId)
    redraw()
    return true
  }

  return {
    object,
    setEdges,
    setVertices,
    hasEdges: (bodyId) => edgeData.has(bodyId),
    hasVertices: (bodyId) => vertexData.has(bodyId),
    edgeCandidates,
    vertexCandidates,
    setHover(edge, vertex) {
      const same =
        hoverEdge?.bodyId === edge?.bodyId &&
        hoverEdge?.edgeIndex === edge?.edgeIndex &&
        hoverVertex?.bodyId === vertex?.bodyId &&
        hoverVertex?.vertexIndex === vertex?.vertexIndex
      if (same) return false
      hoverEdge = edge
      hoverVertex = vertex
      redraw()
      return true
    },
    highlightEdges(refs) {
      selectedEdges = refs
      redraw()
    },
    highlightVertices(refs) {
      selectedVertices = refs
      redraw()
    },
    groupChains(on) {
      if (chainsWanted === on) return
      chainsWanted = on
      redraw()
    },
    chainOf,
    setChains(bodyId, chains) {
      let known = chainsOf.get(bodyId)
      if (known === undefined) {
        known = new Map()
        chainsOf.set(bodyId, known)
      }
      for (const members of chains) {
        for (const index of members) known.set(index, members)
      }
      redraw()
    },
    onChainWanted(listener) {
      chainWanted = listener
    },
    forEachEdge(visit) {
      for (const [bodyId, entry] of edgeData) {
        for (const edge of entry.edges) visit({ bodyId, edgeIndex: edge.index }, edge.points, entry.source.deviation)
      }
    },
    forEachVertex(visit) {
      for (const [bodyId, entry] of vertexData) {
        for (const vertex of entry.vertices) visit({ bodyId, vertexIndex: vertex.index }, vertex.point)
      }
    },
    edgePoint(ref, fraction) {
      const edge = edgeEntry(ref)
      if (edge === undefined) return null
      const count = Math.floor(edge.points.length / 3)
      if (count < 2) return null
      const at = fraction * (count - 1)
      const first = Math.min(Math.floor(at), count - 2)
      const t = at - first
      const a = new THREE.Vector3(edge.points[first * 3], edge.points[first * 3 + 1], edge.points[first * 3 + 2])
      const b = new THREE.Vector3(edge.points[first * 3 + 3], edge.points[first * 3 + 4], edge.points[first * 3 + 5])
      return a.lerp(b, t)
    },
    vertexPoint(ref) {
      return vertexPoint(ref)?.clone() ?? null
    },
    drawnColor(ref) {
      const drawn =
        'edgeIndex' in ref
          ? drawnEdges.get(edgeKey(ref.bodyId, ref.edgeIndex))
          : drawnVertices.get(`${ref.bodyId}|${ref.vertexIndex}`)
      const material = drawn?.material as { color?: THREE.Color } | undefined
      return material?.color ? material.color.getHex() : null
    },
    dispose() {
      batch.clear()
      edgeMaterials.dispose()
      vertexMaterials.dispose()
    },
  }
}
