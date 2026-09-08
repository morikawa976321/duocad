/**
 * 実ブラウザでの**開発用デバッグ表示**の検査(20260905-1700) ——
 * `?debug=space` / `?debug=anchors` / `?voxel=1` が本体で出ることを見る。
 *
 *   web> node scripts/smoke-optimize-debug.mjs [--port 8907] [--http-port 8908]
 *
 * **守るべき最重要の性質は「デモ題材に固定されていないこと」**である —— この検査は
 * **その場で押し出して作った箱2つ**(組み込みの合成題材のどれでもない)を題材にする。
 *
 * 見るもの:
 *
 *   1. **フラグ無しの画面では何も描かれていない**(製品の見た目を1画素も変えない)。
 *   2. `?debug=space` —— 塊(設計空間)のボクセルが出る。**実行を押してすぐ**
 *      (`stage:'derive'` の速報)に出て、最後に本番の塊(`stage:'final'`)へ置き換わる。
 *   3. `?debug=anchors` —— 拘束/荷重に割り当たった節点が**両方とも**出る。
 *      **球の深度検査が切れている**(切れていないと形状の裏側の節点が見えない ——
 *      「見えているつもりで見えていない」)。`anchorsDepthTest` は**固定と荷重のOR**なので、
 *      片方だけ戻した退化もここで落ちる。
 *   4. `?voxel=1` —— 平滑化する前のボクセルが出て、**結果のメッシュボディは隠れている**
 *      (差し替えずボクセルのまま止める)。**文書は変わっていない**(部品一覧には居る)。
 *   5. ブラウザのコンソールにエラーが出ていない。
 *
 * **サーバはこのスクリプトが自分で立てる**(`--models-dir` に使い捨てを渡すので、
 * リポジトリ直下の実 `models/` は触らない)。**本物のSIMPを1回通す**(実測 60〜120秒) ——
 * 塊も節点も密度場も、走らせなければ1つも生まれない。
 */
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { fail, ok, parseArgs } from './args.mjs'
import { findBrowser, launchBrowser, frameAll } from './browser.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const wsPort = options.port ?? '8907'
const httpPort = options['http-port'] ?? '8908'
const timeout = Number.parseInt(options.timeout ?? '60000', 10)
/** 最適化1回ぶんの待ち(`smoke-optimize.mjs` と同じ理由で既定の60秒では足りない)。 */
const runTimeout = Number.parseInt(options['run-timeout'] ?? '420000', 10)
const workdir = mkdtempSync(path.join(tmpdir(), 'duocad-optimize-debug-'))
/** 目で見るための控え(`/screenshots/` は `.gitignore` 対象)。**合否には使わない。** */
const shotDir = options.shots ?? path.join(path.dirname(workdir), 'duocad-debug-shots')

/** 最小部材幅[mm]。6.0 → 要素サイズの上限 4.0mm(= 6.0 / MIN_RMIN=1.5)。 */
const RMIN_MM = 6.0

