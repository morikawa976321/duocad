/**
 * 実機での確認 — **ロフトは通常モードで、別々のスケッチの領域を選んだ順につなぐ**。
 *
 *   web> node scripts/smoke-loft.mjs
 *
 * 通常モードで領域を要求するのはロフトだけ。起動していないときは領域が当たらず(面の選択だけ)、
 * 起動中だけ全スケッチの領域が当たる(`App.svelte` の `wantsRegions` → `viewport.pickRegions`)。
 *
 *   1. ロフトを起動する前は、通常モードで領域を押しても選択に入らない
 *   2. 起動すると、XY の正方形と 30 上の円を順にクリックでき、選んだ順で選択に入る
 *   3. Enter で確定すると、四角から丸へ変わる立体ができる(体積は円柱 π·10²·30 と角柱 20·20·30 の間)
 *
 * 題材(2つのスケッチ)は MCP で描き、ロフトは画面から掛ける。
 */
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, waitFrames, waitOrFail } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { clickSketchPoint } from './pick.mjs'

const CYLINDER = Math.PI * 100 * 30
const PRISM = 20 * 20 * 30

runSmoke('smoke-loft', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  const bottom = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  await mcp.callTool('add_rectangle', { sketch_id: bottom, corner: [-10, -10], width: 20, height: 20 })
  const top = (await mcp.callTool('sketch_create_offset', { base_plane: 'XY', distance: 30 })).sketch_id
  await mcp.callTool('add_circle', { sketch_id: top, center: [0, 0], diameter: 20 })
  await mcp.callTool('leave_sketch')
  await mcp.callTool('clear_selection')

  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.sketches ?? []).length >= 2,
  })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  const regionItems = () =>
    page.evaluate(() =>
      (window.__duocad.selection?.items ?? [])
        .filter((item) => item.index !== undefined && item.sketch !== undefined)
        .map((item) => item.sketch),
    )
  await frameAll(page)
  await waitFrames(page, 8)

  // ================================================================ 1. 起動前は領域が当たらない
  await clickSketchPoint(page, 5, -5, { sketchId: bottom })
  await waitFrames(page, 6)
  if ((await regionItems()).length !== 0) fail('ロフトを起動する前に、通常モードで領域が選べてしまう')
  ok('loft: 起動前は通常モードで領域が当たらない')

  // ================================================================ 2. 起動して、2つのスケッチの領域を順に選ぶ
  await page.click('#loft')
  await page.waitForSelector('#loft-mode-join', { timeout })
  await clickSketchPoint(page, 5, -5, { sketchId: bottom })
  await waitFor((id) => (window.__duocad.selection?.items ?? []).some((item) => item.sketch === id), '下の正方形を押しても領域が選べない', [bottom])
  await clickSketchPoint(page, 0, 0, { sketchId: top })
  await waitFor((id) => (window.__duocad.selection?.items ?? []).some((item) => item.sketch === id), '上の円を押しても領域が選べない', [top])
  const order = await regionItems()
  if (JSON.stringify(order) !== JSON.stringify([bottom, top])) {
    fail(`選んだ順に領域が並ばない: ${JSON.stringify(order)}(期待 ${JSON.stringify([bottom, top])})`)
  }
  ok('loft: 起動中は別々のスケッチの領域を選べ、選んだ順に並ぶ')

  // ================================================================ 3. 確定
  await page.click('#loft-mode-join')
  await page.keyboard.press('Enter')
  try {
    await waitOrFail(page, () => window.__duocad.lastApplied?.verb === 'loft', '確定してもロフトが適用されない', { timeout })
  } catch (error) {
    const state = await page.evaluate(() =>
      JSON.parse(JSON.stringify({ errors: window.__duocad.errors, command: window.__duocad.command })),
    )
    fail(`${error.message}: ${JSON.stringify(state)}`)
  }
  const document = await mcp.callTool('describe_document', {})
  const volume = (document.bodies ?? [])[0]?.volume
  if (typeof volume !== 'number' || !(volume > CYLINDER && volume < PRISM)) {
    fail(`ロフトの体積が四角から丸への立体にならない: ${volume}(${CYLINDER.toFixed(1)} と ${PRISM} の間のはず)`)
  }
  ok(`loft: 四角から丸へ変わる立体ができた(体積 ${volume.toFixed(3)})`)

  await assertNoAppErrors(page, consoleErrors)
})
