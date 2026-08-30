/**
 * 実機での確認 — 基準グリッドの範囲・刻みがモデルの大きさへ追随し、基準平面のピック範囲も
 * 同じ値から引かれ、縮小にはヒステリシスが効く。
 *
 *   node scripts/smoke-grid-scale.mjs [--port 8987] [--http-port 8988]
 *
 * 完了条件を3つの姿勢で見る(A/B/Cは互いに独立):
 *
 *   A. **範囲・刻みが内容から導かれ、丸い数字に載る**。空の文書相当(小さな立方体だけ)は
 *      現状(±100mm/10mm刻み)のまま。タスク文書の完了条件に書かれた例そのもの
 *      (半幅252mm → extent 500 / step 50)を実際に作って `gridScale()` の数値で確かめる。
 *      分割数(extent/step)がGRID_DIVISIONS(=10)のまま保たれることも見る。
 *
 *   B. **基準平面のピック範囲が同じ `gridExtent` から引かれる**。旧来の固定100mmを大きく
 *      超えた画素でも基準平面をホバーできる一方、いまのextentのすぐ外はホバーできない
 *      (`viewport.ts` の `basePlaneAt` が使う `gridExtent` と、格子線・ホバー板を描く
 *      `rebuildGrid` の `extent` が同じ値であることの実機での裏取り)。
 *
 *   C. **縮小のヒステリシス**: 大きな内容を作った後にundoで消しても、グリッドは即座には
 *      縮まない(ふつうの編集)。`frameAll` を呼んだときだけ実際に縮む。**新規文書**
 *      (`#file-new`)でも同じく縮む。
 *
 *   D. **内容を全部消した後の`frameAll`でも縮む**(2026-08-31 QA指摘の回帰検査)。
 *      `frameToContent()`は`root`が空なら姿勢を合わせずに`false`を返す早期returnを持つが、
 *      グリッドの縮小許可はそれとは独立に必ず1回通る必要がある——通っていないと、
 *      大きなモデルを読んで広がったグリッドが、モデルを全部消して`frameAll`しても
 *      二度と縮まない(新規文書にするまで巨大なグリッドが残り続ける)。**姿勢(カメラ)は
 *      合わせられない(=動かない)ままであることも一緒に見る**(この回帰の直し方によっては
 *      姿勢の契約=戻り値の意味を壊しうるため)。
 *
 * サーバはこのスクリプトが空の文書で起動し、形はMCP経由で作る。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 */
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { findBrowser, launchBrowser } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const wsPort = options.port ?? '8987'
const httpPort = options['http-port'] ?? '8988'
const timeout = Number.parseInt(options.timeout ?? '60000', 10)

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** ホバーは描画ループ(`tick`)で解決するので、動かしたあと数フレーム待つ(`smoke-hover.mjs`と同じ)。 */
async function settle(page) {
  await page.evaluate(
    () =>
      new Promise((resolve) => {
        let left = 4
        const step = () => (left-- <= 0 ? resolve() : requestAnimationFrame(step))
        requestAnimationFrame(step)
      }),
  )
}

async function assertNoErrors(page, consoleErrors) {
  const errors = await page.evaluate(() => window.__duocad.errors ?? [])
  if (errors.length > 0) fail(`走行中にエラーが出た: ${JSON.stringify(errors)}`)
  if (consoleErrors.length > 0) fail(`コンソールにエラー: ${consoleErrors.join(' / ')}`)
  ok('走行中にエラーは1件も出ていない')
}

