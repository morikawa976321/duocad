/**
 * 実機での確認 — **face単位の視錐台+バックフェイスカリングが、深いズームを実際に軽くする**。
 *
 *   node scripts/smoke-face-culling.mjs [--step <STEPのパス>] [--port 8967] [--http-port 8968]
 *
 * `smoke-resolution.mjs`(円柱1個で「寄ったら細かくなる」を見る)の続きで、こちらは
 * **大きなアセンブリで「寄っても重くならない」**を見る。判定に使うのは
 * **三角形の総数**と**測り直しの往復時間**の2つだけで、どちらも数値で出る。
 *
 * **同じ走行の中で入り/切りを比べる。** 版を戻して測り直すのでは機械が後から回せる証拠に
 * ならないので、ビューポートに検査用の穴 `__duocad.setFaceCulling(enabled)` を開けてある
 * (切ると `focus` を送らなくなり、サーバは2026-08-29時点の「文書全体で1つの精度」と
 * 同じ振る舞いに戻る)。
 *
 * 見るのは4点:
 *   1. 深いズームで、入りの三角形総数が切りより**大幅に少ない**
 *   2. 深いズームで、入りの測り直しが切りより**速い**
 *   3. **全体表示では悪化しない**(ほぼ全面が視錐台に入るので、入りでも切りでも同じ)
 *   4. 走行中にエラーが出ない(=粗い面が混ざっても描画経路が壊れていない)
 *
 * サーバはこのスクリプトが空の文書で起動し、STEPはMCPの `import_step` で読む。
 */
import { existsSync } from 'node:fs'
import { findBrowser, launchBrowser, frameAll } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'
import { PC_CASE_STEP, PC_CASE_SOLIDS } from './models.mjs'

const options = parseArgs(process.argv.slice(2))
const stepPath = options.step ?? PC_CASE_STEP
const wsPort = options.port ?? '8967'
const httpPort = options['http-port'] ?? '8968'
const timeout = Number.parseInt(options.timeout ?? '600000', 10)
const expectedBodies = Number.parseInt(options['expect-bodies'] ?? String(PC_CASE_SOLIDS), 10)
/** 深いズームの段数(ホイールの刻み)。engineの実測(30段)に合わせてある。 */
const ZOOM_STEPS = Number.parseInt(options['zoom-steps'] ?? '30', 10)
const POLL_MS = 100
/**
 * 状態配信が**この時間だけ止まったら**往復が終わったと見なす[ms]。
 *
 * **画面の値が変わらないこと」を落ち着きの条件にしてはいけない。** サーバは単一の
 * イベントループで、27部品の測り直しの間はWebSocketに何も流れない —— 「変わっていない」で
 * 切ると**要求が出ていく前の画面**を落ち着きと読み、入りと切りが同じ値に見える
 * (2026-08-30に実際にそう読んだ)。したがって見るのは `__duocad.stateEpoch`
 * (状態が1通届くたびに増える)であって、画面の値ではない。
 */
const QUIET_MS = 2000
/**
 * 状態が1通も来ないまま待てる時間[ms]。**この検査の全ての手は必ず1通を起こす**ので、
 * 来なければ検査側の間違いである(黙って0秒と記録しない)。
 *
 * **短くすると測りたいものだけを取り逃がす。** カリングを切った深いズームは、まさに
 * 「サーバが数十秒黙る」場合であり、6秒で打ち切ったときは**その静けさを『何も起きて
 * いない』と読んで往復0秒と記録した**(2026-08-30に実測)。
 */
