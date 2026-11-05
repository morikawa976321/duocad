/** AIパネルが `ai_result` の1件をどう文字にするか(`AiPanel.svelte`)。Svelte に依存しない純粋な部分。 */
import type { AiRunResult, ConnectionStatus } from './session'

/** 本文(`text`)が空のときに出す固定文。 */
const STATUS_LABEL: Record<string, string> = {
  completed: '完了',
  empty_reply: 'AIが結論を出さずに止まった(ここまでの操作は残っている。言い方を変えてもう一度どうぞ)',
  max_turns: '往復の上限に達した(続けるにはもう一度どうぞ)',
  cancelled: '中断した',
  max_tokens: '出力の上限に達した(このターンのツールは実行していない。もう一度どうぞ)',
  refusal: 'AIが断った',
  provider_error: '接続に失敗した',
  timeout: '時間切れで止めた(ここまでの操作は残っている)',
  context_overflow: 'この依頼だけでAIの文脈長を超えた(依頼を分けるか、llama-server の -c を増やす)',
  stopped: '止まった',
}

/**
 * `detail` が固定文に無い情報(接続できなかったURLとOSのエラー・文脈長の数値・停止理由の生値)を足す状態だけ。
 * ほかは `detail` が固定文と同じ内容(中断フラグ・時間切れ)か、使われない。
 */
const STATUSES_WITH_DETAIL = new Set(['provider_error', 'context_overflow', 'stopped'])

/** 理由が長くても会話欄を押し流さない上限[文字]。 */
const DETAIL_LIMIT = 240

/** 会話欄に出す1行。本文があればそれ、無ければ固定文。 */
export function resultLine(result: Pick<AiRunResult, 'status' | 'text'>): string {
  return result.text || STATUS_LABEL[result.status] || result.status
}

function fold(text: string, limit: number): string {
  const folded = text.replace(/\s+/g, ' ').trim()
  return folded.length > limit ? `${folded.slice(0, limit)}…` : folded
}

/** 固定文に添える失敗の理由(`ai_result.detail`)。出すものが無ければ空文字。 */
export function resultDetail(result: Pick<AiRunResult, 'status' | 'detail'>): string {
  if (!STATUSES_WITH_DETAIL.has(result.status)) return ''
  return fold(result.detail ?? '', DETAIL_LIMIT)
}

/**
 * 失敗したツール呼び出し(✗ の行)に添える理由。`ai_tool_call.result` は失敗なら `{"error": {...}}` の封筒
 * (`duocad/agent/loop.py` の `_failure` / `DocumentTools.error_envelope`)か `{"error": "文"}`。
 */
export function toolFailureReason(result: unknown): string {
  const error = typeof result === 'object' && result !== null ? (result as { error?: unknown }).error : undefined
  if (typeof error === 'string') return fold(error, DETAIL_LIMIT)
  if (typeof error === 'object' && error !== null) {
    const message = (error as { message?: unknown }).message
    if (typeof message === 'string' && message.trim()) return fold(message, DETAIL_LIMIT)
  }
  if (result === undefined || result === null) return ''
  return fold(typeof result === 'string' ? result : JSON.stringify(result), DETAIL_LIMIT)
}

/** 札は数枚が重なる(`Toast.svelte`)ので、会話欄より短く切る[文字]。 */
const NOTICE_LIMIT = 80

/** 右サイドバーを閉じている間に依頼が終わった・止まったときの札(`aiChat.svelte.ts`)。 */
export function hiddenResultNotice(result: Pick<AiRunResult, 'status' | 'text'>): string {
  return `AI: ${fold(resultLine(result), NOTICE_LIMIT)}`
}

/**
 * 入力を止める理由(送れるなら null)。繋がっていないときに送らせない: 送れなかった発話が会話欄に残り、
 * 打った文だけが入力欄から消える。`aiStatusKnown` はサーバから `ai_status` が届いたか(届くまで何に繋ぐか分からない)。
 */
export function inputBlockedReason(connection: ConnectionStatus, aiStatusKnown: boolean): string | null {
  if (connection !== 'open') return 'サーバに繋がっていない(繋ぎ直している)'
  return aiStatusKnown ? null : '接続を待っている'
}
