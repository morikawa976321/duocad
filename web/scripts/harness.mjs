/**
 * 実機検査の骨格: 引数 → サーバ → (MCP の握手) → ブラウザ → 本体 → 後始末 → 終了コード。
 *
 * 各本は `runSmoke(name, defaults, body)` を1回呼ぶだけにする(骨格を書き写すと、実 `models/` への書き込み・
 * ポート重複・temp の消し忘れ・`process.exit` が `finally` を飛ばして子プロセスが残る、が起きた)。
 *
 * - `modelsDir` は必ず使い捨ての temp(`--workdir` で外から渡されたときだけ消さない)。
 * - 失敗は `${name}: FAIL ...` を出して `process.exitCode = 1`(後始末を待ってから終わる)。
 * - ポートは OS に空きを選ばせる(`--port` / `--http-port` で明示したときだけ固定)。
 */
import { mkdtempSync, rmSync } from 'node:fs'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fail, parseArgs } from './args.mjs'
import { findBrowser, launchBrowser } from './browser.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

/**
 * 空いているポートを `count` 個。全部を握ってから離す(1個ずつ取って離すと同じ番号が2回返りうる)。
 *
 * 固定ポートにしないのは、無関係なプロセスが先に握っていると「サーバが起動直後に終了した」で落ち、
 * 同じ HTTP ポートを2本が持つと `startServer` の `fetch` が先客のサーバへ通って進みうるため。
 */
async function freePorts(count) {
  const probes = []
  try {
    for (let i = 0; i < count; i += 1) {
      const probe = createServer()
      await new Promise((resolve, reject) => {
        probe.once('error', reject)
        probe.listen(0, '127.0.0.1', resolve)
      })
      probes.push(probe)
    }
    return probes.map((probe) => probe.address().port)
  } finally {
    await Promise.all(probes.map((probe) => new Promise((resolve) => probe.close(resolve))))
  }
}

/**
 * @param {string} name 本の名前(`duocad-<name>` が MCP のクライアント名と temp の接頭辞になる)
 * @param {{timeout?: number, mcp?: boolean, handshake?: boolean, attach?: boolean,
 *          onNoise?: (line: string) => void}} defaults
 *   - `timeout`: `--timeout` の既定(ms)
 *   - `mcp`: サーバの stdio に `McpClient` を繋ぐ(`handshake: false` なら握手は本体が打つ)
 *   - `attach`: `--url` で既に立っているサーバへ繋げる本(サーバを立てず、`mcp` は null)
 *   - `onNoise`: MCP の stdout に JSON-RPC 以外の行が出たときの扱い(既定は例外)
 * @param {(ctx: {options: object, workdir: string, wsPort: string, httpPort: string, timeout: number,
 *          url: string, server: object|null, mcp: McpClient|null, browser: object,
 *          restart: () => Promise<void>}) => Promise<void>} body
 */
export async function runSmoke(name, defaults, body) {
  const options = parseArgs(process.argv.slice(2))
  const ports = await freePorts(2)
  const wsPort = options.port ?? String(ports[0])
  const httpPort = options['http-port'] ?? String(ports[1])
  const timeout = Number.parseInt(options.timeout ?? String(defaults.timeout ?? 120000), 10)
  if (options.url !== undefined && !defaults.attach) {
    throw new Error(`${name} は --url(既に立っているサーバへの接続)に対応していない`)
  }
  const attachUrl = defaults.attach ? (options.url ?? null) : null
  const ownWorkdir = options.workdir === undefined
  const newWorkdir = () => mkdtempSync(join(tmpdir(), `duocad-${name}-`))

  const ctx = {
    options,
    workdir: options.workdir ?? newWorkdir(),
    wsPort,
    httpPort,
    timeout,
    url: attachUrl,
    server: null,
    mcp: null,
    browser: null,
    restart: null,
  }

  const start = async () => {
    if (attachUrl === null) {
      ctx.server = await startServer({ wsPort, httpPort, modelsDir: ctx.workdir })
      ctx.url = ctx.server.url
      if (defaults.mcp) {
        ctx.mcp = new McpClient(ctx.server.child, { timeout, onNoise: defaults.onNoise })
        if (defaults.handshake !== false) await ctx.mcp.handshake(`duocad-${name}`)
      }
    }
    ctx.browser = await launchBrowser(findBrowser())
  }
  const stop = async () => {
    if (ctx.browser !== null) await ctx.browser.close()
    ctx.browser = null
    if (ctx.server !== null) await ctx.server.stop()
    ctx.server = null
    ctx.mcp = null
  }
  const dropWorkdir = () => {
    if (ownWorkdir) rmSync(ctx.workdir, { recursive: true, force: true })
  }
  /** サーバとブラウザを立て直す。 */
  ctx.restart = async () => {
    await stop()
    dropWorkdir()
    if (ownWorkdir) ctx.workdir = newWorkdir()
    await start()
  }

  console.log(`${name}: ws=${wsPort} http=${httpPort} workdir=${ctx.workdir}`)
  try {
    await start()
    if (attachUrl !== null) console.log(`  server : ${attachUrl} (既に立っているサーバへ接続)`)
    await body(ctx)
    console.log(`\n${name}: PASS`)
  } catch (error) {
    console.error(`${name}: FAIL ${error?.stack ?? error}`)
    process.exitCode = 1
  } finally {
    await stop()
    dropWorkdir()
  }
}

export { fail }
