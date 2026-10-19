/**
 * テーパー付き押し出しのゴースト(確定前のプレビュー)を、題材とパラメータを変えて撮る。
 *
 *   web> node scripts/shoot-taper-ghost.mjs [--out <dir>] [--only <場面名の一部>]
 *
 * 場面ごとにサーバを立て直し、題材を MCP で描いてから、画面で押し出しを起動して引数を欄に打つ。`window.__duocad.previewing` が立ったら斜めから撮る。確定はしない。
 * 出力は `<out>/<場面>.png`(既定 `screenshots/taper-ghost`)。
 */
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fail, ok } from './args.mjs'
import { frameAll, openApp, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { clickSketchPoint, planarFace } from './pick.mjs'
import { SCREENSHOTS_DIR } from './server.mjs'

/** 斜め上から見る(右ドラッグで回す。`viewport.ts` の `controls.mouseButtons.RIGHT = ROTATE`)。 */
async function lookObliquely(page) {
  const box = await page.evaluate(() => {
    const rect = document.querySelector('canvas').getBoundingClientRect()
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
  })
  await page.mouse.move(box.x, box.y)
  await page.mouse.down({ button: 'right' })
  await page.mouse.move(box.x - 120, box.y - 90, { steps: 12 })
  await page.mouse.up({ button: 'right' })
  await waitFrames(page, 4)
  await frameAll(page)
  await waitFrames(page, 30)
}

/** SVG の涙形(上が尖り、下がベジエの丸み)。1 ユーザー単位 = 1 mm。 */
const TEARDROP_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="30mm" height="30mm" viewBox="0 0 30 30">' +
  '<path d="M 15 2 C 22 12 28 18 22 25 C 18 29 12 29 8 25 C 2 18 8 12 15 2 Z"/></svg>'

const SCENES = [
  { name: '01-rect-plus10', what: '長方形 20×10、距離10、+10°(太る)、新規', sketch: 'rect', args: { distance: 10, taper: 10, mode: 'new' } },
  { name: '02-rect-minus10', what: '長方形 20×10、距離10、−10°(細る)、新規', sketch: 'rect', args: { distance: 10, taper: -10, mode: 'new' } },
  { name: '03-holed-plus8', what: '穴あき板 40×30・穴Φ12、距離8、+8°(外は太り穴は狭まる)', sketch: 'holed', args: { distance: 8, taper: 8, mode: 'new' } },
  { name: '04-holed-minus8', what: '穴あき板 40×30・穴Φ12、距離8、−8°(外は細り穴は広がる)', sketch: 'holed', args: { distance: 8, taper: -8, mode: 'new' } },
  { name: '05-ellipse-minus6', what: '楕円 24×12、距離10、−6°', sketch: 'ellipse', args: { distance: 10, taper: -6, mode: 'new' } },
  { name: '06-svg-teardrop-minus4', what: 'SVG の涙形(ベジエ)、距離8、−4°', sketch: 'svg', args: { distance: 8, taper: -4, mode: 'new' } },
  { name: '07-face-boss-minus15', what: '箱 30×30×10 の上面を距離8・−15°で結合(先細りのボス)', sketch: 'face', args: { distance: 8, taper: -15, mode: 'join' } },
  { name: '08-cut-pocket-minus10', what: '板 40×30×10 に Φ12 の円を距離10・−10°で切り取り(奥ほど狭い穴)', sketch: 'cut', args: { distance: 10, taper: -10, mode: 'cut' } },
]

runSmoke('shoot-taper-ghost', { timeout: 90000, mcp: true }, async (ctx) => {
  const out = ctx.options.out ?? join(SCREENSHOTS_DIR, 'taper-ghost')
  mkdirSync(out, { recursive: true })

  const scenes = ctx.options.only ? SCENES.filter((scene) => scene.name.includes(ctx.options.only)) : SCENES
  for (const [position, scene] of scenes.entries()) {
    if (position > 0) await ctx.restart() // 作業フォルダも作り直されるので、SVG は場面ごとに書く
    const { mcp, browser, url, timeout } = ctx
    const svgPath = join(ctx.workdir, 'teardrop.svg')
    writeFileSync(svgPath, TEARDROP_SVG)
    let sketchId = null
    let inside = null
    let bodyId = null
    if (scene.sketch === 'face' || scene.sketch === 'cut') {
      const base = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
      const [w, h] = scene.sketch === 'face' ? [30, 30] : [40, 30]
      await mcp.callTool('add_rectangle', { sketch_id: base, corner: [0, 0], width: w, height: h })
      await mcp.callTool('set_selection', { refs: [{ sketch: base, index: 1 }] })
      bodyId = (await mcp.callTool('extrude', { distance: 10, mode: 'new' })).body_id
    }
    if (scene.sketch !== 'face') {
      sketchId = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
      if (scene.sketch === 'rect') {
        await mcp.callTool('add_rectangle', { sketch_id: sketchId, corner: [0, 0], width: 20, height: 10 })
        inside = [10, 5]
      } else if (scene.sketch === 'holed') {
        await mcp.callTool('add_rectangle', { sketch_id: sketchId, corner: [0, 0], width: 40, height: 30 })
        await mcp.callTool('add_circle', { sketch_id: sketchId, center: [20, 15], diameter: 12 })
        inside = [4, 4]
      } else if (scene.sketch === 'ellipse') {
        await mcp.callTool('add_ellipse', { sketch_id: sketchId, center: [0, 0], width: 24, height: 12 })
        inside = [5, 2] // 中心は原点・楕円の中心点に当たり、領域を選べない
      } else if (scene.sketch === 'svg') {
        await mcp.callTool('import_svg', { sketch_id: sketchId, path: svgPath })
        inside = [15, 18]
      } else if (scene.sketch === 'cut') {
        await mcp.callTool('add_circle', { sketch_id: sketchId, center: [20, 15], diameter: 12 })
        inside = [20, 18] // 円の中心は中心点に当たる
      }
    }

    const { page } = await openApp(browser, url, { timeout, until: () => window.__duocad?.ready === true })
    const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
    if (sketchId !== null) {
      await page.waitForSelector(`[data-sketch-id="${sketchId}"] .sketch-name`, { timeout })
      await page.click(`[data-sketch-id="${sketchId}"] .sketch-name`)
      await waitFor((id) => window.__duocad.activeSketch === id, 'スケッチへ入れない', [sketchId])
      await page.click('#sketch-look')
      await waitFrames(page, 8)
      await clickSketchPoint(page, inside[0], inside[1])
      await waitFor(
        () => (window.__duocad.selection?.items ?? []).some((item) => item.sketch !== undefined),
        `${scene.what}: 領域が選べない`,
      )
    } else {
      // 題材の箱を作ったスケッチが開いたままだと、画面は領域の押し出し(EXTRUDE_REGIONS)で面を待たない
      if ((await page.evaluate(() => window.__duocad.mode)) === 'sketch') {
        await page.click('#sketch-exit')
        await waitFor(() => window.__duocad.mode !== 'sketch', 'スケッチを抜けられない')
      }
      const top = await planarFace(page, bodyId, [0, 0, 1])
      await mcp.callTool('set_selection', { refs: [{ body: bodyId, kind: 'face', index: top }] })
      await waitFor(() => (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'face'), '上面の選択が画面に届かない')
    }
    await page.click('#extrude')
    await page.waitForSelector('#extrude-taper', { timeout })
    // 距離・角度は画面の欄に打つ(MCP の set_command_args で入れると、まだ空の画面の距離欄で上書きされる)
    await page.click('#distance', { clickCount: 3 })
    await page.type('#distance', String(scene.args.distance), { delay: 20 })
    await page.click('#extrude-taper', { clickCount: 3 })
    await page.type('#extrude-taper', String(scene.args.taper), { delay: 20 })
    await page.click(`#extrude-mode-${scene.args.mode}`)
    try {
      await waitOrFail(page, () => window.__duocad.previewing === true, `${scene.what}: ゴーストが出ない`, { timeout, polling: 'raf' })
    } catch (error) {
      const state = await page.evaluate(() => JSON.parse(JSON.stringify({ errors: window.__duocad.errors, command: window.__duocad.command, selection: window.__duocad.selection, mode: window.__duocad.mode, host: document.querySelector('.command-host')?.dataset })))
      fail(`${error.message}: ${JSON.stringify(state).slice(0, 600)}`)
    }
    await lookObliquely(page)
    const file = join(out, `${scene.name}.png`)
    // 左上のブラウザパネルが形に重なるので、撮る間だけ畳む
    const image = await withoutBrowserPanel(page, async () => {
      await waitFrames(page, 4)
      return page.screenshot({ type: 'png' })
    })
    writeFileSync(file, image)
    ok(`${scene.name}: ${scene.what} -> ${file}`)
    await page.close()
  }
})
