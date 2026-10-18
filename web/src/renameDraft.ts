/**
 * 名前変更で送る名前。前後の空白を落とす: IME の確定やコピペで末尾に空白が付きやすく、サーバの rename は
 * 前後に空白のある名前を英語のエラーで断る(`duocad/verbs/rename.py`)。落としたあとで空、または今と同じなら `null`(送らず閉じる)。
 */
export function renameTarget(draft: string, current: string): string | null {
  const next = draft.trim()
  return next === '' || next === current ? null : next
}
