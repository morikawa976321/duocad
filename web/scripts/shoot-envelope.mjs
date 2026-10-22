/**
 * 包む(envelope_regions)の画面を撮る撮影台(検査ではない。報告用。issue #206)。
 *
 *   web> node scripts/shoot-envelope.mjs --shots <dir>
 *
 * 場面(MCP で作る): XY のスケッチに、20×10・30度の楕円と、それに重なる 10×6 の長方形、離れた直径 6 の円。
 *   1. 起動前(楕円と長方形を選んだところ)
 *   2. R = 2・d = 1.5 の幽霊(楕円の弧のずらし曲線は3次ベジエで近似される)
 *   3. 確定後
 *   4. 離れた円も選ぶと2つに分かれ、幽霊は警告色、確定はトーストで断る
 */
import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { fail } from './args.mjs'
import { openApp, pressUndo, sleep, typeInto, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { sketchPointAt } from './pick.mjs'
import { runSmoke } from './harness.mjs'

runSmoke('shoot-envelope', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout, options }) => {
  const dir = options.shots
  if (dir === undefined) fail('--shots <dir> が要る')
  mkdirSync(dir, { recursive: true })
  const sketch = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  await mcp.callTool('add_ellipse', { sketch_id: sketch, center: [0, 0], width: 20, height: 10, angle: 30 })
  await mcp.callTool('add_rectangle', { sketch_id: sketch, corner: [6, -3], width: 10, height: 6 })
  await mcp.callTool('add_circle', { sketch_id: sketch, center: [40, 0], diameter: 6 })

  const { page } = await openApp(browser, url, { timeout, until: () => window.__duocad?.mode === 'sketch' })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  await page.click('#sketch-look')
  await waitFrames(page, 10)
  // 楕円と長方形の間(u = 12)へ寄る(`#sketch-look` は円まで入れるので図形が小さい)
  const focus = await sketchPointAt(page, 14, 0, { sketchId: sketch, settle: true })
  await page.mouse.move(focus.x, focus.y)
  for (let step = 0; step < 4; step += 1) {
    await page.mouse.wheel({ deltaY: -200 })
    await waitFrames(page, 3)
  }
  const shot = async (name) => {
    const file = path.join(dir, `${name}.png`)
    await withoutBrowserPanel(page, async () => {
      // 入力の後のプレビューは間を置いて頼まれ、パネルを畳むと視点の寸法が変わる。幽霊が描き直されるのを待つ
      await sleep(1500)
      await waitFrames(page, 8)
      await page.screenshot({ path: file })
    })
    console.log(`  shot: ${file}`)
  }
  const regions = await page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.sketch?.regions ?? [])))
  // 円の領域(直径 6、面積 9π)以外が楕円と長方形の領域
  const isCircle = (region) => Math.abs(region.area - 9 * Math.PI) < 1e-6
  const near = regions.filter((region) => !isCircle(region)).map((region) => region.index)
  const all = regions.map((region) => region.index)
  console.log(`  regions: ${JSON.stringify(regions.map((region) => [region.index, region.area]))}`)

  await page.click('#sketch-envelope')
  await mcp.callTool('set_selection', { refs: near.map((index) => ({ sketch, index })) })
  await waitFor((count) => (window.__duocad.selection?.items ?? []).length === count, '楕円と長方形の領域が選ばれない', [near.length])
  await shot('206-0-before')
  await typeInto(page, '#sketch-envelope-radius', '2')
  await typeInto(page, '#sketch-envelope-offset', '1.5')
  await waitFor(() => window.__duocad.previewing === true, '包む幽霊が出ない')
  await shot('206-1-preview')
  await page.keyboard.press('Enter')
  await waitFor(() => window.__duocad.lastApplied?.verb === 'envelope_regions', '包むが確定しない')
  const applied = await page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.lastApplied)))
  console.log(`  applied: ${JSON.stringify(applied).slice(0, 300)}`)
  await shot('206-2-after')

  await pressUndo(page)
  await waitFor(() => (window.__duocad.sketch?.entities ?? []).length === 3, 'Ctrl+Z で包んだ線が消えない')
  await waitFrames(page, 10)
  if ((await page.$('#sketch-envelope-radius')) === null) await page.click('#sketch-envelope')
  await page.waitForSelector('#sketch-envelope-radius', { timeout })
  await mcp.callTool('set_selection', { refs: all.map((index) => ({ sketch, index })) })
  await typeInto(page, '#sketch-envelope-radius', '2')
  await typeInto(page, '#sketch-envelope-offset', '0')
  await waitFor(() => window.__duocad.previewing === true, '分かれる幽霊が出ない')
  await shot('206-3-warn')
  await page.keyboard.press('Enter')
  await waitFor(() => (window.__duocad.errors ?? []).some((line) => line.includes('個に分かれます')), '分かれる包みを断らない')
  await shot('206-4-refused')
})
