/**
 * 実機での確認 — **ロフトは通常モードで、別々のスケッチの領域を選んだ順につなぐ**。
 *
 *   web> node scripts/smoke-loft.mjs
 *
 * 通常モードでは全スケッチの領域を指せる。ロフトの起動中は領域だけを指せる(`App.svelte` の `pickKinds`)。
 *
 *   0. 断面1つと収束点: 「指す」を押すとスケッチの点も当たり、30 上の点の図形を指すと共有選択に入らず欄に入る。
 *      新規ボディで確定すると四角錐(20²·30/3 = 4000)ができる
 *   1. 起動中は領域だけを指せる(スケッチの線を押しても選択に入らない)
 *   2. 起動すると、XY の正方形と 30 上の円を順にクリックでき、選んだ順で選択に入る
 *   3. 始点のずらしを打つと、作業空間には整数の配列で載る(打ちかけの文字列を出すとサーバが型違いで拒む。sol レビュー)
 *   4. Enter で確定すると、四角から丸へ変わる立体ができる。円の側を 90° ずらしたのでねじれて細り、体積は
 *      ずらさないとき(実測 10742.9。円柱 π·10²·30 と角柱 20·20·30 の間)より小さく、円柱よりも小さい(実測 7141.6)
 *   5. 起動し直すと前回の始点のずらしは残らない(断面と対の値。残ると断面の数と合わず確定できない)
 *
 * 題材(2つのスケッチ)は MCP で描き、ロフトは画面から掛ける。
 */
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, pressUndo, sleep, typeInto, waitFrames, waitOrFail } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { clickSketchPoint } from './pick.mjs'

const CYLINDER = Math.PI * 100 * 30
const PYRAMID = (20 * 20 * 30) / 3

