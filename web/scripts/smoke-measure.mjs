/**
 * 実機での確認 — **測定(issue #10)は verb ではなく、人は2つ指すだけ・AIは同じ答えを読む**。
 *
 *   node scripts/smoke-measure.mjs [--port 8975] [--http-port 8976]
 *
 * 見るもの:
 *
 *   1. 「測定」を押すと対象ボディの頂点が候補として出て、**素のクリック2回**で頂点と頂点の
 *      距離が出る(2回目のクリックが1回目を置き換えない・Enterは要らない)。読み札と3Dの線が
 *      出て、コマンドは無起動へ戻る。文書は変わらない(undo できない)
 *   2. AIが面を2枚提案 → 人が「測定」を押すだけで**その場で**測れる(起動時に揃っていれば走る)。
 *      平行な面は距離、平行でない面は角度(線は出ない)
 *   3. Esc で札が消える。選択が変われば札は消える
 *   4. 測れない組(円筒面と平面)はトーストで断られ、札は出ない。文書は1ビットも変わらない
 *
 * サーバはこのスクリプトが空の文書で起動し、板はMCP経由で作る。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 */
import { assertNoAppErrors, frameAll, openApp } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'

const WIDTH = 40
const DEPTH = 30
const HEIGHT = 20

/** 数値どうしの一致(どちらかが数値でなければ**不一致**。`Math.abs(undefined - x)` は NaN で素通りする)。 */
const near = (a, b, tol = 1e-6) => typeof a === 'number' && typeof b === 'number' && Math.abs(a - b) <= tol

