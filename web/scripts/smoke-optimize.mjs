/**
 * 実ブラウザでの形状最適化の**実行**検査(20260904-1705) —— 条件を置く → 走らせる → 結果が出る。
 *
 *   web> node scripts/smoke-optimize.mjs [--port 8905] [--http-port 8906]
 *
 * `smoke-optimize-mode.mjs` は「条件を**置ける**」までを見る。ここはその続きで、
 * **本物のSIMPを端から端まで回して、結果のメッシュボディが画面に出るところまで**を見る。
 * **薄くしない** —— 実測で4,225要素・46〜60秒・700枚超の三角形になる本物の最適化を1回通す
 * (要素数を落として1秒で終わらせると、通っているのは配線だけで最適化ではなくなる)。
 *
 * 見るもの:
 *
 *   1. **パネルが最適化モードのあいだだけ出る**。条件が足りないうちは実行が押せず、
 *      **足りない理由が画面に文で出ている**(コンソールではなく)。
 *   2. **荷重は面ごとに大きさ・向きを持てる**(面Aへ200Nの下向き、面Bへ50Nの法線)。
 *      **2枚目を置いても1枚目は消えない**(上書き代入)。`clear_loads` で1枚だけ外せる。
 *   3. **最小部材幅を打つと解像度の目盛りが作り直される**。**添字0が既定でいちばん粗く**、
 *      右へ動かすほど要素数と予想時間が増える(＝細かい方向にだけ動く目盛り)。
 *   4. **走らせると進捗が画面に出て、中断できる。** 中断したとき文書は1ビットも
 *      変わっていない(メッシュボディが増えていない)。
 *   5. **保持ソリッドがこの解像度で表現できていないと、消えない通知が画面に出る**
 *      (トーストは8秒で消えるので、結果が信用できないという意味の文はパネルにも残す)。
 *      **SIMPを回さずに戻る**ので速い。
 *   6. **走り切ると結果のメッシュボディが画面に出る**(部品一覧・viewportの三角形・投影)。
 *   7. **結果はSTLへ書き出せる**(元のソリッドと混ざったまま)。
 *   8. **元のボディが全部残っている**(最適化は編集ではない)。
 *   9. **メッシュボディをクリックするとボディとして選ばれる**(面参照が飛ばない)。
 *      **編集verbは起動しても確定できない**(＝押せるのに断られる、が起きない)。
 *  10. ブラウザのコンソールにエラーが出ていない。
 *
 * **サーバはこのスクリプトが自分で立てる**(`--models-dir` に使い捨てを渡すので、
 * リポジトリ直下の実 `models/` は触らない)。
 */
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { fail, ok, parseArgs } from './args.mjs'
import { findBrowser, launchBrowser, frameAll } from './browser.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const wsPort = options.port ?? '8905'
const httpPort = options['http-port'] ?? '8906'
const timeout = Number.parseInt(options.timeout ?? '60000', 10)
/** 最適化1回ぶんの待ち。**既定の60秒では足りない**(実測 約60秒 + 機械の熱状態で1.7倍)。 */
const runTimeout = Number.parseInt(options['run-timeout'] ?? '420000', 10)
const workdir = mkdtempSync(path.join(tmpdir(), 'duocad-optimize-run-'))

/** 最小部材幅[mm]。6.0 → 要素サイズの上限 4.0mm(= 6.0 / 1.5)。 */
const RMIN_MM = 6.0

