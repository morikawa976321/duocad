/**
 * 実機確認(stage2 の完了条件4) — headless Chrome で `?debug=anchors` を開いてボタンを押し、
 * **固定に割り当てた節点と荷重に割り当てた節点が別々の色で描かれた状態**を
 * `screenshots/result_anchors.png` に撮る。
 *
 *   node shot_anchors.mjs
 *
 * `server.py` を子プロセスで起動し、終わったら落とす(先に自分で起動しておく必要はない)。
 * 「撮れた」だけでは合格にしない。**画面に本当に2色が出ているか**まで見る:
 *
 * - `window.__probe.state === 'anchors'`(サーバが対応表を返し、描画まで進んだ)
 * - 固定・荷重の節点数がどちらも 0 でない
 * - **`window.__probe.pixelCensus()` が2色ぶんのピクセルを実際に数える** ——
 *   ページが自分で1フレーム描いてから `gl.readPixels` するので、
 *   「片方が形状の裏に隠れている」「両方同じ色だった」を弾ける。
 *   節点数だけを見ると、真っ黒の画面でも合格になる。
 * - `renderer.info.render.triangles > 0`(GPUへ実際に三角形が投げられている)
 *
 * 撮るのは `screenshots/result_anchors.png` だけ。**既存の `result.png` /
 * `result_smooth.png` には触らない。**
 *
 * puppeteer-core は duocad 本体の `web/node_modules` から借りる(**新規インストールをしない**
 * ための措置)。**`--use-angle=swiftshader` は付けない** —— この環境の headless Chrome は
 * 実GPU(D3D11)を掴める(engine `headless-chrome-can-use-real-gpu`)。作法は `shot.mjs` と同じ。
 */
import { spawn } from 'node:child_process'
import { createRequire } from 'node:module'
import { mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const DUOCAD = dirname(HERE)
const PYTHON = join(DUOCAD, '.venv', 'Scripts', 'python.exe')
const OUT = join(HERE, 'screenshots', 'result_anchors.png')
const HTTP_PORT = Number(process.env.PROBE_HTTP_PORT ?? 8775)
const WS_PORT = Number(process.env.PROBE_WS_PORT ?? 8776)
const URL = `http://127.0.0.1:${HTTP_PORT}/?ws=${WS_PORT}&debug=anchors`

const require = createRequire(join(DUOCAD, 'web', 'package.json'))
const { launch } = require('puppeteer-core')

const CHROME = [
  process.env.DUOCAD_BROWSER,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
].filter(Boolean).find((p) => require('node:fs').existsSync(p))
if (!CHROME) throw new Error('Chrome が見つからない(DUOCAD_BROWSER で指定する)')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function waitForServer(timeoutMs = 60000) {
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

  const glRenderer = await page.evaluate(() => {
    const gl = document.createElement('canvas').getContext('webgl2')
    if (gl === null) return 'WebGLコンテキストを作れない'
    const info = gl.getExtension('WEBGL_debug_renderer_info')
    return info === null ? gl.getParameter(gl.RENDERER) : gl.getParameter(info.UNMASKED_RENDERER_WEBGL)
  })
  console.log(`  GL     : ${glRenderer}`)

  await page.goto(URL, { waitUntil: 'load' })
  await page.waitForFunction(() => window.__probe !== undefined, { timeout: 30000 })

  const t0 = Date.now()
  await page.click('#run')
  console.log('  ボタンを押した。対応表の構築を待つ…')
  await page.waitForFunction(
    () => window.__probe.state === 'anchors' || window.__probe.state === 'error',
    { timeout: 5 * 60 * 1000, polling: 250 },
  )
  // 最終フレームが確実に描かれてから数える・撮る
  await sleep(600)

  const probe = await page.evaluate(() => ({
    state: window.__probe.state,
    error: window.__probe.error,
    anchors: window.__probe.anchors,
    triangles: window.__probe.triangles ?? 0,
    census: window.__probe.pixelCensus(),
  }))
  const label = await page.$eval('#run', (el) => el.textContent)
  const seconds = (Date.now() - t0) / 1000

  mkdirSync(dirname(OUT), { recursive: true })
  await page.screenshot({ path: OUT })

  const a = probe.anchors ?? {}
  console.log(`  state  : ${probe.state}${probe.error ? ` (${probe.error})` : ''}`)
  console.log(`  固定   : 面${a.fixedFace} ${a.fixed} 節点 (担当面積 ${a.fixedArea?.toFixed(3)} mm^2)`)
  console.log(`  荷重   : 面${a.loadFace} ${a.load} 節点 (担当面積 ${a.loadArea?.toFixed(3)} mm^2)`)
  console.log(`  設計   : ${a.design} 要素 / 対応表 ${a.seconds?.toFixed(3)} 秒`)
  console.log(`  色     : 固定 #${a.fixedColor?.toString(16)} / 荷重 #${a.loadColor?.toString(16)}`)
  console.log(`  画素   : 固定色 ${probe.census.fixed} / 荷重色 ${probe.census.load} `
    + `(${probe.census.width}x${probe.census.height})`)
  console.log(`  三角形 : ${probe.triangles}`)
  console.log(`  ボタン : "${label}"`)
  console.log(`  所要   : ${seconds.toFixed(1)}秒`)
  console.log(`  保存   : ${OUT}`)

  const problems = []
  if (probe.state !== 'anchors') problems.push(`state が anchors でない: ${probe.state} (${probe.error})`)
  if (!(a.fixed > 0)) problems.push('固定に割り当てた節点が0')
  if (!(a.load > 0)) problems.push('荷重に割り当てた節点が0')
  if (a.fixedColor === a.loadColor) problems.push('固定と荷重が同じ色で描かれている')
  if (!(probe.census.fixed > 100)) problems.push(`固定色の画素が少なすぎる: ${probe.census.fixed}(描かれていない/隠れている)`)
  if (!(probe.census.load > 20)) problems.push(`荷重色の画素が少なすぎる: ${probe.census.load}(描かれていない/隠れている)`)
  if (probe.triangles < 1) problems.push('三角形が1枚も描かれていない(画面が空)')
  if (/swiftshader|software|llvmpipe/i.test(glRenderer))
    problems.push(`実GPUではなくソフトウェアラスタライザで描画している: ${glRenderer}`)
  if (problems.length > 0) failed = problems
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
