/**
 * 画面の画素と世界を結ぶ写像(カメラの姿勢・投影・画面の寸法)の控えと、その比べ方。同じと見なせれば、前に引いた画面の矩形が
 * 同じものを囲む(届くのを待たせた矩形選択を集め直してよいかの判定。`App.svelte` の `pendingBox`)。
 *
 * 行列は成分ごとに 1e-6 まで同じと見る: スケッチモードでは `OrbitControls` が止まっていても行列を毎フレーム 1e-8 ほど
 * 揺らし続ける(`viewport.ts` の `cameraMovedSinceRender` と同じ揺れ)。小数6桁の文字列で比べていたときは、0 の近くで揺れる
 * 成分が `-0.000000` と `0.000000` に分かれ、待たせた矩形選択が「視点が動いた」として捨てられていた(実測)。
 */

export interface ViewSnapshot {
  readonly view: readonly number[]
  readonly projection: readonly number[]
  readonly width: number
  readonly height: number
}

const TOLERANCE = 1e-6

export function snapshotView(
  view: ArrayLike<number>,
  projection: ArrayLike<number>,
  width: number,
  height: number,
): ViewSnapshot {
  return { view: Array.from(view), projection: Array.from(projection), width, height }
}

function close(a: readonly number[], b: readonly number[]): boolean {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i += 1) {
    if (!(Math.abs(a[i] - b[i]) <= TOLERANCE)) return false
  }
  return true
}

export function sameView(a: ViewSnapshot, b: ViewSnapshot): boolean {
  return a.width === b.width && a.height === b.height && close(a.view, b.view) && close(a.projection, b.projection)
}
