/**
 * 実機での確認 — **ボディ由来のスケッチの吸着先**(`get_sketch_guides`。スケッチの平面と交わる・接するボディの点と線へ、
 * 点を待つ間だけ吸着する)。
 *
 *   web> node scripts/smoke-sketch-guides.mjs [--shots <dir>]
 *
 * 題材(MCP で作る): 角 (0.3, 0.7)・40×30×10 の箱に、中心 (20.4, 15.6)・直径 10.4 の貫通穴。
 *
 *   1. 箱の上面に貼ったスケッチで線分を引くと、画素をずらしても始点は箱の角、終点は穴の中心に乗る(幽霊も確定も同じ座標)
 *   2. 上面の縁の途中は縁の線へ落ち、縁の中点は中点として吸着する
 *   3. 点を待っていない間(コマンド無し)はボディ由来の吸着先に当たらない
 *   4. 箱の途中(z = 5)を通る XY のオフセット平面のスケッチでも、立体に隠れた断面の角と穴の縁へ吸着する
 *
 * `--shots` を渡すと吸着している画面を PNG で残す(報告用。git に載せない場所へ)。
 */
import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedCircle, addExtrudedRect } from './mcp-client.mjs'
import { planarFace, sketchPointAt } from './pick.mjs'

const CORNER = [0.3, 0.7]
const SIZE = [40, 30, 10]
const HOLE = [20.4, 15.6]
const HOLE_RADIUS = 5.2

