/**
 * 実ブラウザでの確認 — **通常モードの「押し出す」が、同一平面の面を何枚でも押せる**。
 *
 *   web> node scripts/smoke-extrude-faces.mjs
 *
 * 「押し出す」は1つで、通常モードではソリッドの面を選ぶ(スケッチ編集中は領域。そちらは
 * `smoke-regions.mjs`)。見るのは2つ:
 *
 *   1. **同一平面でない面を混ぜるとサーバのエラーが既存のトーストに出て、文書は変わらない**
 *   2. **同一平面の2枚を Shift+クリックで選び、結合で押し出せる** — 体積の増分が
 *      (2枚の面積の和)×距離に合い、確定した操作が verb `extrude` の `faces` 引数
 *      (面を送る形。配信では件数だけに畳まれる)である
 *
 * 題材はMCPで作る「コの字」(板の上に、両端に高さのある2つの柱): 柱の上面が**同じ平面(z=8)
 * にある別々の2つの面**になり、その間の溝の底(z=5)は法線が同じでも平面が違う面になる。
 */
import { frameAll, openApp, sleep, waitOrFail } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { clickFace } from './pick.mjs'

const SLAB = { width: 40, height: 30, thickness: 5 }
const PILLAR = { width: 8, height: 30, top: 8 }
const PUSH = 3

