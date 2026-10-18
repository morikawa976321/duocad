/**
 * 実機での確認 — **スケッチの点の図形**(`add_point`。docs/DESIGN_DECISIONS.md「図形（スケッチ）の表現」)。
 *
 *   web> node scripts/smoke-sketch-point.mjs
 *
 * 題材(MCP で作る): 20×20×10 の箱と、XY のスケッチ S に点を2つ(箱の横の P1 と、箱の真下に隠れる P2)。
 *
 *   1. 編集していないスケッチの点の図形は、ホバーで光りクリックで選べる。選択は点 `point: "cx"`
 *   2. 箱に隠れた点の図形は取れず、手前の箱が取れる
 *   3. 矩形選択で点の図形が点として入る
 *   4. AI が `point` を省いて選んでも点 `cx` として入る
 *   5. スケッチモードで「点」を押して1クリックで置ける(Enter 無し)。置いた点はすぐ指せる
 *   6. 「正面から見る」は点の図形にも合わせる(点だけのスケッチの遠くの点が画面に入る)
 */
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, viewportCenter, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { clickAt, sketchPointAt } from './pick.mjs'

const P1 = [30, 5]
const P2 = [10, 10]
const P3 = [40, 5]
const FAR = [300, 0]

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
  const hasPoint = (entity) =>
    page.evaluate(
      (id) => (window.__duocad.selection?.items ?? []).some((item) => item.entity === id && item.point === 'cx'),
      entity,
    )
  const hoverAndClick = async (u, v, what, sketchId = sketch) =>
    withoutBrowserPanel(page, async () => {
      const at = await sketchPointAt(page, u, v, { sketchId, settle: true })
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

  // ================================================================ 1. 編集していないスケッチの点の図形
  let hovered = await hoverAndClick(...P1, '点 P1')
  if (hovered.sketchEntity?.entityId !== p1 || hovered.sketchEntity?.point !== 'cx') {
    fail(`点 P1 にホバーしても光らない: ${JSON.stringify(hovered)}`)
  }
  await waitFor(
    (id) => (window.__duocad.selection?.items ?? []).some((item) => item.entity === id && item.point === 'cx'),
    '点 P1 をクリックしても点として選べない',
    [p1],
  )
  ok('point: 編集していないスケッチの点の図形はホバーで光り、クリックで点として選べる')

  // ================================================================ 2. 隠れた点の図形
  await clear()
  hovered = await hoverAndClick(...P2, '箱に隠れた点 P2')
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

  // ================================================================ 4. AI が point を省いて選ぶ
  await mcp.callTool('set_selection', { refs: [{ sketch, entity: p1 }] })
  await waitFor(
    (id) => {
      const selected = window.__duocad.selection?.items ?? []
      return selected.length === 1 && selected[0].entity === id && selected[0].point === 'cx'
    },
    'point を省いた参照が点 cx にならない',
    [p1],
  )
  ok('point: AI が point を省いて選んでも点 cx として入る')

  // ================================================================ 5. スケッチモードで置く
  await clear()
  await mcp.callTool('enter_sketch', { sketch_id: sketch })
  await waitFor((id) => window.__duocad.activeSketch === id, 'スケッチ S に入れない', [sketch])
  await waitFrames(page, 30)
  await zoomOut()
  const before = await page.evaluate(() => window.__duocad.sketch.entities.length)
  await page.click('#sketch-point')
  await withoutBrowserPanel(page, async () => {
    const at = await sketchPointAt(page, ...P3, { settle: true })
    if (!at) fail('点を置く位置が画面の中に無い')
    await page.mouse.click(at.x, at.y)
  })
  await waitFor((count) => window.__duocad.sketch?.entities.length === count, '「点」で1クリックしても点が置けない', [before + 1])
  const placed = await page.evaluate(() => {
    const entity = window.__duocad.sketch.entities.at(-1)
    return { id: entity.entity_id, kind: entity.kind, params: { ...entity.params } }
  })
  if (placed.kind !== 'point' || Math.hypot(placed.params.cx - P3[0], placed.params.cy - P3[1]) > 0.5) {
    fail(`置いた点が違う: ${JSON.stringify(placed)}`)
  }
  await waitFor(() => window.__duocad.command === null, '点を置いた後に無起動へ戻らない')
  hovered = await hoverAndClick(placed.params.cx, placed.params.cy, '置いた点', null)
  if (hovered.sketchEntity?.entityId !== placed.id) fail(`置いた点にホバーしても光らない: ${JSON.stringify(hovered)}`)
  await waitFor(
    (id) => (window.__duocad.selection?.items ?? []).some((item) => item.entity === id && item.point === 'cx'),
    'スケッチモードで置いた点を選べない',
    [placed.id],
  )
  if (!(await hasPoint(placed.id))) fail('置いた点が点として選ばれていない')
  ok(`point: スケッチモードで「点」を押して1クリックで置け(${placed.params.cx.toFixed(2)}, ${placed.params.cy.toFixed(2)})、すぐ指せる`)

  // ================================================================ 6. 正面から見るは点の図形にも合わせる
  await mcp.callTool('add_point', { sketch_id: sketch, point: FAR })
  await waitFor((count) => window.__duocad.sketch?.entities.length === count, '遠くの点が届かない', [before + 2])
  await page.click('#sketch-look')
  await waitFrames(page, 60)
  const far = await withoutBrowserPanel(page, () => sketchPointAt(page, ...FAR, { settle: true }))
  if (!far) fail(`正面から見ても、点だけのスケッチの遠くの点 ${JSON.stringify(FAR)} が画面の外`)
  ok('point: 正面から見ると点の図形まで画面に入る(点だけのスケッチでも)')

  await assertNoAppErrors(page, consoleErrors)
})