runSmoke('smoke-measure', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  {
    const { bodyId } = await addExtrudedRect(mcp, {
      corner: [0, 0],
      width: WIDTH,
      height: DEPTH,
      distance: HEIGHT,
      mode: 'join',
    })
    ok(`板を作った: ${bodyId}`)

    const { page, consoleErrors } = await openApp(browser, url, { timeout })
    await frameAll(page).catch(() => {})

    const clickAt = async (position) => {
      const rect = await page.evaluate(() => {
        const box = document.querySelector('.viewport').getBoundingClientRect()
        return { left: box.left, top: box.top }
      })
      await page.mouse.click(position.x + rect.left, position.y + rect.top)
    }

    // ---------------------------------------------------------------- 1. 頂点と頂点(本物のクリック2回)
    await page.click('#measure')
    await page.waitForFunction(
      () => (window.__duocad.vertexMarkers ?? []).length === 8,
      { timeout, polling: 50 },
    )
    ok('「測定」を押すと対象ボディの頂点8個が候補として出た')
    const markers = await page.evaluate(() => window.__duocad.vertexMarkers)
    const reachable = []
    for (const marker of markers) {
      const at = await page.evaluate(
        (id, index) => window.__duocad.projectVertex(id, index),
        marker.bodyId,
        marker.vertexIndex,
      )
      if (at) reachable.push({ ...marker, at })
    }
    if (reachable.length < 2) fail(`指せる頂点が2つ無い: ${reachable.length}`)
    const [first, second] = reachable
    await clickAt(first.at)
    try {
      await page.waitForFunction(
        (index) =>
          (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'vertex' && item.index === index),
        { timeout: 10000, polling: 50 },
        first.vertexIndex,
      )
    } catch {
      const state = await page.evaluate(() => ({
        selection: window.__duocad.selection,
        errors: window.__duocad.errors,
        hovered: window.__duocad.hoveredRef(),
        wantedVertices: window.__duocad.wantedVertices,
        wantedPick: window.__duocad.wantedPick,
        wantsEdges: window.__duocad.wantsEdges,
        phase: document.querySelector('.command-host')?.dataset.phase ?? null,
        prompt: document.querySelector('#command-prompt')?.textContent ?? null,
      }))
      fail(`頂点 ${first.vertexIndex} を素のクリックで選べない: ${JSON.stringify(state)}`)
    }
    // **2回目も素のクリック**(Ctrlなし)。測定中は素のクリックで足す —— 置き換えでは永久に2つ揃わない。
    await clickAt(second.at)
    await page.waitForFunction(
      () => window.__duocad.measurement?.between?.kind === 'point-point',
      { timeout, polling: 50 },
    )
    const expected = Math.hypot(
      first.point[0] - second.point[0],
      first.point[1] - second.point[1],
      first.point[2] - second.point[2],
    )
    const vertexResult = await page.evaluate(() => ({
      between: window.__duocad.measurement.between,
      readout: document.querySelector('#measure-result')?.textContent ?? null,
      phase: document.querySelector('.command-host')?.dataset.phase ?? null,
      line: window.__duocad.measurementInfo(),
      count: window.__duocad.selection?.count,
    }))
    if (!near(vertexResult.between.distance, expected)) {
      fail(`頂点間の距離が違う: ${vertexResult.between.distance} (期待 ${expected})`)
    }
    if (!vertexResult.readout || !vertexResult.readout.includes(expected.toFixed(3))) {
      fail(`読み札に距離が出ていない: ${vertexResult.readout}`)
    }
    if (vertexResult.phase !== 'idle') fail(`測った後に無起動へ戻っていない: ${vertexResult.phase}`)
    if (!vertexResult.line || vertexResult.line.label !== `${expected.toFixed(3)} mm`) {
      fail(`3Dの線が出ていない/札が違う: ${JSON.stringify(vertexResult.line)}`)
    }
    if (vertexResult.count !== 2) fail(`測った後に選択が残っていない: ${vertexResult.count}`)
    const undoable = (await mcp.callTool('describe_document')).can_undo
    if (undoable !== true) fail('板を作ったので undo できるはず(前提が崩れている)')
    const logBefore = (await mcp.callTool('describe_document')).applied_count
    ok(`頂点と頂点: ${vertexResult.readout}(Enterなし・無起動へ戻った・3Dの線あり)`)

    // ---------------------------------------------------------------- 2. AIが提案した面2枚を、押すだけで測る
    await mcp.callTool('propose_selection', { predicate: { kind: 'face', where: [] } })
    const faces = (await mcp.callTool('measure')).items
    const top = faces.find((item) => item.normal[2] > 0.9)
    const bottom = faces.find((item) => item.normal[2] < -0.9)
    const side = faces.find((item) => item.normal[0] > 0.9)
    if (!top || !bottom || !side) fail(`面が見つからない: ${JSON.stringify(faces.map((f) => f.normal))}`)
    const proposed = await mcp.callTool('propose_selection', {
      refs: [
        { body: bodyId, kind: 'face', index: top.index },
        { body: bodyId, kind: 'face', index: bottom.index },
      ],
    })
    if (proposed.selection.origin !== 'proposed') fail('提案なのに origin が proposed でない')
    await page.waitForFunction(
      () => window.__duocad.selection?.count === 2 && window.__duocad.measurement === null,
      { timeout, polling: 50 },
    )
    await page.click('#measure')
    await page.waitForFunction(
      () => window.__duocad.measurement?.between?.kind === 'plane-plane',
      { timeout, polling: 50 },
    )
    const planes = await page.evaluate(() => ({
      between: window.__duocad.measurement.between,
      readout: document.querySelector('#measure-result')?.textContent ?? null,
      phase: document.querySelector('.command-host')?.dataset.phase ?? null,
      origin: window.__duocad.selection?.origin,
    }))
    if (!near(planes.between.distance, HEIGHT)) fail(`平行な面の距離が違う: ${planes.between.distance}`)
    if (planes.between.parallel !== true) fail('平行と判定されていない')
    if (planes.phase !== 'idle') fail(`起動時に揃っていたのにその場で測っていない: ${planes.phase}`)
    if (planes.origin !== 'proposed') fail(`測定が提案を確定してしまった: ${planes.origin}`)
    const aiSees = await mcp.callTool('measure')
    if (!near(aiSees.between.distance, planes.between.distance, 1e-9)) {
      fail(`AIが読む距離と画面の距離が違う: ${aiSees.between.distance} / ${planes.between.distance}`)
    }
    ok(`平行な面: ${planes.readout}(提案のまま・AIも同じ ${aiSees.between.distance})`)

    // 平行でない面 → 角度(線は出ない)
    await mcp.callTool('propose_selection', {
      refs: [
        { body: bodyId, kind: 'face', index: top.index },
        { body: bodyId, kind: 'face', index: side.index },
      ],
    })
    await page.waitForFunction(
      () => window.__duocad.selection?.count === 2 && window.__duocad.measurement === null,
      { timeout, polling: 50 },
    )
    await page.click('#measure')
    await page.waitForFunction(
      () => window.__duocad.measurement?.between?.parallel === false,
      { timeout, polling: 50 },
    )
    const angle = await page.evaluate(() => ({
      between: window.__duocad.measurement.between,
      readout: document.querySelector('#measure-result')?.textContent ?? null,
      line: window.__duocad.measurementInfo(),
    }))
    if (!near(angle.between.angle_deg, 90)) fail(`直交する面の角度が違う: ${angle.between.angle_deg}`)
    if (angle.between.distance !== null) fail('平行でないのに距離が出ている')
    if (!angle.readout?.includes('90.000°')) fail(`読み札に角度が出ていない: ${angle.readout}`)
    if (angle.line !== null) fail('角度なのに3Dの線が出ている')
    ok(`平行でない面: ${angle.readout}(線は出ない)`)

    // ---------------------------------------------------------------- 3. Esc / 選択の変化で消える
    await page.keyboard.press('Escape')
    await page.waitForFunction(() => window.__duocad.measurement === null, { timeout, polling: 50 })
    const afterEsc = await page.evaluate(() => ({
      readout: document.querySelector('#measure-result'),
      line: window.__duocad.measurementInfo(),
      count: window.__duocad.selection?.count,
    }))
    if (afterEsc.readout !== null) fail('Esc で読み札が消えていない')
    if (afterEsc.line !== null) fail('Esc で3Dの線が消えていない')
    if (afterEsc.count !== 2) fail(`Esc が選択まで消した: ${afterEsc.count}`)
    ok('Esc で札と線が消えた(選択は残る)')

    await page.click('#measure')
    await page.waitForFunction(
      () => window.__duocad.measurement?.between?.parallel === false,
      { timeout, polling: 50 },
    )
    await mcp.callTool('clear_selection')
    await page.waitForFunction(() => window.__duocad.measurement === null, { timeout, polling: 50 })
    ok('選択が変わると札が消えた')

    // ---------------------------------------------------------------- 文書は1ビットも変わっていない
    const logAfter = (await mcp.callTool('describe_document')).applied_count
    if (logBefore !== logAfter) fail(`測定が操作ログを増やした: ${logBefore} -> ${logAfter}`)
    ok(`操作ログは増えていない(${logAfter} 件のまま)`)

    // ---------------------------------------------------------------- 4. 測れない組(曲面)はトースト
    // ボディそのものは画面からは指せない(測定の対象種別は頂点・辺・面だけ)ので、
    // 画面で作れる「測れない組」は曲面か曲線 —— 縦の辺を1本丸めて円筒面を作る(AI側で)。
    await mcp.callTool('propose_selection', {
      predicate: { kind: 'edge', where: [{ op: 'parallel_to', axis: 'Z' }] },
    })
    const vertical = (await mcp.callTool('measure')).items[0]
    await mcp.callTool('propose_selection', { refs: [{ body: bodyId, kind: 'edge', index: vertical.index }] })
    await mcp.callTool('commit_selection')
    await mcp.callTool('fillet', { radius: 3 })
    await mcp.callTool('propose_selection', { predicate: { kind: 'face', where: [] } })
    const afterFillet = (await mcp.callTool('measure')).items
    const curved = afterFillet.find((item) => Math.abs(item.normal[0]) > 0.5 && Math.abs(item.normal[1]) > 0.5)
    const flatTop = afterFillet.find((item) => item.normal[2] > 0.9)
    if (!curved || !flatTop) fail(`円筒面が見つからない: ${JSON.stringify(afterFillet.map((f) => f.normal))}`)
    await mcp.callTool('propose_selection', {
      refs: [
        { body: bodyId, kind: 'face', index: curved.index },
        { body: bodyId, kind: 'face', index: flatTop.index },
      ],
    })
    await page.waitForFunction(
      () => window.__duocad.selection?.count === 2 && window.__duocad.measurement === null,
      { timeout, polling: 50 },
    )
    const logBeforeRefusal = (await mcp.callTool('describe_document')).applied_count
    await page.click('#measure')
    await page.waitForFunction(
      () => (window.__duocad.errors ?? []).some((item) => String(item).includes('平面ではない')),
      { timeout, polling: 50 },
    )
    const refused = await page.evaluate(() => ({
      measurement: window.__duocad.measurement,
      phase: document.querySelector('.command-host')?.dataset.phase ?? null,
      readout: document.querySelector('#measure-result'),
    }))
    if (refused.measurement !== null) fail('測れない組なのに札が出た')
    if (refused.readout !== null) fail('測れない組なのに読み札が出た')
    if (refused.phase !== 'idle') fail(`断られた後に無起動へ戻っていない: ${refused.phase}`)
    const logAfterRefusal = (await mcp.callTool('describe_document')).applied_count
    if (logBeforeRefusal !== logAfterRefusal) fail('断られた測定が操作ログを増やした')
    ok('測れない組(円筒面と平面)はトーストで断られ、札は出ない(ログも増えない)')

    // 「平面ではない」の札は 5節でわざと出させたもの(測れない組を断る検査)。
    const refusals = await page.evaluate(() =>
      JSON.parse(JSON.stringify(window.__duocad.errors)).filter((item) => String(item).includes('平面ではない')),
    )
    if (refusals.length !== 1) fail(`断りの札が1件ではない: ${JSON.stringify(refusals)}`)
    await assertNoAppErrors(page, consoleErrors, { allow: refusals })
  }
})