runSmoke('smoke-sketch-guides', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout, options }) => {
  const { bodyId: box } = await addExtrudedRect(mcp, { corner: CORNER, width: SIZE[0], height: SIZE[1], distance: SIZE[2] })
  await addExtrudedCircle(mcp, { center: HOLE, radius: HOLE_RADIUS, distance: SIZE[2], mode: 'cut' })

  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => window.__duocad?.mode === 'idle' && (window.__duocad?.parts ?? []).length >= 1,
  })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  const shotsDir = options.shots ?? null
  if (shotsDir !== null) mkdirSync(shotsDir, { recursive: true })
  /** カーソルを置いたまま撮る(パネルを畳むとカーソルの下の当たりが変わらないよう、撮るのは `hoverNear` の中)。 */
  const shot = async (name) => {
    if (shotsDir === null) return
    await waitFrames(page, 8)
    const file = path.join(shotsDir, `${name}.png`)
    await page.screenshot({ path: file })
    console.log(`  shot: ${file}`)
  }
  const frame = () => page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.sketch.frame)))
  const uvOf = (f, point) => {
    const d = point.map((value, axis) => value - f.origin[axis])
    const dot = (axis) => d[0] * axis[0] + d[1] * axis[1] + d[2] * axis[2]
    return [dot(f.u), dot(f.v)]
  }
  const worldOf = (f, [u, v]) => f.origin.map((value, axis) => value + u * f.u[axis] + v * f.v[axis])
  const close = (a, b, tolerance = 1e-6) => a.length === b.length && a.every((value, index) => Math.abs(value - b[index]) <= tolerance)

  /**
   * (u,v) から画素で `nudge` ずらした所へカーソルを置き、ボディ由来の吸着先の当たりと幽霊の座標を返す。`click` なら
   * 同じ画素を押す(パネルを畳んだまま。戻した後に押すと、畳んでいた間の画素がパネルに覆われていることがある)。
   */
  const hoverNear = async ([u, v], nudge, what, { expectGuide = true, click = false, shotName = null } = {}) =>
    withoutBrowserPanel(page, async () => {
      const at = await sketchPointAt(page, u, v, { settle: true })
      if (!at) fail(`${what} が画面の中に無い`)
      await page.mouse.move(at.x + nudge[0], at.y + nudge[1])
      if (expectGuide) {
        await waitFor(() => window.__duocad.hoveredRef()?.sketchGuide != null, `${what} の近くでボディ由来の吸着先に当たらない`)
      } else {
        await waitFrames(page, 6)
      }
      const seen = await page.evaluate(() => ({
        guide: JSON.parse(JSON.stringify(window.__duocad.hoveredRef()?.sketchGuide ?? null)),
        hover: window.__duocad.hoverPoint ? [...window.__duocad.hoverPoint] : null,
      }))
      if (shotName !== null) await shot(shotName)
      if (click) await page.mouse.click(at.x + nudge[0], at.y + nudge[1])
      return seen
    })
  const lines = () =>
    page.evaluate(() =>
      (window.__duocad.sketch?.entities ?? []).filter((item) => item.kind === 'line').map((item) => ({ ...item.params })),
    )

  // ================================================================ 1. 上面に貼ったスケッチ: 角と穴の中心
  await frameAll(page)
  const top = await planarFace(page, box, [0, 0, 1], { at: { axis: 2, value: SIZE[2] } })
  await mcp.callTool('modify_selection', { add: [{ body: box, kind: 'face', index: top }] })
  const faceSketch = (await mcp.callTool('sketch_create', {})).sketch_id
  await waitFor((id) => window.__duocad.activeSketch === id, '上面のスケッチへ入らない', [faceSketch])
  await waitFrames(page, 6)
  const f = await frame()
  const cornerUv = uvOf(f, [CORNER[0], CORNER[1], SIZE[2]])
  const holeUv = uvOf(f, [HOLE[0], HOLE[1], SIZE[2]])

  await page.click('#sketch-line')
  await waitFor(() => window.__duocad.command?.verb === 'add_line', '線分を起動できない')
  const start = await hoverNear(cornerUv, [3, -2], '箱の角', { click: true })
  if (start.guide.kind !== 'point' || !close(start.guide.snapAt, cornerUv)) fail(`箱の角へ吸着しない: ${JSON.stringify(start)}`)
  if (!close(start.hover, start.guide.snapAt, 0)) fail(`幽霊が吸着先に乗らない: ${JSON.stringify(start)}`)
  const end = await hoverNear(holeUv, [-3, 2], '穴の中心', { click: true, shotName: 'guides-1-hole-center' })
  if (end.guide.kind !== 'point' || !close(end.guide.snapAt, holeUv)) fail(`穴の中心へ吸着しない: ${JSON.stringify(end)}`)
  await waitFor(() => (window.__duocad.sketch?.entities ?? []).length === 1, '線分が置けない')
  const [placed] = await lines()
  // 終点は線分の `build` が長さと角度を通して組み直すので、末尾の桁だけずれうる(始点はそのまま渡る)
  if (
    !close([placed.x0, placed.y0], start.guide.snapAt, 0) ||
    !close([placed.x1, placed.y1], end.guide.snapAt, 1e-9)
  ) {
    fail(`置いた線分が吸着先と一致しない: ${JSON.stringify(placed)} / ${JSON.stringify([start.guide, end.guide])}`)
  }
  ok('[1] 上面のスケッチで、画素をずらしても線分の始点は箱の角・終点は穴の中心に乗る(幽霊と確定が同じ座標)')

  // ================================================================ 2. 縁の線と中点
  await page.click('#sketch-line')
  await waitFor(() => window.__duocad.command?.verb === 'add_line', '線分を起動できない(2本目)')
  const edgeY = CORNER[1]
  const onEdge = await hoverNear(uvOf(f, [CORNER[0] + 9.1, edgeY, SIZE[2]]), [0, 3], '上面の手前の縁', { shotName: 'guides-2-edge' })
  if (onEdge.guide.kind !== 'line') fail(`縁の途中で線に当たらない: ${JSON.stringify(onEdge)}`)
  const edgeWorld = worldOf(f, onEdge.guide.snapAt)
  if (Math.abs(edgeWorld[1] - edgeY) > 1e-6 || Math.abs(edgeWorld[2] - SIZE[2]) > 1e-6) {
    fail(`縁の線へ落ちない: ${JSON.stringify(edgeWorld)}`)
  }
  const middle = uvOf(f, [CORNER[0] + SIZE[0] / 2, edgeY, SIZE[2]])
  const atMiddle = await hoverNear(middle, [2, 2], '上面の手前の縁の中点')
  if (atMiddle.guide.kind !== 'midpoint' || !close(atMiddle.guide.snapAt, middle)) fail(`縁の中点へ吸着しない: ${JSON.stringify(atMiddle)}`)
  ok('[2] 縁の途中は縁の線へ落ち、縁の中点は中点として吸着する')

  // ================================================================ 3. 点を待っていない間は当たらない
  await page.keyboard.press('Escape')
  await waitFor(() => window.__duocad.command === null || window.__duocad.command?.verb !== 'add_line', '線分を止められない')
  const idle = await hoverNear(cornerUv, [3, -2], '箱の角(コマンド無し)', { expectGuide: false })
  if (idle.guide !== null) fail(`点を待っていないのにボディ由来の吸着先に当たる: ${JSON.stringify(idle)}`)
  ok('[3] 点を待っていない間はボディ由来の吸着先に当たらない')

  // ================================================================ 4. 箱を横切るオフセット平面: 隠れた断面
  await mcp.callTool('leave_sketch')
  const section = (await mcp.callTool('sketch_create_offset', { distance: SIZE[2] / 2, base_plane: 'XY' })).sketch_id
  await waitFor((id) => window.__duocad.activeSketch === id, '断面のスケッチへ入らない', [section])
  await waitFrames(page, 6)
  const g = await frame()
  await page.click('#sketch-line')
  await waitFor(() => window.__duocad.command?.verb === 'add_line', '線分を起動できない(断面)')
  const hiddenCorner = uvOf(g, [CORNER[0], CORNER[1], SIZE[2] / 2])
  const sectionCorner = await hoverNear(hiddenCorner, [3, 3], '断面の角')
  if (sectionCorner.guide.kind !== 'point' || !close(sectionCorner.guide.snapAt, hiddenCorner)) {
    fail(`立体に隠れた断面の角へ吸着しない: ${JSON.stringify(sectionCorner)}`)
  }
  const rim = uvOf(g, [HOLE[0] + HOLE_RADIUS * Math.cos(1), HOLE[1] + HOLE_RADIUS * Math.sin(1), SIZE[2] / 2])
  const onRim = await hoverNear(rim, [2, 0], '断面の穴の縁', { shotName: 'guides-4-hidden-rim' })
  const rimWorld = worldOf(g, onRim.guide.snapAt)
  if (onRim.guide.kind !== 'line' || Math.abs(Math.hypot(rimWorld[0] - HOLE[0], rimWorld[1] - HOLE[1]) - HOLE_RADIUS) > 1e-6) {
    fail(`断面の穴の縁へ落ちない: ${JSON.stringify(onRim)} / ${JSON.stringify(rimWorld)}`)
  }
  ok('[4] 箱を横切る平面のスケッチでも、立体に隠れた断面の角と穴の縁へ吸着する')
  await page.keyboard.press('Escape')

  await assertNoAppErrors(page, consoleErrors)
})