runSmoke('smoke-extrude-faces', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  {
    // ------------------------------------------------------------------ 題材
    const { bodyId } = await addExtrudedRect(mcp, {
      corner: [0, 0],
      width: SLAB.width,
      height: SLAB.height,
      distance: SLAB.thickness,
      mode: 'join',
    })
    const sketch = await mcp.callTool('sketch_create', { base_plane: 'XY' })
    await mcp.callTool('add_rectangle', {
      sketch_id: sketch.sketch_id,
      corner: [0, 0],
      width: PILLAR.width,
      height: PILLAR.height,
    })
    await mcp.callTool('add_rectangle', {
      sketch_id: sketch.sketch_id,
      corner: [SLAB.width - PILLAR.width, 0],
      width: PILLAR.width,
      height: PILLAR.height,
    })
    await mcp.callTool('propose_selection', {
      refs: [
        { sketch: sketch.sketch_id, index: 1 },
        { sketch: sketch.sketch_id, index: 2 },
      ],
    })
    await mcp.callTool('commit_selection')
    await mcp.callTool('extrude', { distance: PILLAR.top, mode: 'join' })
    await mcp.callTool('clear_selection')
    ok(`コの字を作った: ${bodyId}`)

    const { page, consoleErrors } = await openApp(browser, url, { timeout })
    await frameAll(page)

    const look = async () =>
      JSON.parse(
        await page.evaluate(() => {
          const hook = window.__duocad
          return JSON.stringify({
            volume: hook.parts[0]?.volume ?? null,
            selection: hook.selection?.items ?? [],
            log: hook.state.log.length,
            toasts: Array.from(document.querySelectorAll('#toasts .toast-text')).map((node) =>
              node.textContent.trim(),
            ),
          })
        }),
      )
    const waitFor = (predicate, what, ...args) =>
      waitOrFail(page, predicate, what, { args, timeout, dump: look })

    /** 上向き(法線+Z)の平面の面のうち、重心の高さが z のものを面積つきで返す。 */
    const upFacesAt = (z) =>
      page.evaluate(
        (id, height) => {
          const geometry = window.__duocad.getGeometry(id)
          return geometry.faces
            .filter((face) => face.kind === 'plane' && face.normal[2] > 0.9 && Math.abs(face.centroid[2] - height) < 1e-6)
            .map((face) => ({ index: face.index, area: face.area }))
        },
        bodyId,
        z,
      )

    await waitFor((id) => window.__duocad.getGeometry(id) !== null, 'ボディの形が届かない', bodyId)
    const tops = await upFacesAt(PILLAR.top)
    const floors = await upFacesAt(SLAB.thickness)
    if (tops.length !== 2) fail(`柱の上面が2枚でない: ${JSON.stringify(tops)}`)
    if (floors.length !== 1) fail(`溝の底が1枚でない: ${JSON.stringify(floors)}`)
    const topArea = tops[0].area + tops[1].area
    const volumeBefore = (await look()).volume

    // ================================================================ 1. 平面が違う面を混ぜると断られる
    // 柱の上面(z=8)と溝の底(z=5)は法線が同じでも同一平面ではない。
    const volumeMid = volumeBefore
    const opsMid = (await look()).log
    await clickFace(page, bodyId, tops[0].index, { hidePanel: true })
    await clickFace(page, bodyId, floors[0].index, { modifier: 'Shift', hidePanel: true })
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).filter((item) => item.kind === 'face').length === 2,
      '平面の違う2枚を選べない',
    )
    await page.click('#extrude')
    await page.click('#extrude-mode-join')
    await page.click('#distance')
    await page.type('#distance', '2')
    // プレビューで断られなければ確定で断られる(どちらでも既存のトーストに出る)。
    await sleep(1500)
    if ((await look()).toasts.length === 0) await page.keyboard.press('Enter')
    await waitFor(
      () => Array.from(document.querySelectorAll('#toasts .toast-text')).some((node) => node.textContent.includes('同一平面')),
      '平面の違う面を混ぜたエラーがトーストに出ない',
    )
    const after = await look()
    if (Math.abs(after.volume - volumeMid) > 1e-9 || after.log !== opsMid) {
      fail(`断られたのに文書が変わった: 体積 ${volumeMid} → ${after.volume} / 操作 ${opsMid} → ${after.log}`)
    }
    ok(`平面の違う面を混ぜた押し出しは、トーストに「${after.toasts[0]}」と出て文書は不変だった`)
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Escape')
    // Escは選択を消さない。素のクリックで置き換えて、次の節を1枚目から選び直す。

    // ================================================================ 2. 同一平面の2枚を結合で押す
    await clickFace(page, bodyId, tops[0].index, { hidePanel: true })
    await clickFace(page, bodyId, tops[1].index, { modifier: 'Shift', hidePanel: true })
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).filter((item) => item.kind === 'face').length === 2,
      '柱の上面を2枚選べない',
    )
    await page.click('#extrude')
    await page.click('#extrude-mode-join')
    await page.click('#distance')
    await page.type('#distance', String(PUSH))
    await waitFor(() => window.__duocad.previewing === true, '2枚選んだ押し出しのプレビューが出ない')
    await page.keyboard.press('Enter')
    const expectedDelta = topArea * PUSH
    await waitFor(
      (before, delta) => Math.abs(window.__duocad.parts[0].volume - before - delta) < Math.abs(delta) * 1e-6,
      '体積の増分が (面積の和)×距離 にならない',
      volumeBefore,
      expectedDelta,
    )
    const applied = JSON.parse(
      await page.evaluate(() => {
        const log = window.__duocad.state.log
        return JSON.stringify(log[log.length - 1] ?? null)
      }),
    )
    if (!applied || applied.verb !== 'extrude' || !('faces' in applied.args) || 'regions' in applied.args) {
      fail(`確定した操作が面の extrude(args.faces)ではない: ${JSON.stringify(applied)}`)
    }
    ok(`同一平面の2枚を結合で押し出した(体積 ${volumeBefore} → +${expectedDelta} = 面積の和 ${topArea} × ${PUSH})`)
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Escape')

    // 走行中のエラーは、わざと出させた「同一平面上にない」(プレビューと確定で2件あってよい)だけ。
    const errors = await page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.errors)))
    const unexpected = errors.filter((message) => !message.includes('同一平面'))
    if (unexpected.length > 0) fail(`想定外のエラーが出た: ${unexpected.join(' | ')}`)
    if (consoleErrors.length > 0) fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    ok('走行中のエラーは、わざと出させた同一平面のエラーだけ')
  }
})
