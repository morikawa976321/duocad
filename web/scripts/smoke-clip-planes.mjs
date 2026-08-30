/**
 * 実機での確認 — **near/farが初回フレーミングから切り離され、内容が変わるたびに追随する**。
 *
 *   node scripts/smoke-clip-planes.mjs [--step <STEPのパス>] [--port 8957] [--http-port 8958]
 *
 * 2026-08-30に実測した不具合の再現手順そのもの: **小さな部品で初回フレーミングを消費させた後、
 * 大きなSTEPを開くと、モデルの一部がクリップ面で切り落とされる**(`frameToContent()` が
 * near/far を「最初に届いた形の大きさ」で決め、`framed` フラグにより初回しか走らないため)。
 *
 *   1. MCPで小さな板(40×30×20mm)を1つ作る
 *   2. ブラウザを開く(この時点で小さな板を初回フレーミングし、near/farが小さく固定される
 *      —— 旧コードはここから二度と動かない)
 *   3. 対角約640mmのSTEP(`hanger_bar_attatchment.step`)をMCPの `import_step` で読む
 *   4. `__duocad.cameraPose()` の `near`/`far` を読み、**文書全体の外接球**が
 *      `[near, far]` の内側に完全に収まっていることを数値で確かめる
 *
 * 「モデルの端が消える」は目でしか判定しづらいので、代わりに**外接球の中心からカメラまでの
 * 距離 ± 半径**が `[near, far]` に収まるかを見る(球はboxそのものより大きいので安全側の判定)。
 *
 * サーバはこのスクリプトが空の文書で起動し、板・STEPともMCP経由で作る。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 */
import { mkdtempSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { findBrowser, launchBrowser } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const stepPath =
  options.step ?? 'C:\\Users\\tm976\\mf\\hanger_bar_attatchment.step'
const wsPort = options.port ?? '8957'
const httpPort = options['http-port'] ?? '8958'
const timeout = Number.parseInt(options.timeout ?? '120000', 10)
const workdir = mkdtempSync(join(tmpdir(), 'duocad-clip-planes-'))

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function main() {
  const executablePath = findBrowser()
  if (!existsSync(stepPath)) fail(`STEPが無い: ${stepPath}`)
  console.log(`smoke-clip-planes: ${stepPath}`)

  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null
  try {
    await mcp.handshake('duocad-smoke-clip-planes')

    // ---------------------------------------------------------------- 1. 小さな板
    const sketch = await mcp.callTool('sketch_create', { base_plane: 'XY' })
    await mcp.callTool('add_rectangle', {
      sketch_id: sketch.sketch_id,
      corner: [0, 0],
      width: 40,
      height: 30,
    })
    await mcp.callTool('propose_selection', {
      refs: [{ sketch: sketch.sketch_id, index: 1 }],
    })
    await mcp.callTool('commit_selection')
    await mcp.callTool('extrude', { distance: 20, mode: 'new' })
    await mcp.callTool('clear_selection')
    const smallBodyId = (await mcp.callTool('describe_document')).bodies[0].body_id
    ok(`小さな板を作った: ${smallBodyId}(40×30×20mm、対角約53.9mm)`)

    // ---------------------------------------------------------------- 2. ブラウザを開く(初回フレーミングを消費)
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
    // **`frameAll` は呼ばない** —— 呼ぶと再フレーミングでnear/farも作り直され、
    // 「初回フレーミングを消費した後、二度と動かない」という再現条件が壊れる。
    // ページ読み込み自体が最初の `update()` を起こし、それが初回フレーミングを消費する。
    await page.waitForFunction(
      () => (window.__duocad?.state?.bodies ?? []).length > 0,
      { timeout, polling: 100 },
    )

    const smallPose = await page.evaluate(() => window.__duocad.cameraPose())
    ok(
      `小さな板だけの視錐台: near=${smallPose.near.toFixed(5)}mm / far=${smallPose.far.toFixed(2)}mm`,
    )

    // ---------------------------------------------------------------- 3. 大きなSTEPを読む
    const imported = await mcp.callTool('import_step', { path: stepPath })
    if (imported.body_ids.length === 0) fail('STEPからボディが1つも来ない')
    ok(`大きなSTEPを読んだ: ${imported.body_ids.length}部品`)

    // 状態配信が届いて `viewport.update()` が走るのを待つ(新しいbody_idが揃うまで)。
    const expectedBodies = imported.body_ids.length + 1
    await page.waitForFunction(
      (count) => (window.__duocad?.state?.bodies ?? []).length >= count,
      { timeout, polling: 100 },
      expectedBodies,
    )
    // 描画・視錐台の更新が1フレーム遅れる可能性があるので、少し待って読む。
    await sleep(200)

    // ---------------------------------------------------------------- 4. 判定
    const probe = await page.evaluate(() => {
      const hook = window.__duocad
      const bodies = hook.state?.bodies ?? []
      let min = null
      let max = null
      for (const body of bodies) {
        const geometry = hook.getGeometry(body.body_id)
        if (!geometry) continue
        const { min: bmin, max: bmax } = geometry.bbox
        if (min === null) {
          min = [...bmin]
          max = [...bmax]
        } else {
          for (let i = 0; i < 3; i += 1) {
            min[i] = Math.min(min[i], bmin[i])
            max[i] = Math.max(max[i], bmax[i])
          }
        }
      }
      return { min, max, pose: hook.cameraPose(), bodyCount: bodies.length }
    })

    if (probe.min === null) fail('文書全体の外接直方体が取れない(ボディのgeometryが無い)')

    const [minX, minY, minZ] = probe.min
    const [maxX, maxY, maxZ] = probe.max
    const center = [(minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2]
    const size = [maxX - minX, maxY - minY, maxZ - minZ]
    const diagonal = Math.hypot(size[0], size[1], size[2])
    const radius = diagonal / 2
    const [px, py, pz] = probe.pose.position
    const distance = Math.hypot(px - center[0], py - center[1], pz - center[2])
    const { near, far } = probe.pose

    ok(
      `文書全体(${probe.bodyCount}部品)の外接対角 ${diagonal.toFixed(1)}mm・` +
        `カメラから中心まで ${distance.toFixed(1)}mm / near=${near.toFixed(5)}mm far=${far.toFixed(1)}mm`,
    )

    if (!(far >= distance + radius)) {
      fail(
        `外接球の遠端がfarより先にある(切り落とされる): 遠端=${(distance + radius).toFixed(1)}mm ` +
          `far=${far.toFixed(1)}mm(near/farが初回フレーミングに固定されたままの疑い)`,
      )
    }
    ok(`外接球の遠端(${(distance + radius).toFixed(1)}mm)はfar(${far.toFixed(1)}mm)の内側`)

    const nearEdge = distance - radius
    if (nearEdge > 0 && !(near <= nearEdge)) {
      fail(
        `外接球の近端がnearより手前にある(切り落とされる): 近端=${nearEdge.toFixed(3)}mm ` +
          `near=${near.toFixed(5)}mm`,
      )
    }
    ok(`外接球の近端(${nearEdge.toFixed(3)}mm)はnear(${near.toFixed(5)}mm)より奥`)

    // **旧コードなら確実に落ちること**を確かめる —— 小さな板だけの far で
    // 大きなSTEPの遠端を試すと、必ず追い越されるはず(このテストが空振りでないことの確認)。
    if (far <= smallPose.far * 1.5) {
      fail(
        `farが小さな板のときの値からほとんど動いていない(視錐台が追随していない疑い): ` +
          `板だけ far=${smallPose.far.toFixed(1)}mm -> STEP後 far=${far.toFixed(1)}mm`,
      )
    }
    ok(`farは小さな板の値(${smallPose.far.toFixed(1)}mm)から実際に広がった(${far.toFixed(1)}mm)`)

    const errors = await page.evaluate(() => window.__duocad.errors ?? [])
    if (errors.length > 0) fail(`走行中にエラーが出た: ${JSON.stringify(errors)}`)
    if (consoleErrors.length > 0) fail(`コンソールにエラー: ${consoleErrors.join(' / ')}`)
    ok('走行中にエラーは1件も出ていない')

    console.log('\nsmoke-clip-planes: 全項目 OK')
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

main().catch((error) => {
  console.error(`\nsmoke-clip-planes: 失敗 — ${error.message}`)
  process.exitCode = 1
})
