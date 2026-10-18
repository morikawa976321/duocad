/**
 * `web/scripts/server.mjs` の `startServer` が、起動の途中で失敗したとき python のサーバを止めてから投げること。
 * `tests/test_web_scripts.py` が呼ぶ。
 *
 * 止めないと子のパイプが node のイベントループを生かし続け、投げた後も node が終わらない。ここでは
 * 「失敗の後に node が自分で終わる」を見る(終わらなければ、下の保険のタイマーが 2 で落とす)。
 * ポートは OS に選ばせた空きで、本人の開発版(8765 / 8766)や安定版(8775 / 8776)には触れない。
 */
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { startServer } from '../web/scripts/server.mjs'

async function freePort() {
  const probe = createServer()
  await new Promise((resolve) => probe.listen(0, '127.0.0.1', resolve))
  const { port } = probe.address()
  await new Promise((resolve) => probe.close(resolve))
  return port
}

const HANG_LIMIT_MS = 30000
setTimeout(() => {
  console.error('startServer が失敗した後も node が終わらない(子のサーバが残っている)')
  process.exit(2)
}, HANG_LIMIT_MS).unref()

const modelsDir = mkdtempSync(join(tmpdir(), 'duocad-start-failure-'))
try {
  // 1ms では HTTP が上がる前に期限が切れる。
  await assert.rejects(
    startServer({ wsPort: await freePort(), httpPort: await freePort(), timeout: 1, modelsDir }),
    /以内に応答しなかった/,
  )
} finally {
  rmSync(modelsDir, { recursive: true, force: true })
}
console.log('ok')
