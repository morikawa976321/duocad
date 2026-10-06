/**
 * 実機での確認 — **サーバの先読み(フィレットの幽霊)が、無関係な再配信のあとも戻る**。
 *
 *   node scripts/smoke-preview-refire.mjs
 *
 * 再配信の起こし方はホイールで寄ること: 可変解像度の粗さが `body_revision` の鍵に入っているので、
 * **幽霊が乗っているボディ自身が測り直されて届く**。引数も選択も変わらないので `CommandHost` は
 * 再発火しない —— 戻るなら App が投げ直した先読みである。
 *
 * サーバはこのスクリプトが空の文書で起動し、箱はMCP経由で作る。
 */
import { assertNoAppErrors, frameAll, openApp, sleep, typeInto, viewportCenter, waitOrFail } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'

runSmoke('smoke-preview-refire', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  const { bodyId } = await addExtrudedRect(mcp, { corner: [0, 0], width: 20, height: 20, distance: 10 })
  ok(`箱を作った: ${bodyId}`)

  const { page, consoleErrors } = await openApp(browser, url, { timeout })
  await frameAll(page)

  await page.click('#fillet')
  await mcp.callTool('set_selection', { refs: [{ body: bodyId, kind: 'edge', index: 1 }] })
  await typeInto(page, '#fillet-radius', '2')

  const revision = () =>
    page.evaluate(
      (id) => (window.__duocad.state?.bodies ?? []).find((item) => item.body_id === id)?.revision ?? null,
      bodyId,
    )
  await waitOrFail(page, () => window.__duocad.previewing, 'フィレットの幽霊が出ない', { timeout })
  const before = await revision()
  ok(`フィレットの幽霊が出た(ボディの版 ${before})`)

  const center = await viewportCenter(page)
  await page.mouse.move(center.x, center.y)
  for (let i = 0; i < 12; i += 1) {
    await page.mouse.wheel({ deltaY: -240 })
    await sleep(60)
  }

  // 版が上がった時点で `previewing` は同期に false へ落ちる(`handleState`)。その後に true へ
  // 戻すのは投げ直した先読みの応答だけなので、「版が上がった かつ true」で戻ったと言える。
  const deadline = Date.now() + timeout
  for (;;) {
    const after = await revision()
    const previewing = await page.evaluate(() => window.__duocad.previewing)
    if (after !== before && previewing) {
      ok(`**再配信(版 ${before}->${after})のあとも幽霊が戻った**`)
      break
    }
    if (Date.now() > deadline) {
      fail(`再配信のあと幽霊が戻らない: 版 ${before}->${after} / previewing=${previewing}`)
    }
    await sleep(100)
  }

  await assertNoAppErrors(page, consoleErrors)
})
