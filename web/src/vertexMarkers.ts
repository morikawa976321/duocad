/**
 * 頂点マーカー(選択中のものだけ)。
 *
 * 辺と違い候補は全部出さない(頂点を引数に取るverbが無く、要るのは「AIが選んだ頂点が今どれか」を
 * 人が見て外せることだけ。`duocad/server.py` の `Session.vertices` と同じ理由)。
 *
 * 点(`THREE.Points`)で描き、大きさは画素で固定する(`sizeAttenuation: false`)。
 * 世界座標の球だと寄れば巨大な玉、引けば1画素未満になり、27部品を引きで見ると消える。
 */
import * as THREE from 'three'

import { createBatch } from './sceneBatch'

import { HIGHLIGHT_COLOR, LAYER, VERTEX_PIXELS } from './viewportConstants'
import type { VertexMarker, VertexRef } from './viewportTypes'

/** 本編(`viewport.ts`)から借りるもの。 */
export interface VertexMarkersDeps {
  /** その画素で取れる点(当たり半径は画面上のCSS px)。本編の screen pick を借りる(カメラと寸法を持つのは本編だけ)。 */
  pickPointsAt<T>(
    offsetX: number,
    offsetY: number,
    radiusPx: number,
    meshes: readonly THREE.Points[],
    readItem: (mesh: THREE.Points) => T | null,
    keep?: ((point: THREE.Vector3) => boolean) | null,
  ): T | null
  /** 世界座標 → コンテナ内のCSSピクセル(画面外なら null)。 */
  toScreen(point: THREE.Vector3): { x: number; y: number } | null
  /** 「指せるもの」が増減したことを本編へ知らせる。 */
  pickChanged(): void
  /** 実機検査フックが読む強調色の表。`vertex` だけをここが書く。 */
  highlightColor: { vertex: number | null }
  /** 断面のクリップ平面(共有配列。`sectionView.ts`)。マーカーもボディと一緒に切る。 */
  clipPlanes: THREE.Plane[]
  /** その点が断面で残る側か(OFF なら常に true)。切られたマーカーは指せない。 */
  sectionKeeps(point: THREE.Vector3): boolean
}

export interface VertexMarkers {
  /** マーカーを入れるグループ。`scene` へ足すのは呼び出し側。 */
  object: THREE.Group
  /** 今出しているマーカー(ウィンドウ選択が中身を見る)。 */
  meshes(): THREE.Points[]
  show(markers: VertexMarker[]): void
  /** その画素で取れる頂点(無ければ null)。 */
  at(offsetX: number, offsetY: number): VertexRef | null
  /** その頂点が今いる画素(取れない画素なら null)。 */
  project(bodyId: string, vertexIndex: number): { x: number; y: number } | null
  dispose(): void
}

export function createVertexMarkers(deps: VertexMarkersDeps): VertexMarkers {
  const { pickPointsAt, toScreen, pickChanged, highlightColor, clipPlanes, sectionKeeps } = deps

  const vertexRoot = new THREE.Group()

  /** マーカーの材質。辺と同じく深度検査をしない(見た目だけ隠すと「見えない頂点が外れる」になる)。 */
  const vertexMaterial = new THREE.PointsMaterial({
    color: HIGHLIGHT_COLOR,
    size: VERTEX_PIXELS * 2,
    sizeAttenuation: false,
    depthTest: false,
    clippingPlanes: clipPlanes,
  })

  let vertexMeshes: THREE.Points[] = []
  const vertexBatch = createBatch(vertexRoot)
  /** 直前に描いた印(同じものを渡されたら作り直さない)。 */
  let vertexKey = ''

  function clearVertices(): void {
    vertexBatch.clear()
    vertexMeshes = []
    vertexKey = ''
  }

  function showVertices(markers: VertexMarker[]): void {
    const key = markers
      .map((marker) => `${marker.bodyId}|${marker.vertexIndex}|${marker.point.join(',')}`)
      .join(';')
    // App.svelte は選択が変わるたびに呼び直す(`showEdges` と同じ番人)。
    if (key === vertexKey) return
    clearVertices()
    vertexKey = key
    pickChanged()
    highlightColor.vertex = markers.length === 0 ? null : vertexMaterial.color.getHex()
    if (markers.length === 0) return
    for (const marker of markers) {
      if (marker.point.length < 3) continue
      // 1マーカー1オブジェクト(まとめると当たった点からコアの添字へ戻れない)。
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute(
        'position',
        new THREE.BufferAttribute(new Float32Array(marker.point.slice(0, 3)), 3),
      )
      geometry.computeBoundingSphere()
      const mesh = new THREE.Points(geometry, vertexMaterial)
      mesh.renderOrder = LAYER.region // 深度を書かないので、描く順序で辺より手前に出す
      mesh.userData.bodyId = marker.bodyId
      mesh.userData.vertexIndex = marker.vertexIndex
      mesh.userData.point = marker.point
      vertexMeshes.push(vertexBatch.add(mesh, geometry))
    }
  }

  function vertexAt(offsetX: number, offsetY: number): VertexRef | null {
    return pickPointsAt(
      offsetX,
      offsetY,
      VERTEX_PIXELS,
      vertexMeshes,
      (mesh) => {
        const { bodyId, vertexIndex } = mesh.userData
        if (typeof bodyId !== 'string' || typeof vertexIndex !== 'number') return null
        return { bodyId, vertexIndex }
      },
      sectionKeeps,
    )
  }

  function projectVertex(bodyId: string, vertexIndex: number): { x: number; y: number } | null {
    const mesh = vertexMeshes.find(
      (candidate) =>
        candidate.userData.bodyId === bodyId && candidate.userData.vertexIndex === vertexIndex,
    )
    if (!mesh) return null
    const point = mesh.userData.point as number[]
    const screen = toScreen(new THREE.Vector3(point[0], point[1], point[2]))
    if (screen === null) return null
    // 本当にその頂点が取れる画素か確かめてから返す(面の `project` と同じ)。
    const hit = vertexAt(screen.x, screen.y)
    if (hit === null || hit.bodyId !== bodyId || hit.vertexIndex !== vertexIndex) return null
    return screen
  }

  return {
    object: vertexRoot,
    meshes() {
      return vertexMeshes
    },
    show: showVertices,
    at: vertexAt,
    project: projectVertex,
    dispose() {
      clearVertices()
      vertexMaterial.dispose()
    },
  }
}
