/**
 * 実機での確認 — **当たり半径が画面上のCSSピクセルで一定**であること。
 *
 *   node scripts/smoke-screen-pick.mjs
 *
 * 受入条件1(「ズーム・画角変更・正投影切替後も、同じ px 定数で
 * 体感が大きく変わらない」)は**pytestでは判定できない** —— 純関数テスト
 * (`tests/eval_screen_pick.mjs`)が固定しているのは距離計算の規則だけで、そこへ渡る
 * screen 座標が視点でどう動くかは本物のカメラでしか出ない。
 *
 * そこで**同じ図形・同じ画素オフセット**を、視点を変えながら繰り返し当てる:
 *
 *   - 矩形の辺の中点から**外向きに垂直に 2px** → その図形が取れる
 *   - 同じ向きに **12px**(`EDGE_PICK_PIXELS`=6 の倍) → 何も取れない
 *   - 角の点マーカーの真上 → **点**が取れる(図形全体ではない)
 *
 * これを 5つの視点(既定 / 強いズームイン / 強いズームアウト / 画角90度 / 正投影)で通す。
 * **落ちたら当たり半径が世界座標の量に戻っている** —— 見えている大きさが変わっただけで
 * 掴める画素が変わったということなので、`viewport.ts` の `pickPointsAt`/`pickLinesAt` か
 * `projectForPick` を疑う。
 */
import {
  assertNoAppErrors,
  openApp,
  sleep,
  viewportCenter,
  waitFrames,
  withoutBrowserPanel,
} from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { sketchPointAt } from './pick.mjs'

/** 矩形の寸法[mm]。原点から離してあるのは、原点の軸線と当たりを取り合わないため。 */
const RECT = { x: 10, y: 10, width: 40, height: 30 }
/** 当たると見なす側の押し込み[px]。`EDGE_PICK_PIXELS`(6)より十分内側。 */
const INSIDE_PX = 2
/** 外れると見なす側の押し込み[px]。`EDGE_PICK_PIXELS`(6)の倍。 */
const OUTSIDE_PX = 12

/**
 * **視点を動かした後の往復が終わるまで待つ。** ズームは可変解像度の測り直し
 * (`set_view_resolution`)を起こし、返ってきた状態でスケッチの折れ線が作り直される ——
 * その最中に読むと、指せるものが一瞬入れ替わったところを見てしまう(実測: 待たないと
 * ズームイン直後の判定が時々落ちる)。落ち着きは `stateEpoch` が止まったことで見る。
 */
async function quiet(page, timeout) {
  const deadline = Date.now() + timeout
  let last = -1
  let lastChangeAt = Date.now()
  for (;;) {
    const epoch = await page.evaluate(() => window.__duocad.stateEpoch ?? 0)
    if (epoch !== last) {
      last = epoch
      lastChangeAt = Date.now()
    } else if (Date.now() - lastChangeAt > 400) {
      break
    }
    if (Date.now() > deadline) break
    await sleep(100)
  }
  await waitFrames(page, 4)
}

