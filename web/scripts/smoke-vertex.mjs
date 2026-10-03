/**
 * 実機での確認 — **選択パネルを消した後も、人が選択を直せて、AIが選んだものをそのまま実行できる**。
 *
 *   node scripts/smoke-vertex.mjs
 *
 * `SelectionPanel.svelte`(一覧・「外す」・「全解除」)は無い。
 * その代替が本当に機能することを、**本物のMCPと本物のブラウザ**で見る:
 *
 *   1. AIが頂点を選ぶ → **選択に入っている頂点だけ**が3Dにマーカーで出る。
 *      頂点は3Dで指せない唯一の種別だったので、**ここが消えると人は頂点を外せなくなる**
 *   2. **本物のCtrl+クリックで1つ外せる**(AI側も同じ集合を読む)
 *   3. AIが面を選ぶ → **人がツールバーから押し出すだけでverbが走る**
 *
 * サーバはこのスクリプトが空の文書で起動し、板はMCP経由で作る。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 */
import { assertNoAppErrors, frameAll, openApp, sleep, viewportCenter } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'

runSmoke('smoke-vertex', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  {
    const { bodyId } = await addExtrudedRect(mcp, {
      corner: [0, 0],
      width: 40,
      height: 30,
      distance: 20,
      mode: 'join',
      clear: false,
    })
    ok(`板を作った: ${bodyId}`)

    const { page, consoleErrors } = await openApp(browser, url, { timeout })
    await frameAll(page).catch(() => {})

    // ---------------------------------------------------------------- 1. 頂点マーカー
    await mcp.callTool('set_selection', {
      refs: [1, 2, 3, 4].map((index) => ({ body: bodyId, kind: 'vertex', index })),
    })
    // 頂点の位置がAI側からも読めること(measure の頂点対応)
    const measured = await mcp.callTool('measure')
    if (!measured.items.every((item) => Array.isArray(item.point) && item.point.length === 3)) {
      fail(`measure が頂点の座標を返さない: ${JSON.stringify(measured.items)}`)
    }
    ok(`AIが頂点4つを選んだ(measure も座標を返す: ${JSON.stringify(measured.items[0].point)})`)

    await page.waitForFunction(
      () => (window.__duocad.vertexMarkers ?? []).length === 4,
      { timeout, polling: 50 },
    )
    const markerState = await page.evaluate(() => ({ markers: window.__duocad.vertexMarkers }))
    ok('ブラウザに頂点マーカー4個')

    // **引いても指せる**(当たり判定が画素基準であること)。
    // **見た目の大きさはここでは見ていない** —— マーカーは `sizeAttenuation: false` で
    // 画素固定にしてあるが、それが効いているかは目で見るしかない(当たり判定は
    // `viewport.ts` の `pickPointsAt` が screen 距離で決めており、描画とは別の経路)。
    const viewportBox = await viewportCenter(page)
    await page.mouse.move(viewportBox.x, viewportBox.y)
    await page.mouse.wheel({ deltaY: 900 }) // 引く
    await sleep(300)
    const reachableZoomedOut = await page.evaluate(
      (markers) => markers.filter((m) => window.__duocad.projectVertex(m.bodyId, m.vertexIndex)).length,
      markerState.markers,
    )
    if (reachableZoomedOut === 0) fail('引いたら頂点マーカーが1つも指せなくなった(画素固定が効いていない)')
    await page.mouse.wheel({ deltaY: -900 }) // 戻す
    await sleep(300)
    ok(`引いた状態でも ${reachableZoomedOut}/4 個のマーカーを指せる(大きさは画素で固定)`)

    // ---------------------------------------------------------------- 2. Ctrl+クリックで外す
    let picked = null
    for (const marker of markerState.markers) {
      const at = await page.evaluate(
        (id, index) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectVertex(id, index)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        marker.bodyId,
        marker.vertexIndex,
      )
      if (!at) continue
      await page.keyboard.down('Control')
      await page.mouse.click(at.x, at.y)
      await page.keyboard.up('Control')
      try {
        await page.waitForFunction(
          (index) =>
            !(window.__duocad.selection?.items ?? []).some(
              (item) => item.kind === 'vertex' && item.index === index,
            ),
          { timeout: 5000, polling: 50 },
          marker.vertexIndex,
        )
        picked = marker.vertexIndex
        break
      } catch {
        // この頂点は画面上で取れなかった(裏に回っている)。次を試す
      }
    }
    if (picked === null) fail('頂点マーカーを1つも本物のクリックで外せない')
    const afterRemove = await page.evaluate(() => ({
      count: window.__duocad.selection.count,
      markers: window.__duocad.vertexMarkers.length,
    }))
    if (afterRemove.count !== 3) fail(`外した後の件数が3でない: ${afterRemove.count}`)
    if (afterRemove.markers !== 3) fail(`マーカーが3個に減っていない: ${afterRemove.markers}`)
    const readBack = (await mcp.callTool('get_selection', {})).selection
    if (readBack.count !== 3) fail(`コア側の件数が3でない: ${readBack.count}`)
    ok(`Ctrl+クリックで頂点 ${picked} を外した(4→3件・AI側も同じ)`)

    // ---------------------------------------------------------------- 3. AIが選んだ面をそのまま押し出す
    // AIが上面を選ぶ → **人はツールバーから押し出すだけ**
    const all = await mcp.callTool('set_selection', {
      predicate: { kind: 'face', where: [{ op: 'normal_dot_gt', axis: 'Z', value: 0.9 }] },
    })
    const faces = (await mcp.callTool('measure')).items.filter(
      (item) => item.body === bodyId && item.normal && item.normal[2] > 0.9,
    )
    if (faces.length === 0) fail(`上向きの面が見つからない: ${JSON.stringify(all.selection.items)}`)
    const top = faces[0]
    await mcp.callTool('set_selection', {
      refs: [{ body: bodyId, kind: 'face', index: top.index }],
    })
    await page.waitForFunction(
      () => window.__duocad.selection?.count === 1,
      { timeout, polling: 50 },
    )
    const volumeBefore = (await mcp.callTool('describe_shape', { body_id: bodyId })).volume
    ok(`AIが上面(面 ${top.index})を選んだ`)

    await page.click('#extrude')
    await page.click('#extrude-mode-join')
    await page.click('#distance')
    await page.type('#distance', '5')
    await page.keyboard.press('Enter')
    await page.waitForFunction(
      (id, previous) => {
        const body = window.__duocad.state.bodies.find((item) => item.body_id === id)
        return body && Math.abs(body.volume - previous) > 1
      },
      { timeout, polling: 50 },
      bodyId,
      volumeBefore,
    )
    const volumeAfter = (await mcp.callTool('describe_shape', { body_id: bodyId })).volume
    const errors = await page.evaluate(() => [...window.__duocad.errors])
    if (errors.length) fail(`エラーが出た: ${JSON.stringify(errors)}`)
    ok(`AIが選んだ面を人がそのまま押し出せた(体積 ${volumeBefore.toFixed(1)} → ${volumeAfter.toFixed(1)})`)

    await assertNoAppErrors(page, consoleErrors)
  }
})
