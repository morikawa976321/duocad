/** 状態配信の操作ログ(`ServerState.log`)を前後で比べる。Svelte に依存しない純粋な部分。 */

type LogEntry = { verb: string; args: Record<string, unknown> }

/**
 * `after` で新しく積まれた操作(共通の先頭より後ろ)。undo で外れた操作は `before` 側にだけ残るので数えない。
 * `seq` は位置の番号で、undo の後に積むと同じ番号が別の操作に振られる(`duocad/oplog.py` の `append`)ので比べない。
 */
export function appendedEntries<T extends LogEntry>(before: readonly T[], after: readonly T[]): T[] {
  let common = 0
  while (
    common < before.length &&
    common < after.length &&
    before[common].verb === after[common].verb &&
    JSON.stringify(before[common].args) === JSON.stringify(after[common].args)
  ) {
    common += 1
  }
  return after.slice(common)
}
