/**
 * 実機検査の骨格: 引数 → サーバ → (MCP の握手) → ブラウザ → 本体 → 後始末 → 終了コード。
 *
 * 各本は `runSmoke(name, defaults, body)` を1回呼ぶだけにする。骨格を書き写すと、
 * `modelsDir` を渡し忘れてリポジトリ直下の実 `models/` を書く・既定ポートが別の本と重なる・
 * temp を消さない・`process.exit` が `finally` を飛ばして子プロセスが残る、がそれぞれ別の本で起きた。
 *
 * - `modelsDir` は**必ず**使い捨ての temp(`--workdir` で外から渡されたときだけ消さない)。
 * - 失敗は `${name}: FAIL ...` を出して `process.exitCode = 1`(後始末を待ってから終わる)。
 * - 既定ポートは `SMOKE_PORTS` の表で本ごとに一意にする(読み込み時に重複を検査する)。
 */
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fail, parseArgs } from './args.mjs'
import { findBrowser, launchBrowser } from './browser.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

/**
 * 既定の [WebSocket, HTTP] ポート。**同じ HTTP ポートを2本が持つと、片方が bind に失敗する前に
 * `startServer` の `fetch` が先客のサーバへ通って進みうる**(server.mjs)。順に回す分には踏まないが
 * 並列に回すと壊れるので、ここで一意にする。
 */
export const SMOKE_PORTS = {
  smoke: [8775, 8776],
  'smoke-features': [8785, 8786],
  screenshot: [8787, 8788],
  'smoke-files': [8795, 8796],
  'smoke-svg': [8797, 8798],
  'smoke-toast': [8805, 8806],
  'smoke-browser-sketches': [8807, 8808],
  'smoke-regions': [8815, 8816],
  'smoke-mcp': [8865, 8866],
  acceptance: [8885, 8886],
  'acceptance-solo': [8887, 8888],
  'smoke-optimize': [8905, 8906],
  'smoke-optimize-debug': [8907, 8908],
  'smoke-optimize-min-width': [8915, 8916],
  'smoke-optimize-mode': [8955, 8956],
  'smoke-clip-planes': [8957, 8958],
  'smoke-vertex': [8965, 8966],
  'smoke-face-culling': [8967, 8968],
  'smoke-hover': [8975, 8976],
  'smoke-mirror': [8977, 8978],
  'smoke-move': [8979, 8980],
  'smoke-resolution': [8981, 8982],
  'smoke-reframe-on-import': [8983, 8984],
  'smoke-mesh-body': [8985, 8986],
  'smoke-grid-scale': [8987, 8988],
  'smoke-face-grid': [8991, 8992],
  'smoke-grid-follow-cursor': [8993, 8994],
  'smoke-measure': [8995, 8996],
  'smoke-screen-pick': [8997, 8998],
  'smoke-section': [9015, 9016],
}

{
  const seen = new Map()
  for (const [name, ports] of Object.entries(SMOKE_PORTS)) {
    for (const port of ports) {
      if (seen.has(port)) throw new Error(`SMOKE_PORTS: ${port} が ${seen.get(port)} と ${name} で重なっている`)
      seen.set(port, name)
    }
  }
}

/**
 * @param {string} name 本の名前(`SMOKE_PORTS` の鍵。`duocad-<name>` が MCP のクライアント名になる)
 * @param {{timeout?: number, mcp?: boolean, handshake?: boolean, attach?: boolean,
 *          onNoise?: (line: string) => void}} defaults
 *   - `timeout`: `--timeout` の既定(ms)
 *   - `mcp`: サーバの stdio に `McpClient` を繋ぐ(`handshake: false` なら握手は本体が打つ)
 *   - `attach`: `--url` で既に立っているサーバへ繋げる本(サーバを立てず、`mcp` は null)
 *   - `onNoise`: MCP の stdout に JSON-RPC 以外の行が出たときの扱い(既定は例外)
 * @param {(ctx: {options: object, workdir: string, wsPort: string, httpPort: string, timeout: number,
 *          url: string, server: object|null, mcp: McpClient|null, browser: object,
 *          restart: () => Promise<void>}) => Promise<string[]|void>} body
 *   戻り値に失敗の名前の配列を返すと、その本は例外で止めずに ❌ を集める作り(acceptance)として
 *   終了コードを決める。
 */
export async function runSmoke(name, defaults, body) {
  const options = parseArgs(process.argv.slice(2))
  const ports = SMOKE_PORTS[name]
  if (ports === undefined) throw new Error(`SMOKE_PORTS に ${name} の既定ポートが無い`)
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
  /** サーバとブラウザを立て直す(文書がまっさらな状態からやり直す本のため)。 */
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
    const failed = await body(ctx)
    if (Array.isArray(failed) && failed.length > 0) {
      console.error(`\n${name}: FAIL ${failed.length}件 — ${failed.join(' / ')}`)
      process.exitCode = 1
    } else {
      console.log(`\n${name}: PASS`)
    }
  } catch (error) {
    console.error(`${name}: FAIL ${error?.stack ?? error}`)
    process.exitCode = 1
  } finally {
    await stop()
    dropWorkdir()
  }
}

export { fail }