async function main() {
  const executablePath = findBrowser()
  console.log(`smoke-optimize: ${executablePath}`)
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  const outDir = mkdtempSync(path.join(tmpdir(), 'duocad-smoke-optimize-'))
  let browser = null

  try {
    await mcp.handshake('duocad-smoke-optimize')

    /** 一辺 `size` の立方体。**離して置く**ので、あいだを繋ぐ材料を最適化が起こす。 */
    async function addBox(corner, size, distance) {
      const sketch = await mcp.callTool('sketch_create', { base_plane: 'XY' })
      await mcp.callTool('add_rectangle', {
        sketch_id: sketch.sketch_id,
        corner,
        width: size,
        height: size,
      })
      await mcp.callTool('propose_selection', { refs: [{ sketch: sketch.sketch_id, index: 1 }] })
      await mcp.callTool('commit_selection')
      const extruded = await mcp.callTool('extrude', { distance, mode: 'new' })
      await mcp.callTool('clear_selection')
      return extruded.bodies[0].body_id
    }

    const bodyA = await addBox([0, 0], 12, 12)
    const bodyB = await addBox([48, 0], 12, 12)
    ok(`12mm立方体を2つ、48mm離して作った(A=${bodyA} / B=${bodyB})`)

    browser = await launchBrowser(executablePath)
    const page = await browser.newPage()
    await page.setViewport({ width: 1280, height: 860 })
    const consoleErrors = []
    page.on('pageerror', (error) => consoleErrors.push(String(error)))
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    page.on('response', (response) => {
      if (response.status() >= 400) consoleErrors.push(`HTTP ${response.status()} ${response.url()}`)
    })
    await page.goto(`http://127.0.0.1:${httpPort}/`, { waitUntil: 'load', timeout })
    await page.waitForFunction(() => Boolean(window.__duocad?.ready), { timeout, polling: 100 })
    await page.waitForFunction(() => (window.__duocad?.parts ?? []).length >= 2, {
      timeout,
      polling: 100,
    })
    await frameAll(page)

    async function waitFor(check, what, args = [], limit = timeout) {
      try {
        await page.waitForFunction(check, { timeout: limit, polling: 100 }, ...args)
      } catch {
        fail(`${what}(タイムアウト)`)
      }
    }

    /** 画面から指せる面を1枚(`exclude` に載る添字は避ける)。 */
    async function pickAnyFace(bodyId, exclude = []) {
      const found = await page.evaluate(
        (id, skip) => {
          const geometry = window.__duocad.getGeometry(id)
          if (!geometry) return null
          for (const face of geometry.faces) {
            if (skip.includes(face.index)) continue
            if (window.__duocad.projectFace(id, face.index)) return face.index
          }
          return null
        },
        bodyId,
        exclude,
      )
      if (found === null) fail(`ボディ ${bodyId} に画面から指せる面が無い`)
      return found
    }

    /** 押す直前に射影し直してクリックする。 */
    async function clickFace(bodyId, faceIndex) {
      const at = await page.evaluate(
        (id, index) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectFace(id, index)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        bodyId,
        faceIndex,
      )
      if (!at) fail(`ボディ ${bodyId} の面 ${faceIndex} が画面の中に無い`)
      await page.mouse.click(at.x, at.y)
    }

    async function typeInto(selector, value) {
      await page.click(selector)
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyA')
      await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await page.type(selector, String(value), { delay: 15 })
    }

    const text = (selector) =>
      page.evaluate((sel) => document.querySelector(sel)?.textContent?.trim() ?? null, selector)
    const disabled = (selector) =>
      page.evaluate((sel) => {
        const element = document.querySelector(sel)
        return element === null ? null : element.disabled === true
      }, selector)

    // ================================================================== 1. パネルとモード
    console.log('\n[1] 最適化モードに入るとパネルが出る / 条件が足りないうちは実行が押せない')
    if (await page.$('#optimize-panel')) fail('通常モードなのに実行パネルが出ている')
    await page.click('#optimize-enter')
    await waitFor(() => window.__duocad.optimizeMode === true, 'モードに入らない')
    await waitFor(() => document.querySelector('#optimize-panel') !== null, 'パネルが出ない')
    await waitFor(() => window.__duocad.optimizeConditions !== null, '条件の点検が返ってこない')
    if ((await disabled('#optimize-run')) !== true) fail('条件が空なのに実行ボタンが押せる')
    const emptyProblems = await text('#optimize-conditions')
    if (!emptyProblems || !emptyProblems.includes('保持ソリッド')) {
      fail(`足りない条件が画面に出ていない: ${emptyProblems}`)
    }
    ok(`[1] パネルが出て、足りない条件が画面に文で出ている(「${emptyProblems.slice(0, 34)}…」)`)

    // ================================================================== 2. 条件を置く
    console.log('\n[2] 4条件を本物のクリックで置く')
    await page.click('#set-preserve-bodies')
    await waitFor(() => window.__duocad.wantedKinds.includes('body'), 'ボディ粒度にならない')
    await clickFace(bodyA, await pickAnyFace(bodyA))
    await clickFace(bodyB, await pickAnyFace(bodyB))
    await page.keyboard.press('Enter')
    await waitFor(
      () => (window.__duocad.state.optimize.preserve_bodies ?? []).length === 2,
      '保持ソリッドが2つにならない',
    )
    ok('[2] 保持ソリッド2つ(A・B)')

    await page.click('#set-fixtures')
    await waitFor(
      () => window.__duocad.wantedKinds.includes('face') && !window.__duocad.wantedKinds.includes('body'),
      '拘束面のverbで面粒度にならない',
    )
    const fixtureFace = await pickAnyFace(bodyA)
    await clickFace(bodyA, fixtureFace)
    await page.keyboard.press('Enter')
    await waitFor(
      () => (window.__duocad.state.optimize.fixtures ?? []).length === 1,
      '拘束面が1枚にならない',
    )
    ok(`[2] 拘束面1枚(A の面 ${fixtureFace})`)

    // ---------------------------------------------------------------- 面ごとに違う荷重を置く
    console.log('\n[3] 荷重は面ごとに大きさ・向きを持つ(2枚目を置いても1枚目は消えない)')
    const loadFaceA = await pickAnyFace(bodyB)
    const loadFaceB = await pickAnyFace(bodyB, [loadFaceA])

    await page.click('#set-loads')
    await clickFace(bodyB, loadFaceA)
    await typeInto('#load-magnitude', 200)
    await page.click('#load-direction-down')
    await page.keyboard.press('Enter')
    await waitFor(
      () => (window.__duocad.state.optimize.loads ?? []).length === 1,
      '1枚目の荷重面が載らない',
    )

    // **2枚目を置くときに1枚目が選択状態で現れてはならない**(`OPTIMIZE_PRESET_ROLE` に
    // `set_loads` を載せていない理由。載っていると、ここでEnterを押した瞬間に
    // 1枚目の200Nも50Nへ黙って書き換わる)。
    await page.click('#set-loads')
    const preselected = await page.evaluate(
      () => (window.__duocad.selection?.items ?? []).length,
    )
    if (preselected !== 0) {
      fail(`荷重verbの起動時に ${preselected} 個が選択されている(既存の荷重を巻き込んで上書きする)`)
    }
    await clickFace(bodyB, loadFaceB)
    await typeInto('#load-magnitude', 50)
    await page.click('#load-direction-normal')
    await page.keyboard.press('Enter')
    await waitFor(
      () => (window.__duocad.state.optimize.loads ?? []).length === 2,
      '2枚目の荷重面が載らない(1枚目を置き換えてしまった可能性)',
    )

    const specs = await page.evaluate(() =>
      JSON.parse(JSON.stringify(window.__duocad.state.optimize.load_specs)),
    )
    const byIndex = Object.fromEntries((specs ?? []).map((item) => [item.index, item]))
    if (byIndex[loadFaceA]?.magnitude !== 200 || byIndex[loadFaceA]?.direction !== 'vector') {
      fail(`1枚目の荷重が200Nの下向きのまま残っていない: ${JSON.stringify(specs)}`)
    }
    if (byIndex[loadFaceB]?.magnitude !== 50 || byIndex[loadFaceB]?.direction !== 'normal') {
      fail(`2枚目の荷重が50Nの法線向きになっていない: ${JSON.stringify(specs)}`)
    }
    await waitFor(
      () => window.__duocad.optimizeConditions?.ready === true,
      '荷重が2枚あると条件がそろわない(2枚は正当な入力である)',
    )
    if ((await disabled('#optimize-run')) !== false) fail('条件がそろったのに実行ボタンが押せない')
    ok('[3] 面ごとに違う荷重が共存し(200N下向き / 50N法線)、実行ボタンが押せる')

    // 矢印とラベルも面ごとに違うこと(1本を使い回すと、ここでラベルが同じ文字列になる)
    const loadArrows = await page.evaluate(() => window.__duocad.optimizeAnnotations().loads)
    const labels = new Set(loadArrows.map((item) => item.label))
    if (labels.size !== 2) {
      fail(`荷重の装飾のラベルが面ごとに違わない: ${JSON.stringify(loadArrows)}`)
    }
    ok(`[3] 装飾も面ごとに違う(${[...labels].join(' / ')})`)

    // ---------------------------------------------------------------- 1枚だけ外す
    await page.click('#clear-loads')
    await clickFace(bodyB, loadFaceB)
    await page.keyboard.press('Enter')
    await waitFor(
      () => (window.__duocad.state.optimize.loads ?? []).length === 1,
      'clear_loads で1枚だけ外れない',
    )
    const remaining = await page.evaluate(() =>
      JSON.parse(JSON.stringify(window.__duocad.state.optimize.load_specs)),
    )
    if (remaining.length !== 1 || remaining[0].magnitude !== 200) {
      fail(`外す面を選んだのに残った荷重が違う: ${JSON.stringify(remaining)}`)
    }
    ok('[3] clear_loads で選んだ1枚だけ外れ、残った面の200Nはそのまま')

    // ================================================================== 4. 解像度の目盛り
    console.log('\n[4] 最小部材幅 → 解像度の目盛り(添字0が既定=いちばん粗い。右へだけ動く)')
    await typeInto('#optimize-rmin', RMIN_MM)
    await waitFor(
      (rmin) => {
        const resolution = window.__duocad.optimizeResolution
        return Boolean(resolution) && Math.abs((resolution.from_rmin_mm ?? -1) - rmin / 1.5) < 1e-9
      },
      '最小部材幅を打っても解像度が取り直されない',
      [RMIN_MM],
    )
    const ladder = await page.evaluate(() => {
      const resolution = window.__duocad.optimizeResolution
      return {
        max: resolution.max_element_size_mm,
        min: resolution.min_element_size_mm,
        steps: resolution.steps.map((s) => [s.element_size_mm, s.element_count, s.seconds_typical]),
      }
    })
    if (Math.abs(ladder.max - RMIN_MM / 1.5) > 1e-9) {
      fail(`要素サイズの上限が rmin/1.5 でない: ${ladder.max}`)
    }
    if (ladder.steps.length < 2) fail(`目盛りの段が足りない: ${ladder.steps.length}`)
    if (Math.abs(ladder.steps[0][0] - ladder.max) > 1e-9) {
      fail(`添字0が上限側(いちばん粗い)でない: ${ladder.steps[0][0]} vs ${ladder.max}`)
    }
    for (let i = 1; i < ladder.steps.length; i += 1) {
      if (!(ladder.steps[i][0] < ladder.steps[i - 1][0])) fail('目盛りが細かい方向へ単調でない')
      if (!(ladder.steps[i][1] > ladder.steps[i - 1][1])) fail('右へ動かしても要素数が増えない')
      if (!(ladder.steps[i][2] > ladder.steps[i - 1][2])) fail('右へ動かしても予想時間が増えない')
    }
    const sliderMin = await page.evaluate(() => document.querySelector('#optimize-resolution').min)
    if (sliderMin !== '0') fail(`スライダーが粗い側へ動ける(min=${sliderMin})`)
    const estimateText = await text('#optimize-estimate')
    if (!estimateText || !estimateText.includes('2〜3倍の幅')) {
      fail(`予想時間が「幅で読む値」だと画面に書かれていない: ${estimateText}`)
    }
    ok(
      `[4] 目盛り ${ladder.steps.length}段 / 上限 ${ladder.max.toFixed(2)}mm(=rmin/1.5)・` +
        `既定 ${ladder.steps[0][1]}要素 → 最細 ${ladder.steps[ladder.steps.length - 1][1]}要素。` +
        '予想時間は幅であることが画面に書いてある',
    )

    // ================================================================== 5. 走らせて中断する
    console.log('\n[5] 実行 → 進捗が画面に出る → 中断 → 文書は1ビットも変わっていない')
    const partsBefore = await page.evaluate(() => window.__duocad.parts.length)
    await page.click('#optimize-run')
    await waitFor(() => window.__duocad.optimizeRunning === true, '実行を押しても走り出さない')
    if ((await disabled('#optimize-exit')) !== true) fail('実行中なのにモードから抜けられる')
    await waitFor(
      () => window.__duocad.optimizeProgress !== null,
      '進捗が1件も届かない',
      [],
      runTimeout,
    )
    const progressText = await text('#optimize-progress')
    if (!progressText) fail('進捗が画面に出ていない')
    await page.click('#optimize-cancel')
    await waitFor(() => window.__duocad.optimizeRunning === false, '中断しても止まらない', [], runTimeout)
    const cancelNotice = await text('#optimize-notice')
    if (!cancelNotice || !cancelNotice.includes('中断')) {
      fail(`中断したことが画面に残っていない: ${cancelNotice}`)
    }
    const partsAfterCancel = await page.evaluate(() => window.__duocad.parts.length)
    if (partsAfterCancel !== partsBefore) {
      fail(`中断したのにボディが増えている: ${partsBefore} -> ${partsAfterCancel}`)
    }
    ok(`[5] 進捗が出た(「${progressText.slice(0, 40)}…」)→ 中断できた → ボディは増えていない`)

    // ================================================================== 5b. 表現できない保持
    // **完了条件5(通知が画面に出る)を実ブラウザで見る唯一の場所。**
    // 要素サイズ4mmの格子に1mm角の保持ソリッドを置くと、ボクセル化しても1要素も
    // 掴めない(`20260904-1704` の契約④の実測)。**SIMPを回さずに戻る**ので速い。
    console.log('\n[5b] 保持ソリッドがこの解像度で表現できていないと、消えない通知が出る')
    const tiny = await addBox([24, 5.5], 1, 1)
    await mcp.callTool('propose_selection', {
      refs: [bodyA, bodyB, tiny].map((id) => ({ body: id, kind: 'body', index: null })),
    })
    await mcp.callTool('commit_selection')
    await mcp.callTool('set_preserve_bodies')
    await mcp.callTool('clear_selection')
    await waitFor(
      () => (window.__duocad.state.optimize.preserve_bodies ?? []).length === 3,
      '1mm角の保持ソリッドが載らない',
    )
    await waitFor(() => window.__duocad.optimizeConditions?.ready === true, '条件がそろわない')
    // **`optimizeRunning` の立ち上がりを待たない。** 事後検出はボクセル化した時点で
    // 分かるので SIMP も塊の導出も回らず、実測 0.02秒で戻る —— 100ms のポーリングでは
    // 「走っている」瞬間を捉えられない。**通知の中身**で待つこと。
    await page.click('#optimize-run')
    await waitFor(
      () =>
        (window.__duocad.optimizeNotice?.lines ?? []).some((line) =>
          line.includes('表現できていない'),
        ),
      '表現できていないことが通知されない',
      [],
      runTimeout,
    )
    await waitFor(
      () => window.__duocad.optimizeRunning === false,
      '表現できない保持ソリッドがあるのに止まらない',
      [],
      runTimeout,
    )
    const regionNotice = await text('#optimize-notice')
    if (!regionNotice || !regionNotice.includes('表現できていない')) {
      fail(`表現できていないことが画面に出ていない: ${regionNotice}`)
    }
    if (!regionNotice.includes('文書は1ビットも変わっていない')) {
      fail(`「文書が変わっていない」が画面に出ていない: ${regionNotice}`)
    }
    const partsAfterRegion = await page.evaluate(() => window.__duocad.parts.length)
    if (partsAfterRegion !== partsBefore + 1) {
      fail(`結果のボディが増えている(1mm角を足したぶんだけのはず): ${partsAfterRegion}`)
    }
    ok(`[5b] 消えない通知が出た(「${regionNotice.slice(0, 46)}…」)・結果は足されていない`)

    // 1mm角を役割から外してから消す(役割に残したまま消すと「文書に無い」で走らなくなる)。
    await mcp.callTool('propose_selection', {
      refs: [bodyA, bodyB].map((id) => ({ body: id, kind: 'body', index: null })),
    })
    await mcp.callTool('commit_selection')
    await mcp.callTool('set_preserve_bodies')
    await mcp.callTool('propose_selection', { refs: [{ body: tiny, kind: 'body', index: null }] })
    await mcp.callTool('commit_selection')
    await mcp.callTool('delete')
    await mcp.callTool('clear_selection')
    await waitFor(
      () => window.__duocad.optimizeConditions?.ready === true,
      '1mm角を外しても条件がそろわない',
    )

    // ================================================================== 6. 走り切る
    console.log('\n[6] もう一度、最後まで走らせる(既定の解像度。実測 約60秒)')
    const started = Date.now()
    await page.click('#optimize-run')
    await waitFor(() => window.__duocad.optimizeRunning === true, '2回目が走り出さない')
    await waitFor(
      () => window.__duocad.optimizeRunning === false,
      '最適化が終わらない',
      [],
      runTimeout,
    )
    await waitFor(
      () => (window.__duocad.parts ?? []).some((body) => body.kind === 'mesh'),
      '結果のメッシュボディが部品一覧に出ない',
      [],
      runTimeout,
    )
    const elapsed = ((Date.now() - started) / 1000).toFixed(0)
    const resultNotice = await text('#optimize-notice')
    if (!resultNotice || !resultNotice.includes('三角形')) {
      fail(`結果が画面に出ていない: ${resultNotice}`)
    }
    const mesh = await page.evaluate(() => {
      const body = window.__duocad.parts.find((item) => item.kind === 'mesh')
      const geometry = window.__duocad.getGeometry(body.body_id)
      return {
        bodyId: body.body_id,
        name: body.name,
        faceCount: geometry === null ? null : geometry.faces.length,
        triangles: geometry === null ? null : geometry.faces[0].indices.length / 3,
      }
    })
    if (mesh.faceCount !== 1) fail(`メッシュは面1枚として配る約束: ${mesh.faceCount}`)
    if (!(mesh.triangles > 100)) {
      fail(`三角形が少なすぎる(本物の最適化が回っていない疑い): ${mesh.triangles}`)
    }
    await frameAll(page)
    const projected = await page.evaluate(
      (bodyId) => window.__duocad.projectFace(bodyId, 1),
      mesh.bodyId,
    )
    if (projected === null) fail('結果のメッシュが画面へ投影できない(描かれていない)')
    ok(
      `[6] ${elapsed}秒で終わり、結果「${mesh.name}」が画面に出た` +
        `(三角形 ${mesh.triangles}枚・投影 x=${projected.x.toFixed(0)} y=${projected.y.toFixed(0)})`,
    )
    await page.screenshot({ path: path.join(outDir, 'optimize-result.png') })

    // ================================================================== 7. 元のボディが残る
    const solids = await page.evaluate(() =>
      window.__duocad.parts.filter((body) => body.kind === 'solid').map((body) => body.body_id),
    )
    if (!solids.includes(bodyA) || !solids.includes(bodyB)) {
      fail(`元のボディが残っていない: ${JSON.stringify(solids)}`)
    }
    ok('[7] 元のソリッドは2つとも残っている(最適化は編集ではない)')

    // ================================================================== 8. STLへ書ける
    const stlPath = path.join(outDir, 'optimized.stl')
    const exported = await mcp.callTool('export_stl', { path: stlPath, deviation: 0.2 })
    if (!exported.path) fail(`export_stl の応答にパスが無い: ${JSON.stringify(exported)}`)
    const stl = readFileSync(stlPath)
    const triangles = stl.readUInt32LE(80)
    if (stl.length !== 84 + 50 * triangles) fail('STLの三角形数とファイル長が合わない')
    if (triangles <= mesh.triangles) {
      fail(`STLに元のソリッドが入っていない(${triangles} <= メッシュだけで ${mesh.triangles})`)
    }
    ok(`[8] STLへ書けた(${triangles}三角形 = 結果 ${mesh.triangles} + 元のソリッド)`)

    // ================================================================== 9. メッシュのピック
    console.log('\n[9] メッシュをクリックすると「ボディ」として選ばれる(面参照が飛ばない)')
    await page.click('#optimize-exit')
    await waitFor(() => window.__duocad.mode === 'idle', '最適化モードから抜けられない')
    await page.evaluate(() => window.__duocad.frameAll())
    await clickFace(mesh.bodyId, 1)
    await waitFor(
      (id) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.kind === 'body' && item.body === id,
        ),
      'メッシュをクリックしてもボディとして選ばれない',
      [mesh.bodyId],
    )
    const faceRefs = await page.evaluate(
      (id) =>
        (window.__duocad.selection?.items ?? []).filter(
          (item) => item.kind === 'face' && item.body === id,
        ).length,
      mesh.bodyId,
    )
    if (faceRefs !== 0) fail(`メッシュに面参照が飛んでいる(${faceRefs}件)`)
    ok('[9] メッシュはボディ参照になる(`select_face` の断りを踏まない)')

    // **編集verbは確定できない** —— 起動はできるが、ボディ参照ではどの編集verbの
    // 選択要件も満たせないので、Enterがサーバへ届かない(押せるのに断られる、が起きない)。
    const opsBefore = await page.evaluate(() => window.__duocad.state.applied_count)
    await page.click('#move-body')
    await page.keyboard.press('Enter')
    await new Promise((resolve) => setTimeout(resolve, 400))
    const opsAfter = await page.evaluate(() => window.__duocad.state.applied_count)
    if (opsAfter !== opsBefore) fail('メッシュボディに move_body が通ってしまった')
    const prompt = await text('#command-prompt')
    if (!prompt || !prompt.includes('選ぶ')) {
      fail(`編集verbが「何が足りないか」を言っていない: ${prompt}`)
    }
    ok(`[9] 編集verbは確定できず、足りないものを言う(「${prompt.slice(0, 30)}…」)`)

    // ================================================================== 10. コンソール
    if (consoleErrors.length > 0) {
      fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    }
    ok('[10] ブラウザのコンソールにエラーなし')

    console.log('')
    console.log('smoke-optimize: PASS')
  } finally {
    if (browser !== null) await browser.close()
    await server.stop()
    rmSync(outDir, { recursive: true, force: true })
    rmSync(workdir, { recursive: true, force: true })
  }
}

main().catch((error) => {
  console.error(`smoke-optimize: FAIL ${error.message}`)
  process.exit(1)
})
