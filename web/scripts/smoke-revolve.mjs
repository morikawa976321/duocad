/**
 * 実機での確認 — **回転体の軸に指した線分が、回す領域の選択と混ざらない**。
 *
 *   web> node scripts/smoke-revolve.mjs
 *
 * `revolve` の軸の線分は、領域の選択とは別チャンネル(`PickArgSpec`)で受け取る。混ざると
 * 選択に線分が入り、サーバの `require_selected(region)` が対象の数を取り違える。
 *
 *   1. 「線分を選ぶ」で軸のピックが開き、線分を指しても領域の選択は変わらない
 *   2. Enter で確定すると、x 軸上の線分を軸にした管(体積 π·(15²−5²)·20)ができる
 *
 * 題材(長方形と軸の線分)は MCP で描き、回転体は画面から掛ける。
 */
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, openApp, waitFrames, waitOrFail } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { clickSketchPoint } from './pick.mjs'

/** 長方形 (0,5)-(20,15) を、x 軸上の線分 (0,0)-(20,0) のまわりに回す。 */
const EXPECTED_VOLUME = Math.PI * (15 ** 2 - 5 ** 2) * 20

runSmoke('smoke-revolve', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  const sketchId = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  await mcp.callTool('add_rectangle', { sketch_id: sketchId, corner: [0, 5], width: 20, height: 10 })
  const axisId = (await mcp.callTool('add_line', { sketch_id: sketchId, p0: [0, 0], p1: [20, 0] })).entity_id

  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.sketches ?? []).length >= 1,
  })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  const selectionItems = () =>
    page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.selection?.items ?? [])))

  await page.click(`[data-sketch-id="${sketchId}"] .sketch-name`)
  await waitFor((id) => window.__duocad.activeSketch === id, 'スケッチへ入れない', [sketchId])
  await page.click('#sketch-look')
  await waitFrames(page, 8)

  // ================================================================ 1. 領域を選び、軸の線分を指す
  await clickSketchPoint(page, 10, 10)
  await waitFor(
    () => (window.__duocad.selection?.items ?? []).some((item) => item.index !== undefined && item.sketch !== undefined),
    '長方形の内側を押しても領域が選べない',
  )
  const regionsBefore = await selectionItems()

  await page.click('#revolve')
  await page.waitForSelector('#revolve-axis-line', { timeout })
  await page.click('#revolve-axis-line')
  await waitFor(() => window.__duocad.wantedPick === 'sketchEntity', '「線分を選ぶ」で軸のピックが開かない')
  ok('revolve: 「線分を選ぶ」で軸の線分のピックが開く')

  await clickSketchPoint(page, 10, 0)
  await waitFor(
    () => (document.querySelector('#revolve-axis-line-pick-value')?.textContent ?? '').trim() === '図形1つ',
    '線分をクリックしても軸として入らない',
  )
  const selectedNow = await selectionItems()
  if (selectedNow.some((item) => item.entity === axisId)) {
    fail(`軸に指した線分が領域の選択へ入った: ${JSON.stringify(selectedNow)}`)
  }
  if (JSON.stringify(selectedNow) !== JSON.stringify(regionsBefore)) {
    fail(`軸を指したら領域の選択が変わった: ${JSON.stringify(selectedNow)}(前 ${JSON.stringify(regionsBefore)})`)
  }
  ok('revolve: 軸に指した線分は領域の選択へ入らない(別チャンネル)')

  // ================================================================ 2. 確定
  await page.click('#revolve-mode-join')
  await page.keyboard.press('Enter')
  try {
    await waitOrFail(page, () => window.__duocad.lastApplied?.verb === 'revolve', '確定しても回転体が適用されない', { timeout })
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
    fail(`回転体の体積が合わない: ${volume}(期待 ${EXPECTED_VOLUME})。body=${JSON.stringify(body)}`)
  }
  ok(`revolve: 線分を軸にした管ができた(体積 ${volume.toFixed(3)})`)

  await assertNoAppErrors(page, consoleErrors)
})
