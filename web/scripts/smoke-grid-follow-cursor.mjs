/**
 * 実機での確認 — スケッチで実際に描画中、グリッドがマウスカーソルへ追従して広がること
 * (`20260910-1615-duocad-grid-follow-cursor`)。
 *
 *   node scripts/smoke-grid-follow-cursor.mjs [--port 8993] [--http-port 8994]
 *
 * 完了条件を実機で見る:
 *
 *   1. アクティブなスケッチで図形を描いている間、カーソルが現在のグリッド範囲の外へ出たら
 *      その位置を覆うまでグリッドが広がる(基準平面スケッチ・面スケッチの両方)。
 *   3. カーソルが元の内容の近くへ戻ったら、グリッドは自然に縮む(内容だけのときと同じ値に戻る)。
 *   4. この追従は実際に描画中のときだけ。3平面選択中・スケッチ外の通常表示では、
 *      カーソルを動かしても `gridTiers().planeBounds` は変わらない。
 *
 * サーバはこのスクリプトが空の文書で起動し、形はMCP経由で作る。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 */
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { findBrowser, launchBrowser, withoutBrowserPanel } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const wsPort = options.port ?? '8993'
const httpPort = options['http-port'] ?? '8994'
const timeout = Number.parseInt(options.timeout ?? '60000', 10)

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** 段の不透明度・グリッドの状態は描画ループ(`tick`)で入れ直すので、読む前に数フレーム待つ。 */
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

