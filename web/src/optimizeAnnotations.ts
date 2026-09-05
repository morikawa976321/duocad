/**
 * 形状最適化(第3のモード)の装飾 —— 拘束の固定記号(四角錐)と、荷重の矢印+大きさのラベル。
 * **指せない**(ピックの対象に足さない)。
 */
import * as THREE from 'three'

import { OPTIMIZE_FIXTURE_MARK_COLOR, OPTIMIZE_LOAD_COLOR } from './viewportConstants'
import type { FaceRef, OptimizeVisualState } from './viewportTypes'

/** 本編(`viewport.ts`)から借りるもの。 */
export interface OptimizeAnnotationsDeps {
  /** いま画面にある面メッシュ。**`update()` のたびに作り直される**ので毎回読み直す。 */
  faceMeshes(): THREE.Mesh[]
  /** 内容の外接直方体の対角[mm]。装飾の大きさをこれへ追随させる。 */
  contentDiagonal(): number
}

export interface OptimizeAnnotations {
  /** 装飾を入れるグループ。`scene` へ足すのは呼び出し側。 */
  object: THREE.Group
  /** 拘束記号・荷重矢印+ラベルを全部消す。 */
  clear(): void
  /**
   * 拘束記号・荷重矢印+ラベルを作り直す。**`clear()` を先に呼ぶこと**。
   *
   * 拘束面は固定記号(四角錐)を法線の上に置き、荷重面は矢印(`THREE.ArrowHelper`。長さは
   * 大きさの対数スケール)+ 大きさのラベルを描く。どちらも**モデルの大きさへ追随**させる
   * (`contentDiagonal()` が0なら文書が空なので何も描けない)。
   */
  draw(state: OptimizeVisualState): void
  /** 子オブジェクトの総数(実機検査フック専用)。 */
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
   * いま描いている装飾の**中身**(実機検査フック専用)。
   *
   * `optimizeAnnotationCount()`(子オブジェクトの総数)だけでは、拘束記号と荷重矢印を
   * 取り違えても・矢印が逆向きでも・ラベルが空文字でも数だけは合ってしまい検出できない
   * (2026-08-31 QA指摘)。ここに種別ごとの中身(拘束は対象の面、荷重は単位ベクトルと
   * ラベル文字列)を控えておき、`optimizeAnnotations()` で読めるようにする。
   */
  let optimizeAnnotationInfo: {
    fixtures: FaceRef[]
    loads: Array<{ bodyId: string; faceIndex: number; direction: [number, number, number]; label: string }>
  } = { fixtures: [], loads: [] }

  /** 拘束記号・荷重矢印+ラベルを全部消す。`setOptimizeState` が最初に呼ぶ。 */
  function clearOptimizeAnnotations(): void {
    optimizeRoot.clear()
    for (const item of optimizeDisposables) item.dispose()
    optimizeDisposables = []
    optimizeAnnotationInfo = { fixtures: [], loads: [] }
  }

  /**
   * 面の重心と平均法線を、**いま実際に描かれているメッシュ**から測る。
   *
   * サーバの配りもの(`FaceRef`)は添字だけで重心・法線を持たない。ここは装飾専用の
   * 簡易測定であって `duocad/optimize.py` の指紋(面積加重・照合用)とは別物 ——
   * 頂点の単純平均でよい(タスク仕様「あとから直すのは安い。凝りすぎない」)。
   */
  function faceCentroidNormal(
    bodyId: string,
    faceIndex: number,
  ): { center: THREE.Vector3; normal: THREE.Vector3 } | null {
    const allFaceMeshes = deps.faceMeshes()
    const mesh = allFaceMeshes.find(
      (candidate) => candidate.userData.bodyId === bodyId && candidate.userData.faceIndex === faceIndex,
    )
    if (!mesh) return null
    const position = mesh.geometry.getAttribute('position') as THREE.BufferAttribute
    const normalAttr = mesh.geometry.getAttribute('normal') as THREE.BufferAttribute
    if (!position || position.count === 0) return null
    const center = new THREE.Vector3()
    const normal = new THREE.Vector3()
    for (let i = 0; i < position.count; i++) {
      center.x += position.getX(i)
      center.y += position.getY(i)
      center.z += position.getZ(i)
      if (normalAttr) {
        normal.x += normalAttr.getX(i)
        normal.y += normalAttr.getY(i)
        normal.z += normalAttr.getZ(i)
      }
    }
    center.divideScalar(position.count)
    if (normal.lengthSq() < 1e-12) normal.set(0, 0, 1)
    else normal.normalize()
    return { center, normal }
  }

  /** 荷重の大きさのラベル(canvasテクスチャの札)。**モデルの大きさへ追随**させる
   *  (`height` は呼び出し側が `contentDiagonal` から決める)。 */
  function makeMagnitudeLabel(
    text: string,
    height: number,
  ): { sprite: THREE.Sprite; disposables: Array<{ dispose(): void }> } {
    const canvas = document.createElement('canvas')
    canvas.width = 256
    canvas.height = 96
    const ctx = canvas.getContext('2d')
    if (ctx) {
      ctx.fillStyle = 'rgba(16, 22, 30, 0.85)'
      ctx.fillRect(0, 0, canvas.width, canvas.height)
      ctx.fillStyle = '#ffc857'
      ctx.font = 'bold 48px sans-serif'
      ctx.textBaseline = 'middle'
      ctx.fillText(text, 12, canvas.height / 2)
    }
    const texture = new THREE.CanvasTexture(canvas)
    const material = new THREE.SpriteMaterial({ map: texture, depthTest: false, depthWrite: false, transparent: true })
    const sprite = new THREE.Sprite(material)
    sprite.scale.set(height * (canvas.width / canvas.height), height, 1)
    return { sprite, disposables: [texture, material] }
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
    if (state.load !== null && state.loads.length > 0) {
      const { magnitude, direction, vector } = state.load
      // **長さは大きさの対数スケール**(タスク仕様)。1N未満でも長さが0にならないよう
      // `Math.max(magnitude, 1)` で下駄を履かせる(見た目専用の式で、正確な力学ではない)。
      const length = scale * 0.12 * (1 + Math.log10(Math.max(magnitude, 1)) * 0.3)
      const labelHeight = scale * 0.05
      const labelText = `${Number(magnitude.toFixed(2))}N`
      for (const ref of state.loads) {
        const measured = faceCentroidNormal(ref.bodyId, ref.faceIndex)
        if (!measured) continue
        const dir =
          direction === 'vector' && vector !== null
            ? new THREE.Vector3(vector[0], vector[1], vector[2]).normalize()
            : direction === '-normal'
              ? measured.normal.clone().negate()
              : measured.normal.clone()
        const arrow = new THREE.ArrowHelper(dir, measured.center, length, OPTIMIZE_LOAD_COLOR, length * 0.3, length * 0.18)
        optimizeRoot.add(arrow)
        optimizeDisposables.push(arrow) // `ArrowHelper.dispose()` が線・矢頭の材質を片付ける
        const { sprite, disposables } = makeMagnitudeLabel(labelText, labelHeight)
        sprite.position.copy(measured.center).addScaledVector(dir, length * 1.15)
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