runSmoke('smoke-loft', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  const bottom = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  await mcp.callTool('add_rectangle', { sketch_id: bottom, corner: [-10, -10], width: 20, height: 20 })
  const top = (await mcp.callTool('sketch_create_offset', { base_plane: 'XY', distance: 30 })).sketch_id
  await mcp.callTool('add_circle', { sketch_id: top, center: [0, 0], diameter: 20 })
  // 5. の題材: 右に離した正方形と、その真上 30 の点の図形
  const base = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  await mcp.callTool('add_rectangle', { sketch_id: base, corner: [25, -10], width: 20, height: 20 })
  const apexSketch = (await mcp.callTool('sketch_create_offset', { base_plane: 'XY', distance: 30 })).sketch_id
  const apex = (await mcp.callTool('add_point', { sketch_id: apexSketch, point: [35, 0] })).entity_id
  await mcp.callTool('leave_sketch')
  await mcp.callTool('clear_selection')

  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.sketches ?? []).length >= 4,
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

  // ================================================================ 0. 断面1つと収束点で四角錐(立体が無い初期の視点で行う。`frameAll` はスケッチを範囲に入れない)
  await page.click('#loft')
  await page.waitForSelector('#loft-end-point', { timeout })
  await waitFrames(page, 8)
  await clickSketchPoint(page, 30, -5, { sketchId: base })
  await waitFor((id) => (window.__duocad.selection?.items ?? []).some((item) => item.sketch === id), '右の正方形を押しても領域が選べない', [base])
  await page.click('#loft-end-point')
  await waitFor(() => window.__duocad.wantedPick === 'sketchPoint', '「指す」を押してもスケッチの点を待つ状態にならない')
  await waitFor(
    () => (window.__duocad.pickKinds ?? []).includes('sketchPoint'),
    '収束点を待っている間にスケッチの点が当たる状態にならない',
  )
  await clickSketchPoint(page, 35, 0, { sketchId: apexSketch })
  await waitFor(
    (id) => window.__duocad.command?.args?.end_point?.entity === id,
    '点の図形をクリックしても収束点として入らない',
    [apex],
  )
  const picked = await page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.selection?.items ?? [])))
  if (picked.some((item) => item.entity === apex)) fail(`収束点に指した点が共有選択へ入った: ${JSON.stringify(picked)}`)
  ok('loft: 「指す」で点の図形を収束点にでき、共有選択には入らない')

  await page.click('#loft-mode-new')
  await page.keyboard.press('Enter')
  let after = await mcp.callTool('describe_document', {})
  for (const started = Date.now(); (after.bodies ?? []).length < 1; after = await mcp.callTool('describe_document', {})) {
    if (Date.now() - started > timeout) {
      const state = await page.evaluate(() => JSON.parse(JSON.stringify({ errors: window.__duocad.errors, command: window.__duocad.command })))
      fail(`断面1つと収束点のロフトで新しいボディができない: ${JSON.stringify(state)}`)
    }
    await sleep(100)
  }
  const pyramid = (after.bodies ?? []).map((body) => body.volume).find((v) => Math.abs(v - PYRAMID) < 1e-6 * PYRAMID)
  if (pyramid === undefined) {
    fail(`四角錐(体積 ${PYRAMID})ができない: ${JSON.stringify((after.bodies ?? []).map((body) => body.volume))}`)
  }
  ok(`loft: 断面1つと収束点で四角錐ができた(体積 ${pyramid.toFixed(3)})`)

  // 四角錐を戻して画面を開き直す: 立体ができると最初の1回だけ視点がその立体へ合い(`frameAll` もスケッチを範囲に
  // 入れない)、以降の題材が画面の外へ出る。
  await pressUndo(page)
  for (let left = await mcp.callTool('describe_document', {}); (left.bodies ?? []).length > 0; left = await mcp.callTool('describe_document', {})) {
    await sleep(100)
  }
  await mcp.callTool('clear_selection')
  await page.reload()
  await waitFor(() => window.__duocad?.ready === true && (window.__duocad?.sketches ?? []).length >= 4, '開き直した画面が開かない')
  await waitFrames(page, 8)

  // ================================================================ 1. 起動中は領域だけ
  await page.click('#loft')
  await page.waitForSelector('#loft-mode-join', { timeout })
  await waitFor(() => JSON.stringify(window.__duocad.pickKinds) === '["region"]', 'ロフトの起動中に領域だけを指せる状態にならない')
  await clickSketchPoint(page, 10.2, 0, { sketchId: bottom }) // 正方形の右の辺のすぐ外(線の当たり半径の内、領域の外)
  await waitFrames(page, 6)
  if ((await page.evaluate(() => (window.__duocad.selection?.items ?? []).length)) !== 0) {
    fail('ロフトの起動中にスケッチの線が選べてしまう')
  }
  ok('loft: 起動中は領域だけを指せ、スケッチの線を押しても選択に入らない')

  // ================================================================ 2. 2つのスケッチの領域を順に選ぶ
  await clickSketchPoint(page, 5, -5, { sketchId: bottom })
  await waitFor((id) => (window.__duocad.selection?.items ?? []).some((item) => item.sketch === id), '下の正方形を押しても領域が選べない', [bottom])
  await clickSketchPoint(page, 0, 0, { sketchId: top })
  await waitFor((id) => (window.__duocad.selection?.items ?? []).some((item) => item.sketch === id), '上の円を押しても領域が選べない', [top])
  const order = await regionItems()
  if (JSON.stringify(order) !== JSON.stringify([bottom, top])) {
    fail(`選んだ順に領域が並ばない: ${JSON.stringify(order)}(期待 ${JSON.stringify([bottom, top])})`)
  }
  ok('loft: 起動中は別々のスケッチの領域を選べ、選んだ順に並ぶ')

  // ================================================================ 3. 始点のずらしは配列で作業空間へ載る
  await typeInto(page, '#loft-shifts', '0, 1')
  await waitFor(
    () => JSON.stringify(window.__duocad.command?.args?.shifts) === '[0,1]',
    '始点のずらしが整数の配列として作業空間に載らない',
  )
  ok('loft: 始点のずらしは整数の配列で作業空間に載る(打ちかけの文字列を出さない)')
  await page.click('#loft-mode-join')

  // ================================================================ 4. 確定
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
  if (typeof volume !== 'number' || !(volume > 0 && volume < CYLINDER)) {
    fail(`ずらしたロフトの体積がねじれた形にならない: ${volume}(0 と ${CYLINDER.toFixed(1)} の間のはず)`)
  }
  ok(`loft: 円の側を 90° ずらした、四角から丸へねじれて変わる立体ができた(体積 ${volume.toFixed(3)})`)

  // ================================================================ 5. 起動し直すと始点のずらしは残らない
  await page.click('#loft')
  await page.waitForSelector('#loft-shifts', { timeout })
  const leftover = await page.evaluate(() => document.querySelector('#loft-shifts')?.value ?? '')
  if (leftover !== '') fail(`起動し直したロフトに前回の始点のずらしが残っている: ${JSON.stringify(leftover)}`)
  ok('loft: 起動し直すと前回の始点のずらしは残らない')
  await page.keyboard.press('Escape')

  await assertNoAppErrors(page, consoleErrors)
})
