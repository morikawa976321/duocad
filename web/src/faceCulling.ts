/**
 * 面単位の視錐台・バックフェイス判定 — **画面側だけが持つ**「どの面を細かく測るか」の入力。
 *
 * ズーム連動の可変解像度(`adaptiveResolution.ts`)は目標精度が**文書全体で1つ**だったため、
 * 大きなアセンブリに深く寄ると画面外の部品まで同じ細かさで測り直す(27部品2835面で
 * 三角形118万・往復14.5秒。engine [[one-global-deviation-costs-on-large-assemblies]])。
 * **ここで絞った面添字だけが**サーバの `set_view_resolution.focus` に載る ——
 * 送らなければ従来どおり全面が細かい側になる。
 *
 * **オクルージョン(他部品による遮蔽)は判定しない**(依頼者の明示的な決定)。
 * 視錐台への包含とバックフェイスだけで、どちらも**保守的に「細かい側」へ倒す**:
 * 迷ったら細かく測る(遅くなるだけ)。粗い側へ倒す誤りは**画面に角が出る**ので質が違う。
 */
import * as THREE from 'three'
import type { GeometryPayload } from './viewport'

/** 背を向けていると見なす内積のしきい値。**0より大きくして保守的に倒す**
 *  (真横を向いた面をうっかり粗くしない)。 */
export const BACKFACE_MARGIN = 0.15

export interface FaceSample {
  /** 1始まりの面添字(サーバの `faces[i].index` そのまま)。 */
  index: number
  /**
   * その面の三角形の節点から作った世界座標のバウンディングボックス。
   * **`slack` だけ膨らませてある**(下記 `BodySamples.slack`)。
   */
  box: THREE.Box3
  /** 面の重心(世界座標)。 */
  centroid: THREE.Vector3
  /** 面の外向き単位法線。 */
  normal: THREE.Vector3
  /** 平面か(`kind === 'plane'`)。**平面のfaceにだけバックフェイス判定を使う** ——
   *  円筒などの曲面は「中央の法線」が面全体を代表しないので、背を向けた側だけを見て
   *  粗くすると手前側まで粗くなる。 */
  planar: boolean
  /** **いま届いている三角形が細かい側で測られたものか**(サーバの `detail`)。
   *  ヒステリシス(下記 `selectFineFaces`)がこれを見る。 */
  wasFine: boolean
}

export interface BodySamples {
  /** 閉じた立体か(payload の `closed`)。**開いたシェルでは裏を向いた面が本当に見える**ので
   *  false のときはバックフェイス判定を一切使わない。 */
  closed: boolean
  /**
   * 箱を膨らませた量[mm](= 粗い側の偏差)。**判定を測り方から独立させるためにある。**
   *
   * 三角形は曲面の内側に張るので、**同じ面でも粗く測った箱は細かく測った箱より小さい**。
   * 生の箱で判定すると「粗いから視錐台の外 → 粗いまま」「細かいから中 → 細かいまま」の
   * 両方が安定してしまい、境目の面で**測り直しが往復し続ける**(サーバへの再要求が
   * 止まらない)。偏差ぶん膨らませておけば、粗い箱が細かい箱を覆う。
   */
  slack: number
  faces: FaceSample[]
}

/** サーバから届いた geometry から、可視判定に要る値だけを取り出す(1度だけ作って使い回す)。 */
export function sampleFaces(payload: GeometryPayload): BodySamples {
  // 粗い側の偏差が分からない古いサーバでは、届いた偏差を使う(0にはしない)。
  const slack = Math.max(0, payload.coarse_deviation ?? payload.deviation ?? 0)
  const faces: FaceSample[] = []
  for (const face of payload.faces) {
    const box = new THREE.Box3()
    // **Vector3 を1点ずつ作らない** —— 27部品の初回配信で数十万点を通るので、
    // ここで確保すると配信のたびにGCが走る(`expandByPoint` は座標しか見ない)。
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
 * 細かく測るべき面の添字(1始まり・昇順)。**全部細かいときは `null`** を返す
 * (`null` は「そのBodyの指定を送らない」= 全面細かい、を意味する)。
 *
 * @param samples   `sampleFaces` の結果
 * @param frustum   カメラの視錐台
 * @param viewpoint 透視投影ならカメラの世界位置、正投影なら `null`
 * @param viewDirection カメラの向き(正規化済み。正投影のバックフェイス判定に使う)
 * @param hysteresis **いま細かい面が粗い側へ落ちるまでの余裕[mm]**。0にすると、
 *   視錐台の縁にある面が「入った/出た」を往復し、そのたびに測り直しがサーバへ飛ぶ。
 *   落ちるのは縁から `hysteresis` だけ外へ出たときだけ、戻るのは触れた瞬間、と
 *   **向きで条件を変える**のがヒステリシスである。
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
      // 法線が視線と同じ向き = カメラに背を向けている → 粗い側。
      if (face.normal.dot(toFace) > BACKFACE_MARGIN) continue
    }
    fine.push(face.index)
  }
  // 面は添字の昇順で届くので、ここで作った配列も昇順である(サーバ側の比較が楽になる)。
  return fine.length === samples.faces.length ? null : fine
}
