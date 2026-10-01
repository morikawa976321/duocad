/**
 * アプリ内AIエージェントとの会話の持ち主(設定の写しと、会話の時系列)。
 *
 * **会話の時系列はコアには無い**(`AiPanel.svelte` のdocstring)。届いた順にここへ積むだけで、
 * リロードすれば消える。
 */
import type { AiRunResult, AiStatus, AiToolCall, AiTranscriptEntry } from './session'

export interface AiChat {
  /** 今の設定(`ai_get_status` の返事・`ai_configure`・実行の開始終了で更新)。 */
  readonly status: AiStatus | null
  readonly transcript: AiTranscriptEntry[]
  onStatus(next: AiStatus): void
  onToolCall(call: AiToolCall): void
  onResult(result: AiRunResult): void
  onError(message: string): void
  /** 人の発言を積んで、コアへ送る。 */
  send(text: string): void
}

export function createAiChat(deps: {
  send: (text: string) => void
  fail: (message: string) => void
}): AiChat {
  let status = $state<AiStatus | null>(null)
  let transcript = $state<AiTranscriptEntry[]>([])

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
    onStatus(next) {
      status = next
      // **繋がらない理由を黙って捨てない**(`ai_configure` の拒否)。
      if (next.error) deps.fail(`AI設定: ${next.error}`)
    },
    onToolCall(call) {
      push({ kind: 'tool', call })
    },
    onResult(result) {
      push({ kind: 'result', result })
    },
    onError(message) {
      push({ kind: 'error', message })
      deps.fail(`AI ${message}`)
    },
    send(text) {
      push({ kind: 'user', text })
      deps.send(text)
    },
  }
}
