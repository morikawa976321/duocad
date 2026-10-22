/**
 * 実機での確認 — **測定でスケッチの点・線分を指す**(issue #201)。
 *
 *   web> node scripts/smoke-measure-sketch.mjs [--shots <dir>]
 *
 * 題材(MCP で作る): 20×20×10 の箱と、z=25 のオフセット平面のスケッチ S に点 P(箱の真上)・線分 L・円 C。
 *
 *   1. 測定を起動するとスケッチの点・線を指せる(`pickKinds`)
 *   2. 点 P と箱の上面で「点と平面 15 mm」
 *   3. 点 P と線分 L で「点と直線 20 mm」
 *   4. 円 C まるごとと点 P は理由付きで断る(トースト)
 *
 * `--shots` を渡すと各段の画面を PNG で残す(報告用。git に載せない場所へ)。
 */
import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { fail, ok } from './args.mjs'
import {
  assertNoAppErrors,
  frameAll,
  openApp,
  viewportCenter,
  waitFrames,
  waitOrFail,
  withoutBrowserPanel,
} from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { clickAt, clickFace, planarFace, sketchPointAt } from './pick.mjs'

const HEIGHT = 25
const P = [10, 10]
const L = [
  [30, 0],
  [30, 20],
]
const C = { center: [45, 10], diameter: 6 }

runSmoke('smoke-measure-sketch', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout, options }) => {
  const { bodyId } = await addExtrudedRect(mcp, { corner: [0, 0], width: 20, height: 20, distance: 10 })
  const sketch = (await mcp.callTool('sketch_create_offset', { distance: HEIGHT, base_plane: 'XY' })).sketch_id
  const point = (await mcp.callTool('add_point', { sketch_id: sketch, point: P })).entity_id
  const line = (await mcp.callTool('add_line', { sketch_id: sketch, p0: L[0], p1: L[1] })).entity_id
  const circle = (await mcp.callTool('add_circle', { sketch_id: sketch, center: C.center, diameter: C.diameter })).entity_id
  await mcp.callTool('leave_sketch')
  await mcp.callTool('clear_selection')

  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.sketches ?? []).length >= 2 && window.__duocad.mode === 'idle',
  })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  const shotsDir = options.shots ?? null
  if (shotsDir !== null) mkdirSync(shotsDir, { recursive: true })
  const shot = async (name) => {
    if (shotsDir === null) return
    await waitFrames(page, 6)
    const file = path.join(shotsDir, `${name}.png`)
    await page.screenshot({ path: file })
    console.log(`  shot: ${file}`)
  }
  const clickSketch = (u, v, what) =>
    withoutBrowserPanel(page, async () => {
      const at = await sketchPointAt(page, u, v, { sketchId: sketch, settle: true })
      if (!at) fail(`${what} が画面の中に無い`)
      await clickAt(page, at)
    })
  const startMeasure = async () => {
    await page.keyboard.press('Escape')
    await mcp.callTool('clear_selection')
    await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, '選択が空にならない')
    await page.click('#measure')
    await waitFor(
      () => window.__duocad.pickKinds.includes('sketchPoint') && window.__duocad.pickKinds.includes('sketchLine'),
      '測定の起動中にスケッチの点・線が指せない',
    )
  }
  const readout = () => page.evaluate(() => document.querySelector('#measure-result')?.textContent ?? '')

  // `frameAll` はボディだけに合わせるので、引いてスケッチの図形を画面に入れる(smoke-sketch-point.mjs と同じ手)。
  await frameAll(page)
  const center = await viewportCenter(page)
  await page.mouse.move(center.x, center.y)
  for (let step = 0; step < 3; step += 1) {
    await page.mouse.wheel({ deltaY: 200 })
    await waitFrames(page, 3)
  }
  await waitFrames(page, 20)
  await shot('201-0-before')

  // ================================================================ 1・2. 点と平面
  await startMeasure()
  ok('measure: 起動中はスケッチの点・線を指せる')
  await clickSketch(...P, '点 P')
  await waitFor(
    (id) => (window.__duocad.selection?.items ?? []).some((item) => item.entity === id && item.point === 'cx'),
    '点 P を押しても点として選ばれない',
    [point],
  )
  await shot('201-1-picking')
  const top = await planarFace(page, bodyId, [0, 0, 1])
  await withoutBrowserPanel(page, () => clickFace(page, bodyId, top))
  await waitFor(() => (document.querySelector('#measure-result')?.textContent ?? '').includes('点と平面'), '点と面の測定が出ない')
  const pointPlane = await readout()
  if (!pointPlane.includes('15.000 mm')) fail(`点 P と箱の上面の距離が 15 mm でない: ${pointPlane}`)
  await shot('201-2-point-plane')
  ok(`measure: スケッチの点と箱の上面 → ${pointPlane}`)

  // ================================================================ 3. 点と線分
  await startMeasure()
  await clickSketch(...P, '点 P')
  await waitFor(() => (window.__duocad.selection?.items ?? []).length === 1, '点 P が選ばれない')
  await clickSketch(L[0][0], (L[0][1] + L[1][1]) / 2, '線分 L の中点')
  await waitFor(() => (document.querySelector('#measure-result')?.textContent ?? '').includes('点と直線'), '点と線分の測定が出ない')
  const pointLine = await readout()
  if (!pointLine.includes('20.000 mm')) fail(`点 P と線分 L の距離が 20 mm でない: ${pointLine}`)
  await shot('201-3-point-line')
  ok(`measure: スケッチの点と線分 → ${pointLine}`)

  // ================================================================ 4. 円まるごとは断る
  await startMeasure()
  await clickSketch(C.center[0] + C.diameter / 2, C.center[1], '円 C の周')
  await waitFor(
    (id) => (window.__duocad.selection?.items ?? []).some((item) => item.entity === id && item.point === null),
    '円 C を押しても図形まるごととして選ばれない',
    [circle],
  )
  await clickSketch(...P, '点 P')
  const refusal = 'スケッチの図形まるごとで測れるのは線分だけ(選んだのは circle)。中心や端点は点として指せる'
  await waitFor((text) => (window.__duocad.errors ?? []).includes(text), '円まるごとの測定が断られない', [refusal])
  await shot('201-4-refused')
  ok('measure: 円まるごとは理由付きで断る')

  await assertNoAppErrors(page, consoleErrors, { allow: [refusal] })
})
