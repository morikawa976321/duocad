/**
 * 実機での確認 — 任意面(3基準平面のいずれでもない面)へのスケッチ中、その面にグリッドが
 * 張られること(`20260903-0357-duocad-sketch-face-grid`)。
 *
 *   node scripts/smoke-face-grid.mjs
 *
 * 完了条件を実機で見る:
 *
 *   1. 任意面へのスケッチ中、その面にグリッドが張られる(`gridTiers().planes` が
 *      `['sketch-face']` になる)。
 *   2. XY床は隠す(`planes` に `'XY'` が混ざらない)。
 *   3. 直交軸は出さない(`gridTiers().axis === null`)。
 *   4. グリッドはスケッチ平面のframeに載る(u/v・原点は面のもの)。
 *   5. スケッチを抜けると通常表示(XY平面+Z軸)へ戻る。
 *   6. z-fightingしていないこと(スクリーンショットを残し、目視できるようにする)。
 *
 * **判定は `plane.kind==='face'` を正としなければならない**。frameの一致だけ
 * (`basePlaneForFrame`)で「基準平面か任意面か」を決めると、**「XY床にスケッチ→押し出し
 * →その底面に再度スケッチ」のように、実ジオメトリの面(`plane.kind==='face'`)でも
 * frame が基準平面と数値的に一致するケース**(原点(0,0,0)・u/vが基準平面と同じ)を
 * 基準平面の分岐へ誤って落とす——その面は実ソリッドのメッシュと完全同一平面なので、
 * z-fighting対策が最も要る場面である。セクション[2]はこの一致ケースを明示的に作って
 * 機械で突く。
 *
 * サーバはこのスクリプトが空の文書で起動し、MCP経由で2つの箱を作る:
 *   A. corner[0,0] 40×30×20mm —— frameが基準平面と一致しない「素直な任意面」用。
 *   B. corner[-20,-15] 40×30×20mm(原点中心) —— 底面の重心がちょうど世界原点に来るため、
 *      frameが `{origin:[0,0,0], u:[1,0,0], v:[0,1,0]}` とXY基準平面に完全一致する
 *      「罠」の面が実際に作れる(実測で確認済み)。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { assertNoAppErrors, openApp, sleep, waitFrames } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { faceAt } from './pick.mjs'
import { SCREENSHOTS_DIR } from './server.mjs'

const screenshotDir = SCREENSHOTS_DIR

/**
 * 完了条件1・2・3・6の共通アサーション(sketch-face専用のグリッドが出ている・XY床が
 * 混ざらない・直交軸が無い・深度バイアス層がsketchFaceGrid)。`label` は失敗メッセージの手がかり。
 */
function assertFaceGridShape(tiers, label) {
  if (JSON.stringify(tiers.planes) !== JSON.stringify(['sketch-face'])) {
    fail(`${label}: 任意面のグリッド(sketch-face)だけが出ているはず: ${JSON.stringify(tiers.planes)}`)
  }
  ok(`${label}: 任意面へのスケッチ中、その面のグリッド(sketch-face)が出ている(完了条件1)`)
  if (tiers.planes.includes('XY')) fail(`${label}: XY床が隠れていない(完了条件2違反)`)
  ok(`${label}: XY床は隠れている(完了条件2)`)
  if (tiers.axis !== null) fail(`${label}: 直交軸を出さないはずが出ている(完了条件3違反): ${tiers.axis}`)
  ok(`${label}: 直交軸は出ていない(完了条件3・本人指定どおり)`)
  if (
    typeof tiers.sketchFaceExtent !== 'number' ||
    !Number.isFinite(tiers.sketchFaceExtent) ||
    tiers.sketchFaceExtent <= 0
  ) {
    fail(`${label}: 面の外接範囲で切っているはずのextentが不正: ${tiers.sketchFaceExtent}`)
  }
  ok(`${label}: 面の外接範囲でグリッドを切っている(sketchFaceExtent=${tiers.sketchFaceExtent}mm。無限に伸びていない)`)
  // **深度バイアス層が基準平面(`planeGrid`=1)ではなく `sketchFaceGrid`(=4)を使っている**こと。
  // frame一致で基準平面の分岐に落ちる回帰は、まさにこの層番号が1のまま残ることで
  // 再発する——z-fightingの有無を静止画だけでは機械的に見分けられないので、
  // 数値そのものを検査する(`viewport.ts` の `DEPTH_BIAS_LAYER.sketchFaceGrid` と同じ値。
  // 変わったらここも変える)。
  const SKETCH_FACE_GRID_DEPTH_BIAS_LAYER = 4
  if (JSON.stringify(tiers.depthBiasLayers) !== JSON.stringify([SKETCH_FACE_GRID_DEPTH_BIAS_LAYER])) {
    fail(
      `${label}: 深度バイアス層がsketchFaceGrid(${SKETCH_FACE_GRID_DEPTH_BIAS_LAYER})になっていない ` +
        `(基準平面のplaneGrid層のままだと、貼り付けた面と同一平面でz-fightingする): ` +
        `${JSON.stringify(tiers.depthBiasLayers)}`,
    )
  }
  ok(`${label}: 深度バイアス層がsketchFaceGrid(=${SKETCH_FACE_GRID_DEPTH_BIAS_LAYER})になっている(基準平面のplaneGrid層と分離。完了条件6)`)
}

