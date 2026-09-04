/**
 * 実機での確認 — STEPインポート直後にビューポートがモデル全体へ合わせ直され、
 * かつ「初回フレーミングの一回きりの機会」が空振りで焼き切れない。
 *
 *   node scripts/smoke-reframe-on-import.mjs [--step <STEPのパス>] [--port 8975] [--http-port 8976]
 *
 * 2つの完了条件を別々の姿勢で見る(A/Bは互いに独立した欠陥なので、片方の修正だけでは
 * もう片方の姿勢は緑にならない):
 *
 *   A. **`import_step` での再フレーミング**(完了条件1): 先に小さな部品で初回フレーミングを
 *      消費させておいてから大きなSTEPを読むと、旧コードは視点を一切動かさず、大きなモデルの
 *      端が画面外に出る(2026-08-30に本人が実測: 対角約640mmのSTEPで再現し、
 *      `window.__duocad.frameAll()` で復帰した。それまで画面には視点を戻す手段が無い)。
 *      `applied.verb === 'import_step'` の応答ハンドラに `viewport?.frameAll()` を足して直した
 *      (`web/src/App.svelte`)。
 *
 *   B. **`framed` フラグの空振り消費**(完了条件2): 不可視のBody(`geometry: null`)だけが
 *      載った状態配信が1通あると(**ページを開いた瞬間、他所で隠しておいたBodyがちょうど
 *      それにあたる** —— 接続直後の初期配信は「このクライアントがまだ知らないBody」を
 *      全部含むので、隠したBodyしか無い文書を開けば自然に起きる)、その1通で「初回
 *      フレーミングの一回きりの機会」が消費されてしまい、後から本物の形が届いても
 *      二度とフレーミングされない。旧コードは
 *      `if (!framed) { frameToContent(); framed = true }` で、`frameToContent()` が
 *      箱が空で早期returnしても無条件に `framed = true` にしていた。`frameToContent()`に
 *      「実際に合わせたか」を boolean で持たせ、`update()` はそれを見てから `framed` を
 *      立てるように直した(`web/src/viewport.ts`)。**`import_step` 専用の再フレーミング
 *      (A)を迂回しないよう、Bの検査は素の `extrude`(通常の編集)だけで再現する** ——
 *      `import_step` はAの修正で常に明示的に合わせ直すので、Aの修正だけでBの欠陥を
 *      覆い隠して見えなくしてしまう。
 *
 * どちらも `window.__duocad.projectPoint(world)` で外接箱の8頂点を画面座標に投影し、
 * 全頂点がビューポートの矩形内にあるかを数値で確かめる。**`projectFace` は使わない** ——
 * 最初にピックできた三角形の重心を返すので、視点や画角を変えると同じ面でも数十px跳ぶ
 * (engine [[projected-face-point-jumps-between-triangles]]、2026-08-28に acceptance の
 * FOV検査を実際にこれで直している)。
 *
 * サーバはこのスクリプトが空の文書で起動し、板・STEP・立方体ともMCP経由で作る。
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
const stepPath = options.step ?? 'C:\\Users\\tm976\\mf\\hanger_bar_attatchment.step'
const wsPort = options.port ?? '8975'
const httpPort = options['http-port'] ?? '8976'
const timeout = Number.parseInt(options.timeout ?? '120000', 10)

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** ページ内のエラー(pageerror・console.error・4xx/5xx応答)を控えて、最後にまとめて見る。 */
function watchErrors(page) {
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  page.on('response', (response) => {
    if (response.status() >= 400) errors.push(`HTTP ${response.status()} ${response.url()}`)
  })
  return errors
}

async function assertNoErrors(page, consoleErrors) {
  const errors = await page.evaluate(() => window.__duocad.errors ?? [])
  if (errors.length > 0) fail(`走行中にエラーが出た: ${JSON.stringify(errors)}`)
  if (consoleErrors.length > 0) fail(`コンソールにエラー: ${consoleErrors.join(' / ')}`)
  ok('走行中にエラーは1件も出ていない')
}

