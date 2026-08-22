/**
 * 検査用サーバの起動と停止 — 実ブラウザの検査6本が共有する。
 *
 * **各スクリプトが自分でサーバを立てて、終わったら止める。** 別窓で立てておく手順は無い。
 *
 * こうすると手順が1つ減るだけでなく、**文書が毎回まっさらになる**という要件が自動で満たされる
 * ——文書は接続より長生きするので、前の検査の形が残っていると「最初のソリッドが生まれる」経路を
 * 通れない(各スクリプトのdocstringが「サーバは毎回起動し直す」と書いていたのはこのため)。
 *
 * **stdinを閉じないこと。** `duocad.mcp_server` はstdioでMCPを喋るので、**stdinがEOFになると
 * プロセスごと終了する**(`serve_stdio` が戻る)。`spawn` の既定はパイプなので開いたままになるが、
 * `stdio: 'ignore'` や `< /dev/null` で起動すると**立ち上がった直後に落ちる**。
 */
import { existsSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
export const REPO_ROOT = resolve(HERE, '..', '..')
export const PYTHON = resolve(REPO_ROOT, '.venv', 'Scripts', 'python.exe')

/**
 * サーバを立てて、HTTPが応答するまで待つ。
 *
 * 戻り値の `child` は**MCPで叩く側のため**(`smoke-mcp` / `acceptance` が stdio に
 * `McpClient` を繋ぐ)。ブラウザだけの検査は使わない。
 *
 * @param {{wsPort: number|string, httpPort: number|string, timeout?: number}} options
 * @returns {Promise<{url: string, child: import('node:child_process').ChildProcess,
 *                    stop: () => Promise<void>, log: () => string}>}
 */
export async function startServer({ wsPort, httpPort, timeout = 60000 }) {
  if (!existsSync(PYTHON)) throw new Error(`venvのpythonが無い: ${PYTHON}`)

  const child = spawn(
    PYTHON,
    ['-m', 'duocad.mcp_server', '--port', String(wsPort), '--http-port', String(httpPort)],
    { cwd: REPO_ROOT, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } },
  )
  const chunks = []
  child.stderr.setEncoding('utf8')
  child.stderr.on('data', (chunk) => chunks.push(chunk))
  const log = () => chunks.join('')

  let exited = null
  child.on('exit', (code) => (exited = code))

  const url = `http://127.0.0.1:${httpPort}/`
  const deadline = Date.now() + timeout
  for (;;) {
    if (exited !== null) {
      throw new Error(`サーバが起動直後に終了した(code ${exited}):\n${log()}`)
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

  const stop = async () => {
    if (exited !== null) return
    // **stdinを閉じて終わらせる**(MCPのクライアントが切れたときと同じ経路)。
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

  return { url, child, stop, log }
}
