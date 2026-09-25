/**
 * ボディの幽霊(確定前の姿)。2種類を1つの層に持つ:
 *
 * - **サーバの投機実行**(`showPreview`): 「別の形」。本物を隠して幽霊を出す
 *   (重ねると半透明の中に古い形が透けて読めない)。
 * - **ローカル幽霊**(`showBodyGhosts`): 「同じ形の別の場所」。届いている生メッシュに行列を
 *   掛けるだけでサーバへ投げない。本物は隠さない(隠すと移動前と移動後を見比べられない)。
 *
 * スケッチの幽霊はここに無い(スケッチの枠と線の組み立てを読むので本編のスケッチの節)。
 */
import * as THREE from 'three'

import { createBatch, type SceneBatch } from './sceneBatch'
import {
  bodyGhostMatrix,
  usableRotation,
  type BodyGeometryUpdate,
  type BodyGhost,
  type FaceGeometry,
  type GeometryPayload,
} from './viewportTypes'

/** 本編(`viewport.ts`)から借りるもの。 */
export interface GhostLayerDeps {
  /** 幽霊を入れる親(本物と同じ `root`。断面・視錐台の扱いを本物と揃える)。 */
  parent: THREE.Object3D
  /** 半透明の幽霊の材質(本編が持ち、捨てるのも本編)。 */
  material: THREE.Material
  /**
   * 1面ぶんの `BufferGeometry`。**本物の `buildBody` と同じ関数を渡す** —— ここがずれると
   * 「プレビューと確定結果が違う形に見える」(本編 `faceGeometry` のdocstring)。
   */
  faceGeometry(face: FaceGeometry): THREE.BufferGeometry
  /** 直近に届いた本物の生メッシュ(非表示・未読込なら undefined)。 */
  rawGeometry(bodyId: string): GeometryPayload | undefined
  /** 本物の表示を切り替える(`optimizeDebug` も使うので本編が持つ)。 */
  setBodyVisible(bodyId: string, visible: boolean): void
  /** 「指せるもの」が変わったことを本編へ知らせる。 */
  pickChanged(): void
}

export interface GhostLayer {
  showPreview(entries: BodyGeometryUpdate[]): void
  showBodyGhosts(entries: BodyGhost[]): void
  /** 投機実行の幽霊を消して本物を戻す。**ローカル幽霊も一緒に消す**。 */
  clearPreview(): void
  /**
   * 幽霊の指示を掴む → 全部消す → `rebuild()` で本物を作り直す → 掴んだ指示でローカル幽霊を組み直す。
   * 本物の形が届いたとき(`update`)の口。
   */
  rebuildAround(rebuild: () => void): void
  /** `ghostInfo().bodies`(実機検査が読む)。 */
  info(): Array<BodyGhost & { matrix: number[] }>
}