async function main() {
  const executablePath = findBrowser()
  const workdir = mkdtempSync(join(tmpdir(), 'duocad-grid-scale-'))
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null
  try {
    await mcp.handshake('duocad-smoke-grid-scale')

    // ---------------------------------------------------------------- 準備: 原点近くの小さな立方体(常駐)
    // 大きな箱をundoで消しても文書が完全には空にならないようにしておく(完全に空だと
    // `frameToContent`が早期returnし、`frameAll`が縮小を試す機会そのものが無くなって
    // Cの検査の切り分けができない)。
    async function addBox(corner, width, height, distance) {
      const sketch = await mcp.callTool('sketch_create', { base_plane: 'XY' })
      await mcp.callTool('add_rectangle', { sketch_id: sketch.sketch_id, corner, width, height })
      await mcp.callTool('propose_selection', { refs: [{ sketch: sketch.sketch_id, index: 1 }] })
      await mcp.callTool('commit_selection')
      const extruded = await mcp.callTool('extrude', { distance, mode: 'new' })
      await mcp.callTool('clear_selection')
      return { sketchId: sketch.sketch_id, bodyId: extruded.bodies[0].body_id }
    }

    await addBox([0, 0], 10, 10, 10)
    ok('原点近くの小さな立方体を作った(10×10×10mm、以後ずっと残す)')

    browser = await launchBrowser(executablePath)
    const page = await browser.newPage()
    await page.setViewport({ width: 1280, height: 860 })
    const consoleErrors = []
    page.on('pageerror', (error) => consoleErrors.push(String(error)))
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    await page.goto(`http://127.0.0.1:${httpPort}/`, { waitUntil: 'load', timeout })
    await page.waitForFunction(() => Boolean(window.__duocad?.ready), { timeout, polling: 100 })
    await page.waitForFunction(() => (window.__duocad?.state?.bodies ?? []).length >= 1, {
      timeout,
      polling: 100,
    })
    await sleep(200)

    // ====================================================================== A. 範囲・刻みが内容から導かれる
    console.log('\n[A] 範囲・刻みが内容から導かれ、丸い数字に載る')
    const baseline = await page.evaluate(() => window.__duocad.gridScale())
    if (baseline.extent !== 100 || baseline.step !== 10) {
      fail(`小さな立方体(10mm)だけでは現状(±100mm/10mm刻み)のはず: ${JSON.stringify(baseline)}`)
    }
    ok(`[A] 小さな内容(10mm)ではBASE_EXTENTのまま(現状と同じ見え方): ${JSON.stringify(baseline)}`)

    const sketchCountBaseline = await page.evaluate(() => window.__duocad.sketches.length)
    // タスクの完了条件に書かれた例そのもの(半幅252mm → extent 500 / step 50)。
    const big = await addBox([0, 0], 252, 10, 10)
    await page.waitForFunction((before) => window.__duocad.parts.length > before, {
      timeout,
      polling: 100,
    }, 1)
    await sleep(300)

    const grown = await page.evaluate(() => window.__duocad.gridScale())
    if (grown.extent !== 500 || grown.step !== 50) {
      fail(`半幅252mmの内容で extent=500/step=50 にならない(タスクの完了条件の例と食い違う): ${JSON.stringify(grown)}`)
    }
    ok(`[A] 半幅252mmの内容(タスクの完了条件の例)で extent=500/step=50 になった: ${JSON.stringify(grown)}`)
    if (grown.extent / grown.step !== 10) {
      fail(`分割数(extent/step)がGRID_DIVISIONS(10)からずれている: ${JSON.stringify(grown)}`)
    }
    ok('[A] 分割数(extent/step)が10のまま保たれている(モデルが大きくなっても線が密になって潰れない)')

    // カメラを大きな内容へ合わせ直す(実機検査が図形を画面内へ収めるための穴。この後の
    // Bのピック範囲検査は画面に映っている画素でしか行えない)。
    await page.evaluate(() => window.__duocad.frameAll())
    await sleep(200)
    const afterFrameAll = await page.evaluate(() => window.__duocad.gridScale())
    if (afterFrameAll.extent !== 500 || afterFrameAll.step !== 50) {
      fail(`frameAll後にextent/stepが変わった(内容が縮んでいないのに縮小してはいけない): ${JSON.stringify(afterFrameAll)}`)
    }
    ok('[A] frameAllで再フレーミングしても、内容が縮んでいないのでextent/stepは変わらない')

    // ====================================================================== B. ピック範囲が同じgridExtentから引かれる
    console.log('\n[B] 基準平面のピック範囲が同じgridExtentから引かれる')
    // 面に隠れないよう全部隠してから、スケッチ作成を起動して基準平面を指せる状態にする
    // (`smoke.mjs`の「先に部品を全部隠す」と同じ流儀)。
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

    /**
     * **`frameAll`は内容(bodyの外接箱、約252mm)に合わせるだけで、500mmのグリッド全体は
     * 画面に入らない。** 実際のホイール操作で、指定したワールド座標が画面内に映るまで
     * 引く(`smoke-clip-planes.mjs`の「実カメラで寄る」と対の「実カメラで引く」)。
     * **ホイールはカーソル位置で発火する**ので、先にビューポート中央へ置く。
     */
    async function zoomOutUntilVisible(worldPoint, maxNotches = 200) {
      const center = await page.evaluate(() => {
        const box = document.querySelector('.viewport').getBoundingClientRect()
        return { x: box.left + box.width / 2, y: box.top + box.height / 2 }
      })
      await page.mouse.move(center.x, center.y)
      for (let i = 0; i < maxNotches; i += 1) {
        const rect = await page.evaluate(() => {
          const box = document.querySelector('.viewport').getBoundingClientRect()
          return { width: box.width, height: box.height }
        })
        const at = await page.evaluate((point) => window.__duocad.projectPoint(point), worldPoint)
        if (at && at.x >= 0 && at.x <= rect.width && at.y >= 0 && at.y <= rect.height) return true
        await page.mouse.wheel({ deltaY: 480 }) // 正で引く(ズームアウト)
        await sleep(40)
      }
      return false
    }

    /** ワールド座標をXY平面上でホバーし、`hoveredRef().plane` を返す(座標は使う直前に測る)。 */
    async function hoverPlaneAt(worldPoint) {
      const rect = await page.evaluate(() => {
        const box = document.querySelector('.viewport').getBoundingClientRect()
        return { left: box.left, top: box.top }
      })
      const at = await page.evaluate((point) => window.__duocad.projectPoint(point), worldPoint)
      if (!at) return { screen: null, plane: null }
      await page.mouse.move(rect.left + at.x, rect.top + at.y)
      await settle(page)
      const hovered = await page.evaluate(() => window.__duocad.hoveredRef())
      return { screen: at, plane: hovered ? hovered.plane : null }
    }

    const insideExtent = grown.extent - 30 // 旧来の固定100mmを大きく超えるが、いまの範囲の内側
    const outsideExtent = grown.extent + 30
    // **外側の点が画面に映るまで引く** —— 内側の点(原点により近い)はそれより広い視野で
    // 必ず一緒に映るので、これで両方の判定に必要な視野が揃う。
    if (!(await zoomOutUntilVisible([outsideExtent, 0, 0]))) {
      fail(`ズームアウトを繰り返しても extent+30(${outsideExtent}mm)が画面に映らない`)
    }
    ok(`ズームアウトして外側の判定点(u=${outsideExtent})が画面に映るところまで引いた`)

    const inside = await hoverPlaneAt([insideExtent, 0, 0])
    if (inside.plane !== 'XY') {
      fail(
        `拡張後のextent(${grown.extent}mm)の内側(u=${insideExtent})なのにXY平面がホバーできない: ` +
          JSON.stringify(inside),
      )
    }
    ok(`[B] 旧来の固定100mmを大きく超えた u=${insideExtent} でXY平面をホバーできる(pick範囲がgridExtentに追随)`)

    const outside = await hoverPlaneAt([outsideExtent, 0, 0])
    if (outside.plane === 'XY') {
      fail(`いまのextent(${grown.extent}mm)の外側(u=${outsideExtent})なのにXY平面がホバーできてしまう`)
    }
    ok(`[B] いまのextentのすぐ外(u=${outsideExtent})はホバーできない(pick範囲がgridExtentで足切りされている)`)

    await page.keyboard.press('Escape')
    await page.click('#parts-show-all')
    await page.waitForFunction(() => window.__duocad.parts.every((item) => item.visible), {
      timeout,
      polling: 50,
    })

    // ====================================================================== C. 縮小のヒステリシス
    console.log('\n[C] 縮小のヒステリシス: ふつうの編集では縮まない、frameAll/新規文書でだけ縮む')
    // 大きな箱ぶん(sketch_create → add_rectangle → extrude の3verb)をundoで消す。
    // **`describe_document`ではなくブラウザ側の状態で判定する** —— グリッドの
    // 縮小許可を見るのはブラウザ(viewport.ts)側の仕事だから。
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

    const afterUndo = await page.evaluate(() => window.__duocad.gridScale())
    if (afterUndo.extent !== 500 || afterUndo.step !== 50) {
      fail(
        `ふつうの編集(undo)で内容が縮んだ直後にグリッドが即座に縮んでしまった` +
          `(ヒステリシス違反): ${JSON.stringify(afterUndo)}`,
      )
    }
    ok(`[C] undoで内容が縮んでも、グリッドは即座には縮まない(点滅しない): ${JSON.stringify(afterUndo)}`)

    await page.evaluate(() => window.__duocad.frameAll())
    await sleep(200)
    const afterFrameAllShrink = await page.evaluate(() => window.__duocad.gridScale())
    if (afterFrameAllShrink.extent !== 100 || afterFrameAllShrink.step !== 10) {
      fail(`frameAllを呼んでもグリッドが縮まない(縮小の許可が効いていない): ${JSON.stringify(afterFrameAllShrink)}`)
    }
    ok(`[C] frameAllを呼ぶと実際に縮む(BASE_EXTENTへ戻った): ${JSON.stringify(afterFrameAllShrink)}`)

    // ---- 新規文書でも縮む(カメラは動かさない`resetGrid`経路)
    await addBox([0, 0], 252, 10, 10)
    await page.waitForFunction((before) => window.__duocad.parts.length > before, {
      timeout,
      polling: 100,
    }, 1)
    await sleep(300)
    const grownAgain = await page.evaluate(() => window.__duocad.gridScale())
    if (grownAgain.extent !== 500 || grownAgain.step !== 50) {
      fail(`もう一度252mmの箱を作ってもextent=500/step=50に戻らない: ${JSON.stringify(grownAgain)}`)
    }
    ok('[C] もう一度大きな箱を作るとグリッドが即座に広がった(広げるのは常に即座)')

    // ====================================================================== D. 全部消してframeAllで縮む
    console.log('\n[D] 全部消した後のframeAll: 姿勢を合わせられなくてもグリッドは縮む')
    // **root(ボディ)もsketchRoot(スケッチ)も両方空にする** —— QA指摘の再現条件そのもの:
    // `frameToContent()`は`root`が空なら早期returnするが、`contentBoxForGrid()`は
    // `sketchRoot`も見るので、ボディだけ消してスケッチが残っていても同じ早期returnを通る。
    // 常駐させていた小さな立方体(sketch_create→add_rectangle→extrudeの3verb)と、直前で
    // 作り直した大きな箱(同じく3verb)の両方をundoで消し、文書を完全に空にする。
    for (let i = 0; i < 6; i += 1) await mcp.callTool('undo')
    await page.waitForFunction(
      () => window.__duocad.parts.length === 0 && window.__duocad.sketches.length === 0,
      { timeout, polling: 100 },
    )
    await sleep(200)
    ok('文書を完全に空にした(parts=0 / sketches=0)')

    const emptyBeforeFrameAll = await page.evaluate(() => window.__duocad.gridScale())
    if (emptyBeforeFrameAll.extent !== 500 || emptyBeforeFrameAll.step !== 50) {
      fail(
        `文書を空にした直後にグリッドが即座に縮んでしまった(ヒステリシス違反。空にする操作は` +
          `frameAllでも新規文書でもない、ふつうの編集のはず): ${JSON.stringify(emptyBeforeFrameAll)}`,
      )
    }
    ok(`[D] 空にした直後はまだ縮まない(点滅しない): ${JSON.stringify(emptyBeforeFrameAll)}`)

    const poseBeforeEmptyFrameAll = await page.evaluate(() => window.__duocad.cameraPose())
    await page.evaluate(() => window.__duocad.frameAll())
    await sleep(200)
    const afterEmptyFrameAll = await page.evaluate(() => window.__duocad.gridScale())
    if (afterEmptyFrameAll.extent !== 100 || afterEmptyFrameAll.step !== 10) {
      fail(
        `文書が空の状態でframeAllを呼んでもグリッドが縮まない` +
          `(frameToContentの早期returnがapplyGridScaleより前にあると起きる回帰): ` +
          `${JSON.stringify(afterEmptyFrameAll)}`,
      )
    }
    ok(`[D] 内容が空でもframeAllでグリッドは縮む(BASE_EXTENTへ戻った): ${JSON.stringify(afterEmptyFrameAll)}`)

    // **姿勢(カメラ)は動かない契約のまま** —— 空の箱には合わせられないので、
    // frameToContentは早期returnし続ける(戻り値=falseの意味を壊していないことの裏取り)。
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
    ok('[D] 空の文書でframeAllを呼んでもカメラは動かない(姿勢の契約は変えていない)')

    const poseBeforeNew = await page.evaluate(() => window.__duocad.cameraPose())
    await page.click('#file-new')
    await page.waitForFunction(
      () => window.__duocad.parts.length === 0 && window.__duocad.currentDocumentName === null,
      { timeout, polling: 100 },
    )
    await sleep(200)
    const afterNew = await page.evaluate(() => window.__duocad.gridScale())
    if (afterNew.extent !== 100 || afterNew.step !== 10) {
      fail(`新規文書にしてもグリッドが縮まない(resetGridの配線が効いていない): ${JSON.stringify(afterNew)}`)
    }
    ok(`[C] 新規文書にするとグリッドが縮む(BASE_EXTENTへ戻った): ${JSON.stringify(afterNew)}`)
    const poseAfterNew = await page.evaluate(() => window.__duocad.cameraPose())
    const cameraMoved = Math.hypot(
      poseAfterNew.position[0] - poseBeforeNew.position[0],
      poseAfterNew.position[1] - poseBeforeNew.position[1],
      poseAfterNew.position[2] - poseBeforeNew.position[2],
    )
    if (cameraMoved > 1e-6) {
      fail(`新規文書でグリッドを縮めるついでにカメラまで動いてしまった(移動量=${cameraMoved.toFixed(3)}mm、本タスクの範囲外の副作用)`)
    }
    ok('[C] 新規文書でグリッドを縮めても、カメラの姿勢は一切動いていない')

    await assertNoErrors(page, consoleErrors)
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

main().catch((error) => {
  console.error(`\nsmoke-grid-scale: 失敗 — ${error.message}`)
  process.exitCode = 1
})
