/**
 * 頂点マーカー(選択中のものだけ)。
 *
 * **辺と違い、候補を全部出すことはしない。** 頂点を引数に取るverbが無いので
 * 「候補を見せて選ばせる」場面が無く、要るのは「AIが選んだ頂点が今どれか」を人が見て
 * 外せることだけである(`duocad/server.py` の `Session.vertices` と同じ理由)。
 *
 * **点(`THREE.Points`)で描き、大きさは画素で固定する**(`sizeAttenuation: false`)。
 * 世界座標の球にすると、描いた時のカメラ距離で大きさが決まってしまい、**寄れば巨大な玉、
 * 引けば1画素未満**になる —— 頂点マーカーは選択中の頂点を見る唯一の手段なので、
 * 27部品を引きで見ているときに消えるのは機能が無いのと同じである。
 */
import * as THREE from 'three'

import { HIGHLIGHT_COLOR, LAYER, PROPOSED_COLOR, VERTEX_PIXELS } from './viewportConstants'
import type { VertexMarker, VertexRef } from './viewportTypes'

/** 本編(`viewport.ts`)から借りるもの。 */
export interface VertexMarkersDeps {
  /** コンテナのCSSピクセル寸法。 */
  size(): { width: number; height: number }
  /** 本編がいま使っているカメラ。**投影の切替で差し替わる**ので毎回読み直す。 */
  camera(): THREE.PerspectiveCamera | THREE.OrthographicCamera
  /** 本編と**同じ** Raycaster(`params.Points` を借りて戻すので共有する)。 */
  raycaster: THREE.Raycaster
  /** 注視点あたりの mm/画素。当たり半径を画素で決めるのに使う。 */
  worldPerPixel(): number
  /** 世界座標 → コンテナ内のCSSピクセル(画面外なら null)。 */
  toScreen(point: THREE.Vector3): { x: number; y: number } | null
  /** 「指せるもの」が増減したことを本編へ知らせる。 */
  pickChanged(): void
  /** 実機検査フックが読む強調色の表。`vertex` だけをここが書く。 */
  highlightColor: { vertex: number | null }
}

export interface VertexMarkers {
  /** マーカーを入れるグループ。`scene` へ足すのは呼び出し側。 */
  object: THREE.Group
  /** 今出しているマーカー(ウィンドウ選択が中身を見る)。 */
  meshes(): THREE.Points[]
  show(markers: VertexMarker[], origin: 'user' | 'proposed'): void
  clear(): void
  /** その画素で取れる頂点(無ければ null)。 */
  at(offsetX: number, offsetY: number): VertexRef | null
  /** その頂点が今いる画素(取れない画素なら null)。 */
  project(bodyId: string, vertexIndex: number): { x: number; y: number } | null
  dispose(): void
}

export function createVertexMarkers(deps: VertexMarkersDeps): VertexMarkers {
  const { size, raycaster, worldPerPixel, toScreen, pickChanged, highlightColor } = deps

  const vertexRoot = new THREE.Group()

  const vertexMaterial = new THREE.PointsMaterial({
    color: HIGHLIGHT_COLOR,
    size: VERTEX_PIXELS * 2,
    sizeAttenuation: false,
    // 辺と同じく**深度検査をしない**(奥のマーカーも見え、奥のマーカーも指せる)。
    // 見た目だけ隠すと「見えない頂点が外れる」になる。
    depthTest: false,
  })
  const vertexProposedMaterial = new THREE.PointsMaterial({
    color: PROPOSED_COLOR,
    size: VERTEX_PIXELS * 2,
    sizeAttenuation: false,
    depthTest: false,
  })

  /** 今出している頂点マーカー。空なら選択に頂点が入っていない。 */
  let vertexMeshes: THREE.Points[] = []
  const vertexDisposables: Array<{ dispose(): void }> = []
  /** 直前に描いた印(**同じものを渡されたら作り直さない**ため)。 */
  let vertexKey = ''

  function clearVertices(): void {
    for (const mesh of vertexMeshes) vertexRoot.remove(mesh)
    for (const item of vertexDisposables) item.dispose()
    vertexDisposables.length = 0
    vertexMeshes = []
    vertexKey = ''
  }

  function showVertices(markers: VertexMarker[], origin: 'user' | 'proposed'): void {
    const key = `${origin}|${markers
      .map((marker) => `${marker.bodyId}|${marker.vertexIndex}|${marker.point.join(',')}`)
      .join(';')}`
    // 呼び出し側(App.svelte)は選択が変わるたびに呼び直すので、毎回作り直すと
    // ジオメトリを捨てて作り直し続けることになる(`showEdges` と同じ番人)。
    if (key === vertexKey) return
    clearVertices()
    vertexKey = key
    pickChanged() // 頂点マーカーは面より先に取られるので、増減で「指せるもの」が変わる
    const material = origin === 'proposed' ? vertexProposedMaterial : vertexMaterial
    highlightColor.vertex = markers.length === 0 ? null : material.color.getHex()
    if (markers.length === 0) return
    for (const marker of markers) {
      if (marker.point.length < 3) continue
      // **1マーカー1オブジェクト**(辺が1本1 `THREE.Line` なのと同じ) —— まとめると
      // 当たった点からコアの添字へ戻れない。数は選択中のものだけなので費用は無視できる。
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute(
        'position',
        new THREE.BufferAttribute(new Float32Array(marker.point.slice(0, 3)), 3),
      )
      geometry.computeBoundingSphere()
      const mesh = new THREE.Points(geometry, material)
      mesh.renderOrder = LAYER.region // 深度を書かないぶん、描く順序で辺より手前に出す
      mesh.userData.bodyId = marker.bodyId
      mesh.userData.vertexIndex = marker.vertexIndex
      mesh.userData.point = marker.point
      vertexRoot.add(mesh)
      vertexMeshes.push(mesh)
      vertexDisposables.push(geometry)
    }
  }

  function vertexAt(offsetX: number, offsetY: number): VertexRef | null {
    const camera = deps.camera()
    if (vertexMeshes.length === 0) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    // 点の当たり判定も**画素で決める**(辺の `edgeAt` と同じ理由 —— 寄るほど掴みにくく
    // なるのを避ける)。描いている大きさと揃えておく。
    const previous = raycaster.params.Points?.threshold
    raycaster.params.Points = { threshold: worldPerPixel() * VERTEX_PIXELS }
    const hits = raycaster.intersectObjects(vertexMeshes, false)
    if (previous !== undefined) raycaster.params.Points = { threshold: previous }
    if (hits.length === 0) return null
    const bodyId = hits[0].object.userData.bodyId
    const vertexIndex = hits[0].object.userData.vertexIndex
    if (typeof bodyId !== 'string' || typeof vertexIndex !== 'number') return null
    return { bodyId, vertexIndex }
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
    // **本当にその頂点が取れる画素か**を確かめてから返す(面の `project` と同じ流儀)。
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
    clear: clearVertices,
    at: vertexAt,
    project: projectVertex,
    dispose() {
      clearVertices()
      vertexMaterial.dispose()
      vertexProposedMaterial.dispose()
    },
  }
}
