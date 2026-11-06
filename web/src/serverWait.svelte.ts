/**
 * ビューポート上端の待ちバーを出すかどうか(issue #112)。
 *
 * 数えるのは「人が頼み、結果がビューポートに出るまで待たせている」操作だけ。
 * WebSocket の待ちは `session.ts` が `ack_id` で追い、App がファイル本文の送信と合わせて最古の開始時刻を渡す。
 */

/** これより短く済む待ちには出さない(選択のクリックなど数msの往復でちらつかせない)。 */
export const SHOW_AFTER_MS = 500
/** 一度出したら最低これだけ出しておく(0.5秒をわずかに超えた待ちで一瞬だけ光らせない)。 */
export const MIN_VISIBLE_MS = 300

export interface ServerWait {
  readonly visible: boolean
  /** いちばん古い待ちの送信時刻(`performance.now()`)。待ちが無ければ null。 */
  update(oldestSentAt: number | null): void
  dispose(): void
}

export function createServerWait(): ServerWait {
  let visible = $state(false)
  let since: number | null = null
  let shownAt = 0
  let timer: ReturnType<typeof setTimeout> | null = null

  function settle(): void {
    if (timer !== null) clearTimeout(timer)
    timer = null
    const now = performance.now()
    if (since !== null) {
      if (visible) return
      const left = since + SHOW_AFTER_MS - now
      if (left <= 0) {
        visible = true
        shownAt = now
      } else {
        timer = setTimeout(settle, left)
      }
      return
    }
    if (!visible) return
    const left = shownAt + MIN_VISIBLE_MS - now
    if (left <= 0) visible = false
    else timer = setTimeout(settle, left)
  }

  return {
    get visible() {
      return visible
    },
    update(oldestSentAt) {
      since = oldestSentAt
      settle()
    },
    dispose() {
      if (timer !== null) clearTimeout(timer)
      timer = null
    },
  }
}
