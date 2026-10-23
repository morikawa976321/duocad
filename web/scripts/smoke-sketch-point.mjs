/**
 * 実機での確認 — **スケッチの点の図形**(`add_point`。docs/DESIGN_DECISIONS.md「図形（スケッチ）の表現」)。
 *
 *   web> node scripts/smoke-sketch-point.mjs
 *
 * 題材(MCP で作る): 20×20×10 の箱と、XY のスケッチ S に点を2つ(箱の横の P1 と、箱の真下に隠れる P2)。
 *
 *   1. 見える点はホバーとクリックで点として取れる(遮蔽の検査の対照)
 *   2. 箱に隠れた点を押しても、点ではなく手前の箱が選ばれる
 *   3. 矩形選択で点の図形が点として入る
 */
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, viewportCenter, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { clickAt, sketchPointAt } from './pick.mjs'

const P1 = [30, 5]
const P2 = [10, 10]

runSmoke('smoke-sketch-point', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  const { bodyId } = await addExtrudedRect(mcp, { corner: [0, 0], width: 20, height: 20, distance: 10 })
  const sketch = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  const p1 = (await mcp.callTool('add_point', { sketch_id: sketch, point: P1 })).entity_id
  await mcp.callTool('add_point', { sketch_id: sketch, point: P2 })
  await mcp.callTool('leave_sketch')
  await mcp.callTool('clear_selection')

  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.sketches ?? []).length >= 2 && window.__duocad.mode === 'idle',
  })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  const items = () => page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.selection?.items ?? [])))
  const clear = async () => {
    await mcp.callTool('clear_selection')
    await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, '選択が空にならない')
  }
  const hoverAndClick = async (u, v, what) =>
    withoutBrowserPanel(page, async () => {
      const at = await sketchPointAt(page, u, v, { sketchId: sketch, settle: true })
      if (!at) fail(`${what} が画面の中に無い`)
      await page.mouse.move(at.x, at.y)
      await waitFrames(page, 4)
      const hovered = await page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.hoveredRef())))
      await clickAt(page, at)
      return hovered
    })
  const zoomOut = async () => {
    await frameAll(page)
    const center = await viewportCenter(page)
    await page.mouse.move(center.x, center.y)
    for (let step = 0; step < 3; step += 1) {
      await page.mouse.wheel({ deltaY: 200 })
      await waitFrames(page, 3)
    }
    await waitFrames(page, 20)
  }
  await zoomOut()

  // ================================================================ 1. 見える点の図形(遮蔽の対照)
  const visible = await hoverAndClick(...P1, '点 P1')
  if (visible.sketchEntity?.entityId !== p1 || visible.sketchEntity?.point !== 'cx') {
    fail(`見える点がホバーで取れない: ${JSON.stringify(visible)}`)
  }
  await waitFor(
    (id) => {
      const selected = window.__duocad.selection?.items ?? []
      return selected.length === 1 && selected[0].entity === id && selected[0].point === 'cx'
    },
    '見える点をクリックしても点として選べない',
    [p1],
  )
  ok('point: 遮蔽の対照となる見える点はホバーとクリックで取れる')

  // ================================================================ 2. 隠れた点の図形
  await clear()
  const hovered = await hoverAndClick(...P2, '箱に隠れた点 P2')
  if (hovered.sketchEntity !== null) fail(`箱に隠れた点がホバーで光る: ${JSON.stringify(hovered)}`)
  await waitFor(() => (window.__duocad.selection?.items ?? []).length === 1, '箱に隠れた点を押しても何も選ばれない')
  const under = await items()
  if (under[0].body !== bodyId) fail(`箱に隠れた点を押すと、手前の箱ではなく ${JSON.stringify(under)} が選ばれる`)
  ok('point: 箱に隠れた点の図形は取れず、手前の箱が取れる')

  // ================================================================ 3. 矩形選択
  await clear()
  await withoutBrowserPanel(page, async () => {
    const at = await sketchPointAt(page, ...P1, { sketchId: sketch, settle: true })
    if (!at) fail('点 P1 が画面の中に無い')
    await page.mouse.move(at.x - 15, at.y - 15)
    await page.mouse.down()
    await page.mouse.move(at.x, at.y, { steps: 4 })
    await page.mouse.move(at.x + 15, at.y + 15, { steps: 4 })
    await page.mouse.up()
  })
  await waitFor(
    (id) => (window.__duocad.selection?.items ?? []).some((item) => item.entity === id && item.point === 'cx'),
    '矩形選択で点 P1 が点として入らない',
    [p1],
  )
  ok('point: 矩形選択で点の図形が点として入る')

  await assertNoAppErrors(page, consoleErrors)
})