async function main() {
  const executablePath = findBrowser()
  console.log(`smoke-optimize-debug: ${executablePath}`)
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null

  try {
    await mcp.handshake('duocad-smoke-optimize-debug')

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

    async function selectFaces(refs) {
      await mcp.callTool('propose_selection', { refs })
      await mcp.callTool('commit_selection')
    }

    // ================================================================== 題材(デモではない)
    let fixtureFaceIndex = null
    let loadFaceIndex = null
    const bodyA = await addBox([0, 0], 12, 12)
    const bodyB = await addBox([48, 0], 12, 12)
    ok(`12mm立方体を2つ、48mm離して押し出した(A=${bodyA} / B=${bodyB})`)

    await selectFaces([bodyA, bodyB].map((id) => ({ body: id, kind: 'body', index: null })))
    await mcp.callTool('set_preserve_bodies')
    await mcp.callTool('clear_selection')

    browser = await launchBrowser(executablePath)

    /** ページを1枚開いて、共通の穴が立つまで待つ。 */
    async function openPage(query) {
      const page = await browser.newPage()
      await page.setViewport({ width: 1280, height: 860 })
      const consoleErrors = []
      page.on('pageerror', (error) => consoleErrors.push(String(error)))
      page.on('console', (message) => {
        if (message.type() === 'error') consoleErrors.push(message.text())
      })
      await page.goto(`http://127.0.0.1:${httpPort}/${query}`, { waitUntil: 'load', timeout })
      await page.waitForFunction(() => Boolean(window.__duocad?.ready), { timeout, polling: 100 })
      await page.waitForFunction(() => (window.__duocad?.parts ?? []).length >= 2, {
        timeout,
        polling: 100,
      })
      await frameAll(page)
      return { page, consoleErrors }
    }

    // ================================================================== 1. フラグ無し
    console.log('\n[1] フラグ無しの画面には、デバッグ表示が1つも無い')
    {
      const { page, consoleErrors } = await openPage('')
      const flags = await page.evaluate(() => window.__duocad.debugFlags)
      if (flags.space || flags.anchors || flags.voxel) {
        fail(`フラグ無しなのに立っている: ${JSON.stringify(flags)}`)
      }
      const counts = await page.evaluate(() => window.__duocad.optimizeDebugCounts())
      if (counts.space !== 0 || counts.rho !== 0 || counts.fixed !== 0 || counts.load !== 0) {
        fail(`フラグ無しなのに描かれている: ${JSON.stringify(counts)}`)
      }
      if (counts.hiddenBodyId !== null) fail('フラグ無しなのにボディを隠している')
      if (consoleErrors.length > 0) fail(`コンソールにエラー: ${consoleErrors[0]}`)

      // ついでに、拘束面・荷重面をここで決める(面の添字は押し出しの結果で決まるので、
      // **画面が持っている実際の面**から選ぶ —— 添字を決め打ちしない)。
      const faceOf = async (bodyId, axis, wantMax) =>
        page.evaluate(
          (id, a, mx) => {
            const geometry = window.__duocad.getGeometry(id)
            if (!geometry) return null
            let best = null
            for (const face of geometry.faces) {
              if (face.indices.length === 0) continue
              if (best === null) { best = face; continue }
              const better = mx ? face.centroid[a] > best.centroid[a] : face.centroid[a] < best.centroid[a]
              if (better) best = face
            }
            return best === null ? null : best.index
          },
          bodyId,
          axis,
          wantMax,
        )
      fixtureFaceIndex = await faceOf(bodyA, 0, false) // A の x 最小面(いちばん外側)
      loadFaceIndex = await faceOf(bodyB, 2, true) // B の上面
      if (fixtureFaceIndex === null || loadFaceIndex === null) fail('条件に使う面が見つからない')

      await page.close()
      ok('[1] `debugFlags` は全部 false・描かれているものは0・隠しているボディも無い')
    }

    await selectFaces([{ body: bodyA, kind: 'face', index: fixtureFaceIndex }])
    await mcp.callTool('set_fixtures')
    await mcp.callTool('clear_selection')
    await selectFaces([{ body: bodyB, kind: 'face', index: loadFaceIndex }])
    await mcp.callTool('set_loads', { magnitude: 200, direction: [0, 0, -1] })
    await mcp.callTool('clear_selection')
    ok(`4条件を置いた(拘束=A面${fixtureFaceIndex} / 荷重=B面${loadFaceIndex} 200N -Z)`)

    // ================================================================== 2〜4. 3つとも立てる
    console.log('\n[2-4] `?debug=space,anchors&voxel=1` で走らせる(本物のSIMP。60〜120秒)')
    const { page, consoleErrors } = await openPage('?debug=space,anchors&voxel=1')
    const flags = await page.evaluate(() => window.__duocad.debugFlags)
    if (!flags.space || !flags.anchors || !flags.voxel) {
      fail(`フラグが読めていない: ${JSON.stringify(flags)}`)
    }

    async function waitFor(check, what, args = [], limit = timeout) {
      try {
        await page.waitForFunction(check, { timeout: limit, polling: 100 }, ...args)
      } catch {
        fail(`${what}(タイムアウト)`)
      }
    }

    await page.click('#optimize-enter')
    await waitFor(() => window.__duocad.optimizeMode === true, 'モードに入らない')
    await waitFor(() => window.__duocad.optimizeConditions?.ready === true, '条件がそろわない')
    // 最小部材幅を打って、いちばん粗い段(既定)で回す。
    await page.click('#optimize-rmin')
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyA')
    await page.keyboard.up('Control')
    await page.keyboard.press('Backspace')
    await page.type('#optimize-rmin', String(RMIN_MM), { delay: 15 })
    await waitFor(
      (rmin) => {
        const resolution = window.__duocad.optimizeResolution
        return Boolean(resolution?.steps) && Math.abs((resolution.from_rmin_mm ?? -1) - rmin / 1.5) < 1e-9
      },
      '解像度の目盛りが取り直されない',
      [RMIN_MM],
    )

    const partsBefore = await page.evaluate(() => window.__duocad.parts.length)
    await page.click('#optimize-run')
    await waitFor(() => window.__duocad.optimizeRunning === true, '走り出さない')

    // -------------------------------------------------- 2. 塊は**走り切る前に**出る
    await waitFor(
      () => window.__duocad.optimizeDebugCounts().space > 0,
      '塊(?debug=space)が出ない',
      [],
      runTimeout,
    )
    const early = await page.evaluate(() => ({
      counts: window.__duocad.optimizeDebugCounts(),
      running: window.__duocad.optimizeRunning,
    }))
    ok(
      `[2] 塊が ${early.counts.space} 要素で出た(実行中=${early.running}・` +
        `間引き 1/${early.counts.spaceStride})`,
    )

    await waitFor(() => window.__duocad.optimizeRunning === false, '最適化が終わらない', [], runTimeout)
    await waitFor(
      () => (window.__duocad.parts ?? []).some((body) => body.kind === 'mesh'),
      '結果のメッシュボディが部品一覧に出ない',
      [],
      runTimeout,
    )

    const counts = await page.evaluate(() => window.__duocad.optimizeDebugCounts())
    const parts = await page.evaluate(() => window.__duocad.parts.length)

    // -------------------------------------------------- 2(続き). 本番の塊へ置き換わった
    if (!(counts.space > 0)) fail(`走り切ったのに塊が消えている: ${JSON.stringify(counts)}`)
    ok(`[2] 走り切った後も塊が出ている(${counts.space} 要素・間引き 1/${counts.spaceStride})`)

    // -------------------------------------------------- 3. 面アンカー
    if (!(counts.fixed > 0)) fail('拘束面に割り当たった節点が1つも出ていない')
    if (!(counts.load > 0)) fail('荷重面に割り当たった節点が1つも出ていない')
    if (counts.anchorsDepthTest !== false) {
      fail(
        '面アンカーの球の深度検査が切れていない(形状の裏側の節点が見えない=' +
          `「見えているつもりで見えていない」): depthTest=${counts.anchorsDepthTest}`,
      )
    }
    ok(`[3] 節点が出た(固定 ${counts.fixed} / 荷重 ${counts.load})・深度検査は切れている`)

    // -------------------------------------------------- 4. 平滑化前のボクセル
    if (!(counts.rho > 0)) fail('平滑化前のボクセル(?voxel=1)が出ていない')
    if (counts.hiddenBodyId === null) fail('?voxel=1 なのに結果のメッシュボディが隠れていない')
    if (parts !== partsBefore + 1) {
      fail(`結果のボディが文書に入っていない(隠すのは表示だけのはず): ${partsBefore} -> ${parts}`)
    }
    ok(
      `[4] ボクセル ${counts.rho} 個(間引き 1/${counts.rhoStride})・` +
        `結果ボディ ${counts.hiddenBodyId} は表示だけ隠れている(部品一覧には居る: ${parts}件)`,
    )

    // **probe が画面の文字で出していた情報**(面の添字・担当面積・間引く前の総数)が
    // 本体でも読めることを、突き合わせの記録として出す。
    const payloads = await page.evaluate(() => window.__duocad.optimizeDebugPayloads())
    console.log(`  info  ${JSON.stringify(payloads)}`)

    // 目で見るための控えを1枚だけ残す(**合否はここまでの数えで決まっている**。
    // 画像は「色と重なりが読める形になっているか」を人が後から見るためのもの)。
    mkdirSync(shotDir, { recursive: true })
    const shot = path.join(shotDir, 'optimize-debug.png')
    await page.screenshot({ path: shot })
    ok(`控えを撮った: ${shot}`)

    // ================================================================== 5. コンソール
    if (consoleErrors.length > 0) fail(`コンソールにエラー: ${consoleErrors[0]}`)
    ok('[5] コンソールにエラーは出ていない')

    console.log('\nsmoke-optimize-debug: PASS')
  } finally {
    if (browser) await browser.close()
    await server.stop()
    rmSync(workdir, { recursive: true, force: true })
  }
}

main().catch((error) => {
  console.error(`\nsmoke-optimize-debug: FAIL\n${error.stack ?? error}`)
  process.exitCode = 1
})
