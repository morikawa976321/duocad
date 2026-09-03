/**
 * 実機での確認 — 任意面(3基準平面のいずれでもない面)へのスケッチ中、その面にグリッドが
 * 張られること(`20260903-0357-duocad-sketch-face-grid`)。
 *
 *   node scripts/smoke-face-grid.mjs [--port 8991] [--http-port 8992]
 *
 * 完了条件を実機で見る:
 *
 *   1. 任意面へのスケッチ中、その面にグリッドが張られる(`gridTiers().planes` が
 *      `['sketch-face']` になる)。
 *   2. XY床は隠す(`planes` に `'XY'` が混ざらない)。
 *   3. 直交軸は出さない(`gridTiers().axis === null`)。
 *   4. グリッドはスケッチ平面のframeに載る(u/v・原点は面のもの。`基準平面ではない`ことは
 *      `basePlaneForFrame` が null を返す=`plane.kind==='face'` かつ原点が原点でない面を
 *      選ぶことで保証する)。
 *   5. スケッチを抜けると通常表示(XY平面+Z軸)へ戻る。
 *   6. z-fightingしていないこと(スクリーンショットを残し、目視できるようにする)。
 *
 * サーバはこのスクリプトが空の文書で起動し、40×30×20mmの箱をMCP経由で作る。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 */
import { mkdirSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { findBrowser, launchBrowser } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const wsPort = options.port ?? '8991'
const httpPort = options['http-port'] ?? '8992'
const timeout = Number.parseInt(options.timeout ?? '60000', 10)
const screenshotDir = 'C:\\Users\\tm976\\mf\\duocad\\screenshots'

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** 段の不透明度・グリッドの状態は描画ループ(`tick`)で入れ直すので、読む前に数フレーム待つ。 */
async function settle(page) {
  await page.evaluate(
    () =>
      new Promise((resolve) => {
        let left = 4
        const step = () => (left-- <= 0 ? resolve() : requestAnimationFrame(step))
        requestAnimationFrame(step)
      }),
  )
}

async function main() {
  const executablePath = findBrowser()
  const workdir = mkdtempSync(join(tmpdir(), 'duocad-face-grid-'))
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null
  try {
    await mcp.handshake('duocad-smoke-face-grid')

    // ---------------------------------------------------------------- 準備: 40×30×20mmの箱
    const sketch = await mcp.callTool('sketch_create', { base_plane: 'XY' })
    await mcp.callTool('add_rectangle', { sketch_id: sketch.sketch_id, corner: [0, 0], width: 40, height: 30 })
    await mcp.callTool('propose_selection', { refs: [{ sketch: sketch.sketch_id, index: 1 }] })
    await mcp.callTool('commit_selection')
    const extruded = await mcp.callTool('extrude', { distance: 20, mode: 'new' })
    await mcp.callTool('clear_selection')
    const bodyId = extruded.bodies[0].body_id
    ok(`40×30×20mmの箱を作った(body=${bodyId})`)

    browser = await launchBrowser(executablePath)
    const page = await browser.newPage()
    await page.setViewport({ width: 1280, height: 860 })
    const consoleErrors = []
    page.on('pageerror', (error) => consoleErrors.push(String(error)))
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    await page.goto(`http://127.0.0.1:${httpPort}/`, { waitUntil: 'load', timeout })
    await page.waitForFunction(() => Boolean(window.__duocad?.ready), { timeout, polling: 100 })
    await page.waitForFunction(() => (window.__duocad?.state?.bodies ?? []).length >= 1, {
      timeout,
      polling: 100,
    })
    await sleep(200)

    const tiersNow = () => page.evaluate(() => window.__duocad.gridTiers())

    async function locateFace(id, index) {
      return page.evaluate(
        (bodyId, faceIndex) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectFace(bodyId, faceIndex)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        id,
        index,
      )
    }

    // **ページの中で JSON.stringify する**(Svelteの状態はProxyで、そのまま返すと
    // CDPの構造化複製でプロパティが運ばれず `{}` になる。engine
    // `puppeteer-page-evaluate-loses-svelte5-runes-proxy-objects` と同じ落とし穴)。
    async function sketchPlane() {
      const raw = await page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch) ?? null
        return JSON.stringify(active?.plane ?? null)
      })
      return JSON.parse(raw)
    }

    // ====================================================================== 通常表示の確認(復帰の比較対象)
    console.log('\n[0] 通常表示(スケッチ外)はXY平面+Z軸')
    await settle(page)
    const plain = await tiersNow()
    if (JSON.stringify(plain.planes) !== JSON.stringify(['XY']) || plain.axis !== 'Z') {
      fail(`[0] 通常はXY平面+Z軸のはず: ${JSON.stringify(plain)}`)
    }
    ok('[0] スケッチ外ではXY平面+Z軸だけが出ている')

    // ====================================================================== 任意面へのスケッチを起こす
    console.log('\n[1] 任意面(基準平面でない面)へのスケッチを起こす')
    const planarCandidates = await page.evaluate((bodyId) => {
      const geometry = window.__duocad.getGeometry(bodyId)
      return geometry.faces
        .filter((face) => face.kind === 'plane')
        .map((face) => face.index)
        .filter((index) => window.__duocad.projectFace(bodyId, index) !== null)
    }, bodyId)
    if (planarCandidates.length === 0) fail('平面状の面が画面から1枚も指せない(この裏取りが成立しない)')

    let onFace = null
    const attemptLog = []
    for (const faceIndex of planarCandidates) {
      if (
        await page.evaluate(
          () => document.querySelector('.command-host')?.dataset.command !== 'sketch_create',
        )
      ) {
        await page.click('#sketch-create')
      }
      const at = await locateFace(bodyId, faceIndex)
      if (!at) {
        attemptLog.push(`面${faceIndex}: 射影できない`)
        continue
      }
      await page.mouse.click(at.x, at.y)
      const created = await page
        .waitForFunction(() => window.__duocad.activeSketch !== null, { timeout: 15000, polling: 50 })
        .then(() => true)
        .catch(() => false)
      if (!created) {
        attemptLog.push(`面${faceIndex}: スケッチができない`)
        await page.keyboard.press('Escape')
        continue
      }
      await settle(page)
      const plane = await sketchPlane()
      const tiers = await tiersNow()
      // **`plane.kind==='face'` かつグリッドが `sketch-face` を選んでいること**の両方を見る ——
      // 基準平面と偶然同一平面の面(例: 箱の底面がXYと一致)を拾うと、`plane.kind` は `face` でも
      // グリッド側は基準平面(XY)として扱われる(`basePlaneForFrame` がframeの一致で見るため)。
      // この検査は「任意面のグリッド経路」を裏取りしたいので、後者まで一致する面だけを使う。
      if (plane?.kind === 'face' && JSON.stringify(tiers.planes) === JSON.stringify(['sketch-face'])) {
        onFace = { faceIndex, plane, tiers }
        break
      }
      attemptLog.push(`面${faceIndex}: plane=${JSON.stringify(plane)} tiers=${JSON.stringify(tiers)}`)
      await page.click('#sketch-exit')
      await page.waitForFunction(() => window.__duocad.activeSketch === null, { timeout, polling: 50 })
      // MCPで作った箱の面から作ったスケッチなので、undoで消してから次を試す(smoke.mjsと同じ流儀)。
      const before = await page.evaluate(() => window.__duocad.sketches.length)
      await mcp.callTool('undo')
      await page.waitForFunction(
        (expected) => window.__duocad.sketches.length < expected,
        { timeout, polling: 100 },
        before,
      )
    }
    if (onFace === null) {
      fail(`任意面へのスケッチが1つも作れなかった: ${attemptLog.join(' | ')}`)
    }
    ok(`既存ソリッドの面(面${onFace.faceIndex})にスケッチを作れた(plane=${JSON.stringify(onFace.plane)})`)

    // ====================================================================== 完了条件1・2・3・4
    console.log('\n[2] 完了条件1・2・3・4をgridTiers()で見る')
    const tiers = onFace.tiers
    if (JSON.stringify(tiers.planes) !== JSON.stringify(['sketch-face'])) {
      fail(`[1] 任意面のグリッド(sketch-face)だけが出ているはず: ${JSON.stringify(tiers.planes)}`)
    }
    ok('[1] 任意面へのスケッチ中、その面のグリッド(sketch-face)が出ている')
    if (tiers.planes.includes('XY')) fail('[2] XY床が隠れていない')
    ok('[2] XY床は隠れている(出ている平面は面のグリッドだけ)')
    if (tiers.axis !== null) fail(`[3] 直交軸を出さないはずが出ている: ${tiers.axis}`)
    ok('[3] 直交軸は出ていない(本人指定どおり)')
    if (typeof tiers.sketchFaceExtent !== 'number' || !Number.isFinite(tiers.sketchFaceExtent) || tiers.sketchFaceExtent <= 0) {
      fail(`[条件40-45行] 面の外接範囲で切っているはずのextentが不正: ${tiers.sketchFaceExtent}`)
    }
    ok(`面の外接範囲でグリッドを切っている(sketchFaceExtent=${tiers.sketchFaceExtent}mm。無限に伸びていない)`)
    // **原点・u/v はサーバの `SketchFrame` をコードが変形せずそのまま渡している**
    // (`App.svelte` の `sketchDraws()` → `setGridSketchFrame` → `desiredGridPlanes` の
    // `sketch-face` 分岐は `frame: sketchGridFrame` を素通しするだけで、コピーも計算もしない)。
    // ここでは、渡された面のframeが確かに基準平面と異なる(原点・向きがこの面固有)ことを
    // サーバのデータで直接確かめる —— 見た目の一致はこのすぐ下のスクリーンショットで見る。
    const frame = onFace.plane.frame
    if (!frame || !Array.isArray(frame.origin) || !Array.isArray(frame.u) || !Array.isArray(frame.v)) {
      fail(`[4] スケッチ平面のframeが届いていない: ${JSON.stringify(onFace.plane)}`)
    }
    ok(`[4] グリッドに渡ったframeは面固有のもの(origin=${JSON.stringify(frame.origin)} u=${JSON.stringify(frame.u)} v=${JSON.stringify(frame.v)})`)

    // ====================================================================== 完了条件6(z-fighting): スクリーンショット
    console.log('\n[3] z-fightingしていないことをスクリーンショットで残す')
    mkdirSync(screenshotDir, { recursive: true })
    const screenshotPath = join(screenshotDir, 'sketch-face-grid.png')
    await page.screenshot({ path: screenshotPath })
    ok(`スクリーンショットを保存した: ${screenshotPath}(目視でちらつき・二重線が無いことを確認する)`)

    // ====================================================================== 完了条件5: 抜けると通常表示へ戻る
    console.log('\n[4] 完了条件5: スケッチを抜けると通常表示(XY+Z軸)へ戻る')
    await page.click('#sketch-exit')
    await page.waitForFunction(() => window.__duocad.activeSketch === null, { timeout, polling: 50 })
    await settle(page)
    const backToPlain = await tiersNow()
    if (JSON.stringify(backToPlain.planes) !== JSON.stringify(['XY']) || backToPlain.axis !== 'Z') {
      fail(`[5] スケッチを抜けたら通常(XY+Z軸)へ戻るはず: ${JSON.stringify(backToPlain)}`)
    }
    ok('[5] スケッチを抜けると通常(XY平面+Z軸)へ戻る')

    const errors = await page.evaluate(() => window.__duocad.errors ?? [])
    if (errors.length > 0) fail(`走行中にエラーが出た: ${JSON.stringify(errors)}`)
    if (consoleErrors.length > 0) fail(`コンソールにエラー: ${consoleErrors.join(' / ')}`)
    ok('走行中にエラーは1件も出ていない')
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

main().catch((error) => {
  console.error(`\nsmoke-face-grid: 失敗 — ${error.message}`)
  process.exitCode = 1
})
