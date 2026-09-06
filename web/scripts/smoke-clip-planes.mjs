/**
 * 実機での確認 — **near/farが初回フレーミングから切り離され、内容が変わるたびに追随する**。
 *
 *   node scripts/smoke-clip-planes.mjs [--step <STEPのパス>] [--port 8957] [--http-port 8958]
 *
 * 2つの独立した回帰を見る(遠端=far側・近端=near側は互いに鏡像の欠陥なので、別々の
 * 姿勢でないと検査できない。片方の姿勢では他方が必ず「対象外」になる)。
 *
 *   A. **遠端(far)**: 小さな部品で初回フレーミングを消費させた後、大きなSTEPを開くと、
 *      farが小さな部品の対角で固定されたままモデルの端が切り落とされる
 *      (実測。500×366×142mm・対角約640mmのSTEPで再現)。
 *   B. **近端(near)**: 大きな形で初回フレーミングを消費させた後、内容が小さくなって
 *      カメラが外へ出ると、nearが大きな形の対角で固定されたままになる
 *      (`frameToContent()` が near/far を「最初に届いた形の大きさ」で決め、`framed`
 *      フラグにより初回しか走らないため)。
 *
 * どちらも `__duocad.cameraPose()` の `near`/`far` を読み、**文書全体の外接球**が
 * `[near, far]` の内側に完全に収まっていることを数値で確かめる。「モデルの端が消える」は
 * 目でしか判定しづらいので、代わりに**外接球の中心からカメラまでの距離 ± 半径**が
 * `[near, far]` に収まるかを見る(球はboxそのものより大きいので安全側の判定)。
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
const stepPath =
  options.step ?? 'C:\\Users\\tm976\\mf\\hanger_bar_attatchment.step'
const wsPort = options.port ?? '8957'
const httpPort = options['http-port'] ?? '8958'
const timeout = Number.parseInt(options.timeout ?? '120000', 10)

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/** 現在の画面から、文書全体の外接直方体とカメラ姿勢をまとめて読む。 */
async function probeDocument(page) {
  return page.evaluate(() => {
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
}

/** 外接直方体(min/max)から中心・対角・半径を出す。 */
function sphereOf(min, max) {
  const center = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2]
  const size = [max[0] - min[0], max[1] - min[1], max[2] - min[2]]
  const diagonal = Math.hypot(size[0], size[1], size[2])
  return { center, diagonal, radius: diagonal / 2 }
}

// ====================================================================== A. 遠端(far)
/**
 * 小さな板→大きなSTEPの順で、farが追い越されないことを見る。
 */
async function checkFarSideRegression(executablePath) {
  console.log(`\n[A] 遠端(far): 小さな板 → 大きなSTEP(${stepPath})`)
  if (!existsSync(stepPath)) fail(`STEPが無い: ${stepPath}`)
  const workdir = mkdtempSync(join(tmpdir(), 'duocad-clip-planes-far-'))
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null
  try {
    await mcp.handshake('duocad-smoke-clip-planes-far')

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
    const probe = await probeDocument(page)
    if (probe.min === null) fail('文書全体の外接直方体が取れない(ボディのgeometryが無い)')

    const { center, diagonal, radius } = sphereOf(probe.min, probe.max)
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

    // **近端はこの姿勢では検査できない**(カメラは外接球の内側にいる=nearEdge<0が
    // ほぼ確実。ここで「奥にある」と無条件に言うと、検査していないのに検査したと
    // 報告することになる。近端は checkNearSideRegression が見る)。
    const nearEdge = distance - radius
    if (nearEdge > 0) {
      if (!(near <= nearEdge)) {
        fail(
          `外接球の近端がnearより手前にある(切り落とされる): 近端=${nearEdge.toFixed(3)}mm ` +
            `near=${near.toFixed(5)}mm`,
        )
      }
      ok(`外接球の近端(${nearEdge.toFixed(3)}mm)はnear(${near.toFixed(5)}mm)より奥`)
    } else {
      ok(
        `近端は検査対象外(カメラが外接球の内側にいる: 中心まで${distance.toFixed(1)}mm ` +
          `< 半径${radius.toFixed(1)}mm)。近端の検査は checkNearSideRegression が別姿勢で行う`,
      )
    }

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
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

// ====================================================================== B. 近端(near)
/**
 * 巨大な板→小さな立方体の順で、nearが追い越されないことを見る(遠端側の鏡像)。
 *
 * **姿勢の作り方**: 40000×40000×2mmの巨大な薄板と2×2×2mmの極小立方体を、
 * **同じ中心点**(20000,20000,1)に重ねて先に両方作ってからブラウザを開く
 * (両方が最初の状態配信に一緒に乗るので、初回フレーミングは合体の外接直方体
 * =巨大な板が支配する対角で1回だけ決まる)。そのあと `undo()` で巨大な板だけを
 * 消す(操作ログの最後に積んだのが巨大な板なので、1回のundoで消える)。
 * 残るのは極小立方体だけだが、**中心点は同じ**なので、そこへ実際のホイールで
 * 寄れば、外接球(=極小立方体)の外側にいながら近くまで迫れる
 * (`window.__duocad.frameAll()` は使わない — 呼ぶと視錐台も作り直され、
 * 「初回フレーミング以降どのイベントで引き直るか」という検査したいことそのものを
 * 迂回してしまう)。
 */
async function checkNearSideRegression(executablePath) {
  console.log('\n[B] 近端(near): 巨大な板(初回フレーミングを消費) → 極小の立方体だけが残る')
  const workdir = mkdtempSync(join(tmpdir(), 'duocad-clip-planes-near-'))
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null
  try {
    await mcp.handshake('duocad-smoke-clip-planes-near')

    const CENTER = [20000, 20000, 1]
    const SMALL_HALF = 1
    const BIG_HALF = 20000

    // ---------------------------------------------------------------- 1. 極小立方体(先に作る=undoスタックの下)
    const sketchSmall = await mcp.callTool('sketch_create', { base_plane: 'XY' })
    await mcp.callTool('add_rectangle', {
      sketch_id: sketchSmall.sketch_id,
      corner: [CENTER[0] - SMALL_HALF, CENTER[1] - SMALL_HALF],
      width: SMALL_HALF * 2,
      height: SMALL_HALF * 2,
    })
    await mcp.callTool('propose_selection', {
      refs: [{ sketch: sketchSmall.sketch_id, index: 1 }],
    })
    await mcp.callTool('commit_selection')
    const smallExtrude = await mcp.callTool('extrude', { distance: SMALL_HALF * 2, mode: 'new' })
    await mcp.callTool('clear_selection')
    const smallBodyId = smallExtrude.bodies[0].body_id
    ok(`極小立方体を作った: ${smallBodyId}(2×2×2mm、中心(${CENTER.join(',')}))`)

    // ---------------------------------------------------------------- 2. 巨大な板(後で作る=undoスタックの上)
    const sketchBig = await mcp.callTool('sketch_create', { base_plane: 'XY' })
    await mcp.callTool('add_rectangle', {
      sketch_id: sketchBig.sketch_id,
      corner: [CENTER[0] - BIG_HALF, CENTER[1] - BIG_HALF],
      width: BIG_HALF * 2,
      height: BIG_HALF * 2,
    })
    await mcp.callTool('propose_selection', {
      refs: [{ sketch: sketchBig.sketch_id, index: 1 }],
    })
    await mcp.callTool('commit_selection')
    const bigExtrude = await mcp.callTool('extrude', { distance: SMALL_HALF * 2, mode: 'new' })
    await mcp.callTool('clear_selection')
    const bigBodyId = bigExtrude.bodies[0].body_id
    const bigDiagonal = Math.hypot(BIG_HALF * 2, BIG_HALF * 2, SMALL_HALF * 2)
    ok(
      `巨大な板を作った: ${bigBodyId}(${BIG_HALF * 2}×${BIG_HALF * 2}×${SMALL_HALF * 2}mm、` +
        `対角約${bigDiagonal.toFixed(0)}mm、同じ中心)`,
    )

    // ---------------------------------------------------------------- 3. ブラウザを開く(合体=巨大な板が支配する対角で初回フレーミングを消費)
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
    await page.waitForFunction(
      (count) => (window.__duocad?.state?.bodies ?? []).length >= count,
      { timeout, polling: 100 },
      2,
    )
    await sleep(200)

    const framedPose = await page.evaluate(() => window.__duocad.cameraPose())
    ok(
      `合体(巨大な板+極小立方体)の視錐台: near=${framedPose.near.toFixed(4)}mm / ` +
        `far=${framedPose.far.toFixed(1)}mm(巨大な板の対角が支配している)`,
    )

    // ---------------------------------------------------------------- 4. undoで巨大な板だけを消す
    // **`framed` は既にtrueなので、この内容変化でも旧コードはnear/farを引き直さない**
    // (`checkFarSideRegression` と対称の欠陥)。新コードは `prune()` 直後の
    // `updateClipPlanes()` で引き直す。
    await mcp.callTool('undo')
    await page.waitForFunction(
      (id) => !(window.__duocad?.state?.bodies ?? []).some((body) => body.body_id === id),
      { timeout, polling: 100 },
      bigBodyId,
    )
    const stillHasSmall = await page.evaluate(
      (id) => (window.__duocad?.state?.bodies ?? []).some((body) => body.body_id === id),
      smallBodyId,
    )
    if (!stillHasSmall) {
      fail('undoが極小立方体まで消してしまった(undo単位の想定がずれている)')
    }
    ok('undoで巨大な板だけを消した(極小立方体は残っている)')

    // ---------------------------------------------------------------- 5. 実カメラで極小立方体の外側・近くまで寄る
    // **`frameAll()` は使わない** —— 呼ぶと視錐台も引き直されてしまい、
    // 「内容が変わっただけでは引き直らない旧コード」を再現できなくなる。
    // 本物のホイールでカメラを注視点(=極小立方体と同じ中心)へ寄せる。
    // **ホイールはカーソル位置で発火する。** マウスを動かさないままだと
    // `page.mouse.wheel()` は既定位置(左上)へ飛ばし、`.viewport` の外に出て
    // OrbitControlsのリスナーに1件も届かない(実測: 400回ズームしても
    // 距離が1mmも動かなかった)。他の実機smoke(`smoke-resolution.mjs` 等)と同じく、
    // 先にビューポート中央へカーソルを置く。
    const center = await page.evaluate(() => {
      const box = document.querySelector('.viewport').getBoundingClientRect()
      return { x: box.left + box.width / 2, y: box.top + box.height / 2 }
    })
    await page.mouse.move(center.x, center.y)

    const smallSphere = { center: CENTER, radius: Math.hypot(SMALL_HALF, SMALL_HALF, SMALL_HALF) }
    // 目標窓: (半径, 巨大な板からの凍結near) ≈ (1.7mm, 56.6mm)。中央寄りの30mmを狙う。
    const TARGET_DISTANCE = 30
    const MAX_NOTCHES = 400
    let lastDistance = Infinity
    for (let i = 0; i < MAX_NOTCHES; i += 1) {
      const pose = await page.evaluate(() => window.__duocad.cameraPose())
      const [px, py, pz] = pose.position
      lastDistance = Math.hypot(
        px - smallSphere.center[0],
        py - smallSphere.center[1],
        pz - smallSphere.center[2],
      )
      if (lastDistance <= TARGET_DISTANCE) break
      await page.mouse.wheel({ deltaY: -480 })
      await sleep(60)
    }
    if (lastDistance > TARGET_DISTANCE) {
      fail(
        `${MAX_NOTCHES}回ズームしても極小立方体まで${TARGET_DISTANCE}mmへ寄れない` +
          `(最後の距離 ${lastDistance.toFixed(1)}mm)`,
      )
    }
    ok(`実際のホイール操作で極小立方体の中心まで ${lastDistance.toFixed(2)}mm まで寄った`)

    // ---------------------------------------------------------------- 6. 判定
    const probe = await probeDocument(page)
    if (probe.min === null) fail('文書全体の外接直方体が取れない(ボディのgeometryが無い)')
    if (probe.bodyCount !== 1) fail(`残っているボディが1個でない: ${probe.bodyCount}`)

    const { radius } = sphereOf(probe.min, probe.max)
    const [px, py, pz] = probe.pose.position
    const distance = Math.hypot(
      px - smallSphere.center[0],
      py - smallSphere.center[1],
      pz - smallSphere.center[2],
    )
    const { near, far } = probe.pose
    const nearEdge = distance - radius

    ok(
      `寄った後: 極小立方体(半径${radius.toFixed(3)}mm)の中心まで${distance.toFixed(2)}mm / ` +
        `near=${near.toFixed(5)}mm far=${far.toFixed(1)}mm`,
    )

    // **この検査が空振りでないことの確認**。nearEdge<=0だと遠端側と同じ理由で
    // 検査が対象外になり、何も見ないまま緑になる。
    if (!(nearEdge > 0)) {
      fail(
        `想定した姿勢(外接球の外側)を作れていない: 近端=${nearEdge.toFixed(3)}mm ` +
          `(カメラが球の内側にいる。TARGET_DISTANCEを見直すこと)`,
      )
    }
    ok(`外接球の近端(${nearEdge.toFixed(3)}mm)は0より大きい(検査対象になっている)`)

    if (!(near <= nearEdge)) {
      fail(
        `外接球の近端がnearより手前にある(切り落とされる): 近端=${nearEdge.toFixed(3)}mm ` +
          `near=${near.toFixed(5)}mm(巨大な板の初回フレーミングにnearが固定されたままの疑い)`,
      )
    }
    ok(`外接球の近端(${nearEdge.toFixed(3)}mm)はnear(${near.toFixed(5)}mm)より奥`)

    // **旧コードなら確実に落ちること**を確かめる —— 巨大な板だけのnear(約56.6mm)を
    // このnearEdge(30mm前後)に当てると、必ず追い越されるはず(空振りでないことの確認)。
    if (near >= framedPose.near * 0.5) {
      fail(
        `nearが巨大な板のときの値からほとんど動いていない(視錐台が追随していない疑い): ` +
          `板だけ near=${framedPose.near.toFixed(3)}mm -> 立方体後 near=${near.toFixed(5)}mm`,
      )
    }
    ok(`nearは巨大な板の値(${framedPose.near.toFixed(3)}mm)から実際に縮んだ(${near.toFixed(5)}mm)`)

    const errors = await page.evaluate(() => window.__duocad.errors ?? [])
    if (errors.length > 0) fail(`走行中にエラーが出た: ${JSON.stringify(errors)}`)
    if (consoleErrors.length > 0) fail(`コンソールにエラー: ${consoleErrors.join(' / ')}`)
    ok('走行中にエラーは1件も出ていない')
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

async function main() {
  const executablePath = findBrowser()
  await checkFarSideRegression(executablePath)
  await checkNearSideRegression(executablePath)
  console.log('\nsmoke-clip-planes: 全項目 OK(遠端・近端とも)')
}

main().catch((error) => {
  console.error(`\nsmoke-clip-planes: 失敗 — ${error.message}`)
  process.exitCode = 1
})