runSmoke('smoke-screen-pick', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  {
    // **ソリッドも作る。** 折れ線の読み口はスケッチ(`Line2` の `userData.polylineWorld`)と
    // ソリッドの辺(素の `BufferGeometry` の position)で別物なので、片方だけでは
    // `pickLinesAt` の半分しか通らない。
    const { sketchId, bodyId } = await addExtrudedRect(mcp, {
      corner: [RECT.x, RECT.y],
      width: RECT.width,
      height: RECT.height,
      distance: 20,
    })
    ok(`矩形(${RECT.width}×${RECT.height}mm)を置いて20mm押し出した: ${bodyId}`)

    const { page, consoleErrors } = await openApp(browser, url, { timeout })

    // ================================================ A. ソリッドの辺(`bufferPolyline` 経路)
    await mcp.callTool('set_selection', { refs: [{ kind: 'face', body: bodyId, index: 1 }] })
    await page.click('#fillet')
    await page.waitForFunction(
      () => Boolean(window.__duocad.wantsEdges && window.__duocad.edges?.edges.length > 0),
      { timeout, polling: 50 },
    )

    /**
     * 画面上で最も長い辺の線分1本を選び、その中点と**外向き**の法線を返す。
     *
     * 辺の点列は世界座標で `__duocad.edges` に来ているので、射影は
     * `__duocad.projectPoint`(z しかクリップしない)で行う —— `projectEdge` は
     * 「その画素で本当に取れるか」まで見る別物で、方向を取り出せない。
     */
    async function longestEdgeSegment() {
      return page.evaluate(() => {
        const hook = window.__duocad
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const project = (point) => {
          const at = hook.projectPoint(point)
          return at ? { x: at.x + rect.left, y: at.y + rect.top } : null
        }
        // 全部の点の重心(画面上)。外向きの向きを決めるのに使う。
        let sumX = 0
        let sumY = 0
        let seen = 0
        let best = null
        for (const edge of hook.edges.edges) {
          for (let at = 0; at + 5 < edge.points.length; at += 3) {
            const a = project(edge.points.slice(at, at + 3))
            const b = project(edge.points.slice(at + 3, at + 6))
            if (!a || !b) continue
            sumX += a.x + b.x
            sumY += a.y + b.y
            seen += 2
            const length = Math.hypot(b.x - a.x, b.y - a.y)
            if (best === null || length > best.length) best = { index: edge.index, a, b, length }
          }
        }
        if (best === null || seen === 0) return null
        return { ...best, center: { x: sumX / seen, y: sumY / seen } }
      })
    }

    async function probeEdge(label) {
      const segment = await longestEdgeSegment()
      if (segment === null) fail(`[${label}] 辺が画面に出ていない`)
      if (segment.length < 60) fail(`[${label}] 辺が画面上で短すぎる: ${segment.length.toFixed(1)}px`)
      const mid = { x: (segment.a.x + segment.b.x) / 2, y: (segment.a.y + segment.b.y) / 2 }
      let nx = -(segment.b.y - segment.a.y) / segment.length
      let ny = (segment.b.x - segment.a.x) / segment.length
      // ボディの外へ逃がす —— 内側には面と別の辺があり、外には何も無い。
      if (nx * (segment.center.x - mid.x) + ny * (segment.center.y - mid.y) > 0) {
        nx = -nx
        ny = -ny
      }
      const hoveredEdge = async (x, y) => {
        await page.mouse.move(x, y)
        for (let attempt = 0; attempt < 3; attempt += 1) {
          await waitFrames(page, 4)
          const hit = await page.evaluate(() => window.__duocad.hoveredRef()?.edge ?? null)
          if (hit !== null) return hit
        }
        return null
      }
      const near = await hoveredEdge(mid.x + nx * INSIDE_PX, mid.y + ny * INSIDE_PX)
      if (near === null || near.edgeIndex !== segment.index) {
        fail(`[${label}] 辺から${INSIDE_PX}pxで辺${segment.index}が取れない: ${JSON.stringify(near)}`)
      }
      const far = await hoveredEdge(mid.x + nx * OUTSIDE_PX, mid.y + ny * OUTSIDE_PX)
      if (far !== null) {
        fail(`[${label}] 辺から${OUTSIDE_PX}px離れているのに取れた: ${JSON.stringify(far)}`)
      }
      ok(`[${label}] ソリッドの辺 ${segment.length.toFixed(0)}px: ${INSIDE_PX}pxで辺${segment.index}・${OUTSIDE_PX}pxで外れ`)
      return segment.length
    }

    const edgeLengths = []
    await withoutBrowserPanel(page, async () => {
      const center = await viewportCenter(page)
      edgeLengths.push(await probeEdge('辺・既定'))
      await page.mouse.move(center.x, center.y)
      for (let step = 0; step < 10; step += 1) await page.mouse.wheel({ deltaY: 240 })
      await quiet(page, timeout)
      edgeLengths.push(await probeEdge('辺・ズームアウト'))
    })
    const edgeSpread = Math.max(...edgeLengths) / Math.min(...edgeLengths)
    if (edgeSpread < 2) fail(`辺の見かけの大きさが変わっていない(${edgeSpread.toFixed(2)}倍)`)

    // ================================================ B. スケッチの図形(`sketchPolyline` 経路)
    // フィレットの構えと選択を捨ててからスケッチへ入る(隠したボディの辺を指したまま
    // 選択を触るとサーバが断る。`smoke-hover` と同じ手当て)。
    await page.keyboard.press('Escape')
    await mcp.callTool('clear_selection')
    await page.click('#parts-hide-all')
    await page.waitForFunction(() => window.__duocad.parts.every((item) => !item.visible), {
      timeout,
      polling: 50,
    })
    await page.click('#browser-sketches button.sketch-name')
    await page.waitForFunction(
      (id) => window.__duocad.activeSketch === id,
      { timeout, polling: 50 },
      sketchId,
    )
    // **カメラの再フレーム(`lookAtSketch`)は数フレーム遅れて反映される**(`smoke-hover` と同じ)。
    await waitFrames(page, 4)

    const entities = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      return (active?.entities ?? []).map((item) => ({
        entityId: item.entity_id,
        kind: item.kind,
        params: { ...item.params },
      }))
    })
    const rectangle = entities.find((item) => item.kind === 'rectangle')
    if (!rectangle) fail(`スケッチに矩形が無い: ${JSON.stringify(entities)}`)

    async function hoveredSketchEntity(x, y) {
      await page.mouse.move(x, y)
      // **null なら数フレーム置いて読み直す。** 当たり側だけが救われる非対称な待ちで、
      // 外れ側(12px)は何度読んでも null のままなので判定は緩まない。
      for (let attempt = 0; attempt < 3; attempt += 1) {
        await waitFrames(page, 4)
        const hit = await page.evaluate(() => window.__duocad.hoveredRef()?.sketchEntity ?? null)
        if (hit !== null) return hit
      }
      return null
    }

    /**
     * 1つの視点で「同じ画素オフセットなら同じ結果」を確かめる。
     *
     * 辺の向きは**画面上で測る**(世界座標の向きではない) —— 斜めから見た辺の垂直方向は
     * 投影で変わるので、両端を射影してから法線を作る。
     */
    async function probe(label) {
      const bottomLeft = await sketchPointAt(page, RECT.x, RECT.y)
      const bottomRight = await sketchPointAt(page, RECT.x + RECT.width, RECT.y)
      const center = await sketchPointAt(page, RECT.x + RECT.width / 2, RECT.y + RECT.height / 2)
      if (!bottomLeft || !bottomRight || !center) {
        fail(`[${label}] 矩形が画面に出ていない(射影できない)`)
      }
      const dx = bottomRight.x - bottomLeft.x
      const dy = bottomRight.y - bottomLeft.y
      const length = Math.hypot(dx, dy)
      // **短すぎる辺では測れない**(2pxと12pxの差が辺の長さに埋もれる)。
      if (length < 60) fail(`[${label}] 辺が画面上で短すぎる: ${length.toFixed(1)}px`)
      const mid = { x: (bottomLeft.x + bottomRight.x) / 2, y: (bottomLeft.y + bottomRight.y) / 2 }
      // 矩形の中心と反対側(外)へ逃がす —— 内側には領域と対辺があり、外には何も無い。
      let nx = -dy / length
      let ny = dx / length
      if (nx * (center.x - mid.x) + ny * (center.y - mid.y) > 0) {
        nx = -nx
        ny = -ny
      }

      const near = await hoveredSketchEntity(mid.x + nx * INSIDE_PX, mid.y + ny * INSIDE_PX)
      if (near === null || near.entityId !== rectangle.entityId || near.point !== null) {
        fail(
          `[${label}] 辺から${INSIDE_PX}pxで図形が取れない: ${JSON.stringify(near)}` +
            ` / 端 ${JSON.stringify(bottomLeft)} ${JSON.stringify(bottomRight)}`,
        )
      }
      const far = await hoveredSketchEntity(mid.x + nx * OUTSIDE_PX, mid.y + ny * OUTSIDE_PX)
      if (far !== null) {
        fail(`[${label}] 辺から${OUTSIDE_PX}px離れているのに取れた: ${JSON.stringify(far)}`)
      }

      const corner = await sketchPointAt(page, RECT.x, RECT.y)
      const onPoint = await hoveredSketchEntity(corner.x, corner.y)
      if (onPoint === null || onPoint.entityId !== rectangle.entityId || onPoint.point === null) {
        fail(`[${label}] 角の点マーカーの真上で点が取れない: ${JSON.stringify(onPoint)}`)
      }
      ok(`[${label}] 辺 ${length.toFixed(0)}px: ${INSIDE_PX}pxで図形・${OUTSIDE_PX}pxで外れ・角で点`)
      return length
    }

    const lengths = []
    await withoutBrowserPanel(page, async () => {
      const box = await page.evaluate(() => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        return { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
      })
      const center = { x: box.left + box.width / 2, y: box.top + box.height / 2 }

      lengths.push(await probe('既定'))

      await page.mouse.move(center.x, center.y)
      for (let step = 0; step < 12; step += 1) await page.mouse.wheel({ deltaY: 240 })
      await quiet(page, timeout)
      lengths.push(await probe('ズームアウト'))

      // **寄れるところまで寄る。** 何段で画面からはみ出すかは画角と初期距離で変わるので、
      // 段数を決め打ちにしない —— 両端が射影できなくなったら1段戻して止める。
      // **上限は引いた段数より多くする** —— 同じだけ戻すと既定の視点に着地するだけで、
      // 「寄った状態」を1度も測らないまま通ってしまう。
      for (let step = 0; step < 30; step += 1) {
        await page.mouse.wheel({ deltaY: -240 })
        await waitFrames(page, 4)
        const left = await sketchPointAt(page, RECT.x, RECT.y)
        const right = await sketchPointAt(page, RECT.x + RECT.width, RECT.y)
        if (!left || !right) {
          await page.mouse.wheel({ deltaY: 240 })
          await waitFrames(page, 4)
          break
        }
      }
      await quiet(page, timeout)
      lengths.push(await probe('ズームイン'))

      await page.evaluate(() => window.__duocad.setFov(90))
      await quiet(page, timeout)
      lengths.push(await probe('画角90度'))

      await page.evaluate(() => window.__duocad.setFov(0))
      await quiet(page, timeout)
      lengths.push(await probe('正投影'))
    })

    // **見えている大きさが実際に変わっていること**を確かめる。変わっていなければ、
    // 上の5回は同じ視点を5回測っただけで、不変性を1つも固定していない。
    const spread = Math.max(...lengths) / Math.min(...lengths)
    if (spread < 4) {
      fail(`視点を変えても見かけの大きさが変わっていない(最大/最小 = ${spread.toFixed(2)}倍)`)
    }
    ok(`辺の見かけの長さは ${Math.min(...lengths).toFixed(0)}〜${Math.max(...lengths).toFixed(0)}px(${spread.toFixed(1)}倍)動いたのに、掴める画素は同じだった`)

    await assertNoAppErrors(page, consoleErrors)
  }
})
