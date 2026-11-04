/**
 * 画面の画素と世界を結ぶ写像の控えと、その比べ方。同じと見なせれば、前に引いた画面の矩形が同じものを囲む
 * (届くのを待たせた矩形選択を集め直してよいかの判定。`App.svelte` の `pendingBox`)。
 *
 * 行列の成分ではなく画素で比べる: 注視点のまわりの見本点を控えのときの写像と今の写像で画面へ写し、どれも 0.25 px 以内なら同じ。
 * - スケッチモードでは `OrbitControls` が止まっていても行列を毎フレーム 1e-8 ほど揺らし続ける(`viewport.ts` の
 *   `cameraMovedSinceRender` と同じ揺れ。画素では 1e-5 px ほど)。小数6桁の文字列で比べていたときは、0 の近くで揺れる成分が
 *   `-0.000000` と `0.000000` に分かれ、待たせた矩形選択が「視点が動いた」として捨てられていた(実測)。
 * - 成分の差の絶対値で比べると、正投影で上下 ±100 m を映すような投影行列の小さな成分では、5% のズームも同じと見てしまう。
 */

export interface ViewSnapshot {
  /** 見本点(世界座標 xyz の並び)。 */
  readonly points: readonly number[]
  /** 控えのときの見本点の画素(x, y の並び)。 */
  readonly pixels: readonly number[]
  readonly width: number
  readonly height: number
}

const TOLERANCE_PX = 0.25

/** 世界 → クリップの行列(列優先 16 成分)で点を画素へ写す。カメラの後ろ(w ≤ 0)は NaN(どれとも同じにならない)。 */
export function toPixels(worldToClip: ArrayLike<number>, points: readonly number[], width: number, height: number): number[] {
  const m = worldToClip
  const pixels: number[] = []
  for (let at = 0; at < points.length; at += 3) {
    const x = points[at], y = points[at + 1], z = points[at + 2]
    const w = m[3] * x + m[7] * y + m[11] * z + m[15]
    if (!(w > 0)) {
      pixels.push(NaN, NaN)
      continue
    }
    const nx = (m[0] * x + m[4] * y + m[8] * z + m[12]) / w
    const ny = (m[1] * x + m[5] * y + m[9] * z + m[13]) / w
    pixels.push(((nx + 1) / 2) * width, ((1 - ny) / 2) * height)
  }
  return pixels
}

/**
 * 見本点: 注視点と、カメラの右・上・奥の向きへ ±`reach`(見えている広さの半分ほど)。奥の向きの点があるので、
 * 注視点の面の写り方を保ったまま奥行きの写り方だけが変わる動き(寄りながら画角を広げる)も見分ける。
 * `axes` はカメラの `matrixWorld`(列優先 16 成分。0〜2 が右、4〜6 が上、8〜10 が後ろ)。
 */
export function probePoints(target: readonly number[], axes: ArrayLike<number>, reach: number): number[] {
  const points = [target[0], target[1], target[2]]
  for (const column of [0, 4, 8]) {
    for (const sign of [1, -1]) {
      points.push(
        target[0] + sign * reach * axes[column],
        target[1] + sign * reach * axes[column + 1],
        target[2] + sign * reach * axes[column + 2],
      )
    }
  }
  return points
}

export function snapshotView(worldToClip: ArrayLike<number>, points: readonly number[], width: number, height: number): ViewSnapshot {
  return { points: [...points], pixels: toPixels(worldToClip, points, width, height), width, height }
}

/** 控えの見本点を今の写像で写し直し、どれも `TOLERANCE_PX` 以内か。画面の寸法が違えば違う。 */
export function sameView(snapshot: ViewSnapshot, worldToClip: ArrayLike<number>, width: number, height: number): boolean {
  if (snapshot.width !== width || snapshot.height !== height) return false
  const now = toPixels(worldToClip, snapshot.points, width, height)
  for (let i = 0; i < now.length; i += 1) {
    if (!(Math.abs(now[i] - snapshot.pixels[i]) <= TOLERANCE_PX)) return false
  }
  return true
}