const NO_CHANGE_MS = 120000

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function main() {
  const executablePath = findBrowser()
  if (!existsSync(stepPath)) fail(`STEPが無い: ${stepPath}`)
  console.log(`smoke-face-culling: ${stepPath}`)

  const server = await startServer({ wsPort, httpPort })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null
  try {
    await mcp.handshake('duocad-smoke-face-culling')
    const imported = await mcp.callTool('import_step', { path: stepPath })
    if (imported.body_ids.length !== expectedBodies) {
      fail(`ボディが ${expectedBodies} 個でない: ${imported.body_ids.length}`)
    }
    ok(`STEPを読んだ: ${imported.body_ids.length}部品`)

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
    await frameAll(page)

    const center = await page.evaluate(() => {
      const box = document.querySelector('.viewport').getBoundingClientRect()
      return { x: box.left + box.width / 2, y: box.top + box.height / 2 }
    })
    await page.mouse.move(center.x, center.y)

    /** いま画面が持っている形を丸ごと数える(**GPUに載っている量そのもの**)。 */
    function probe() {
      return page.evaluate(() => {
        const hook = window.__duocad
        const resolution = hook.viewResolution?.() ?? null
        let triangles = 0
        let fineFaces = 0
        let faces = 0
        let culledBodies = 0
        for (const part of hook.parts ?? []) {
          const geometry = hook.getGeometry(part.body_id)
          if (!geometry) continue
          for (const face of geometry.faces) triangles += face.indices.length / 3
          faces += geometry.face_count
          fineFaces += geometry.fine_face_count ?? geometry.face_count
          if ((geometry.fine_face_count ?? geometry.face_count) === 0) culledBodies += 1
        }
        const focus = resolution?.focus ?? null
        return {
          stateEpoch: hook.stateEpoch ?? 0,
          worldPerPixel: resolution?.worldPerPixel ?? 0,
          deviation: resolution?.deviation ?? 0,
          focusBodies: focus === null ? null : Object.keys(focus).length,
          triangles,
          faces,
          fineFaces,
          culledBodies,
        }
      })
    }

    /**
     * **測り直しの往復が終わるまで**待ち、最後の状態配信までの経過[ms]を返す。
     *
     * 落ち着きの判定は `stateEpoch`(状態が1通届くたびに増える)で行う。画面の値の
     * 変化では判定しない —— 上の `QUIET_MS` のコメントの理由による。
     */
    async function settle(label, startedAt) {
      const deadline = Date.now() + timeout
      const first = await probe()
      const baseEpoch = first.stateEpoch
      let lastEpoch = baseEpoch
      let lastChangeAt = startedAt
      let seen = first
      for (;;) {
        await sleep(POLL_MS)
        seen = await probe()
        if (seen.stateEpoch !== lastEpoch) {
          lastEpoch = seen.stateEpoch
          lastChangeAt = Date.now()
        }
        const quietFor = Date.now() - lastChangeAt
        if (lastEpoch !== baseEpoch && quietFor >= QUIET_MS) break
        if (lastEpoch === baseEpoch && Date.now() - startedAt >= NO_CHANGE_MS) {
          fail(`${label}: 状態が1通も届かない(要求が出ていない可能性)`)
        }
        if (Date.now() > deadline) fail(`${label}: 落ち着かない(${JSON.stringify(seen)})`)
      }
      const elapsed = lastChangeAt - startedAt
      ok(
        `${label}: 1画素=${seen.worldPerPixel.toFixed(5)}mm / 目標偏差 ${seen.deviation.toFixed(5)}mm / ` +
          `三角形 ${seen.triangles.toLocaleString()} / 細かく測った面 ${seen.fineFaces}/${seen.faces} / ` +
          `全面を粗くしたBody ${seen.culledBodies} / 往復 ${(elapsed / 1000).toFixed(1)}秒`,
      )
      return { ...seen, elapsed }
    }

    async function setCulling(enabled) {
      const startedAt = Date.now()
      await page.evaluate((value) => window.__duocad.setFaceCulling(value), enabled)
      return startedAt
    }

    async function wheel(deltaY, times) {
      for (let i = 0; i < times; i += 1) {
        await page.mouse.wheel({ deltaY })
        await sleep(50)
      }
    }

    // ---------------------------------------------------------------- 全体表示
    const farOn = await settle('全体表示・カリング入り', Date.now())
    const farOffAt = await setCulling(false)
    const farOff = await settle('全体表示・カリング切り', farOffAt)
    await setCulling(true)
    await settle('全体表示・カリング入りへ戻す', Date.now())

    // ---------------------------------------------------------------- 深いズーム
    let startedAt = Date.now()
    await wheel(-240, ZOOM_STEPS)
    startedAt = Date.now()
    const nearOn = await settle(`${ZOOM_STEPS}段ズーム・カリング入り`, startedAt)

    const nearOffAt = await setCulling(false)
    const nearOff = await settle(`${ZOOM_STEPS}段ズーム・カリング切り`, nearOffAt)

    const backOnAt = await setCulling(true)
    const nearOn2 = await settle(`${ZOOM_STEPS}段ズーム・カリング入りへ戻す`, backOnAt)

    // ---------------------------------------------------------------- 判定
    console.log('')
    if (!(nearOn.worldPerPixel < farOn.worldPerPixel / 4)) {
      fail(`寄れていない: ${farOn.worldPerPixel} -> ${nearOn.worldPerPixel}`)
    }
    if (!(nearOn.triangles * 2 < nearOff.triangles)) {
      fail(
        `深いズームで三角形が半分にもならない: 入り ${nearOn.triangles} / 切り ${nearOff.triangles}`,
      )
    }
    ok(
      `**深いズームの三角形が ${nearOff.triangles.toLocaleString()} -> ${nearOn.triangles.toLocaleString()}** ` +
        `(${(nearOff.triangles / Math.max(1, nearOn.triangles)).toFixed(1)}分の1)`,
    )

    if (!(nearOn2.elapsed < nearOff.elapsed)) {
      fail(
        `深いズームの測り直しが速くなっていない: 入り ${nearOn2.elapsed}ms / 切り ${nearOff.elapsed}ms`,
      )
    }
    ok(
      `**深いズームの測り直しが ${(nearOff.elapsed / 1000).toFixed(1)}秒 -> ${(nearOn2.elapsed / 1000).toFixed(1)}秒**`,
    )

    if (nearOn.culledBodies < 1) fail('深く寄っても粗くしたBodyが1つも無い(判定が効いていない)')
    ok(`深いズームで ${nearOn.culledBodies}/${expectedBodies} 部品が全面粗い側に落ちた`)

    // **全体表示を悪化させない**(タスクの完了条件のもう半分)。
    if (!(farOn.triangles <= farOff.triangles * 1.05)) {
      fail(`全体表示が重くなっている: 入り ${farOn.triangles} / 切り ${farOff.triangles}`)
    }
    ok(
      `全体表示は悪化していない(入り ${farOn.triangles.toLocaleString()} / ` +
        `切り ${farOff.triangles.toLocaleString()} 三角形)`,
    )

    const errors = await page.evaluate(() => window.__duocad.errors ?? [])
    if (errors.length > 0) fail(`走行中にエラーが出た: ${JSON.stringify(errors)}`)
    if (consoleErrors.length > 0) fail(`コンソールにエラー: ${consoleErrors.join(' / ')}`)
    ok('走行中にエラーは1件も出ていない')

    console.log('\nsmoke-face-culling: 全項目 OK')
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

main().catch((error) => {
  console.error(`\nsmoke-face-culling: 失敗 — ${error.message}`)
  process.exitCode = 1
})
