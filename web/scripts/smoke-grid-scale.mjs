/**
 * 実機での確認 — グリッドの3段(1/10/100mm)が固定で、線間隔pxで連続にフェードし、
 * 表示する平面が状態で決まり、**ピック範囲だけ**が内容の大きさへ追随する。
 *
 *   node scripts/smoke-grid-scale.mjs [--port 8987] [--http-port 8988]
 *
 * 完了条件を6つの姿勢で見る(A〜Fは互いに独立):
 *
 *   A. **刻みは3段固定**。内容を25倍にしても `gridTiers().tiers` の刻みは 1/10/100mm の
 *      まま。段ごとの不透明度は常に [0,1] で、**粗い段ほど濃い**(細い段から順に消える)。
 *
 *   B. **連続的なフェード**。寄り切って1mm段が完全に濃い状態から少しずつ引くと、不透明度は
 *      単調に下がり、**0でも1でもない中間値を何度も通ってから**0になる。ON/OFFの瞬間切替
 *      だとここが取れない(閾値を跨ぐたびの点滅は、この中間値があることで原理的に起きない)。
 *      同時に10mm段が濃いままであること(3段が一斉に消えたりしないこと)も見る。
 *
 *   C. **ピック範囲は内容から導かれ、丸い数字に載る**(`gridScale().extent`)。描画は無限に
 *      なったが、**指せる範囲まで無限にはしていない**ことの裏取り。
 *
 *   D. **基準平面のピック範囲が同じ `gridExtent` から引かれる**。旧来の固定100mmを大きく
 *      超えた画素でも基準平面をホバーできる一方、いまのextentのすぐ外はホバーできない。
 *      **対象待ちでは3平面すべてが出ている**ことも一緒に見る。
 *
 *   E. **縮小のヒステリシス**(ピック範囲): 大きな内容をundoで消しても即座には縮まず、
 *      `frameAll` と**新規文書**でだけ縮む。内容を全部消した後の `frameAll` でも縮む
 *      (姿勢=カメラは動かないままであることも見る)。
 *
 *   F. **表示する平面は状態で決まる**。通常はXY平面+Z軸、基準平面へのスケッチ中はその平面+
 *      それに直交する軸(XZへ貼れば XZ + Y軸)。
 *
 * サーバはこのスクリプトが空の文書で起動し、形はMCP経由で作る。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 */
import { assertNoAppErrors, openApp, sleep, viewportCenter, waitFrames } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { createSketchOnBasePlane, zoomOutUntilVisible } from './pick.mjs'

/** 3段の刻みが固定で、不透明度が [0,1] かつ粗い段ほど濃いこと(どの姿勢でも成り立つ)。 */
function assertTierShape(tiers, where) {
  const steps = tiers.tiers.map((tier) => tier.step)
  if (JSON.stringify(steps) !== JSON.stringify([1, 10, 100])) {
    fail(`${where}: 刻みが 1/10/100mm から動いた: ${JSON.stringify(steps)}`)
  }
  const alphas = tiers.tiers.map((tier) => tier.alpha)
  for (const alpha of alphas) {
    if (!(alpha >= 0 && alpha <= 1)) fail(`${where}: 不透明度が [0,1] の外: ${JSON.stringify(alphas)}`)
  }
  if (!(alphas[0] <= alphas[1] + 1e-9 && alphas[1] <= alphas[2] + 1e-9)) {
    fail(`${where}: 粗い段のほうが薄い(段の順序が壊れている): ${JSON.stringify(alphas)}`)
  }
}

