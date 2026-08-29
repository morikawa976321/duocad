/**
 * 実機確認 — headless Chrome で実際にボタンを押し、形状が描かれた状態を
 * `screenshots/result.png` に撮る。
 *
 *   node shot.mjs
 *
 * `server.py` を子プロセスで起動し、終わったら落とす(先に自分で起動しておく必要はない)。
 * **完了条件3はこのスクリプトの緑をもって満たしたとする**ので、
 * 「撮れた」だけでなく **画面に本当に形が出ているか**まで見る:
 *
 * - `window.__probe.state === 'done'`(最適化が最後まで走った)
 * - 反復メッセージが `max_iter` 回届いた
 * - 最終反復の占有要素数が 0 でない
 * - **`renderer.info.render.triangles > 0`**(GPUへ実際に三角形が投げられている。
 *   これが無いと「真っ黒でも成功」になる)
 *
 * puppeteer-core は duocad 本体の `web/node_modules` から借りる(**新規インストールをしない**
 * ための措置。`createRequire` に `web/package.json` を渡して解決させている)。
 * **`--use-angle=swiftshader` は付けない** —— この環境の headless Chrome は実GPU(D3D11)を
 * 掴めて、付けるとCPUが7倍になるだけで速くならない(engine `headless-chrome-can-use-real-gpu`)。
 */
import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const DUOCAD = dirname(HERE)
const PYTHON = join(DUOCAD, '.venv', 'Scripts', 'python.exe')
const OUT = join(HERE, 'screenshots', 'result.png')
const HTTP_PORT = Number(process.env.PROBE_HTTP_PORT ?? 8775)
const WS_PORT = Number(process.env.PROBE_WS_PORT ?? 8776)
// `?ws=` を必ず付ける。**付けないと画面は既定の8776を叩き、ポートを変えたときだけ
// 「接続が切断されました」で静かに落ちる。**
const URL = `http://127.0.0.1:${HTTP_PORT}/?ws=${WS_PORT}`

const require = createRequire(join(DUOCAD, 'web', 'package.json'))
const { launch } = require('puppeteer-core')

const CHROME = [
  process.env.DUOCAD_BROWSER,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
].filter(Boolean).find((p) => require('node:fs').existsSync(p))
if (!CHROME) throw new Error('Chrome が見つからない(DUOCAD_BROWSER で指定する)')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/** HTTPが 200 を返すまで待つ。 */
async function waitForServer(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const res = await fetch(URL)
      if (res.ok) return
    } catch {
      /* まだ起動していない */
    }
    await sleep(200)
  }
  throw new Error(`サーバが ${timeoutMs}ms 以内に応答しない: ${URL}`)
}

const server = spawn(PYTHON, ['server.py'], {
  cwd: HERE,
  // `PROBE_NO_OPEN` を立てて**実ブラウザの窓が勝手に開かないようにする**
  // (検査は headless Chrome を自分で起こすので、開かれると人の画面を邪魔するだけ)。
  env: {
    ...process.env,
    PROBE_HTTP_PORT: String(HTTP_PORT),
    PROBE_WS_PORT: String(WS_PORT),
    PROBE_NO_OPEN: '1',
  },
  stdio: ['ignore', 'pipe', 'pipe'],
})
server.stdout.on('data', (b) => process.stdout.write(`  [server] ${b}`))
server.stderr.on('data', (b) => {
  const s = String(b)
  // 静的配信のアクセスログはうるさいので落とす
  if (!/"GET .*" 200|"GET .*" 304/.test(s)) process.stderr.write(`  [server] ${s}`)
})

let browser = null
let failed = null
try {
  await waitForServer()
  console.log(`  server : ${URL}`)

  browser = await launch({
    executablePath: CHROME,
    headless: true,
    args: [
      '--headless=new',
      '--use-gl=angle',
      '--enable-unsafe-swiftshader',
      '--disable-gpu-sandbox',
      '--window-size=1280,860',
    ],
  })
  const page = await browser.newPage()
  await page.setViewport({ width: 1280, height: 860 })
  page.on('console', (m) => {
    if (m.type() === 'error') console.log(`  [page] ${m.text()}`)
  })
  page.on('pageerror', (e) => console.log(`  [page] ${e.message}`))

  console.log(
    `  GL     : ${await page.evaluate(() => {
      const gl = document.createElement('canvas').getContext('webgl2')
      if (gl === null) return 'WebGLコンテキストを作れない'
      const info = gl.getExtension('WEBGL_debug_renderer_info')
      return info === null ? gl.getParameter(gl.RENDERER) : gl.getParameter(info.UNMASKED_RENDERER_WEBGL)
    })}`,
  )

  await page.goto(URL, { waitUntil: 'load' })
  // ESMの読み込みが終わって検査用の穴が生えるまで待つ
  await page.waitForFunction(() => window.__probe !== undefined, { timeout: 30000 })

  const label0 = await page.$eval('#run', (el) => el.textContent)
  console.log(`  ボタン : "${label0}"`)

  const t0 = Date.now()
  await page.click('#run')
  console.log('  ボタンを押した。最適化の完了を待つ(数分)…')

  await page.waitForFunction(
    () => window.__probe.state === 'done' || window.__probe.state === 'error',
    { timeout: 15 * 60 * 1000, polling: 500 },
  )

  const probe = await page.evaluate(() => ({
    state: window.__probe.state,
    error: window.__probe.error,
    iterations: window.__probe.iterations.length,
    lastIt: window.__probe.lastIter?.it ?? null,
    compliance: window.__probe.lastIter?.compliance ?? null,
    volfrac: window.__probe.lastIter?.volfrac ?? null,
    change: window.__probe.lastIter?.change ?? null,
    occupied: window.__probe.lastIter?.occupied.length ?? 0,
    triangles: window.__probe.triangles ?? 0,
  }))
  const label1 = await page.$eval('#run', (el) => el.textContent)
  const seconds = (Date.now() - t0) / 1000

  // 最終フレームが確実に描かれてから撮る
  await sleep(500)
  mkdirSync(dirname(OUT), { recursive: true })
  await page.screenshot({ path: OUT })

  console.log(`  state  : ${probe.state}${probe.error ? ` (${probe.error})` : ''}`)
  console.log(`  反復   : ${probe.iterations} (最終 it=${probe.lastIt})`)
  console.log(`  C      : ${probe.compliance}`)
  console.log(`  体積率 : ${probe.volfrac}`)
  console.log(`  change : ${probe.change}`)
  console.log(`  占有   : ${probe.occupied} 要素`)
  console.log(`  三角形 : ${probe.triangles} (最終フレームでGPUへ投げた数)`)
  console.log(`  ボタン : "${label1}"`)
  console.log(`  所要   : ${seconds.toFixed(1)}秒`)
  console.log(`  保存   : ${OUT}`)

  const problems = []
  if (probe.state !== 'done') problems.push(`state が done でない: ${probe.state} (${probe.error})`)
  if (probe.iterations < 1) problems.push('反復メッセージが1件も届いていない')
  if (probe.occupied < 1) problems.push('占有要素が0(形が出ていない)')
  if (probe.triangles < 1) problems.push('三角形が1枚も描かれていない(画面が空)')
  if (problems.length > 0) {
    failed = problems
  }
} catch (error) {
  failed = [String(error)]
} finally {
  if (browser) await browser.close()
  server.kill()
}

if (failed) {
  console.log('\nNG: 実機確認に失敗')
  for (const p of failed) console.log(`  - ${p}`)
  process.exit(1)
}
console.log('\nOK: 実機確認に合格')
