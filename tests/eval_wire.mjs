/**
 * `web/src/wire.ts`(画面がサーバの配信を読む口)を**実際に評価する**ための小さな口。`tests/test_wire.py` が呼ぶ。
 *
 * 入力: 1つの接続に届いた順のフレーム `[{"binary": bool, "data": <base64 | JSON 文字列>}, ...]`。
 * 出力: 画面と同じ手順(`decodeFrame` → `state` なら `createDelivered().restore`)で埋め戻した配りものの並び
 * (型付き配列は数の配列にして JSON で返す)。
 */
import { createDelivered, decodeFrame } from '../web/src/wire.ts'

const chunks = []
for await (const chunk of process.stdin) chunks.push(chunk)
const frames = JSON.parse(Buffer.concat(chunks).toString('utf8'))

const delivered = createDelivered()
const restored = frames.map((frame) => {
  let message
  if (frame.binary) {
    const bytes = Buffer.from(frame.data, 'base64')
    message = decodeFrame(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength))
  } else {
    message = JSON.parse(frame.data)
  }
  if (message.type === 'state') message.state = delivered.restore(message.state, message.delta?.bodies ?? [])
  return message
})

process.stdout.write(JSON.stringify(restored, (_key, value) => (ArrayBuffer.isView(value) ? Array.from(value) : value)))
