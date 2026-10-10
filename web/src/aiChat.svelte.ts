/** アプリ内AIエージェントとの会話の持ち主。会話の時系列はコアには無く、リロードで消える(`AiPanel.svelte` 参照)。 */
import type { AiRunResult, AiStatus, AiToolCall, AiTranscriptEntry } from './session'

export interface AiChat {
  readonly status: AiStatus | null
  readonly transcript: AiTranscriptEntry[]
  onStatus(next: AiStatus): void
  onToolCall(call: AiToolCall): void
  onResult(result: AiRunResult): void
  onError(message: string): void
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
      // 繋がらない理由(`ai_configure` の拒否)を黙って捨てない。
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
