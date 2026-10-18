/**
 * 検査用サーバの起動と停止(実ブラウザの検査が共有する)。
 *
 * 各スクリプトが自分でサーバを立てて終わったら止める。文書は接続より長生きするので、
 * 毎回起動し直さないと前の検査の形が残り、「最初のソリッドが生まれる」経路を通れない。
 *
 * stdinを閉じないこと。`duocad.mcp_server` は stdio で MCP を喋り、stdin が EOF になるとプロセスごと
 * 終了する(`serve_stdio` が戻る)。`spawn` の既定(パイプ)なら開いたままだが、`stdio: 'ignore'` や
 * `< /dev/null` で起動すると立ち上がった直後に落ちる。
 */
import { existsSync, mkdtempSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
export const REPO_ROOT = resolve(HERE, '..', '..')
export const PYTHON = resolve(REPO_ROOT, '.venv', 'Scripts', 'python.exe')
export const SCREENSHOTS_DIR = resolve(REPO_ROOT, 'screenshots')

/**
 * サーバを立てて、HTTPが応答するまで待つ。
 *
 * 戻り値の `child` は MCP で叩く側のため(`smoke-mcp` / `acceptance` が stdio に `McpClient` を繋ぐ)。
 *
 * `modelsDir`(`--models-dir`)を渡さなければ使い捨ての一時ディレクトリを渡す: 1操作ごとの自動保存
 * (issue #5)が `untitled-<n>.json` を書くので、既定(実 `models/`)のままだとファイルが増える。
 * 保存/開くを検査するスクリプトは自分の workdir を明示して渡す。
 *
 * `newDocument`(既定 true)は立ち上がり後に「新規」を送る(トップ画面そのものを見る検査だけ false)。
 *
 * @param {{wsPort: number|string, httpPort: number|string, timeout?: number,
 *          modelsDir?: string, newDocument?: boolean}} options
 * @returns {Promise<{url: string, child: import('node:child_process').ChildProcess,
 *                    stop: () => Promise<void>, log: () => string}>}
 */
export async function startServer({ wsPort, httpPort, timeout = 60000, modelsDir, newDocument = true }) {
  if (!existsSync(PYTHON)) throw new Error(`venvのpythonが無い: ${PYTHON}`)

  const args = ['-m', 'duocad.mcp_server', '--port', String(wsPort), '--http-port', String(httpPort)]
  args.push('--models-dir', modelsDir ?? mkdtempSync(join(tmpdir(), 'duocad-models-')))
  const child = spawn(PYTHON, args, {
    cwd: REPO_ROOT,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  })
  const chunks = []
  child.stderr.setEncoding('utf8')
  child.stderr.on('data', (chunk) => chunks.push(chunk))
  const log = () => chunks.join('')

  const exited = () => child.exitCode !== null || child.signalCode !== null
  const stop = async () => {
    if (exited()) return
    // stdinを閉じて終わらせる(MCPのクライアントが切れたときと同じ経路)。
    child.stdin.end()
    await new Promise((done) => {
      const timer = setTimeout(() => {
        child.kill()
        done()
      }, 5000)
      child.on('exit', () => {
        clearTimeout(timer)
        done()
      })
    })
  }

  const url = `http://127.0.0.1:${httpPort}/`
  // 起動の途中で失敗したら、呼び手は `stop` を受け取れない。子を止めてから投げる(止めないと python のサーバとポートが
  // 残り、子のパイプが node のイベントループを生かし続けて、検査が FAIL を出した後も終了しない)。
  try {
    const deadline = Date.now() + timeout
    for (;;) {
      if (exited()) {
        throw new Error(`サーバが起動直後に終了した(code ${child.exitCode ?? child.signalCode}):\n${log()}`)
      }
      try {
        const response = await fetch(url, { cache: 'no-store' })
        if (response.ok) break
      } catch {
        // まだ listen していない
      }
      if (Date.now() > deadline) {
        throw new Error(`サーバが ${timeout}ms 以内に応答しなかった(${url}):\n${log()}`)
      }
      await new Promise((done) => setTimeout(done, 200))
    }
    // サーバはトップ画面で起動し、その間は MCP からの操作も断られる。
    if (newDocument) await sendNewDocument(wsPort, timeout)
  } catch (error) {
    await stop()
    throw error
  }

  return { url, child, stop, log }
}

/** WebSocket で `new_document` を送り、開いた状態が配られるまで待つ。 */
async function sendNewDocument(wsPort, timeout) {
  const socket = new WebSocket(`ws://127.0.0.1:${wsPort}`)
  socket.binaryType = 'arraybuffer'
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`new_document の応答が ${timeout}ms 以内に来なかった`)), timeout)
      socket.addEventListener('error', () => {
        clearTimeout(timer)
        reject(new Error(`WebSocket(ws://127.0.0.1:${wsPort})に繋がらなかった`))
      })
      socket.addEventListener('open', () => socket.send(JSON.stringify({ cmd: 'new_document', client_op: 1 })))
      socket.addEventListener('message', (event) => {
        // 状態は1枚のバイナリフレーム(`duocad/net/wire.py`): [u32 ヘッダ長][JSON ヘッダ][配列]。ヘッダだけ読む
        const data = event.data
        const message = JSON.parse(
          data instanceof ArrayBuffer
            ? new TextDecoder().decode(new Uint8Array(data, 4, new DataView(data).getUint32(0, true)))
            : String(data),
        )
        if (message.type === 'error' && message.where !== 'initial_state') {
          clearTimeout(timer)
          reject(new Error(`new_document が断られた: ${message.message}`))
        } else if (message.type === 'state' && message.state?.document_open) {
          clearTimeout(timer)
          resolve()
        }
      })
    })
  } finally {
    socket.close()
  }
}
