/**
 * 干渉チェック(issue #186)の画面を撮る撮影台(検査ではない。報告用)。
 *
 *   web> node scripts/shoot-interference.mjs --shots <dir>
 *
 * 場面(MCP で作る。どれも 20×20×20 の箱):
 *   A (0,0)  と B (10,0) は 10 mm 重なる(干渉)
 *   B と C (30,0) は面で接する(接触)
 *   C と D (50.1,0) は 0.1 mm 離れる(隙間 0.2 なら隙間不足)
 *   E (100,0) は離れている
 *
 * 撮るもの: 起動前 / 隙間 0 の結果(干渉の赤い立体と一覧) / 干渉の行を選んだところ / 隙間 0.2 の結果 /
 * 見えているボディが1つだけのときに断る画面。`--step <STEP のパス>` を渡すと、最後にその STEP を読んだ文書でも撮る(読むだけ)。
 */
import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { fail } from './args.mjs'
import { frameAll, openApp, sleep, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'

runSmoke('shoot-interference', { timeout: 120000, mcp: true }, async ({ browser, url, timeout, mcp, options }) => {
  const dir = options.shots
  if (dir === undefined) fail('--shots <dir> が要る')
  mkdirSync(dir, { recursive: true })
  const corners = [[0, 0], [10, 0], [30, 0], [50.1, 0], [100, 0]]
  const ids = []
  const sketchIds = []
  for (const corner of corners) {
    const made = await addExtrudedRect(mcp, { corner, width: 20, height: 20, distance: 20 })
    ids.push(made.bodyId)
    sketchIds.push(made.sketchId)
  }
  // 押し出しの元のスケッチの板が箱に重なって見えるので隠す
  for (const id of sketchIds) await mcp.callTool('set_sketch_visible', { sketch_id: id, visible: false })
  const names = ['A', 'B', 'C', 'D', 'E']
  for (let i = 0; i < ids.length; i += 1) await mcp.callTool('rename', { target: ids[i], name: names[i] })

  const { page } = await openApp(browser, url, { timeout, height: 900 })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  await waitFor(() => window.__duocad.parts.length === 5, '5つのボディが届く')
  await frameAll(page)
  await waitFrames(page, 10)
  const shot = async (name) => {
    await waitFrames(page, 8)
    const file = path.join(dir, `${name}.png`)
    await withoutBrowserPanel(page, async () => {
      await waitFrames(page, 8)
      await page.screenshot({ path: file })
    })
    console.log(`  shot: ${file}`)
  }
  await shot('186-0-before')

  await page.click('#interference')
  await waitFor(() => window.__duocad.interference.result !== null, '干渉チェックの結果が届く')
  await waitFor(() => window.__duocad.interference.solids >= 1, '赤い立体が載る')
  const first = await page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.interference.result)))
  console.log(`  clearance 0: ${JSON.stringify(first.counts)} ${first.seconds}s`)
  // 接触の見出しを開いて、畳まれた行も写す
  await page.evaluate(() => {
    for (const details of document.querySelectorAll('#interference-panel details')) details.open = true
  })
  await shot('186-1-result')

  await page.click('[data-pair]')
  await waitFor(() => document.querySelector('[data-pair].picked') !== null, '行を選ぶと強調される')
  await shot('186-2-picked')
  await page.click('[data-pair]')

  await page.click('#interference-clearance')
  await page.keyboard.down('Control')
  await page.keyboard.press('KeyA')
  await page.keyboard.up('Control')
  await page.keyboard.type('0.2')
  await page.click('#interference-run')
  await waitFor(() => window.__duocad.interference.result?.clearance === 0.2, '隙間 0.2 の結果が届く')
  const second = await page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.interference.result)))
  console.log(`  clearance 0.2: ${JSON.stringify(second.counts)} ${second.seconds}s`)
  await page.evaluate(() => {
    for (const details of document.querySelectorAll('#interference-panel details')) details.open = true
  })
  const short = await page.$('#interference-panel details[data-group="short"] [data-pair]')
  if (short !== null) {
    await short.click()
    await waitFor(() => document.querySelector('[data-pair].picked') !== null, '隙間不足の行を選べる')
  }
  await shot('186-3-clearance')
  await page.click('#interference-close')

  // 断る形: 見えているボディを1つにする
  for (const id of ids.slice(1)) await mcp.callTool('set_visible', { body_id: id, visible: false })
  await waitFor(() => (window.__duocad.state?.bodies ?? []).filter((body) => body.visible !== false).length === 1, '1つ以外を隠せない')
  await page.click('#interference')
  await sleep(1500)
  await shot('186-4-refused')
  console.log(`  errors: ${JSON.stringify(await page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.errors ?? []))))}`)

  if (options.step !== undefined) {
    await page.click('#file-new')
    await mcp.callTool('import_step', { path: options.step })
    await waitFor(() => window.__duocad.parts.length > 5, 'STEP のボディが届く')
    await frameAll(page)
    await waitFrames(page, 10)
    await page.click('#interference')
    await waitFor(() => window.__duocad.interference.result !== null, 'STEP の文書で結果が届く')
    const real = await page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.interference.result)))
    console.log(`  step: ${JSON.stringify(real.counts)} ${real.seconds}s`)
    await waitFor(() => window.__duocad.interference.solids >= 1, 'STEP の文書で赤い立体が載る')
    await shot('186-5-step')
  }
})