export function createGhostLayer(deps: GhostLayerDeps): GhostLayer {
  const { parent, material, faceGeometry, rawGeometry, setBodyVisible, pickChanged } = deps

  /** body_id -> 投機実行の幽霊。 */
  const previewMeshes = new Map<string, SceneBatch>()

  /**
   * 幽霊(半透明の材質)のメッシュを面ごとに作って `target` へ足す。サーバの投機実行
   * (`showPreview`。本物を隠す)と、ローカル幽霊(`showBodyGhosts`。隠さずにグループへ行列を掛ける)が
   * 同じ組み立てを使う。本物(`buildBody`)は含めない(本編 `faceGeometry` のdocstring)。
   */
  function ghostMeshes(target: THREE.Object3D, faces: FaceGeometry[]): SceneBatch {
    const batch = createBatch(target)
    for (const face of faces) {
      if (face.indices.length === 0) continue // 三角形が取れなかった面(`buildBody` と同じ)
      const geometry = faceGeometry(face)
      batch.add(new THREE.Mesh(geometry, material), geometry)
    }
    return batch
  }

  function showPreview(entries: BodyGeometryUpdate[]): void {
    pickChanged() // 本物のBodyを隠すので、同じ画素が別のもの(または何も)を指す
    clearPreview()
    for (const entry of entries) {
      if (!entry.geometry) continue
      // 本物を隠して幽霊を出す(重ねると半透明の中に古い形が透けて読めない)
      setBodyVisible(entry.bodyId, false)
      previewMeshes.set(entry.bodyId, ghostMeshes(parent, entry.geometry.faces))
    }
  }

  /**
   * body_id -> ローカル幽霊(変換後の姿)。**`previewMeshes` とは別のMapである** ——
   * あちらはサーバが投機実行した「別の形」で本物を隠す。こちらは「同じ形の別の場所」で、
   * 本物を隠さない(隠すと移動前と移動後を見比べられない)。
   */
  const bodyGhosts = new Map<string, { group: THREE.Group; meshes: SceneBatch }>()
  /**
   * いま出しているローカル幽霊の指示そのもの。`info` が返す(実機検査が読む)ほかに、
   * **`rebuildAround` が「届いた形で組み直す」ための入力でもある** —— 検査用のデータではないので、
   * 消したり形を変えたりすると製品の挙動が変わる。
   *
   * **差し替え(`bodyGhostInfo = []`)で空にすること**(`clearBodyGhosts` の契約)。
   */
  let bodyGhostInfo: BodyGhost[] = []

  function showBodyGhosts(entries: BodyGhost[]): void {
    clearBodyGhosts()
    for (const entry of entries) {
      // **使えない回転は幽霊ごと落とす**(軸の向きが長さ0・値が非有限)。無視して
      // 平行移動だけ掛けると、元の位置にぴったり重なった幽霊が出て「回っていないのに
      // 幽霊は出ている」という嘘になる —— 描かないほうが正しい。
      if (entry.rotate && !usableRotation(entry.rotate)) continue
      // **届いている生メッシュをそのまま使う**(サーバへ投げない)。非表示・未読込の
      // ボディはここに無いので黙って落ちる —— 見えないものの幽霊を出す意味が無い。
      const payload = rawGeometry(entry.bodyId)
      if (!payload) continue
      const group = new THREE.Group()
      const meshes = ghostMeshes(group, payload.faces)
      if (meshes.objects.length === 0) continue
      // **変換はグループに掛ける**(面ごとに座標を書き換えない)。回転が入っても
      // 変わるのはここが行列になることだけで、生成・破棄・消える場面は `bodyGhosts` の
      // 側に閉じたままである(`BodyGhost` のdocstring)。
      group.matrixAutoUpdate = false // 位置ではなく行列を直に置くので自動再計算を切る
      group.matrix.copy(bodyGhostMatrix(entry))
      group.matrixWorldNeedsUpdate = true
      parent.add(group)
      bodyGhosts.set(entry.bodyId, { group, meshes })
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
    // **必ず新しい配列へ差し替える**(`splice` / `length = 0` で中身を空にしない)。
    // `rebuildAround` は消す前に `bodyGhostInfo` の参照を掴んで、消した後にそれで組み直す ——
    // 中身を空にする実装へ変えると掴んだ側も空になり、**型検査にも実行時エラーにも出ないまま**
    // 「無関係な再配信で幽霊が消えたまま戻らない」が再発する。
    bodyGhostInfo = []
    if (bodyGhosts.size === 0) return
    for (const entry of bodyGhosts.values()) {
      parent.remove(entry.group)
      entry.meshes.clear()
    }
    bodyGhosts.clear()
  }

  function clearPreview(): void {
    // **ローカル幽霊も一緒に消す**(interfaceの `clearPreview` のdocstring)。ここに
    // 入れてあるので、`update`/`prune`/`App.svelte` の中断は何も足さずに正しく消える。
    clearBodyGhosts()
    pickChanged() // 隠していた本物が戻る(`showPreview` の対)
    if (previewMeshes.size === 0) return
    for (const [bodyId, entry] of previewMeshes) {
      entry.clear()
      setBodyVisible(bodyId, true)
    }
    previewMeshes.clear()
  }

  function rebuildAround(rebuild: () => void): void {
    // **ローカル幽霊の指示だけ控える**(末尾で組み直す。`clearPreview()` は
    // `bodyGhostInfo` を新しい配列へ差し替えるので、掴んだこちらは巻き添えにならない)。
    const ghosts = bodyGhostInfo
    // 本物の形が届いた時点で、確定前の予想は用済み(古い幽霊を残さない)。
    clearPreview()
    rebuild()
    // **ローカル幽霊は「いま届いている本物をずらした姿」なので、消して終わりにせず
    // 届いたばかりの形で組み直す。** 上で消すのは正しい(古い形から作ったメッシュを
    // 残せば嘘の姿になる)が、消すだけだと**形の変わっていない再配信でも落ちたまま
    // 戻らない** —— カメラを動かすと可変解像度の `focus` が変わり、粗さが
    // `view.body_revision` の鍵に入っているので**幽霊が乗っているボディ自身が
    // 測り直されて届く**(2026-09-06実測: 右ドラッグ1回でそのボディの版が 2→3)。
    // 呼び直す側の `CommandHost` は「引数が変わっていない」と見て再発火しない
    // (`previewedKey`)ので、引数を打ち直すまで幽霊は戻らなかった。
    if (ghosts.length > 0) showBodyGhosts(ghosts)
  }

  return {
    showPreview,
    showBodyGhosts,
    clearPreview,
    rebuildAround,
    info() {
      return bodyGhostInfo.map((item) => ({
        bodyId: item.bodyId,
        offset: [...item.offset] as [number, number, number],
        rotate: item.rotate ?? null,
        // **グループに乗っている行列そのもの**を読む(`bodyGhostMatrix` を呼び直さない)
        // —— 組んだ行列を実際に掛け忘れていても気づけるようにするため。
        matrix: bodyGhosts.get(item.bodyId)?.group.matrix.toArray() ?? [],
      }))
    },
  }
}
