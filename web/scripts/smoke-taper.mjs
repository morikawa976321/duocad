/**
 * 実機での確認 — **押し出しのテーパー角の欄が、打った角度のままサーバへ届く**。
 *
 *   web> node scripts/smoke-taper.mjs
 *
 *   1. 押し出しを起動すると、テーパー角の欄が既定 0 で出る
 *   2. 長方形 20×10 を距離 10・テーパー 8°(太る)で新規ボディにすると、体積が角錐台の値になる
 *
 * 題材の長方形は MCP で描き、押し出しは画面から掛ける。
 */
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, openApp, waitFrames, waitOrFail } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { clickSketchPoint } from './pick.mjs'

/** 断面は距離 z で各辺が z·tan8° ずつ外へ出る: ∫(20+2az)(10+2az)dz (0..10)。 */
const SLOPE = Math.tan((8 * Math.PI) / 180)
const EXPECTED_VOLUME = 2000 + 3000 * SLOPE + (4000 / 3) * SLOPE ** 2

runSmoke('smoke-taper', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  const sketchId = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  await mcp.callTool('add_rectangle', { sketch_id: sketchId, corner: [0, 0], width: 20, height: 10 })

  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.sketches ?? []).length >= 1,
  })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })

  await page.click(`[data-sketch-id="${sketchId}"] .sketch-name`)
  await waitFor((id) => window.__duocad.activeSketch === id, 'スケッチへ入れない', [sketchId])
  await page.click('#sketch-look')
  await waitFrames(page, 8)

  // ================================================================ 1. 欄が既定 0 で出る
  await clickSketchPoint(page, 10, 5)
  await waitFor(
    () => (window.__duocad.selection?.items ?? []).some((item) => item.index !== undefined && item.sketch !== undefined),
    '長方形の内側を押しても領域が選べない',
  )
  await page.click('#extrude')
  await page.waitForSelector('#extrude-taper', { timeout })
  const initial = await page.$eval('#extrude-taper', (input) => input.value)
  if (Number(initial) !== 0) fail(`テーパー角の欄の既定が 0 でない: ${JSON.stringify(initial)}`)
  ok('extrude: テーパー角の欄が既定 0 で出る')

  // ================================================================ 2. 打った角度で押し出される
  await page.click('#distance')
  await page.type('#distance', '10', { delay: 20 })
  await page.click('#extrude-taper', { clickCount: 3 })
  await page.type('#extrude-taper', '8', { delay: 20 })
  await page.click('#extrude-mode-new')
  await page.keyboard.press('Enter')
  try {
    await waitOrFail(page, () => window.__duocad.lastApplied?.verb === 'extrude', '確定しても押し出しが適用されない', { timeout })
  } catch (error) {
    const state = await page.evaluate(() =>
      JSON.parse(JSON.stringify({ errors: window.__duocad.errors, command: window.__duocad.command })),
    )
    fail(`${error.message}: ${JSON.stringify(state)}`)
  }
  const document = await mcp.callTool('describe_document', {})
  const body = (document.bodies ?? [])[0]
  const volume = body?.volume
  if (typeof volume !== 'number' || Math.abs(volume - EXPECTED_VOLUME) > 1e-6 * EXPECTED_VOLUME) {
    fail(`テーパー付きの押し出しの体積が合わない: ${volume}(期待 ${EXPECTED_VOLUME})。body=${JSON.stringify(body)}`)
  }
  ok(`extrude: テーパー 8° で太る角錐台ができた(体積 ${volume.toFixed(3)})`)

  await assertNoAppErrors(page, consoleErrors)
})
