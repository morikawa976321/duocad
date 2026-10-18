/**
 * 描画ループ。1フレームの処理(`step`)が例外を投げても、次のフレームは必ず予約する。
 *
 * 次のフレームを `step` の後で予約すると、アプリ側のリスナーや描き直しが1回投げただけで予約されず、3Dビューが以後描かれなくなる。
 * 先に予約し、例外は握りつぶさず報告する。続けて投げ続けるときは最初の1回だけ報告する(毎フレームの洪水を避ける)。
 */
export interface FrameLoopDeps {
  request(callback: () => void): number
  cancel(handle: number): void
  report(error: unknown): void
}

export interface FrameLoop {
  /** 止める。予約済みのフレームも取り消し、以後 `step` は呼ばれない。 */
  stop(): void
}

export function startFrameLoop(step: () => void, deps: FrameLoopDeps): FrameLoop {
  let alive = true
  let failing = false
  let handle = 0

  function frame(): void {
    if (!alive) return
    handle = deps.request(frame)
    try {
      step()
      failing = false
    } catch (error) {
      if (!failing) deps.report(error)
      failing = true
    }
  }

  handle = deps.request(frame)
  return {
    stop() {
      alive = false
      deps.cancel(handle)
    },
  }
}
