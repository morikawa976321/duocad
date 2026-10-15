/**
 * 形状最適化(第3のモード)の装飾 —— 拘束の固定記号(四角錐)と、荷重の矢印+大きさのラベル。
 * **指せない**(ピックの対象に足さない)。
 */
import * as THREE from 'three'

import { makeTextSprite } from './textSprite'
import { OPTIMIZE_FIXTURE_MARK_COLOR, OPTIMIZE_LOAD_COLOR } from './viewportConstants'
import type { FaceRef, OptimizeVisualState } from './viewportTypes'

export interface OptimizeAnnotationsDeps {
  /** いま画面にある面メッシュ。**`update()` のたびに作り直される**ので毎回読み直す。 */
  /** その面の頂点位置と頂点法線(xyz の並び。見つからなければ null)。 */
  faceVertices(bodyId: string, faceIndex: number): { positions: ArrayLike<number>; normals: ArrayLike<number> | null } | null
  /** 内容の外接直方体の対角[mm]。装飾の大きさをこれへ追随させる。 */
  contentDiagonal(): number
}

export interface OptimizeAnnotations {
  /** 装飾を入れるグループ。`scene` へ足すのは呼び出し側。 */
  object: THREE.Group
  clear(): void
  /** 拘束記号・荷重矢印+ラベルを作り直す。`clear()` を先に呼ぶこと。`contentDiagonal()` が0なら文書が空なので何も描けない。 */
  draw(state: OptimizeVisualState): void
  count(): number
  /** いま描いている装飾の中身(実機検査フック専用)。 */
  info(): {
    fixtures: FaceRef[]
    loads: Array<{ bodyId: string; faceIndex: number; direction: [number, number, number]; label: string }>
  }
}