/** 1つのBodyの外接直方体8頂点を全て投影し、ビューポート矩形内にあるかを返す。 */
async function projectedCornersInsideViewport(page, bodyId) {
  return page.evaluate((id) => {
    const hook = window.__duocad
    const geometry = hook.getGeometry(id)
    if (!geometry) return { ok: false, reason: `getGeometry(${id}) が null(ジオメトリが届いていない)` }
    const { min, max } = geometry.bbox
    const corners = []
    for (const x of [min[0], max[0]]) {
      for (const y of [min[1], max[1]]) {
        for (const z of [min[2], max[2]]) corners.push([x, y, z])
      }
    }
    const el = document.querySelector('.viewport')
    const rect = el.getBoundingClientRect()
    const projected = corners.map((point) => hook.projectPoint(point))
    const outside = projected
      .map((point, index) => ({ point, corner: corners[index] }))
      .filter(
        ({ point }) =>
          point === null || point.x < 0 || point.x > rect.width || point.y < 0 || point.y > rect.height,
      )
    return {
      ok: outside.length === 0,
      viewport: { width: rect.width, height: rect.height },
      cornerCount: corners.length,
      outside,
    }
  }, bodyId)
}

// ====================================================================== A. import_step での再フレーミング
async function checkImportReframes(executablePath) {
  console.log(`\n[A] import_stepでの再フレーミング: 小さな板(初回フレーミングを消費) → 大きなSTEP(${stepPath})`)
  if (!existsSync(stepPath)) fail(`STEPが無い: ${stepPath}`)
  const workdir = mkdtempSync(join(tmpdir(), 'duocad-reframe-import-'))
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null
  try {
    await mcp.handshake('duocad-smoke-reframe-import')

    // ---------------------------------------------------------------- 1. 小さな板(初回フレーミングを消費)
    const sketch = await mcp.callTool('sketch_create', { base_plane: 'XY' })
    await mcp.callTool('add_rectangle', { sketch_id: sketch.sketch_id, corner: [0, 0], width: 40, height: 30 })
    await mcp.callTool('propose_selection', { refs: [{ sketch: sketch.sketch_id, index: 1 }] })
    await mcp.callTool('commit_selection')
    await mcp.callTool('extrude', { distance: 20, mode: 'new' })
    await mcp.callTool('clear_selection')
    ok('小さな板を作った(40×30×20mm)')

    // ---------------------------------------------------------------- 2. ブラウザを開く(初回フレーミングを消費)
    browser = await launchBrowser(executablePath)
    const page = await browser.newPage()
    await page.setViewport({ width: 1280, height: 860 })
    const consoleErrors = watchErrors(page)
    await page.goto(`http://127.0.0.1:${httpPort}/`, { waitUntil: 'load', timeout })
    await page.waitForFunction(() => Boolean(window.__duocad?.ready), { timeout, polling: 100 })
    await page.waitForFunction(() => (window.__duocad?.state?.bodies ?? []).length > 0, {
      timeout,
      polling: 100,
    })
    await sleep(200)
    const poseBeforeImport = await page.evaluate(() => window.__duocad.cameraPose())
    ok(`小さな板だけで初回フレーミング済み: position=${poseBeforeImport.position.map((v) => v.toFixed(1))}`)

    // ---------------------------------------------------------------- 3. 大きなSTEPを読む
    // **MCP経由(`mcp.callTool('import_step', ...)`)では読まない。** MCPが起こす変更は
    // `applied` を持たずに配信される(`duocad/net/ws.py` の `_on_core_change`: 「MCPからの
    // 変更は誰の操作でもない外からの変化であり、`applied` は操作を出したクライアントへの
    // 確認応答だから」)。`App.svelte` の再フレーミングは `applied.verb === 'import_step'`
    // で分岐するので、MCP起点だと**この検査したい経路そのものを一度も通らない**まま
    // 別の経路(smoke-clip-planes.mjsが見ている `framed` フラグの一般則)だけで緑になる
    // 恐れがある。**本物のUI操作(ファイルパネルの入力+クリック)で、ブラウザ自身のWS接続
    // から読む**(`smoke-files.mjs` と同じ作法。これなら `applied` がこの接続に付く)。
    const idsBefore = await page.evaluate(() => window.__duocad.parts.map((body) => body.body_id))
    async function typeInto(selector, value) {
      await page.click(selector)
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyA')
      await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await page.type(selector, String(value), { delay: 15 })
    }
    // **STEP読み込みはツールバーのコマンド**(2026-09-05に `FilesPanel` から戻った)。
    // 起動して初めてパス欄が現れるので**押す→打つ→Enter**の順になる
    // (`commands/files.ts` の `IMPORT_STEP`)。
    await page.click('#file-import-step')
    await typeInto('#file-import-path', stepPath)
    await page.keyboard.press('Enter')
    await page.waitForFunction(
      (before) => window.__duocad.parts.length > before,
      { timeout, polling: 100 },
      idsBefore.length,
    )
    await sleep(300) // 描画・視錐台の更新が1フレーム遅れる可能性があるので少し待つ
    const importedIds = await page.evaluate(
      (before) => window.__duocad.parts.filter((body) => !before.includes(body.body_id)).map((body) => body.body_id),
      idsBefore,
    )
    if (importedIds.length === 0) fail('STEPからボディが1つも来ない')
    ok(`大きなSTEPを読んだ: ${importedIds.length}部品`)

    // ---------------------------------------------------------------- 4. 判定
    const poseAfterImport = await page.evaluate(() => window.__duocad.cameraPose())
    const [bx, by, bz] = poseBeforeImport.position
    const [ax, ay, az] = poseAfterImport.position
    const moved = Math.hypot(ax - bx, ay - by, az - bz)
    // **空振りでないことの確認**: 大きなSTEP(対角約640mm)を読んだのに視点がほとんど動いて
    // いなければ、この後の8頂点判定がたまたま緑になっただけの疑いがある。
    if (moved < 50) {
      fail(
        `STEPを読んでもカメラがほとんど動いていない(再フレーミングが起きていない疑い): ` +
          `移動量=${moved.toFixed(1)}mm`,
      )
    }
    ok(`STEPを読んでカメラが実際に動いた(移動量=${moved.toFixed(1)}mm)`)

    for (const bodyId of importedIds) {
      const result = await projectedCornersInsideViewport(page, bodyId)
      if (!result.ok) {
        fail(
          `Body ${bodyId} の外接箱8頂点がビューポート(${JSON.stringify(result.viewport)})に` +
            `収まっていない: ${JSON.stringify(result.outside)}`,
        )
      }
    }
    ok(`インポートした${importedIds.length}部品全ての外接箱8頂点がビューポート内に投影される`)

    await assertNoErrors(page, consoleErrors)
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

// ====================================================================== B. framedフラグの空振り消費
async function checkFramedFlagNotWastedByHiddenBody(executablePath) {
  console.log('\n[B] framedフラグの空振り消費: 隠したBodyだけの初回配信 → 素のextrudeで実際にフレーミングされるか')
  const workdir = mkdtempSync(join(tmpdir(), 'duocad-reframe-hidden-'))
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null
  try {
    await mcp.handshake('duocad-smoke-reframe-hidden')

    // ---------------------------------------------------------------- 1. 原点近くの小さな立方体を作って隠す
    // **ブラウザはまだ開いていない** —— この時点で隠しておけば、これから開く接続にとっては
    // 「geometry: null のBodyだけが載った最初の1通」が自然にできる(接続直後の初期配信は
    // そのクライアントがまだ知らない全Bodyを含むので、隠したBodyしか無い文書を開けば
    // 必ずこの形になる)。
    const sketchHidden = await mcp.callTool('sketch_create', { base_plane: 'XY' })
    await mcp.callTool('add_rectangle', {
      sketch_id: sketchHidden.sketch_id,
      corner: [0, 0],
      width: 10,
      height: 10,
    })
    await mcp.callTool('propose_selection', { refs: [{ sketch: sketchHidden.sketch_id, index: 1 }] })
    await mcp.callTool('commit_selection')
    const hiddenExtrude = await mcp.callTool('extrude', { distance: 10, mode: 'new' })
    await mcp.callTool('clear_selection')
    const hiddenBodyId = hiddenExtrude.bodies[0].body_id
    await mcp.callTool('set_visible', { body_id: hiddenBodyId, visible: false })
    ok(`原点近くの立方体を作って隠した: ${hiddenBodyId}(10×10×10mm)`)

    // ---------------------------------------------------------------- 2. ブラウザを開く(隠したBodyだけの初回配信)
    browser = await launchBrowser(executablePath)
    const page = await browser.newPage()
    await page.setViewport({ width: 1280, height: 860 })
    const consoleErrors = watchErrors(page)
    await page.goto(`http://127.0.0.1:${httpPort}/`, { waitUntil: 'load', timeout })
    await page.waitForFunction(() => Boolean(window.__duocad?.ready), { timeout, polling: 100 })
    await page.waitForFunction(() => (window.__duocad?.state?.bodies ?? []).length >= 1, {
      timeout,
      polling: 100,
    })
    const initialBodies = JSON.parse(
      await page.evaluate(() =>
        JSON.stringify(
          (window.__duocad.state.bodies ?? []).map((body) => ({
            body_id: body.body_id,
            kind: body.kind,
            visible: body.visible,
          })),
        ),
      ),
    )
    if (initialBodies.length !== 1 || initialBodies[0].visible !== false) {
      fail(`初回配信の前提が崩れている(隠したBody1個だけのはず): ${JSON.stringify(initialBodies)}`)
    }
    const geometryOfHidden = await page.evaluate((id) => window.__duocad.getGeometry(id), hiddenBodyId)
    if (geometryOfHidden !== null) {
      fail(`隠したBodyのgeometryがnullでない(前提が崩れている): ${JSON.stringify(geometryOfHidden)}`)
    }
    ok('隠したBodyだけの初回配信を確認した(state.bodies=1件・geometry=null)')
    await sleep(200)
    const poseAfterHiddenOnly = await page.evaluate(() => window.__duocad.cameraPose())
    ok(`geometry:null だけの配信の直後のカメラ: position=${poseAfterHiddenOnly.position.map((v) => v.toFixed(1))}`)

    // ---------------------------------------------------------------- 3. 原点から遠く離れた場所へ素のextrudeで本物の形を作る
    // **`import_step` は使わない** —— 完了条件1の修正(`viewport?.frameAll()`)が
    // `framed` フラグを迂回して常に合わせ直すので、Bの欠陥(フラグの空振り消費)を
    // 検査したいのにAの修正で覆い隠されてしまう。ふつうの `extrude` はどの再フレーミング
    // 分岐にも入らない(`App.svelte` は import_step 以外へ再フレーミングを広げていない)ので、
    // ここで実際に画面へ収まるなら、それは `update()` 自身の一回きりの機会がまだ
    // 生きていたということである。
    const FAR = 5000
    const sketchFar = await mcp.callTool('sketch_create', { base_plane: 'XY' })
    await mcp.callTool('add_rectangle', {
      sketch_id: sketchFar.sketch_id,
      corner: [FAR, FAR],
      width: 10,
      height: 10,
    })
    await mcp.callTool('propose_selection', { refs: [{ sketch: sketchFar.sketch_id, index: 1 }] })
    await mcp.callTool('commit_selection')
    const farExtrude = await mcp.callTool('extrude', { distance: 10, mode: 'new' })
    await mcp.callTool('clear_selection')
    const farBodyId = farExtrude.bodies[0].body_id
    ok(`原点から離れた立方体を素のextrudeで作った: ${farBodyId}(中心付近 (${FAR},${FAR},5))`)

    await page.waitForFunction(() => (window.__duocad?.state?.bodies ?? []).length >= 2, {
      timeout,
      polling: 100,
    })
    await sleep(300)

    // ---------------------------------------------------------------- 4. 判定
    const poseAfterFar = await page.evaluate(() => window.__duocad.cameraPose())
    const distanceMoved = Math.hypot(
      poseAfterFar.position[0] - poseAfterHiddenOnly.position[0],
      poseAfterFar.position[1] - poseAfterHiddenOnly.position[1],
      poseAfterFar.position[2] - poseAfterHiddenOnly.position[2],
    )
    // **空振りでないことの確認**: 原点近くの空の文書用カメラのままなら、
    // (5000,5000,5)付近の立方体までは何千mmも動かないはず。
    if (distanceMoved < 1000) {
      fail(
        `離れた立方体を作ってもカメラがほとんど動いていない(初回フレーミングが空振りで` +
          `焼き切れた疑い): 移動量=${distanceMoved.toFixed(1)}mm`,
      )
    }
    ok(`離れた立方体を作ってカメラが実際に動いた(移動量=${distanceMoved.toFixed(1)}mm)`)

    const result = await projectedCornersInsideViewport(page, farBodyId)
    if (!result.ok) {
      fail(
        `離れた立方体の外接箱8頂点がビューポート(${JSON.stringify(result.viewport)})に` +
          `収まっていない(初回フレーミングの機会が隠したBodyの空振りで消費された疑い): ` +
          `${JSON.stringify(result.outside)}`,
      )
    }
    ok('離れた立方体の外接箱8頂点が全てビューポート内に投影される(空振りに機会を奪われていない)')

    await assertNoErrors(page, consoleErrors)
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

async function main() {
  const executablePath = findBrowser()
  await checkImportReframes(executablePath)
  await checkFramedFlagNotWastedByHiddenBody(executablePath)
  console.log('\nsmoke-reframe-on-import: 全項目 OK(A・Bとも)')
}

main().catch((error) => {
  console.error(`\nsmoke-reframe-on-import: 失敗 — ${error.message}`)
  process.exitCode = 1
})
