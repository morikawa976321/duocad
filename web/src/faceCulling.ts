/**
 * 面単位の視錐台・バックフェイス判定 — 画面側だけが持つ「どの面を細かく測るか」の入力。
 *
 * 可変解像度の目標精度は文書全体で1つなので、大きなアセンブリに深く寄ると画面外の部品まで同じ細かさで
 * 測り直す(27部品2835面で三角形118万・往復14.5秒)。ここで絞った面添字だけがサーバの
 * `set_view_resolution.focus` に載る(送らなければ全面が細かい側)。
 *
 * オクルージョン(他部品による遮蔽)は判定しない(依頼者の明示的な決定)。視錐台への包含とバックフェイスだけで、
 * どちらも保守的に「細かい側」へ倒す: 粗い側へ倒す誤りは画面に角が出る。
 */
import * as THREE from 'three'
import type { GeometryPayload } from './viewport'

/** 背を向けていると見なす内積のしきい値。0より大きくして保守的に倒す(真横を向いた面を粗くしない)。 */
export const BACKFACE_MARGIN = 0.15

export interface FaceSample {
  /** 1始まりの面添字(サーバの `faces[i].index` そのまま)。 */
  index: number
  /** 三角形の節点から作った世界座標のバウンディングボックス。`slack` だけ膨らませてある(`BodySamples.slack`)。 */
  box: THREE.Box3
  centroid: THREE.Vector3
  normal: THREE.Vector3
  /** 平面か(`kind === 'plane'`)。平面のfaceにだけバックフェイス判定を使う(曲面は「中央の法線」が面全体を代表しない)。 */
  planar: boolean
  /** いま届いている三角形が細かい側で測られたものか(サーバの `detail`)。ヒステリシスが見る。 */
  wasFine: boolean
}

export interface BodySamples {
  /** 閉じた立体か(payload の `closed`)。開いたシェルでは裏向きの面が本当に見えるので、falseならバックフェイス判定を使わない。 */
  closed: boolean
  /**
   * 箱を膨らませた量[mm](= 粗い側の偏差)。判定を測り方から独立させる。
   * 三角形は曲面の内側に張るので、粗く測った箱は細かく測った箱より小さい。生の箱で判定すると
   * 境目の面で測り直しが往復し続けるので、偏差ぶん膨らませて粗い箱が細かい箱を覆うようにする。
   */
  slack: number
  faces: FaceSample[]
}

export function sampleFaces(payload: GeometryPayload): BodySamples {
  // 粗い側の偏差が分からない古いサーバでは、届いた偏差を使う(0にはしない)。
  const slack = Math.max(0, payload.coarse_deviation ?? payload.deviation ?? 0)
  const faces: FaceSample[] = []
  for (const face of payload.faces) {
    const box = new THREE.Box3()
    // Vector3 を1点ずつ作らない(27部品の初回配信で数十万点を通り、GCが走る。`expandByPoint` は座標しか見ない)。
    const positions = face.positions
    if (face.indices.length > 0 && positions.length >= 3) {
      let minX = Infinity
      let minY = Infinity
      let minZ = Infinity
      let maxX = -Infinity
      let maxY = -Infinity
      let maxZ = -Infinity
      for (let i = 0; i + 2 < positions.length; i += 3) {
        const x = positions[i]
        const y = positions[i + 1]
        const z = positions[i + 2]
        if (x < minX) minX = x
        if (y < minY) minY = y
        if (z < minZ) minZ = z
        if (x > maxX) maxX = x
        if (y > maxY) maxY = y
        if (z > maxZ) maxZ = z
      }
      box.min.set(minX - slack, minY - slack, minZ - slack)
      box.max.set(maxX + slack, maxY + slack, maxZ + slack)
    }
    faces.push({
      index: face.index,
      box,
      centroid: new THREE.Vector3(face.centroid[0], face.centroid[1], face.centroid[2]),
      normal: new THREE.Vector3(face.normal[0], face.normal[1], face.normal[2]).normalize(),
      planar: face.kind === 'plane',
      // **古いサーバは `detail` を送らない** —— そのときは全面が細かい側で測られている。
      wasFine: face.detail !== 'coarse',
    })
  }
  return {
    // 古いサーバは `closed` を送らない。分からなければ裏判定は使わない
    // (開いたシェルの内側を粗くすると、そこは本当に見えているので角が出る)。
    closed: payload.closed ?? false,
    slack,
    faces,
  }
}

/**
 * 細かく測るべき面の添字(1始まり・昇順)。全部細かいときは `null`(= そのBodyの指定を送らない)。
 *
 * @param samples   `sampleFaces` の結果
 * @param frustum   カメラの視錐台
 * @param viewpoint 透視投影ならカメラの世界位置、正投影なら `null`
 * @param viewDirection カメラの向き(正規化済み。正投影のバックフェイス判定に使う)
 * @param hysteresis いま細かい面が粗い側へ落ちるまでの余裕[mm]。0だと視錐台の縁の面が入/出を往復し、
 *   そのたびに測り直しがサーバへ飛ぶ。落ちるのは縁から `hysteresis` 外へ出たときだけ、戻るのは触れた瞬間。
 */
export function selectFineFaces(
  samples: BodySamples,
  frustum: THREE.Frustum,
  viewpoint: THREE.Vector3 | null,
  viewDirection: THREE.Vector3,
  hysteresis = 0,
): number[] | null {
  const fine: number[] = []
  const padded = new THREE.Box3()
  const toFace = new THREE.Vector3()
  const margin = Math.max(0, hysteresis)
  for (const face of samples.faces) {
    // 三角形が取れなかった面は保守的に細かい側(箱が無いので視錐台判定もできない)。
    if (face.box.isEmpty()) {
      fine.push(face.index)
      continue
    }
    let box = face.box
    if (face.wasFine && margin > 0) {
      padded.copy(face.box)
      padded.min.subScalar(margin)
      padded.max.addScalar(margin)
      box = padded
    }
    if (!frustum.intersectsBox(box)) continue
    // **閉じた立体の平面にだけ**裏判定を使う(それ以外は本当に見えている可能性がある)。
    if (samples.closed && face.planar) {
      if (viewpoint !== null) {
        toFace.copy(face.centroid).sub(viewpoint).normalize()
      } else {
        toFace.copy(viewDirection)
      }
      if (face.normal.dot(toFace) > BACKFACE_MARGIN) continue
    }
    fine.push(face.index)
  }
  return fine.length === samples.faces.length ? null : fine
}
