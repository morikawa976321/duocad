/**
 * 実機での確認 — **選択パネルを消した後も、人が選択を直せて、AIの提案をそのまま実行できる**。
 *
 *   node scripts/smoke-vertex.mjs [--port 8965] [--http-port 8966]
 *
 * 2026-08-23に `SelectionPanel.svelte`(一覧・「外す」・「全解除」・「承認する」)を削除した。
 * その代替が本当に機能することを、**本物のMCPと本物のブラウザ**で見る:
 *
 *   1. AIが頂点を提案 → **選択に入っている頂点だけ**が3Dにマーカーで出る(提案の色=水色)。
 *      頂点は3Dで指せない唯一の種別だったので、**ここが消えると人は頂点を外せなくなる**
 *   2. **本物のCtrl+クリックで1つ外せる**(origin が proposed → user・AI側も同じ集合を読む)
 *   3. AIが面を提案 → **人がツールバーから押し出すだけでverbが走る**
 *      (旧「承認する」ボタンの代替。`duocad/server.py` の `_commit_before_verb`)
 *
 * サーバはこのスクリプトが空の文書で起動し、板はMCP経由で作る。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 */
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { findBrowser, launchBrowser, frameAll } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const wsPort = options.port ?? '8965'
const httpPort = options['http-port'] ?? '8966'
const timeout = 60000
const workdir = mkdtempSync(join(tmpdir(), 'duocad-vertex-'))

async function main() {
  const executablePath = findBrowser()
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null
  try {
    await mcp.handshake('duocad-smoke-vertex')
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
    await mcp.callTool('extrude', { distance: 20, mode: 'join' })
    const bodyId = (await mcp.callTool('describe_document')).bodies[0].body_id
    ok(`板を作った: ${bodyId}`)

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
    await frameAll(page).catch(() => {})

    // ---------------------------------------------------------------- 1. 頂点マーカー
    const proposed = await mcp.callTool('propose_selection', {
      refs: [1, 2, 3, 4].map((index) => ({ body: bodyId, kind: 'vertex', index })),
    })
    if (proposed.selection.origin !== 'proposed') fail('提案なのに origin が proposed でない')
    // 頂点の位置がAI側からも読めること(measure の頂点対応)
    const measured = await mcp.callTool('measure')
    if (!measured.items.every((item) => Array.isArray(item.point) && item.point.length === 3)) {
      fail(`measure が頂点の座標を返さない: ${JSON.stringify(measured.items)}`)
    }
    ok(`AIが頂点4つを提案(measure も座標を返す: ${JSON.stringify(measured.items[0].point)})`)

    await page.waitForFunction(
      () => (window.__duocad.vertexMarkers ?? []).length === 4,
      { timeout, polling: 50 },
    )
    const markerState = await page.evaluate(() => ({
      markers: window.__duocad.vertexMarkers,
      origin: window.__duocad.selection.origin,
    }))
    if (markerState.origin !== 'proposed') fail('画面側の origin が proposed でない')
    ok(`ブラウザに頂点マーカー4個(origin=${markerState.origin} = 水色)`)

    // **引いても指せる**(当たり判定が画素基準であること)。
    // **見た目の大きさはここでは見ていない** —— マーカーは `sizeAttenuation: false` で
    // 画素固定にしてあるが、それが効いているかは目で見るしかない(当たり判定は
    // `viewport.ts` の `worldPerPixel` 由来で、描画の大きさとは別の経路)。
    const viewportBox = await page.evaluate(() => {
      const box = document.querySelector('.viewport').getBoundingClientRect()
      return { x: box.left + box.width / 2, y: box.top + box.height / 2 }
    })
    await page.mouse.move(viewportBox.x, viewportBox.y)
    await page.mouse.wheel({ deltaY: 900 }) // 引く
    await new Promise((resolve) => setTimeout(resolve, 300))
    const reachableZoomedOut = await page.evaluate(
      (markers) => markers.filter((m) => window.__duocad.projectVertex(m.bodyId, m.vertexIndex)).length,
      markerState.markers,
    )
    if (reachableZoomedOut === 0) fail('引いたら頂点マーカーが1つも指せなくなった(画素固定が効いていない)')
    await page.mouse.wheel({ deltaY: -900 }) // 戻す
    await new Promise((resolve) => setTimeout(resolve, 300))
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
      origin: window.__duocad.selection.origin,
      markers: window.__duocad.vertexMarkers.length,
    }))
    if (afterRemove.count !== 3) fail(`外した後の件数が3でない: ${afterRemove.count}`)
    if (afterRemove.origin !== 'user') fail(`触ったのに origin が user でない: ${afterRemove.origin}`)
    if (afterRemove.markers !== 3) fail(`マーカーが3個に減っていない: ${afterRemove.markers}`)
    const readBack = (await mcp.callTool('get_selection', {})).selection
    if (readBack.count !== 3) fail(`コア側の件数が3でない: ${readBack.count}`)
    ok(`Ctrl+クリックで頂点 ${picked} を外した(4→3件・origin=user・AI側も同じ)`)

    // ---------------------------------------------------------------- 3. 自動commit
    // AIが上面を提案 → **人はツールバーから押し出すだけ**(「承認する」ボタンは無い)
    const all = await mcp.callTool('propose_selection', {
      predicate: { kind: 'face', where: [{ op: 'normal_dot_gt', axis: 'Z', value: 0.9 }] },
    })
    const faces = (await mcp.callTool('measure')).items.filter(
      (item) => item.body === bodyId && item.normal && item.normal[2] > 0.9,
    )
    if (faces.length === 0) fail(`上向きの面が見つからない: ${JSON.stringify(all.selection.items)}`)
    const top = faces[0]
    const again = await mcp.callTool('propose_selection', {
      refs: [{ body: bodyId, kind: 'face', index: top.index }],
    })
    if (again.selection.origin !== 'proposed') fail('提案の origin が proposed でない')
    await page.waitForFunction(
      () => window.__duocad.selection?.origin === 'proposed' && window.__duocad.selection.count === 1,
      { timeout, polling: 50 },
    )
    const volumeBefore = (await mcp.callTool('describe_shape', { body_id: bodyId })).volume
    ok(`AIが上面(面 ${top.index})を提案 → 画面は origin=proposed のまま(承認ボタンは無い)`)

    await page.click('#extrude')
    const promptAfterStart = await page.$eval('#command-prompt', (el) => el.textContent.trim())
    if (promptAfterStart.includes('承認')) fail(`起動しただけで承認を求められた: ${promptAfterStart}`)
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
    const blocked = errors.filter((item) => String(item).includes('SelectionNotCommitted'))
    if (blocked.length) fail(`承認されずに止まった: ${JSON.stringify(blocked)}`)
    ok(
      `提案のまま人が押し出せた(体積 ${volumeBefore.toFixed(1)} → ${volumeAfter.toFixed(1)}・` +
        `SelectionNotCommitted は0件)`,
    )

    if (consoleErrors.length) fail(`ブラウザのコンソールにエラー: ${consoleErrors.join(' / ')}`)
    ok('ブラウザのコンソールにエラーなし')
    console.log('\nsmoke-vertex: PASS')
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

main().catch((error) => {
  console.error(`\nsmoke-vertex: FAIL ${error.message ?? error}`)
  process.exitCode = 1
})
