/**
 * 実機確認(完了条件1・4) — headless Chrome で**既定ページ**(フラグ無し)を開き、
 * **ボタンを押す前**に4条件(保持/除外/拘束/荷重)が duocad 本体と同じ色で描かれている
 * ことを `screenshots/result_anchors.png` に撮る。
 *
 *   node shot_anchors.mjs
 *
 * **2026-09-04に対象を作り直した。** 旧版は `?debug=anchors`(固定=緑シアン/荷重=橙赤の
 * 節点の球)を撮っていたが、そのモードは面アンカーが効いていることの目視確認として
 * `web/index.html` に残したまま(消していない)、このスクリプトの対象は**起動直後の
 * 既定表示**へ移した(4色を実際に検査するのはこちらの役目になったため)。
 *
 * `server.py` を子プロセスで起動し、終わったら落とす(先に自分で起動しておく必要はない)。
 * 「撮れた」だけでは合格にしない。**画面に本当に4色が出ているか**まで見る:
 *
 * - `window.__probe.state === 'conditions'`(サーバが条件表示の材料を返し、描画まで進んだ。
 *   ボタンは押さない —— 完了条件1は「フラグ不要・ボタンを押す前」の話)
 * - `window.__probe.pixelCensus()` が4色ぶんの画素を実際に数える(判定式は
 *   `web/index.html` の `pixelCensus`)
 * - **検出力の確認(タスク要求)**: 4色それぞれを`probe.setVisible(role, false)`で
 *   1色ずつ隠し、その色の画素数が実際に0近くまで落ちることを見る。集計だけの検査は
 *   「描かれていない色を見逃す」ことがある(2026-09-02の実例)ので、閾値を置くだけで
 *   済ませない。
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

// 判定式(`web/index.html` の `pixelCensus`)ごとの色名と、下の変異検査で使うキー。
const ROLES = [
  { key: 'keep', label: '保持(#3f9b58)' },
  { key: 'exclude', label: '除外(#c75448)' },
  { key: 'fixture', label: '拘束面(#3f7fc2)' },
  { key: 'load', label: '荷重(#ffc857 矢印/ラベル)', censusKey: 'loadColor' },
]

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

  const t0 = Date.now()
  await page.goto(URL, { waitUntil: 'load' })
  await page.waitForFunction(() => window.__probe !== undefined, { timeout: 30000 })
  console.log('  既定表示の読み込みを待つ(ボタンは押さない)…')
  await page.waitForFunction(
    () => window.__probe.state === 'conditions' || window.__probe.state === 'error',
    { timeout: 60000, polling: 250 },
  )
  // 最終フレームが確実に描かれてから数える・撮る
  await sleep(600)

  const probe = await page.evaluate(() => ({
    state: window.__probe.state,
    error: window.__probe.error,
    conditions: window.__probe.conditions,
    triangles: window.__probe.triangles ?? 0,
    census: window.__probe.pixelCensus(),
  }))
  const label = await page.$eval('#run', (el) => el.textContent)
  const seconds = (Date.now() - t0) / 1000

  mkdirSync(dirname(OUT), { recursive: true })
  await page.screenshot({ path: OUT })

  const c = probe.conditions ?? {}
  console.log(`  state   : ${probe.state}${probe.error ? ` (${probe.error})` : ''}`)
  console.log(`  保持    : ${c.keepTriangles} 三角形 / 除外: ${c.obstacleTriangles} 三角形 / `
    + `拘束面: ${c.fixtureTriangles} 三角形 / 荷重: ${c.loadMagnitude}N`)
  console.log(`  画素    : keep=${probe.census.keep} exclude=${probe.census.exclude} `
    + `fixture=${probe.census.fixture} loadColor=${probe.census.loadColor} `
    + `(${probe.census.width}x${probe.census.height})`)
  console.log(`  三角形  : ${probe.triangles}`)
  console.log(`  ボタン  : "${label}"`)
  console.log(`  所要    : ${seconds.toFixed(1)}秒`)
  console.log(`  保存    : ${OUT}`)

  const problems = []
  if (probe.state !== 'conditions') problems.push(`state が conditions でない: ${probe.state} (${probe.error})`)
  if (probe.triangles < 1) problems.push('三角形が1枚も描かれていない(画面が空)')
  if (/swiftshader|software|llvmpipe/i.test(glRenderer)) {
    problems.push(`実GPUではなくソフトウェアラスタライザで描画している: ${glRenderer}`)
  }

  const THRESHOLD = { keep: 300, exclude: 300, fixture: 300, loadColor: 30 }
  for (const key of Object.keys(THRESHOLD)) {
    if (!(probe.census[key] > THRESHOLD[key])) {
      problems.push(`${key} の画素が少なすぎる: ${probe.census[key]}(描かれていない/隠れている)`)
    }
  }

  // **検出力の確認**: 1色ずつ隠して、その色の画素数が実際に0近くまで落ちることを見る。
  // 集計(閾値だけの比較)は「描かれていない色を見逃す」ことがある(2026-09-02の実例)ので、
  // ここで実際に消して確かめる。閾値の10%未満まで落ちれば「その色は確かにこの判定式で
  // 検出されている」とみなす(アンチエイリアスの縁が数ピクセル残るのは許容する)。
  console.log('  検出力の確認(1色ずつ隠して0近くまで落ちるか)…')
  for (const role of ROLES) {
    const censusKey = role.censusKey ?? role.key
    const without = await page.evaluate((r) => {
      window.__probe.setVisible(r, false)
      const c = window.__probe.pixelCensus()
      window.__probe.setVisible(r, true)
      return c
    }, role.key)
    const before = probe.census[censusKey]
    const after = without[censusKey]
    const ok = after < Math.max(before * 0.1, 20)
    console.log(`    ${role.label}: 表示時 ${before} → 非表示時 ${after} ${ok ? 'OK' : 'NG'}`)
    if (!ok) {
      problems.push(
        `${role.label} を隠しても画素が落ちない(表示時${before}→非表示時${after})`
        + ' —— 閾値が別の色を誤検出している疑い',
      )
    }
  }

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
