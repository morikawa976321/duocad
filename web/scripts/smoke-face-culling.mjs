/**
 * 実機での確認 — face単位の視錐台+バックフェイスカリングが、深いズームを実際に軽くする。
 *
 *   node scripts/smoke-face-culling.mjs [--step <STEPのパス>]
 *
 * 大きなアセンブリで「寄っても重くならない」を、三角形の総数と測り直しの往復時間で見る。
 * 同じ走行の中で入り/切りを比べる(検査用の穴 `__duocad.setFaceCulling(enabled)`。
 * 切ると `focus` を送らなくなり、サーバは文書全体で1つの精度に戻る)。
 *
 *   1. 深いズームで、入りの三角形総数が切りより大幅に少ない
 *   2. 深いズームで、入りの測り直しが切りより速い
 *   3. 全体表示では悪化しない
 *   4. 走行中にエラーが出ない
 *
 * サーバはこのスクリプトが空の文書で起動し、STEPはMCPの `import_step` で読む。
 */
import { existsSync } from 'node:fs'
import { assertNoAppErrors, frameAll, openApp, sleep, viewportCenter } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { PC_CASE_STEP, PC_CASE_SOLIDS } from './models.mjs'

const POLL_MS = 100
/** 画面が新しい要求を出すまで待てる時間[ms]。**この検査の全ての手は要求を変える**ので、変わらなければ検査側の間違い。 */
const REQUEST_MS = 20000
/**
 * 「届いた形が要求と一致している」がこの時間続いたら収束と見なす[ms]。届いた形で面の見本が変わると
 * 画面は次のフレームで `focus` を採り直すので、採り直しが起きるならこの間に要求が変わって不一致へ戻る。
 */
const STABLE_MS = 1000
runSmoke('smoke-face-culling', { timeout: 600000, mcp: true }, async ({ options, mcp, browser, url, timeout }) => {
  const stepPath = options.step ?? PC_CASE_STEP
  const expectedBodies = Number.parseInt(options['expect-bodies'] ?? String(PC_CASE_SOLIDS), 10)
  /** 深いズームの段数(ホイールの刻み)。engineの実測(30段)に合わせてある。 */
  const ZOOM_STEPS = Number.parseInt(options['zoom-steps'] ?? '30', 10)
  if (!existsSync(stepPath)) fail(`STEPが無い: ${stepPath}`)
  console.log(`smoke-face-culling: ${stepPath}`)
  {
    const imported = await mcp.callTool('import_step', { path: stepPath })
    if (imported.body_ids.length !== expectedBodies) {
      fail(`ボディが ${expectedBodies} 個でない: ${imported.body_ids.length}`)
    }
    ok(`STEPを読んだ: ${imported.body_ids.length}部品`)

    const { page, consoleErrors } = await openApp(browser, url, { timeout })
    await frameAll(page)

    const center = await viewportCenter(page)
    await page.mouse.move(center.x, center.y)

    /** いま画面が持っている形を数える。`mismatched` は届いた形が現在の要求(`viewResolution()` の `focus` と目標偏差)と食い違う Body の数。 */
    function probe() {
      return page.evaluate(() => {
        const hook = window.__duocad
        const resolution = hook.viewResolution?.() ?? null
        const focus = resolution?.focus ?? null
        let triangles = 0
        let fineFaces = 0
        let faces = 0
        let culledBodies = 0
        let mismatched = 0
        for (const part of hook.parts ?? []) {
          const geometry = hook.getGeometry(part.body_id)
          if (!geometry) continue
          for (const face of geometry.faces) triangles += face.indices.length / 3
          const fine = geometry.fine_face_count ?? geometry.face_count
          faces += geometry.face_count
          fineFaces += fine
          if (fine === 0) culledBodies += 1
          const wanted = focus === null ? null : (focus[part.body_id] ?? null)
          const wantedFine = wanted === null ? geometry.face_count : wanted.length
          const deviationOff =
            resolution !== null &&
            wantedFine > 0 &&
            Math.abs(geometry.deviation - resolution.deviation) > resolution.deviation * 1e-6
          if (fine !== wantedFine || deviationOff) mismatched += 1
        }
        return {
          request: JSON.stringify([resolution?.worldPerPixel ?? 0, resolution?.deviation ?? 0, focus]),
          worldPerPixel: resolution?.worldPerPixel ?? 0,
          deviation: resolution?.deviation ?? 0,
          triangles,
          faces,
          fineFaces,
          culledBodies,
          mismatched,
        }
      })
    }

    /**
     * 測り直しが収束するまで待ち、届いた形が要求と一致するまでの経過[ms]を返す(`requestBefore` は手を打つ前の要求)。
     * 「状態配信が一定時間止まったら終わり」では判定しない: サーバは単一のイベントループで、
     * 27部品の測り直しの間は WebSocket に何も流れず、静けさで切ると返事が届く前の画面を読む
     * (実測: 2秒の静けさで切ると読み値が1手ずつずれた)。MCP の道具呼び出しを柵にしても同じ。
     */
    async function waitQuiet(label, startedAt, requestBefore) {
      const deadline = Date.now() + timeout
      let seen = await probe()
      while (seen.worldPerPixel === 0 || seen.request === requestBefore) {
        if (Date.now() - startedAt > REQUEST_MS) fail(`${label}: 画面の要求が変わらない(手が効いていない)`)
        await sleep(POLL_MS)
        seen = await probe()
      }
      let matchedSince = null
      for (;;) {
        seen = await probe()
        if (seen.mismatched > 0) matchedSince = null
        else if (matchedSince === null) matchedSince = Date.now()
        else if (Date.now() - matchedSince >= STABLE_MS) break
        if (Date.now() > deadline) fail(`${label}: 収束しない(${JSON.stringify({ ...seen, request: undefined })})`)
        await sleep(POLL_MS)
      }
      const elapsed = matchedSince - startedAt
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
    const farOn = await waitQuiet('全体表示・カリング入り', Date.now(), null)
    const farOffAt = await setCulling(false)
    const farOff = await waitQuiet('全体表示・カリング切り', farOffAt, farOn.request)
    const farBackAt = await setCulling(true)
    const farBack = await waitQuiet('全体表示・カリング入りへ戻す', farBackAt, farOff.request)

    // ---------------------------------------------------------------- 深いズーム
    await wheel(-240, ZOOM_STEPS)
    const nearOn = await waitQuiet(`${ZOOM_STEPS}段ズーム・カリング入り`, Date.now(), farBack.request)

    const nearOffAt = await setCulling(false)
    const nearOff = await waitQuiet(`${ZOOM_STEPS}段ズーム・カリング切り`, nearOffAt, nearOn.request)

    const backOnAt = await setCulling(true)
    const nearOn2 = await waitQuiet(`${ZOOM_STEPS}段ズーム・カリング入りへ戻す`, backOnAt, nearOff.request)
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

    if (!(farOn.triangles <= farOff.triangles * 1.05)) {
      fail(`全体表示が重くなっている: 入り ${farOn.triangles} / 切り ${farOff.triangles}`)
    }
    ok(
      `全体表示は悪化していない(入り ${farOn.triangles.toLocaleString()} / ` +
        `切り ${farOff.triangles.toLocaleString()} 三角形)`,
    )

    await assertNoAppErrors(page, consoleErrors)
  }
})
