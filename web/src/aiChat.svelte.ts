/**
 * アプリ内AIエージェントとの会話の持ち主。会話の時系列はコアには無く、リロードで消える(`AiPanel.svelte` 参照)。
 * 打ちかけの依頼文(`draft`)もここに置く: パネルは右サイドバーごと `{#if}` で消えるので、パネルに持たせると閉じた瞬間に消える。
 */
import { hiddenResultNotice } from './aiResult'
import type { AiRunResult, AiStatus, AiToolCall, AiTranscriptEntry } from './session'

export interface AiChat {
  readonly status: AiStatus | null
  readonly transcript: AiTranscriptEntry[]
  draft: string
  onStatus(next: AiStatus): void
  onToolCall(call: AiToolCall): void
  onResult(result: AiRunResult): void
  onError(message: string): void
  send(text: string): void
}

export function createAiChat(deps: {
  send: (text: string) => void
  fail: (message: string) => void
  inform: (message: string) => void
  /** パネルが画面に出ていないか(右サイドバーを閉じている)。 */
  hidden: () => boolean
}): AiChat {
  let status = $state<AiStatus | null>(null)
  let transcript = $state<AiTranscriptEntry[]>([])
  let draft = $state('')

  function push(entry: AiTranscriptEntry): void {
    transcript = [...transcript, entry]
  }

  return {
    get status() {
      return status
    },
    get transcript() {
      return transcript
    },
    get draft() {
      return draft
    },
    set draft(next: string) {
      draft = next
    },
    onStatus(next) {
      status = next
      // 繋がらない理由(`ai_configure` の拒否)を黙って捨てない。
      if (next.error) deps.fail(`AI設定: ${next.error}`)
      const last = transcript.at(-1)
      if (next.conversationReset && last !== undefined && last.kind !== 'reset') push({ kind: 'reset' })
    },
    onToolCall(call) {
      push({ kind: 'tool', call })
    },
    onResult(result) {
      push({ kind: 'result', result })
      if (deps.hidden()) deps.inform(hiddenResultNotice(result))
    },
    onError(message) {
      push({ kind: 'error', message })
      // 会話欄に出ているときは二重に出さない。
      if (deps.hidden()) deps.fail(`AI ${message}`)
    },
    send(text) {
      push({ kind: 'user', text })
      deps.send(text)
    },
  }
}
