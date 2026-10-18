/**
 * `web/scripts/mcp-client.mjs` の `McpClient` が、サーバの stdout に JSON-RPC 以外の行が出たとき、
 * 例外を呼び手の `await` に届けること(`data` イベントの中で投げて node ごと落とさない)。`tests/test_web_scripts.py` が呼ぶ。
 */
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'

import { McpClient } from '../web/scripts/mcp-client.mjs'

const NOISE = 'OCCT noise line'

// 1. 既定: 待っている呼び出しも、そのあとの呼び出しも、雑音の行で落ちる。
const silent = spawn(process.execPath, ['-e', `console.log(${JSON.stringify(NOISE)}); setInterval(() => {}, 1000)`])
const strict = new McpClient(silent, { timeout: 5000 })
await assert.rejects(strict.send('ping', {}), /JSON-RPC以外の行/)
await assert.rejects(strict.send('ping', {}), /JSON-RPC以外の行/)
silent.kill()

// 2. 自前の onNoise を渡したときは、その行を受け取って応答の待ちを続ける。
const echo = spawn(process.execPath, [
  '-e',
  `
  console.log(${JSON.stringify(NOISE)})
  require('node:readline').createInterface({ input: process.stdin }).on('line', (line) => {
    console.log(JSON.stringify({ jsonrpc: '2.0', id: JSON.parse(line).id, result: { ok: true } }))
  })
  `,
])
const seen = []
const lenient = new McpClient(echo, { timeout: 5000, onNoise: (line) => seen.push(line) })
const reply = await lenient.send('ping', {})
assert.deepEqual(reply.result, { ok: true })
assert.deepEqual(seen, [NOISE])
echo.kill()
console.log('ok')