runSmoke('smoke-grid-scale', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  {
    // ---------------------------------------------------------------- 準備: 原点近くの小さな立方体(常駐)
    // 大きな箱をundoで消しても文書が完全には空にならないようにしておく(完全に空だと
    // `frameToContent`が早期returnし、`frameAll`が縮小を試す機会そのものが無くなって
    // Eの検査の切り分けができない)。
    const addBox = (corner, width, height, distance) =>
      addExtrudedRect(mcp, { corner, width, height, distance })

    await addBox([0, 0], 10, 10, 10)
    ok('原点近くの小さな立方体を作った(10×10×10mm、以後ずっと残す)')

    const { page, consoleErrors } = await openApp(browser, url, {
      timeout,
      until: () => (window.__duocad?.state?.bodies ?? []).length >= 1,
    })
    await sleep(200)

    const tiersNow = () => page.evaluate(() => window.__duocad.gridTiers())
    const extentNow = () => page.evaluate(() => window.__duocad.gridScale().extent)

    /** ビューポート中央へカーソルを置く(ホイールはカーソル位置で発火する)。 */
    async function centerCursor() {
      const center = await viewportCenter(page)
      await page.mouse.move(center.x, center.y)
      return center
    }

    // ====================================================================== A. 刻みは3段固定
    console.log('\n[A] 刻みは 1/10/100mm の3段固定(内容の大きさで変わらない)')
    const baseline = await tiersNow()
    assertTierShape(baseline, '[A] 小さな内容')
    ok(`[A] 小さな内容(10mm)で3段固定: ${JSON.stringify(baseline.tiers)}`)

    const sketchCountBaseline = await page.evaluate(() => window.__duocad.sketches.length)
    await addBox([0, 0], 252, 10, 10)
    await page.waitForFunction((before) => window.__duocad.parts.length > before, {
      timeout,
      polling: 100,
    }, 1)
    await sleep(300)
    const afterBig = await tiersNow()
    assertTierShape(afterBig, '[A] 25倍の内容')
    if (JSON.stringify(afterBig.tiers.map((t) => t.step)) !== JSON.stringify([1, 10, 100])) {
      fail(`[A] 内容を大きくしたら刻みが動いた: ${JSON.stringify(afterBig.tiers)}`)
    }
    ok('[A] 内容を25倍(半幅252mm)にしても刻みは 1/10/100mm のまま(内容に追随しない)')

    // ====================================================================== B. 連続的なフェード
    console.log('\n[B] 段は線間隔pxで連続にフェードする(ON/OFFの瞬間切替ではない)')
    await page.evaluate(() => window.__duocad.frameAll())
    await sleep(200)
    await centerCursor()

    // まず1mm段が完全に濃くなるまで寄る。
    let zoomedIn = false
    for (let i = 0; i < 120; i += 1) {
      const tiers = await tiersNow()
      if (tiers.tiers[0].alpha >= 1) {
        zoomedIn = true
        break
      }
      await page.mouse.wheel({ deltaY: -240 })
      await sleep(30)
    }
    if (!zoomedIn) fail('寄り切っても1mm段の不透明度が1にならない')
    ok('[B] 寄ると1mm段が完全に濃くなる')

    // そこから少しずつ引いて、不透明度の列を採る。
    const fineAlphas = []
    const midAlphas = []
    for (let i = 0; i < 200; i += 1) {
      await waitFrames(page, 4)
      const tiers = await tiersNow()
      assertTierShape(tiers, `[B] 引きの途中(${i})`)
      fineAlphas.push(tiers.tiers[0].alpha)
      midAlphas.push(tiers.tiers[1].alpha)
      if (tiers.tiers[0].alpha <= 0) break
      await page.mouse.wheel({ deltaY: 120 })
      await sleep(25)
    }
    if (fineAlphas[fineAlphas.length - 1] > 0) {
      fail(`引き続けても1mm段が消えない: ${JSON.stringify(fineAlphas.slice(-5))}`)
    }
    for (let i = 1; i < fineAlphas.length; i += 1) {
      if (fineAlphas[i] > fineAlphas[i - 1] + 1e-6) {
        fail(`引いたのに1mm段が濃くなった(単調性が壊れている): ${JSON.stringify(fineAlphas)}`)
      }
    }
    const partial = fineAlphas.filter((alpha) => alpha > 0.05 && alpha < 0.95)
    if (partial.length < 3) {
      fail(
        `1mm段が0と1しか取らない(＝ON/OFFの瞬間切替になっている。閾値を跨ぐたびに点滅する): ` +
          JSON.stringify(fineAlphas),
      )
    }
    ok(`[B] 1mm段が1→0へ単調に、中間値を${partial.length}回通って連続に消えた`)
    if (midAlphas[midAlphas.length - 1] < 0.5) {
      fail(`1mm段が消えるところで10mm段まで薄くなっている(段が一斉に消えている): ${midAlphas.at(-1)}`)
    }
    ok('[B] 1mm段が消えた時点でも10mm段は濃いまま(段は順に消える)')

    // ====================================================================== C. ピック範囲は内容から
    console.log('\n[C] ピック範囲(gridScale().extent)は内容から導かれ、丸い数字に載る')
    await page.evaluate(() => window.__duocad.frameAll())
    await sleep(200)
    const grownExtent = await extentNow()
    if (grownExtent !== 500) {
      fail(`半幅252mmの内容で extent=500 にならない(1-2-5の丸めが効いていない): ${grownExtent}`)
    }
    ok(`[C] 半幅252mmの内容でピック範囲が extent=500 になった(描画は無限でもここは有限)`)

    // ====================================================================== D. ピック範囲と対象待ちの3平面
    console.log('\n[D] 基準平面のピック範囲が同じgridExtentから引かれる(対象待ちは3平面)')
    await page.click('#parts-hide-all')
    await page.waitForFunction(() => window.__duocad.parts.every((item) => !item.visible), {
      timeout,
      polling: 50,
    })
    await page.click('#sketch-create')
    await page.waitForFunction(() => window.__duocad.wantsBasePlanes === true, {
      timeout,
      polling: 50,
    })
    await waitFrames(page, 4)
    const armed = await tiersNow()
    if (JSON.stringify(armed.planes) !== JSON.stringify(['XY', 'XZ', 'YZ'])) {
      fail(`[D] 対象待ちなのに3平面が出ていない(基準平面を指せなくなる): ${JSON.stringify(armed.planes)}`)
    }
    if (armed.axis !== null) {
      fail(`[D] 3平面のときは面外の軸を別に描く必要が無いはず: ${armed.axis}`)
    }
    ok('[D] 対象待ちでは3平面すべてが出ている(基準平面をクリックして指せる)')

    // **`frameAll`は内容(bodyの外接箱、約252mm)に合わせるだけで、500mmのピック範囲全体は
    // 画面に入らない。** 実際のホイール操作で、指定したワールド座標が画面内に映るまで引く
    // (`pick.mjs` の `zoomOutUntilVisible`)。

    /** ワールド座標をXY平面上でホバーし、`hoveredRef().plane` を返す(座標は使う直前に測る)。 */
    async function hoverPlaneAt(worldPoint) {
      const rect = await page.evaluate(() => {
        const box = document.querySelector('.viewport').getBoundingClientRect()
        return { left: box.left, top: box.top }
      })
      const at = await page.evaluate((point) => window.__duocad.projectPoint(point), worldPoint)
      if (!at) return { screen: null, plane: null }
      await page.mouse.move(rect.left + at.x, rect.top + at.y)
      await waitFrames(page, 4)
      const hovered = await page.evaluate(() => window.__duocad.hoveredRef())
      return { screen: at, plane: hovered ? hovered.plane : null }
    }

    const insideExtent = grownExtent - 30 // 旧来の固定100mmを大きく超えるが、いまの範囲の内側
    const outsideExtent = grownExtent + 30
    if (!(await zoomOutUntilVisible(page, [outsideExtent, 0, 0]))) {
      fail(`ズームアウトを繰り返しても extent+30(${outsideExtent}mm)が画面に映らない`)
    }
    ok(`ズームアウトして外側の判定点(u=${outsideExtent})が画面に映るところまで引いた`)

    const inside = await hoverPlaneAt([insideExtent, 0, 0])
    if (inside.plane !== 'XY') {
      fail(
        `拡張後のextent(${grownExtent}mm)の内側(u=${insideExtent})なのにXY平面がホバーできない: ` +
          JSON.stringify(inside),
      )
    }
    ok(`[D] 旧来の固定100mmを大きく超えた u=${insideExtent} でXY平面をホバーできる(pick範囲がgridExtentに追随)`)

    const outside = await hoverPlaneAt([outsideExtent, 0, 0])
    if (outside.plane === 'XY') {
      fail(`いまのextent(${grownExtent}mm)の外側(u=${outsideExtent})なのにXY平面がホバーできてしまう`)
    }
    ok(`[D] いまのextentのすぐ外(u=${outsideExtent})はホバーできない(描画が無限でもpick範囲は有限)`)

    await page.keyboard.press('Escape')
    await page.click('#parts-show-all')
    await page.waitForFunction(() => window.__duocad.parts.every((item) => item.visible), {
      timeout,
      polling: 50,
    })

    // ====================================================================== E. 縮小のヒステリシス
    console.log('\n[E] 縮小のヒステリシス: ふつうの編集では縮まない、frameAll/新規文書でだけ縮む')
    // 大きな箱ぶん(sketch_create → add_rectangle → extrude の3verb)をundoで消す。
    for (let i = 0; i < 3; i += 1) await mcp.callTool('undo')
    await page.waitForFunction(
      (expectedSketches, expectedParts) =>
        window.__duocad.sketches.length === expectedSketches && window.__duocad.parts.length === expectedParts,
      { timeout, polling: 100 },
      sketchCountBaseline,
      1,
    )
    await sleep(200)
    ok(`大きな箱をundo(3回)で消した(sketches=${sketchCountBaseline} / parts=1 に戻った)`)

    if ((await extentNow()) !== 500) {
      fail(`ふつうの編集(undo)で内容が縮んだ直後にピック範囲が即座に縮んでしまった(ヒステリシス違反)`)
    }
    ok('[E] undoで内容が縮んでも、ピック範囲は即座には縮まない')

    await page.evaluate(() => window.__duocad.frameAll())
    await sleep(200)
    if ((await extentNow()) !== 100) {
      fail(`frameAllを呼んでもピック範囲が縮まない(縮小の許可が効いていない): ${await extentNow()}`)
    }
    ok('[E] frameAllを呼ぶと実際に縮む(BASE_EXTENTへ戻った)')

    await addBox([0, 0], 252, 10, 10)
    await page.waitForFunction((before) => window.__duocad.parts.length > before, {
      timeout,
      polling: 100,
    }, 1)
    await sleep(300)
    if ((await extentNow()) !== 500) fail('もう一度252mmの箱を作っても extent=500 に戻らない')
    ok('[E] もう一度大きな箱を作るとピック範囲が即座に広がった(広げるのは常に即座)')

    // ---- 全部消してframeAllで縮む
    // **root(ボディ)もsketchRoot(スケッチ)も両方空にする** —— `frameToContent()`は`root`が
    // 空なら早期returnするが、グリッドの縮小許可はそれとは独立に必ず1回通る必要がある。
    for (let i = 0; i < 6; i += 1) await mcp.callTool('undo')
    await page.waitForFunction(
      () => window.__duocad.parts.length === 0 && window.__duocad.sketches.length === 0,
      { timeout, polling: 100 },
    )
    await sleep(200)
    if ((await extentNow()) !== 500) {
      fail('文書を空にした直後にピック範囲が即座に縮んでしまった(ヒステリシス違反)')
    }
    ok('[E] 空にした直後はまだ縮まない')

    const poseBeforeEmptyFrameAll = await page.evaluate(() => window.__duocad.cameraPose())
    await page.evaluate(() => window.__duocad.frameAll())
    await sleep(200)
    if ((await extentNow()) !== 100) {
      fail(
        `文書が空の状態でframeAllを呼んでもピック範囲が縮まない` +
          `(frameToContentの早期returnがapplyGridScaleより前にあると起きる回帰)`,
      )
    }
    ok('[E] 内容が空でもframeAllでピック範囲は縮む(BASE_EXTENTへ戻った)')

    // **姿勢(カメラ)は動かない契約のまま**(戻り値=falseの意味を壊していないことの裏取り)。
    const poseAfterEmptyFrameAll = await page.evaluate(() => window.__duocad.cameraPose())
    const emptyFrameAllMoved = Math.hypot(
      poseAfterEmptyFrameAll.position[0] - poseBeforeEmptyFrameAll.position[0],
      poseAfterEmptyFrameAll.position[1] - poseBeforeEmptyFrameAll.position[1],
      poseAfterEmptyFrameAll.position[2] - poseBeforeEmptyFrameAll.position[2],
    )
    if (emptyFrameAllMoved > 1e-6) {
      fail(
        `空の文書でframeAllを呼んだのにカメラが動いた(frameToContentの「実際に合わせたか」の` +
          `契約が壊れている疑い): 移動量=${emptyFrameAllMoved.toFixed(3)}mm`,
      )
    }
    ok('[E] 空の文書でframeAllを呼んでもカメラは動かない(姿勢の契約は変えていない)')

    const poseBeforeNew = await page.evaluate(() => window.__duocad.cameraPose())
    await page.click('#file-new')
    await page.waitForFunction(
      () => window.__duocad.parts.length === 0 && window.__duocad.currentDocumentName === null,
      { timeout, polling: 100 },
    )
    await sleep(200)
    if ((await extentNow()) !== 100) fail('新規文書にしてもピック範囲が縮まない(resetGridの配線が効いていない)')
    ok('[E] 新規文書にするとピック範囲が縮む(BASE_EXTENTへ戻った)')
    const poseAfterNew = await page.evaluate(() => window.__duocad.cameraPose())
    const cameraMoved = Math.hypot(
      poseAfterNew.position[0] - poseBeforeNew.position[0],
      poseAfterNew.position[1] - poseBeforeNew.position[1],
      poseAfterNew.position[2] - poseBeforeNew.position[2],
    )
    if (cameraMoved > 1e-6) {
      fail(`新規文書でピック範囲を縮めるついでにカメラまで動いてしまった(移動量=${cameraMoved.toFixed(3)}mm)`)
    }
    ok('[E] 新規文書でピック範囲を縮めても、カメラの姿勢は一切動いていない')

    // ====================================================================== F. 表示する平面は状態で決まる
    console.log('\n[F] 表示する平面: 通常=XY+Z軸 / 基準平面へのスケッチ中=その平面+直交する軸')
    await waitFrames(page, 4)
    const plain = await tiersNow()
    if (JSON.stringify(plain.planes) !== JSON.stringify(['XY']) || plain.axis !== 'Z') {
      fail(`[F] 通常はXY平面+Z軸のはず: ${JSON.stringify({ planes: plain.planes, axis: plain.axis })}`)
    }
    ok('[F] スケッチ外ではXY平面+Z軸だけが出ている')

    // **スケッチを作る2手**: 「スケッチ」を起動 → 貼る先(XZ平面)をクリック
    // (`smoke.mjs` の `createSketchOnBase` と同じ流儀。MCPから作っても画面側は
    // スケッチモードへ入らないので、人と同じ経路で入る)。
    await createSketchOnBasePlane(page, 'XZ', { timeout })
    await page.waitForFunction(
      () => {
        const tiers = window.__duocad.gridTiers()
        return tiers !== null && tiers.planes.length === 1 && tiers.planes[0] === 'XZ'
      },
      { timeout, polling: 50 },
    )
    const onXz = await tiersNow()
    if (onXz.axis !== 'Y') {
      fail(`[F] XZへのスケッチ中は直交するY軸を描くはず: ${JSON.stringify(onXz)}`)
    }
    assertTierShape(onXz, '[F] XZへのスケッチ中')
    ok('[F] XZへのスケッチ中はXZ平面+Y軸だけが出ている(3段の規則はそのまま)')

    await page.click('#sketch-exit')
    await page.waitForFunction(() => window.__duocad.activeSketch === null, { timeout, polling: 50 })
    await waitFrames(page, 4)
    const backToPlain = await tiersNow()
    if (JSON.stringify(backToPlain.planes) !== JSON.stringify(['XY']) || backToPlain.axis !== 'Z') {
      fail(`[F] スケッチを抜けたら通常(XY+Z軸)へ戻るはず: ${JSON.stringify(backToPlain)}`)
    }
    ok('[F] スケッチを抜けると通常(XY平面+Z軸)へ戻る')

    await assertNoAppErrors(page, consoleErrors)
  }
})
