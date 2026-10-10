/**
 * 実機での確認 — ズームに応じて分割数が増減する(可変解像度)。
 *
 *   node scripts/smoke-resolution.mjs
 *
 * 「寄っても多角形の角が出ない」を、分割数と目標偏差の数値で見る。見るのは3経路:
 *
 *   1. スケッチの円 …… ブラウザ内で折る(`entityKinds.ts` + `adaptiveResolution.ts`)
 *   2. 通常のソリッド … サーバの `BRepMesh_IncrementalMesh`(`kernel.tessellate`)
 *   3. 辺オーバーレイ … サーバの `GCPnts_TangentialDeflection`(`kernel.tessellate_edges`)
 *
 * 本物のホイールで寄る/引く → 落ち着くのを待つ → 3経路の分割数を読む。サーバへ何を送るかは見ず、
 * 画面に実際に届いた形だけを見る(プロトコルの契約は `tests/test_adaptive_resolution.py`)。
 * サーバは空の文書で起動し、円柱はMCP経由で作る。
 */
import { assertNoAppErrors, frameAll, openApp, sleep, viewportCenter } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedCircle } from './mcp-client.mjs'

const RADIUS = 10
const HEIGHT = 20

runSmoke('smoke-resolution', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  {
    const { bodyId } = await addExtrudedCircle(mcp, { center: [0, 0], radius: RADIUS, distance: HEIGHT })
    ok(`半径${RADIUS}mmの円柱を作った: ${bodyId}(曲面1・円い辺2・スケッチの円1)`)

    const { page, consoleErrors } = await openApp(browser, url, { timeout })
    await frameAll(page)

    // 辺オーバーレイを出す(フィレットを起動している間だけ辺が届く契約)。
    await page.click('#fillet')

    const center = await viewportCenter(page)
    await page.mouse.move(center.x, center.y)

    /** 今の画面から、判定に要る値をまとめて読む。 */
    function probe(id) {
      return page.evaluate((bodyId) => {
        const hook = window.__duocad
        const resolution = hook.viewResolution?.() ?? null
        const geometry = hook.getGeometry(bodyId)
        const body = (hook.state?.bodies ?? []).find((item) => item.body_id === bodyId) ?? null
        const edges = hook.edges
        const circles = (edges?.edges ?? []).filter((edge) => edge.kind === 'circle')
        const counts = Object.values(hook.sketchPolylineCounts?.() ?? {}).flat()
        return {
          resolution,
          deviation: geometry?.deviation ?? null,
          triangles: geometry
            ? geometry.faces.reduce((sum, face) => sum + face.indices.length / 3, 0)
            : 0,
          edgePoints: circles.length ? Math.max(...circles.map((e) => e.points.length / 3)) : 0,
          edgeCount: circles.length,
          edgeRevision: edges?.revision ?? null,
          bodyRevision: body?.revision ?? null,
          sketchPoints: counts.length ? Math.max(...counts) : 0,
        }
      }, id)
    }

    /**
     * 画面が落ち着くまで待ってから3経路の分割数を読む。「値が既定(0.2mm)と一致したら終わり」にはできない:
     * 解像度を採る前の既定値とサーバが最初に配った粗さは同じで、ズーム前後どちらでも即座に真になり、
     * 検査が何も見ないまま緑になる(実際に一度そうなった)。次の3つが同時に満たされるまで待つ:
     *   (a) ビューポートが解像度を実際に採っている(`worldPerPixel > 0`)
     *   (b) 採った値がサーバから届いた `geometry.deviation` と一致している
     *   (c) 辺が今の版のものである
     * さらに (a) の値が3回続けて同じであること(減衰つきのドリーはホイールを止めた後も動き続ける)。
     */
    async function measure(label) {
      const deadline = Date.now() + timeout
      let previous = null
      let stable = 0
      let seen = null
      for (;;) {
        seen = await probe(bodyId)
        const resolution = seen.resolution
        const settled =
          resolution !== null &&
          resolution.worldPerPixel > 0 &&
          seen.deviation !== null &&
          Math.abs(seen.deviation - resolution.deviation) <= resolution.deviation * 1e-6 &&
          seen.edgeCount > 0 &&
          seen.edgeRevision !== null &&
          seen.edgeRevision === seen.bodyRevision
        stable = settled && previous === resolution.worldPerPixel ? stable + 1 : 0
        previous = resolution?.worldPerPixel ?? null
        if (stable >= 2) break
        if (Date.now() > deadline) {
          fail(`${label}: 画面が落ち着かない(最後に見た値: ${JSON.stringify(seen)})`)
        }
        await sleep(150)
      }
      ok(
        `${label}: 1画素=${seen.resolution.worldPerPixel.toFixed(5)}mm / ` +
          `目標偏差 ${seen.deviation.toFixed(5)}mm・角度偏差 ${seen.resolution.angularDeflection.toFixed(4)}rad / ` +
          `三角形 ${seen.triangles} / 円い辺 ${seen.edgePoints}点 / スケッチの円 ${seen.sketchPoints}点`,
      )
      return seen
    }

    /** 本物のホイールで寄る(引く)。 */
    async function wheel(deltaY, times) {
      for (let i = 0; i < times; i += 1) {
        await page.mouse.wheel({ deltaY })
        await sleep(60)
      }
    }

    const base = await measure('既定の視点(frameAll 直後)')
    if (base.edgeCount < 2) fail(`円柱の円い辺が2本届いていない: ${base.edgeCount}`)

    // ---------------------------------------------------------------- 寄る
    await wheel(-240, 12)
    const near = await measure('寄った状態')

    if (!(near.deviation < base.deviation)) {
      fail(`寄ったのに目標偏差が細かくならない: ${base.deviation} -> ${near.deviation}`)
    }
    if (!(near.triangles > base.triangles)) {
      fail(`寄ったのにソリッドの三角形が増えない: ${base.triangles} -> ${near.triangles}`)
    }
    if (!(near.edgePoints > base.edgePoints)) {
      fail(`寄ったのに辺の分割点が増えない: ${base.edgePoints} -> ${near.edgePoints}`)
    }
    if (!(near.sketchPoints > base.sketchPoints)) {
      fail(`寄ったのにスケッチの円の分割点が増えない: ${base.sketchPoints} -> ${near.sketchPoints}`)
    }
    ok(
      '**寄ったら3経路すべてで分割数が増えた**' +
        `(三角形 ${base.triangles}->${near.triangles} / 辺 ${base.edgePoints}->${near.edgePoints}点 / ` +
        `スケッチ ${base.sketchPoints}->${near.sketchPoints}点)`,
    )

    // 画面上の弦の誤差で見る(「角が見えるか」に対応する量)。
    const nearPixelError = near.deviation / near.resolution.worldPerPixel
    if (!(nearPixelError <= 1)) {
      fail(`寄った状態で弦の誤差が1画素を超えている: ${nearPixelError.toFixed(3)}px`)
    }
    ok(`寄っても弦と弧の乖離は ${nearPixelError.toFixed(3)}画素(1画素以下 = 角として見えない)`)

    // ---------------------------------------------------------------- 引く
    await wheel(360, 20)
    const far = await measure('引いた状態')

    if (!(far.deviation > base.deviation)) {
      fail(`引いたのに目標偏差が粗くならない: ${base.deviation} -> ${far.deviation}`)
    }
    if (!(far.triangles < base.triangles)) {
      fail(`引いたのにソリッドの三角形が減らない: ${base.triangles} -> ${far.triangles}`)
    }
    if (!(far.edgePoints < base.edgePoints)) {
      fail(`引いたのに辺の分割点が減らない: ${base.edgePoints} -> ${far.edgePoints}`)
    }
    if (!(far.sketchPoints < base.sketchPoints)) {
      fail(`引いたのにスケッチの円の分割点が減らない: ${base.sketchPoints} -> ${far.sketchPoints}`)
    }
    ok(
      '**引いたら3経路すべてで分割数が減った**' +
        `(三角形 ${base.triangles}->${far.triangles} / 辺 ${base.edgePoints}->${far.edgePoints}点 / ` +
        `スケッチ ${base.sketchPoints}->${far.sketchPoints}点)`,
    )

    // ---------------------------------------------------------------- 壊していないこと
    // 辺は**今の版のもの**でなければならない(古い添字の辺を指すと別の辺が丸まる)。
    const stale = await page.evaluate((id) => {
      const hook = window.__duocad
      const body = (hook.state?.bodies ?? []).find((item) => item.body_id === id)
      return { known: hook.edges?.revision ?? null, current: body?.revision ?? null }
    }, bodyId)
    if (stale.known !== stale.current) {
      fail(`ズームの後に古い版の辺が残っている: 辺=${stale.known} / Body=${stale.current}`)
    }
    ok(`ズームの後も辺は今の版(revision ${stale.current})—— 古い添字を指したままにならない`)

    await assertNoAppErrors(page, consoleErrors)
  }
})
