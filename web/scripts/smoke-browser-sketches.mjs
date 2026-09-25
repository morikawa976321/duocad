/**
 * 実ブラウザでの**ブラウザ(BrowserPanel)のスケッチ行**の検査 —— 表示/非表示(issue #7)。
 *
 * 図形はMCPで作る(描く操作の検査は `smoke-features.mjs` 等が持つ。ここで見たいのは
 * 行の操作なので、材料は最短で用意する)。見るのは:
 *
 *   1. 行のトグルで隠すと、コアの `sketches[].visible` が落ち、ビューポートから消え、
 *      AIからもその領域を指せない(見えないものは指せない)
 *   2. 全表示/全非表示のボタンがスケッチ群に効く
 *   3. **隠したスケッチの行をクリックすると表示に戻してから入る**(拒否しない)。
 *      編集中のスケッチのトグルは無効
 *   4. 可視性は undo で戻らない(ボディと同じ契約)
 *
 *   web> node scripts/smoke-browser-sketches.mjs
 *
 * **サーバはこのスクリプトが自分で立てる**(`web/scripts/server.mjs`)。
 */
import { assertNoAppErrors, openApp, pressUndo, pressRedo, waitOrFail } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'

// 握手は材料を作る節で打つ(`handshake: false`)。
runSmoke('smoke-browser-sketches', { mcp: true, handshake: false }, async ({ mcp, browser, url, timeout }) => {
  {
    const { page, consoleErrors } = await openApp(browser, url, { timeout, height: 900 })
    const expectedErrors = []
    ok('空の文書で起動した')

    const look = async () =>
      JSON.parse(
        await page.evaluate(() =>
          JSON.stringify({
            sketches: window.__duocad.sketches.map((item) => ({
              id: item.sketch_id,
              visible: item.visible,
              entities: item.entities.length,
              patterns: item.patterns.length,
              regions: item.regions.length,
            })),
            drawn: Object.keys(window.__duocad.sketchPolylineCounts() ?? {}),
            activeSketch: window.__duocad.activeSketch,
            mode: window.__duocad.mode,
            parameters: window.__duocad.state.parameters,
            log: window.__duocad.state.log.length,
            errors: window.__duocad.errors,
          }),
        ),
      )
    const waitFor = (predicate, what, ...args) =>
      waitOrFail(page, predicate, what, { args, timeout, dump: look })
    const rowOf = (sketchId) => `#browser-sketches li.sketch-row[data-sketch-id="${sketchId}"]`
    const rowState = (sketchId) =>
      page.$eval(rowOf(sketchId), (li) => ({
        hidden: li.classList.contains('hidden-row'),
        active: li.classList.contains('active'),
        checked: li.querySelector('input.visible-toggle').checked,
        disabled: li.querySelector('input.visible-toggle').disabled,
      }))

    // ------------------------------------------------------------------ 材料(MCP)
    await mcp.handshake('duocad-smoke-browser-sketches')
    const sketchA = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
    await mcp.callTool('add_rectangle', { sketch_id: sketchA, corner: [0, 0], width: 20, height: 10 })
    const sketchB = (await mcp.callTool('sketch_create', { base_plane: 'XZ' })).sketch_id
    await mcp.callTool('add_circle', { sketch_id: sketchB, center: [40, 0], radius: 5 })
    await waitFor(
      () =>
        window.__duocad.sketches.length === 2 &&
        window.__duocad.sketches.every((item) => item.visible === true && item.regions.length === 1),
      'MCPで作った2枚のスケッチが画面に届かない',
    )
    await waitFor(
      () => document.querySelectorAll('#browser-sketches li.sketch-row input.visible-toggle').length === 2,
      'スケッチの行にトグルが出ない',
    )
    ok('スケッチ2枚(XYに長方形・XZに円)が可視で届き、行にトグルが出た')

    // ------------------------------------------------------------------ 1. 行のトグルで隠す
    await page.click(`${rowOf(sketchA)} input.visible-toggle`)
    await waitFor(
      (id) => window.__duocad.sketches.find((item) => item.sketch_id === id)?.visible === false,
      'トグルを外してもコアの visible が落ちない',
      sketchA,
    )
    await waitFor(
      (id) => !(id in (window.__duocad.sketchPolylineCounts() ?? {})),
      '隠したスケッチがビューポートに描かれたまま',
      sketchA,
    )
    let row = await rowState(sketchA)
    if (!row.hidden || row.checked) fail(`隠した行の見た目が違う: ${JSON.stringify(row)}`)
    let refused = null
    try {
      await mcp.callTool('propose_selection', { refs: [{ sketch: sketchA, index: 1 }] })
    } catch (error) {
      refused = String(error)
    }
    if (refused === null || !refused.includes('hidden')) {
      fail(`隠したスケッチの領域をAIが指せてしまう(または断り文が理由を言わない): ${refused}`)
    }
    ok(`行のトグルで隠すと、コアも描画も落ち、AIからも指せない("${refused.slice(0, 80)}…")`)

    // ------------------------------------------------------------------ 2. 全表示 / 全非表示
    await page.click('#sketches-show-all')
    await waitFor(() => window.__duocad.sketches.every((item) => item.visible), '全表示が効かない')
    await page.click('#sketches-hide-all')
    await waitFor(() => window.__duocad.sketches.every((item) => !item.visible), '全非表示が効かない')
    await waitFor(
      () => Object.keys(window.__duocad.sketchPolylineCounts() ?? {}).length === 0,
      '全非表示なのにビューポートに残っている',
    )
    ok('全表示 / 全非表示がスケッチ群に効いた')

    // ------------------------------------------------------------------ 3. 隠した行をクリックして入る
    await page.click(`${rowOf(sketchA)} .sketch-name`)
    await waitFor(
      (id) =>
        window.__duocad.activeSketch === id &&
        window.__duocad.sketches.find((item) => item.sketch_id === id)?.visible === true &&
        id in (window.__duocad.sketchPolylineCounts() ?? {}),
      '隠したスケッチの行をクリックしても表示に戻って入らない',
      sketchA,
    )
    row = await rowState(sketchA)
    if (!row.active || !row.checked || !row.disabled) {
      fail(`編集中の行の見た目が違う(active/checked/disabled): ${JSON.stringify(row)}`)
    }
    const other = await rowState(sketchB)
    if (other.checked || !other.hidden) fail(`他のスケッチまで表示に戻った: ${JSON.stringify(other)}`)
    ok('**隠したスケッチの行をクリックすると表示に戻して入った**(編集中の行のトグルは無効。他のスケッチは隠れたまま)')
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'スケッチを抜けられない')
    if ((await rowState(sketchA)).checked !== true) fail('抜けたら表示が戻ってしまった(隠し直しはしない契約)')
    ok('抜けても表示のまま')

    // ------------------------------------------------------------------ 4. undo で可視性は戻らない
    const logBefore = (await look()).log
    await pressUndo(page) // 直前のverb(Bの円)が戻る。Bの可視性(隠れている)はそのまま
    await waitFor((before) => window.__duocad.state.log.length === before - 1, 'undoが効かない', logBefore)
    const afterUndo = (await look()).sketches.find((item) => item.id === sketchB)
    if (afterUndo.visible !== false || afterUndo.entities !== 0) {
      fail(`undoが可視性まで戻した / 図形が戻っていない: ${JSON.stringify(afterUndo)}`)
    }
    await pressRedo(page)
    await waitFor((before) => window.__duocad.state.log.length === before, 'redoが効かない', logBefore)
    ok('undo は直前のverb(円)を戻し、スケッチの可視性は動かなかった')
    await page.click('#sketches-show-all')
    await waitFor(() => window.__duocad.sketches.every((item) => item.visible), '全表示が効かない(2回目)')

    // ------------------------------------------------------------------ 最後
    await assertNoAppErrors(page, consoleErrors, { allow: expectedErrors })
  }
})