export function createOptimizeAnnotations(deps: OptimizeAnnotationsDeps): OptimizeAnnotations {
  const optimizeRoot = new THREE.Group()
  let optimizeDisposables: Array<{ dispose(): void }> = []

  /**
   * いま描いている装飾の中身(実機検査フック専用)。子の総数だけでは拘束と荷重の取り違え・矢印の逆向き・空ラベルを
   * 検出できないので、種別ごとの中身を控えて `optimizeAnnotations()` で読めるようにする。
   */
  let optimizeAnnotationInfo: {
    fixtures: FaceRef[]
    loads: Array<{ bodyId: string; faceIndex: number; direction: [number, number, number]; label: string }>
  } = { fixtures: [], loads: [] }

  function clearOptimizeAnnotations(): void {
    optimizeRoot.clear()
    for (const item of optimizeDisposables) item.dispose()
    optimizeDisposables = []
    optimizeAnnotationInfo = { fixtures: [], loads: [] }
  }

  /**
   * 面の重心と平均法線を、いま実際に描かれているメッシュから測る(サーバの `FaceRef` は添字だけ持つ)。
   * 装飾専用の簡易測定で、`duocad/optimize.py` の指紋(面積加重・照合用)とは別物。
   */
  function faceCentroidNormal(
    bodyId: string,
    faceIndex: number,
  ): { center: THREE.Vector3; normal: THREE.Vector3 } | null {
    const vertices = deps.faceVertices(bodyId, faceIndex)
    if (vertices === null || vertices.positions.length < 3) return null
    const { positions, normals } = vertices
    const center = new THREE.Vector3()
    const normal = new THREE.Vector3()
    for (let i = 0; i + 2 < positions.length; i += 3) {
      center.x += positions[i]
      center.y += positions[i + 1]
      center.z += positions[i + 2]
      if (normals) {
        normal.x += normals[i]
        normal.y += normals[i + 1]
        normal.z += normals[i + 2]
      }
    }
    center.divideScalar(positions.length / 3)
    if (normal.lengthSq() < 1e-12) normal.set(0, 0, 1)
    else normal.normalize()
    return { center, normal }
  }

  /** 荷重の大きさのラベル(canvasテクスチャの札)。`height` は呼び出し側が `contentDiagonal` から決める。 */
  function makeMagnitudeLabel(
    text: string,
    height: number,
  ): { sprite: THREE.Sprite; disposables: Array<{ dispose(): void }> } {
    return makeTextSprite(text, height, '#ffc857')
  }

  function draw(state: OptimizeVisualState): void {
    const contentDiagonal = deps.contentDiagonal()
    const scale = Math.max(contentDiagonal, 1e-6)
    if (scale <= 1e-6) return // 空の文書(装飾できる面が無い)

    // -------------------------------------------------- 拘束の固定記号(四角錐)
    if (state.fixtures.length > 0) {
      const markSize = scale * 0.03
      const markMaterial = new THREE.MeshLambertMaterial({ color: OPTIMIZE_FIXTURE_MARK_COLOR })
      optimizeDisposables.push(markMaterial)
      for (const ref of state.fixtures) {
        const measured = faceCentroidNormal(ref.bodyId, ref.faceIndex)
        if (!measured) continue
        // **4角錐**(`radialSegments=4`)。既定は+Y軸を高さの向きに持つので、面へ刺さる向き
        // (法線の逆)へ回してから、根元が面の少し上に来るよう法線方向へ持ち上げる。
        const geometry = new THREE.ConeGeometry(markSize * 0.6, markSize, 4)
        optimizeDisposables.push(geometry)
        const cone = new THREE.Mesh(geometry, markMaterial)
        cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), measured.normal.clone().negate())
        cone.position.copy(measured.center).addScaledVector(measured.normal, markSize * 0.6)
        optimizeRoot.add(cone)
        optimizeAnnotationInfo.fixtures.push({ bodyId: ref.bodyId, faceIndex: ref.faceIndex })
      }
    }

    // -------------------------------------------------- 荷重の矢印+ラベル
    if (state.loads.length > 0) {
      const labelHeight = scale * 0.05
      // 大きさと向きは面ごとに引く(ループの外で使い回すと、違う荷重を置いても全部同じ矢印になる)。
      for (const ref of state.loads) {
        const { magnitude, direction, vector } = ref
        // 長さは大きさの対数スケール。1N未満でも0にならないよう `Math.max(magnitude, 1)` で下駄を履かせる(見た目専用)。
        const length = scale * 0.12 * (1 + Math.log10(Math.max(magnitude, 1)) * 0.3)
        const labelText = `${Number(magnitude.toFixed(2))}N`
        const measured = faceCentroidNormal(ref.bodyId, ref.faceIndex)
        if (!measured) continue
        const dir =
          direction === 'vector' && vector !== null
            ? new THREE.Vector3(vector[0], vector[1], vector[2]).normalize()
            : direction === '-normal'
              ? measured.normal.clone().negate()
              : measured.normal.clone()
        // **押す向き(面の内側へ向く荷重)は、矢の先を面に当てて外側から描く。** 面から荷重の向きへ
        // 伸ばすと矢が固体の中に入って深度テストで隠れ、薄い部品では反対の面から突き出て
        // そちらへの荷重に見える。引く向きは面から外へ伸ばす。
        const pushes = dir.dot(measured.normal) < 0
        const tail = pushes ? measured.center.clone().addScaledVector(dir, -length) : measured.center.clone()
        const arrow = new THREE.ArrowHelper(dir, tail, length, OPTIMIZE_LOAD_COLOR, length * 0.3, length * 0.18)
        optimizeRoot.add(arrow)
        optimizeDisposables.push(arrow) // `ArrowHelper.dispose()` が線・矢頭の材質を片付ける
        const { sprite, disposables } = makeMagnitudeLabel(labelText, labelHeight)
        // ラベルは面から遠い側の端(引く向きは矢の先の先、押す向きは矢の根元の手前)。
        sprite.position.copy(measured.center).addScaledVector(dir, pushes ? -length * 1.15 : length * 1.15)
        optimizeRoot.add(sprite)
        optimizeDisposables.push(...disposables)
        optimizeAnnotationInfo.loads.push({
          bodyId: ref.bodyId,
          faceIndex: ref.faceIndex,
          direction: [dir.x, dir.y, dir.z],
          label: labelText,
        })
      }
    }
  }

  return {
    object: optimizeRoot,
    clear: clearOptimizeAnnotations,
    draw,
    count() {
      return optimizeRoot.children.length
    },
    info() {
      return {
        fixtures: optimizeAnnotationInfo.fixtures.map((ref) => ({ ...ref })),
        loads: optimizeAnnotationInfo.loads.map((item) => ({
          ...item,
          direction: [...item.direction] as [number, number, number],
        })),
      }
    },
  }
}