async function main() {
  const executablePath = findBrowser()
  const workdir = mkdtempSync(join(tmpdir(), 'duocad-grid-follow-'))
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null
  try {
    await mcp.handshake('duocad-smoke-grid-follow-cursor')

    // XY基準平面のスケッチ(40×30mm)を押し出して箱にする。**このスケッチ自身**を
    // 編集し直して基準平面グリッドの追従を見る一方、箱の側面は面グリッドの追従に使う。
    const sketch = await mcp.callTool('sketch_create', { base_plane: 'XY' })
    await mcp.callTool('add_rectangle', {
      sketch_id: sketch.sketch_id,
      corner: [0, 0],
      width: 40,
      height: 30,
    })
    await mcp.callTool('propose_selection', { refs: [{ sketch: sketch.sketch_id, index: 1 }] })
    await mcp.callTool('commit_selection')
    const extruded = await mcp.callTool('extrude', { distance: 20, mode: 'new' })
    await mcp.callTool('clear_selection')
    const bodyId = extruded.bodies[0].body_id
    ok(`40×30×20mmの箱を作った(body=${bodyId}、スケッチ=${sketch.sketch_id})`)

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

    const tiersNow = () => page.evaluate(() => window.__duocad.gridTiers())

    async function viewportBox() {
      return page.evaluate(() => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        return { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
      })
    }

    /**
     * ワールド座標 `[x,y,z]` を画面へ射影する(ページ絶対)。画面外なら null。
     *
     * **`projectPoint` 自身はxy方向の画面内判定をしない**(zクリップだけ見て、
     * 投影先がどれだけ画面の外にはみ出ていても値を返す — `viewport.ts` の実装参照)。
     * ここで `.viewport` の矩形と突き合わせて弾く(`smoke-grid-scale.mjs` の
     * `zoomOutUntilVisible`/`hoverPlaneAt` と同じ手当て)。
     */
    async function screenPointFor(worldPoint) {
      const box = await viewportBox()
      const at = await page.evaluate((point) => window.__duocad.projectPoint(point), worldPoint)
      if (!at || at.x < 0 || at.x > box.width || at.y < 0 || at.y > box.height) return null
      return { x: box.left + at.x, y: box.top + at.y }
    }

    /** 指定のワールド座標が画面に映るまで実際にホイールでズームアウトする。 */
    async function zoomOutUntilVisible(worldPoint, maxNotches = 200) {
      for (let i = 0; i < maxNotches; i += 1) {
        if (await screenPointFor(worldPoint)) return true
        const box = await viewportBox()
        await page.mouse.move(box.left + box.width / 2, box.top + box.height / 2)
        await page.mouse.wheel({ deltaY: 480 }) // 正で引く(ズームアウト)
        await sleep(30)
      }
      return false
    }

    // ====================================================================== [0] 完了条件4: 通常表示ではカーソルを動かしても変わらない
    console.log('\n[0] 完了条件4: スケッチ外の通常表示ではカーソルを動かしてもグリッド範囲は変わらない')
    await settle(page)
    const plainBefore = (await tiersNow()).planeBounds[0]
    await withoutBrowserPanel(page, async () => {
      const box = await viewportBox()
      // 画面の隅々をなめる(内容の近く・遠く両方を含む)。
      for (const [fx, fy] of [[0.1, 0.1], [0.9, 0.1], [0.1, 0.9], [0.9, 0.9], [0.5, 0.5]]) {
        await page.mouse.move(box.left + box.width * fx, box.top + box.height * fy)
        await settle(page)
      }
    })
    const plainAfter = (await tiersNow()).planeBounds[0]
    if (JSON.stringify(plainAfter) !== JSON.stringify(plainBefore)) {
      fail(
        `[0] 通常表示なのにカーソルでグリッド範囲が動いた(完了条件4違反): ` +
          `${JSON.stringify(plainBefore)} -> ${JSON.stringify(plainAfter)}`,
      )
    }
    ok(`[0] 通常表示ではカーソルを動かしても不変: ${JSON.stringify(plainBefore)}`)

    // 完了条件4の後半: 3平面選択中(`basePlanesOn`。スケッチのコマンドを起動して貼る先を
    // 待っている状態)でもカーソルを動かして範囲が変わらないこと。
    await page.click('#sketch-create')
    await page.waitForFunction(() => window.__duocad.wantsBasePlanes === true, { timeout, polling: 50 })
    await settle(page)
    const pendingBefore = (await tiersNow()).planeBounds
    await withoutBrowserPanel(page, async () => {
      const box = await viewportBox()
      for (const [fx, fy] of [[0.1, 0.1], [0.9, 0.1], [0.1, 0.9], [0.9, 0.9], [0.5, 0.5]]) {
        await page.mouse.move(box.left + box.width * fx, box.top + box.height * fy)
        await settle(page)
      }
    })
    const pendingAfter = (await tiersNow()).planeBounds
    if (JSON.stringify(pendingAfter) !== JSON.stringify(pendingBefore)) {
      fail(
        `[0] 3平面選択中なのにカーソルでグリッド範囲が動いた(完了条件4違反): ` +
          `${JSON.stringify(pendingBefore)} -> ${JSON.stringify(pendingAfter)}`,
      )
    }
    ok(`[0] 3平面選択中(basePlanesOn)でもカーソルを動かしても不変: ${JSON.stringify(pendingBefore)}`)
    await page.click('#sketch-exit')
    await page.waitForFunction(() => window.__duocad.wantsBasePlanes === false, { timeout, polling: 50 })

    // ====================================================================== [1] 基準平面スケッチの追従
    console.log('\n[1] 基準平面(XY)スケッチ: カーソルが範囲外へ出るとグリッドが広がり、戻ると縮む')
    await page.click(`li[data-sketch-id="${sketch.sketch_id}"] .sketch-name`)
    await page.waitForFunction(
      (id) => window.__duocad.activeSketch === id,
      { timeout, polling: 50 },
      sketch.sketch_id,
    )
    await settle(page)
    const planeInBase = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      return JSON.stringify(active?.plane ?? null)
    })
    if (JSON.parse(planeInBase)?.kind !== 'base') {
      fail(`[1] 前提が崩れている(plane.kindがbaseでない): ${planeInBase}`)
    }

    const baseBaseline = (await tiersNow()).planeBounds[0]
    if (baseBaseline === null) fail('[1] 基準平面スケッチのplaneBoundsがnull(前提が崩れている)')
    ok(`[1] 内容だけのグリッド範囲(基準平面): ${JSON.stringify(baseBaseline)}`)

    // 現在の範囲より確実に外側の点(u軸方向)。
    const baseFarU = baseBaseline.centerU + baseBaseline.half + 300
    const baseFarPoint = [baseFarU, baseBaseline.centerV, 0] // XY平面: u=x, v=y, z=0
    if (!(await zoomOutUntilVisible(baseFarPoint))) {
      fail(`[1] ズームアウトを繰り返してもカーソル追従の判定点が画面に映らない: u=${baseFarU}`)
    }
    await withoutBrowserPanel(page, async () => {
      const at = await screenPointFor(baseFarPoint)
      if (!at) fail(`[1] 判定点を射影できない: ${JSON.stringify(baseFarPoint)}`)
      await page.mouse.move(at.x, at.y)
      await settle(page)
    })
    const baseGrown = (await tiersNow()).planeBounds[0]
    if (!(baseGrown.half > baseBaseline.half)) {
      fail(
        `[1] カーソルを範囲外へ出したのにグリッドが広がっていない(完了条件1違反): ` +
          `baseline=${JSON.stringify(baseBaseline)} grown=${JSON.stringify(baseGrown)}`,
      )
    }
    if (baseGrown.centerU + baseGrown.half < baseFarU - 1) {
      fail(
        `[1] 広がった範囲がカーソル位置(u=${baseFarU})を覆っていない: ${JSON.stringify(baseGrown)}`,
      )
    }
    ok(`[1] カーソルが遠くへ出るとグリッドが広がり、その位置を覆う: ${JSON.stringify(baseGrown)}`)

    // カーソルを元の内容の内側(重心付近)へ戻す —— 縮む(完了条件3)。
    const backPoint = [baseBaseline.centerU, baseBaseline.centerV, 0]
    await withoutBrowserPanel(page, async () => {
      const at = await screenPointFor(backPoint)
      if (!at) fail(`[1] 戻す先の点を射影できない: ${JSON.stringify(backPoint)}`)
      await page.mouse.move(at.x, at.y)
      await settle(page)
    })
    const baseShrunk = (await tiersNow()).planeBounds[0]
    if (Math.abs(baseShrunk.half - baseBaseline.half) > 1e-6) {
      fail(
        `[1] カーソルが内容の近くへ戻ったのに、内容だけの範囲まで縮んでいない(完了条件3違反): ` +
          `baseline=${JSON.stringify(baseBaseline)} shrunk=${JSON.stringify(baseShrunk)}`,
      )
    }
    ok(`[1] カーソルが内容の近くへ戻るとグリッドが自然に縮む: ${JSON.stringify(baseShrunk)}`)

    await page.click('#sketch-exit')
    await page.waitForFunction(() => window.__duocad.activeSketch === null, { timeout, polling: 50 })
    // [1]で大きくズームアウトしたままだと箱が小さすぎて面を指せない。視点を戻す。
    await page.evaluate(() => window.__duocad.frameAll())
    await settle(page)

    // ====================================================================== [2] 面スケッチの追従
    console.log('\n[2] 任意面(箱の側面)スケッチ: 同じ追従が働く')
    const planarCandidates = await page.evaluate((id) => {
      const geometry = window.__duocad.getGeometry(id)
      return geometry.faces
        .filter((face) => face.kind === 'plane')
        .map((face) => face.index)
        .filter((index) => window.__duocad.projectFace(id, index) !== null)
    }, bodyId)
    if (planarCandidates.length === 0) fail('[2] 平面状の面が画面から1枚も指せない(この裏取りが成立しない)')

    async function locateFace(id, index) {
      return page.evaluate(
        (bId, faceIndex) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectFace(bId, faceIndex)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        id,
        index,
      )
    }

    let onFace = null
    for (const faceIndex of planarCandidates) {
      if (
        await page.evaluate(
          () => document.querySelector('.command-host')?.dataset.command !== 'sketch_create',
        )
      ) {
        await page.click('#sketch-create')
      }
      const at = await locateFace(bodyId, faceIndex)
      if (!at) continue
      await page.mouse.click(at.x, at.y)
      const created = await page
        .waitForFunction(() => window.__duocad.activeSketch !== null, { timeout: 15000, polling: 50 })
        .then(() => true)
        .catch(() => false)
      if (!created) {
        await page.keyboard.press('Escape')
        continue
      }
      await settle(page)
      const planeRaw = await page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return JSON.stringify(active?.plane ?? null)
      })
      const plane = JSON.parse(planeRaw)
      if (plane?.kind === 'face') {
        onFace = { faceIndex, plane }
        break
      }
      // 基準平面のピックに取られた(既知の落とし穴。本タスクの対象外) —— 抜けて次の面へ。
      await page.click('#sketch-exit')
      await page.waitForFunction(() => window.__duocad.activeSketch === null, { timeout, polling: 50 })
    }
    if (onFace === null) fail('[2] 任意面へのスケッチが1つも作れなかった(この裏取りが成立しない)')
    ok(`[2] 面${onFace.faceIndex}にスケッチを作れた`)

    const faceBaseline = (await tiersNow()).planeBounds[0]
    if (faceBaseline === null) fail('[2] 面スケッチのplaneBoundsがnull(前提が崩れている)')
    ok(`[2] 内容だけのグリッド範囲(面): ${JSON.stringify(faceBaseline)}`)

    const frame = onFace.plane.frame
    const worldFromUv = (u, v) => [
      frame.origin[0] + u * frame.u[0] + v * frame.v[0],
      frame.origin[1] + u * frame.u[1] + v * frame.v[1],
      frame.origin[2] + u * frame.u[2] + v * frame.v[2],
    ]
    const faceFarU = faceBaseline.centerU + faceBaseline.half + 300
    const faceFarPoint = worldFromUv(faceFarU, faceBaseline.centerV)
    if (!(await zoomOutUntilVisible(faceFarPoint))) {
      fail(`[2] ズームアウトを繰り返してもカーソル追従の判定点が画面に映らない: u=${faceFarU}`)
    }
    await withoutBrowserPanel(page, async () => {
      const at = await screenPointFor(faceFarPoint)
      if (!at) fail(`[2] 判定点を射影できない: ${JSON.stringify(faceFarPoint)}`)
      await page.mouse.move(at.x, at.y)
      await settle(page)
    })
    const faceGrown = (await tiersNow()).planeBounds[0]
    if (!(faceGrown.half > faceBaseline.half)) {
      fail(
        `[2] カーソルを範囲外へ出したのにグリッドが広がっていない(完了条件1違反・面スケッチ): ` +
          `baseline=${JSON.stringify(faceBaseline)} grown=${JSON.stringify(faceGrown)}`,
      )
    }
    if (faceGrown.centerU + faceGrown.half < faceFarU - 1) {
      fail(
        `[2] 広がった範囲がカーソル位置(u=${faceFarU})を覆っていない: ${JSON.stringify(faceGrown)}`,
      )
    }
    ok(`[2] 面スケッチでもカーソルが遠くへ出るとグリッドが広がり、その位置を覆う: ${JSON.stringify(faceGrown)}`)

    const faceBackPoint = worldFromUv(faceBaseline.centerU, faceBaseline.centerV)
    await withoutBrowserPanel(page, async () => {
      const at = await screenPointFor(faceBackPoint)
      if (!at) fail(`[2] 戻す先の点を射影できない: ${JSON.stringify(faceBackPoint)}`)
      await page.mouse.move(at.x, at.y)
      await settle(page)
    })
    const faceShrunk = (await tiersNow()).planeBounds[0]
    if (Math.abs(faceShrunk.half - faceBaseline.half) > 1e-6) {
      fail(
        `[2] 面スケッチでカーソルが内容の近くへ戻ったのに縮んでいない(完了条件3違反): ` +
          `baseline=${JSON.stringify(faceBaseline)} shrunk=${JSON.stringify(faceShrunk)}`,
      )
    }
    ok(`[2] 面スケッチでもカーソルが内容の近くへ戻ると自然に縮む: ${JSON.stringify(faceShrunk)}`)

    await page.click('#sketch-exit')
    await page.waitForFunction(() => window.__duocad.activeSketch === null, { timeout, polling: 50 })

    const errors = await page.evaluate(() => window.__duocad.errors ?? [])
    if (errors.length > 0) fail(`走行中にエラーが出た: ${JSON.stringify(errors)}`)
    if (consoleErrors.length > 0) fail(`コンソールにエラー: ${consoleErrors.join(' / ')}`)
    ok('走行中にエラーは1件も出ていない')
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

main().catch((error) => {
  console.error(`\nsmoke-grid-follow-cursor: 失敗 — ${error.message}`)
  process.exitCode = 1
})