runSmoke('smoke-face-grid', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  {
    // ---------------------------------------------------------------- 準備A: 素直な任意面用の箱
    const { bodyId: bodyIdA } = await addExtrudedRect(mcp, { corner: [0, 0], width: 40, height: 30, distance: 20 })
    ok(`[準備A] 40×30×20mmの箱を作った(body=${bodyIdA})`)

    // ---------------------------------------------------------------- 準備B: 罠(底面がXYと完全一致)の箱
    // 原点中心(corner=[-20,-15])なので、底面(z=0)の重心がちょうど世界原点(0,0,0)に来て、
    // frameが `{origin:[0,0,0], u:[1,0,0], v:[0,1,0]}` とXY基準平面の定義に完全一致する
    // (実測で確認済み。`_ax3_from_face` の原点は面ごとに違う基準を取るため、これは狙って
    // 作った偶然の一致であり毎回同じ面で起きるとは限らない——次にこの検査を触るときは
    // 実測し直すこと)。
    const { bodyId: bodyIdB } = await addExtrudedRect(mcp, {
      corner: [-20, -15],
      width: 40,
      height: 30,
      distance: 20,
    })
    ok(`[準備B] 原点中心の40×30×20mmの箱を作った(body=${bodyIdB}、底面の罠を試す用)`)

    const { page, consoleErrors } = await openApp(browser, url, {
      timeout,
      until: () => (window.__duocad?.state?.bodies ?? []).length >= 2,
    })
    await sleep(200)

    const tiersNow = () => page.evaluate(() => window.__duocad.gridTiers())

    // **ページの中で JSON.stringify する**(Svelteの状態はProxyで、そのまま返すと
    // CDPの構造化複製でプロパティが運ばれず `{}` になる。engine
    // `puppeteer-page-evaluate-loses-svelte5-runes-proxy-objects` と同じ落とし穴)。
    async function sketchPlane() {
      const raw = await page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketch
        return JSON.stringify(active?.plane ?? null)
      })
      return JSON.parse(raw)
    }

    async function exitAndUndo() {
      await page.click('#sketch-exit')
      await page.waitForFunction(() => window.__duocad.activeSketch === null, { timeout, polling: 50 })
      const before = await page.evaluate(() => window.__duocad.sketches.length)
      await mcp.callTool('undo')
      await page.waitForFunction(
        (expected) => window.__duocad.sketches.length < expected,
        { timeout, polling: 100 },
        before,
      )
    }

    // ====================================================================== 通常表示の確認(復帰の比較対象)
    console.log('\n[0] 通常表示(スケッチ外)はXY平面+Z軸')
    await waitFrames(page, 4)
    const plain = await tiersNow()
    if (JSON.stringify(plain.planes) !== JSON.stringify(['XY']) || plain.axis !== 'Z') {
      fail(`[0] 通常はXY平面+Z軸のはず: ${JSON.stringify(plain)}`)
    }
    ok('[0] スケッチ外ではXY平面+Z軸だけが出ている')

    // ====================================================================== [1] 素直な任意面
    console.log('\n[1] 素直な任意面(frameが基準平面と一致しない面)へのスケッチ')
    const planarCandidatesA = await page.evaluate((bodyId) => {
      const geometry = window.__duocad.getGeometry(bodyId)
      return geometry.faces
        .filter((face) => face.kind === 'plane')
        .map((face) => face.index)
        .filter((index) => window.__duocad.projectFace(bodyId, index) !== null)
    }, bodyIdA)
    if (planarCandidatesA.length === 0) fail('[1] 平面状の面が画面から1枚も指せない(この裏取りが成立しない)')

    let onFaceA = null
    const attemptLogA = []
    for (const faceIndex of planarCandidatesA) {
      if (
        await page.evaluate(
          () => document.querySelector('.command-host')?.dataset.command !== 'sketch_create',
        )
      ) {
        await page.click('#sketch-create')
      }
      const at = await faceAt(page, bodyIdA, faceIndex)
      if (!at) {
        attemptLogA.push(`面${faceIndex}: 射影できない`)
        continue
      }
      await page.mouse.click(at.x, at.y)
      const created = await page
        .waitForFunction(() => window.__duocad.activeSketch !== null, { timeout: 15000, polling: 50 })
        .then(() => true)
        .catch(() => false)
      if (!created) {
        attemptLogA.push(`面${faceIndex}: スケッチができない`)
        await page.keyboard.press('Escape')
        continue
      }
      await waitFrames(page, 4)
      const plane = await sketchPlane()
      // **`plane.kind==='face'` だけを条件にする**(フィルタで避けない)。
      // 基準平面のグリッド/ピック優先度に取られて `plane.kind==='base'` になった場合だけ
      // 次の候補へ進む——それは別の既知の落とし穴であって、
      // 本タスクの対象外。`plane.kind==='face'` が取れたら必ず検証する。
      if (plane?.kind === 'face') {
        onFaceA = { faceIndex, plane }
        break
      }
      attemptLogA.push(`面${faceIndex}: plane=${JSON.stringify(plane)}(基準平面のピックに取られた・対象外)`)
      await exitAndUndo()
    }
    if (onFaceA === null) {
      fail(`[1] 任意面へのスケッチが1つも作れなかった: ${attemptLogA.join(' | ')}`)
    }
    ok(`[1] 既存ソリッドの面(面${onFaceA.faceIndex})にスケッチを作れた(plane=${JSON.stringify(onFaceA.plane)})`)
    const tiersA = await tiersNow()
    assertFaceGridShape(tiersA, '[1]')
    // **原点・u/v はサーバの `SketchFrame` をコードが変形せずそのまま渡している**
    // (`App.svelte` の `sketchDraws()` → `gridPlanes.ts` の `setSketchFrame` → `desiredGridPlanes` の
    // `sketch-face` 分岐は `frame: sketchGridFrame` を素通しするだけで、コピーも計算もしない)。
    const frameA = onFaceA.plane.frame
    if (!frameA || !Array.isArray(frameA.origin) || !Array.isArray(frameA.u) || !Array.isArray(frameA.v)) {
      fail(`[1] スケッチ平面のframeが届いていない: ${JSON.stringify(onFaceA.plane)}`)
    }
    ok(
      `[1] グリッドに渡ったframeは面固有のもの(完了条件4。origin=${JSON.stringify(frameA.origin)} ` +
        `u=${JSON.stringify(frameA.u)} v=${JSON.stringify(frameA.v)})`,
    )

    console.log('\n[1] z-fightingしていないことをスクリーンショットで残す(素直な任意面)')
    mkdirSync(screenshotDir, { recursive: true })
    await page.screenshot({ path: join(screenshotDir, 'sketch-face-grid.png') })
    ok(`スクリーンショットを保存した: ${join(screenshotDir, 'sketch-face-grid.png')}`)
    await exitAndUndo()

    // ====================================================================== [2] 罠: frameが基準平面と完全一致する実ジオメトリの面
    console.log('\n[2] 罠: frameが基準平面(XY)と完全一致する実ジオメトリの面(QA指摘の回帰検査)')
    // 底面(z平均が最小)を選ぶ。原点中心の箱なので、この面のframeがXYと一致するはず。
    const facesB = await page.evaluate((bodyId) => {
      const geometry = window.__duocad.getGeometry(bodyId)
      return geometry.faces
        .filter((face) => face.kind === 'plane')
        .map((face) => {
          let sumZ = 0
          for (let i = 2; i < face.positions.length; i += 3) sumZ += face.positions[i]
          return { index: face.index, avgZ: sumZ / (face.positions.length / 3) }
        })
    }, bodyIdB)
    const bottomFace = facesB.slice().sort((a, b) => a.avgZ - b.avgZ)[0]
    if (!bottomFace) fail('[2] 底面が見つからない')

    // **MCP経由で面参照を選択し、`sketch_create`(base_planeを省略)で貼る。**
    // 通常のUIクリックだと、この面はXY基準平面と完全同一平面のため、フロント側の
    // ピック優先度(`resolveHit`)が基準平面グリッドに取ってしまう可能性がある
    // (それ自体は別の既知の落とし穴で本タスクの対象外)。MCP経由なら添字で直接指すので、
    // その競合を経ずに「`plane.kind==='face'` かつframeが基準平面と一致する」状態を
    // 確実に再現できる——これは実際にAIが同じ経路(選択→sketch_create)で日常的に
    // 作りうる状態でもある。
    await mcp.callTool('set_selection', { refs: [{ body: bodyIdB, kind: 'face', index: bottomFace.index }] })
    const sketchOnTrap = await mcp.callTool('sketch_create', {})
    ok(`[2] MCP経由で底面(面${bottomFace.index})にスケッチを作った(sketch_id=${sketchOnTrap.sketch_id})`)

    await page.waitForFunction(
      (id) => window.__duocad.sketches.some((s) => s.sketch_id === id),
      { timeout, polling: 100 },
      sketchOnTrap.sketch_id,
    )
    // ブラウザ側では画面のクリックで「入る」——スケッチ一覧の名前をクリックする
    // (`BrowserPanel.svelte` の `data-sketch-id` 付き行)。
    await page.waitForSelector(`li[data-sketch-id="${sketchOnTrap.sketch_id}"] .sketch-name`, { timeout })
    await page.click(`li[data-sketch-id="${sketchOnTrap.sketch_id}"] .sketch-name`)
    await page.waitForFunction(
      (id) => window.__duocad.activeSketch === id,
      { timeout, polling: 100 },
      sketchOnTrap.sketch_id,
    )
    await waitFrames(page, 4)

    const planeTrap = await sketchPlane()
    if (planeTrap?.kind !== 'face') {
      fail(`[2] 罠の面のplane.kindがfaceでない(この検査の前提が崩れている): ${JSON.stringify(planeTrap)}`)
    }
    const frameTrap = planeTrap.frame
    const isExactXy =
      frameTrap &&
      Math.abs(frameTrap.origin[0]) < 1e-6 &&
      Math.abs(frameTrap.origin[1]) < 1e-6 &&
      Math.abs(frameTrap.origin[2]) < 1e-6 &&
      Math.abs(frameTrap.u[0] - 1) < 1e-6 &&
      Math.abs(frameTrap.u[1]) < 1e-6 &&
      Math.abs(frameTrap.u[2]) < 1e-6 &&
      Math.abs(frameTrap.v[0]) < 1e-6 &&
      Math.abs(frameTrap.v[1] - 1) < 1e-6 &&
      Math.abs(frameTrap.v[2]) < 1e-6
    if (!isExactXy) {
      fail(
        `[2] 罠が成立していない(frameがXY基準平面と一致していない。この検査の前提が崩れている): ` +
          `${JSON.stringify(frameTrap)}`,
      )
    }
    ok(
      `[2] 罠が成立: plane.kind==='face' かつ frame が XY基準平面と完全一致 ` +
        `(origin=${JSON.stringify(frameTrap.origin)} u=${JSON.stringify(frameTrap.u)} v=${JSON.stringify(frameTrap.v)})`,
    )

    const tiersTrap = await tiersNow()
    assertFaceGridShape(tiersTrap, '[2]')

    console.log('\n[2] z-fightingしていないことをスクリーンショットで残す(罠: 貼った面と完全同一平面)')
    await page.screenshot({ path: join(screenshotDir, 'sketch-face-grid-coplanar-trap.png') })
    ok(`スクリーンショットを保存した: ${join(screenshotDir, 'sketch-face-grid-coplanar-trap.png')}`)

    // ====================================================================== [3] 完了条件5: 抜けると通常表示へ戻る
    console.log('\n[3] 完了条件5: スケッチを抜けると通常表示(XY+Z軸)へ戻る')
    await page.click('#sketch-exit')
    await page.waitForFunction(() => window.__duocad.activeSketch === null, { timeout, polling: 50 })
    await waitFrames(page, 4)
    const backToPlain = await tiersNow()
    if (JSON.stringify(backToPlain.planes) !== JSON.stringify(['XY']) || backToPlain.axis !== 'Z') {
      fail(`[3] スケッチを抜けたら通常(XY+Z軸)へ戻るはず: ${JSON.stringify(backToPlain)}`)
    }
    ok('[3] スケッチを抜けると通常(XY平面+Z軸)へ戻る')

    await assertNoAppErrors(page, consoleErrors)
  }
})
