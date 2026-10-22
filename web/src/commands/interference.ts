/**
 * 干渉チェック(issue #186)—— verb ではない問い合わせ。文書もログも変えない。
 * 起動すると引数なしでその場で走る(読み取り専用の即適用)。隙間(clearance)の欄は結果のパネルにあり、文書を開いて最初は 0、
 * 同じ文書のあいだは直前の値(`interference.svelte.ts`)。対象はコアの選択と可視性から決まる(選択なし=見えているボディ全部 /
 * 1つ=その1つ対ほか全部 / 2つ以上=選んだものどうし)ので、`build` が作る引数は無い。
 * 送るのは `{cmd:'check_interference', clearance, client_op}` で、返事は状態配信ではなく `{type:'interference', client_op, ...}`。
 */
import type { CommandDef } from './command'

export const INTERFERENCE: CommandDef<'check_interference'> = {
  id: 'check_interference',
  label: '干渉チェック',
  buttonId: 'interference',
  preview: 'none',
  args: [],
  build() {
    return { ok: true, args: {} }
  },
}

export type ClearanceParse = { ok: true; value: number } | { ok: false; reason: string }

/** 隙間の欄の文字列。空欄は 0(隙間検査なし)。0 以上の有限の数だけ通す(コアの `check_clearance` と同じ規則)。 */
export function parseClearance(text: string): ClearanceParse {
  const trimmed = text.trim()
  if (trimmed === '') return { ok: true, value: 0 }
  const value = Number(trimmed)
  if (!Number.isFinite(value)) return { ok: false, reason: `隙間は数(mm)で入れる: ${trimmed}` }
  if (value < 0) return { ok: false, reason: '隙間は 0 以上(mm)で入れる' }
  return { ok: true, value }
}
