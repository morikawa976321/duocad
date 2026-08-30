/**
 * 実機での確認 — 形状最適化(第3のモード)を画面から使い切れる。
 *
 *   node scripts/smoke-optimize-mode.mjs [--port 8955] [--http-port 8956]
 *
 * タスク `20260831-0109-duocad-optimize-mode-frontend` の完了条件を実ブラウザで見る:
 *
 *   1. **「形状最適化」ボタンで第3のモードに入れる**。抜けると通常モードへ戻る。
 *   2. **4つのverbがツールバーから使える** —— 起動 → 既存の割り当てが選択状態で現れる →
 *      クリックで付け外し → Enterで確定。保持/除外はボディ粒度、拘束/荷重は面粒度で拾う。
 *   3. **保持ソリッドが緑、除外ソリッドが赤**。**選択が勝つ**(選ばれている間はハイライト色
 *      になり役割色は隠れる)ことも、`faceColor()`(実機検査フック)で見る。
 *   4. **拘束(青塗り+固定記号)と荷重(矢印+ラベル)が見える**
 *      (`optimizeAnnotationCount()` で存在を機械的に確かめ、`screenshots/optimize-mode.png`
 *      で目視できるようにする)。
 *   5. **モードを抜けると装飾も役割色も消える**。
 *
 * サーバはこのスクリプトが空の文書で起動し、2つの立方体をMCP経由で作る(離して置くので
 * 画面上で別々の画素になる)。**`--models-dir` に使い捨てディレクトリを渡す**
 * (リポジトリ直下の実 `models/` を触らない)。
 */
import { mkdirSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { findBrowser, launchBrowser, frameAll, pressUndo } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const wsPort = options.port ?? '8955'
const httpPort = options['http-port'] ?? '8956'
const timeout = Number.parseInt(options.timeout ?? '60000', 10)
const workdir = mkdtempSync(join(tmpdir(), 'duocad-optimize-mode-'))
const screenshotDir = 'C:\\Users\\tm976\\mf\\duocad\\screenshots'

// `viewport.ts` の役割色(16進)。**このスクリプトが正の写しを持つと事故る**ので、
// 値そのものは `viewport.ts` の定数と一致しているかを最後にひとつ確かめる
// (色を直したらこの検査だけが古い値のまま赤くなる、という保険)。
const COLOR = {
  base: 0x9aa6b2,
  highlight: 0xffb454,
  preserve: 0x3f9b58,
  exclude: 0xc75448,
  fixture: 0x3f7fc2,
}

async function main() {
  const executablePath = findBrowser()
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null
  try {
    await mcp.handshake('duocad-smoke-optimize-mode')

    /** 板(領域を押し出してボディを作る)。離して置くので画面で別々の画素になる。 */
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

    const bodyA = await addBox([0, 0], 20, 20)
    const bodyB = await addBox([60, 0], 20, 20)
    ok(`2つの立方体を作った(A=${bodyA} / B=${bodyB})`)

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
    await page.waitForFunction(() => (window.__duocad?.parts ?? []).length >= 2, { timeout, polling: 100 })
    await frameAll(page)
    await new Promise((resolve) => setTimeout(resolve, 200))

    async function waitFor(check, what, args = []) {
      try {
        await page.waitForFunction(check, { timeout, polling: 50 }, ...args)
      } catch {
        fail(`${what}(タイムアウト)`)
      }
    }

    /** **画面から指せる面を1つ見つける**(`exclude` に載る添字は避ける)。 */
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
      if (found === null) fail(`ボディ ${bodyId} に画面から指せる面が無い(除外=${JSON.stringify(exclude)})`)
      return found
    }

    /** その面を**押し直前に射影し直して**クリックする(コマンドが変わるとツールバーの高さが
     *  動くので、画素は持ち越さない。engine `e2e-pixel-cannot-cross-command-switch`)。 */
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

    /** ホバーは描画ループ(`tick`)で解決するので、動かしたあと数フレーム待つ(`smoke-hover.mjs`と同じ)。 */
    async function settle() {
      await page.evaluate(
        () =>
          new Promise((resolve) => {
            let left = 4
            const step = () => (left-- <= 0 ? resolve() : requestAnimationFrame(step))
            requestAnimationFrame(step)
          }),
      )
    }

    /**
     * その面の色。**先にポインタを画面の外へ逃がす** —— ホバーは色相を変えず明度だけ足すので
     * (`HOVER_LIGHTEN`)、クリック直後はカーソルがその面の上に残ったままで、素の判定色と
     * 微妙に違う値が返る(実測: `0xffb454` ではなく明るくした `0xffd2ab`)。役割/選択の色
     * そのものを見たいここでは、ホバーの影響を切り離す。
     */
    async function faceColor(bodyId, faceIndex) {
      await page.mouse.move(2, 2)
      await settle()
      return page.evaluate((id, index) => window.__duocad.faceColor(id, index), bodyId, faceIndex)
    }

    // ====================================================================== 1. モードへ入る
    console.log('\n[1] 「形状最適化」ボタンで第3のモードへ入る')
    if (await page.evaluate(() => window.__duocad.optimizeMode)) fail('起動直後から最適化モードに入っている')
    await page.click('#optimize-enter')
    await waitFor(() => window.__duocad.optimizeMode === true, '#optimize-enter を押してもモードに入らない')
    ok('[1] 「形状最適化」ボタンでモードに入った')

    // ====================================================================== 2. 保持ソリッド(ボディ粒度)
    console.log('\n[2] 保持ソリッド — ボディ粒度で選び、Enterで確定')
    await page.click('#set-preserve-bodies')
    await waitFor(() => window.__duocad.wantedKinds.includes('body'), '保持ソリッドのverbでボディ粒度にならない')
    const faceA_preserve = await pickAnyFace(bodyA)
    await clickFace(bodyA, faceA_preserve)
    await waitFor(
      (id) => (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'body' && item.body === id),
      '面をクリックしてもボディ(BodyRef)が選ばれない',
      [bodyA],
    )
    ok('[2] 面をクリックすると、そのボディ全体が選ばれる(ピックの粒度がボディ)')
    const selectedHighlight = await faceColor(bodyA, faceA_preserve)
    if (selectedHighlight !== COLOR.highlight) {
      fail(`選択中のボディがハイライト色(${COLOR.highlight.toString(16)})でない: ${selectedHighlight?.toString(16)}`)
    }
    ok('[2] 選択中は役割よりハイライト色が勝つ(選択が勝つ、確定前)')
    await page.keyboard.press('Enter')
    await waitFor(
      (id) => (window.__duocad.state.optimize.preserve_bodies ?? []).some((item) => item.body === id),
      'Enterで確定しても state.optimize.preserve_bodies に載らない',
      [bodyA],
    )
    ok('[2] Enterで確定 → state.optimize.preserve_bodies に載った')
    const stillHighlighted = await faceColor(bodyA, faceA_preserve)
    if (stillHighlighted !== COLOR.highlight) {
      fail(`確定後も選択は残るはずだが色が変わった: ${stillHighlighted?.toString(16)}`)
    }
    ok('[2] 確定後もコマンドは構えたまま(選択が残り、まだハイライト色)')

    // ====================================================================== 3. 除外ソリッド(ボディ粒度)
    console.log('\n[3] 除外ソリッド — 起動するとAの選択が外れ、保持の役割色(緑)が現れる')
    await page.click('#set-exclude-bodies')
    await waitFor(() => window.__duocad.wantedKinds.includes('body'), '除外ソリッドのverbでボディ粒度にならない')
    await waitFor(
      (id) => !(window.__duocad.selection?.items ?? []).some((item) => item.kind === 'body' && item.body === id),
      '除外verbを起動してもAの選択(BodyRef)が外れない(空の割り当てのプリセレクト)',
      [bodyA],
    )
    const preserveColor = await faceColor(bodyA, faceA_preserve)
    if (preserveColor !== COLOR.preserve) {
      fail(`選択が外れた保持ソリッドが緑(${COLOR.preserve.toString(16)})でない: ${preserveColor?.toString(16)}`)
    }
    ok(`[3] 保持ソリッドAが緑で見える(${preserveColor.toString(16)})`)

    const faceB_exclude = await pickAnyFace(bodyB)
    await clickFace(bodyB, faceB_exclude)
    await waitFor(
      (id) => (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'body' && item.body === id),
      'Bをクリックしても選ばれない',
      [bodyB],
    )
    await page.keyboard.press('Enter')
    await waitFor(
      (id) => (window.__duocad.state.optimize.exclude_bodies ?? []).some((item) => item.body === id),
      'Enterで確定しても state.optimize.exclude_bodies に載らない',
      [bodyB],
    )
    ok('[3] Bを除外ソリッドとして確定した')

    // ====================================================================== 4. 拘束面(面粒度)
    console.log('\n[4] 拘束面 — 面粒度で選び、青く塗られて固定記号が付く')
    await page.click('#set-fixtures')
    await waitFor(
      () => window.__duocad.wantedKinds.includes('face') && !window.__duocad.wantedKinds.includes('body'),
      '拘束面のverbで面粒度にならない',
    )
    const excludeColor = await faceColor(bodyB, faceB_exclude)
    if (excludeColor !== COLOR.exclude) {
      fail(`選択が外れた除外ソリッドが赤(${COLOR.exclude.toString(16)})でない: ${excludeColor?.toString(16)}`)
    }
    ok(`[4] 除外ソリッドBが赤で見える(${excludeColor.toString(16)})`)

    // **`faceA_preserve` は必ず避ける** —— 同じ面を選ぶと、この後の「拘束にした面以外は
    // 緑のまま」の検査が、拘束にした当の面(=保持の検査に使った面)を見てしまい、
    // 役割が面単位で効いているかどうかを実際には確かめないまま緑になる誤検査になる。
    const faceA_fixture = await pickAnyFace(bodyA, [faceA_preserve])
    await clickFace(bodyA, faceA_fixture)
    await waitFor(
      (id, index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.kind === 'face' && item.body === id && item.index === index,
        ),
      '拘束verbで面(ShapeRef)が選ばれない(ボディ粒度のまま拾っている疑い)',
      [bodyA, faceA_fixture],
    )
    ok('[4] 拘束面のverbでは面そのものが選ばれる(ボディではない)')
    await page.keyboard.press('Enter')
    await waitFor(
      (id, index) =>
        (window.__duocad.state.optimize.fixtures ?? []).some((item) => item.body === id && item.index === index),
      'Enterで確定しても state.optimize.fixtures に載らない',
      [bodyA, faceA_fixture],
    )
    ok('[4] 拘束面として確定した')

    // ====================================================================== 5. 荷重面(面粒度)+ 大きさ・向き
    console.log('\n[5] 荷重面 — 拘束面が青く残り、保持ソリッドの緑は他の面のまま')
    await page.click('#set-loads')
    await waitFor(() => window.__duocad.wantedKinds.includes('face'), '荷重面のverbで面粒度にならない')
    const fixtureColor = await faceColor(bodyA, faceA_fixture)
    if (fixtureColor !== COLOR.fixture) {
      fail(`拘束面が青(${COLOR.fixture.toString(16)})で塗られていない: ${fixtureColor?.toString(16)}`)
    }
    ok(`[5] 拘束面が青で見える(${fixtureColor.toString(16)}。役割は面単位で、ボディ全体は変わらない)`)
    const stillPreserveA = await faceColor(bodyA, faceA_preserve)
    if (stillPreserveA !== COLOR.preserve) {
      fail(`拘束面を1つ足しただけなのにAの他の面まで緑でなくなった: ${stillPreserveA?.toString(16)}`)
    }
    ok('[5] 拘束にした面以外は保持ソリッドの緑のまま(役割は面単位)')

    const faceA_load = await pickAnyFace(bodyA, [faceA_fixture])
    await clickFace(bodyA, faceA_load)
    await waitFor(
      (id, index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.kind === 'face' && item.body === id && item.index === index,
        ),
      '荷重面が選ばれない',
      [bodyA, faceA_load],
    )
    await page.click('#load-direction-normal')
    await typeInto('#load-magnitude', 250)
    await page.keyboard.press('Enter')
    await waitFor(
      (id, index) =>
        (window.__duocad.state.optimize.loads ?? []).some((item) => item.body === id && item.index === index),
      'Enterで確定しても state.optimize.loads に載らない',
      [bodyA, faceA_load],
    )
    // **`JSON.parse(JSON.stringify(...))` で素のオブジェクトへ複製してから返す** ——
    // `state` はSvelte 5 runesのリアクティブProxyで、Proxyのまま`page.evaluate`の戻り値に
    // 使うと、CDP越しの構造化複製がプロパティを1つも運ばず`{}`になる(実測)。文字列化して
    // からNode側で読めば中身が正しく渡る。
    const loadState = await page.evaluate(() =>
      JSON.parse(JSON.stringify(window.__duocad.state.optimize.load)),
    )
    if (!loadState || loadState.magnitude !== 250 || loadState.direction !== 'normal') {
      fail(`load(大きさ・向き)が期待どおりでない: ${JSON.stringify(loadState)}`)
    }
    ok(`[5] 荷重面を確定した(magnitude=250N, direction=normal): ${JSON.stringify(loadState)}`)

    // ====================================================================== 6. 装飾(拘束記号・荷重矢印)が見える
    console.log('\n[6] 拘束の固定記号・荷重の矢印が実際に描かれている')
    const annotationCount = await page.evaluate(() => window.__duocad.optimizeAnnotationCount())
    if (annotationCount < 2) fail(`装飾がほぼ描かれていない(count=${annotationCount})`)
    ok(`[6] 装飾が${annotationCount}個描かれている(拘束の固定記号1 + 荷重の矢印1 + ラベル1が目安)`)

    // ====================================================================== 7. スクリーンショット
    // モードに入り・保持と除外を割り当て・拘束と荷重を置いた状態(タスクの完了条件そのもの)。
    console.log('\n[7] スクリーンショット')
    await page.evaluate(() => document.activeElement?.blur?.())
    await frameAll(page)
    await new Promise((resolve) => setTimeout(resolve, 300))
    mkdirSync(screenshotDir, { recursive: true })
    const screenshotPath = join(screenshotDir, 'optimize-mode.png')
    await page.screenshot({ path: screenshotPath })
    ok(`[7] スクリーンショットを保存した: ${screenshotPath}`)

    // ====================================================================== 8. undo で戻る
    console.log('\n[8] undo で荷重面の確定が戻る')
    await pressUndo(page)
    await waitFor(
      () => (window.__duocad.state.optimize.loads ?? []).length === 0,
      'undoしても state.optimize.loads が空に戻らない',
    )
    ok('[8] Ctrl+Z(undo)で荷重面の確定が戻った')

    // ====================================================================== 9. モードを抜けると装飾も役割色も消える
    console.log('\n[9] モードを抜けると装飾も役割色も消える')
    await page.click('#optimize-exit')
    await waitFor(() => window.__duocad.optimizeMode === false, '#optimize-exit を押してもモードを抜けない')
    await waitFor(
      () => window.__duocad.optimizeAnnotationCount() === 0,
      'モードを抜けても装飾が残っている',
    )
    const baseAfterExit = await faceColor(bodyA, faceA_preserve)
    if (baseAfterExit !== COLOR.base) {
      fail(`モードを抜けたのに素の色(${COLOR.base.toString(16)})に戻らない: ${baseAfterExit?.toString(16)}`)
    }
    ok('[9] モードを抜けると装飾が消え、ボディの色も素へ戻った')

    // ====================================================================== エラーが出ていないこと
    const errors = await page.evaluate(() => window.__duocad.errors ?? [])
    if (errors.length > 0) fail(`走行中にエラーが出た: ${JSON.stringify(errors)}`)
    if (consoleErrors.length > 0) fail(`コンソールにエラー: ${consoleErrors.join(' / ')}`)
    ok('走行中にエラーは1件も出ていない')
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

main().catch((error) => {
  console.error(`\nsmoke-optimize-mode: FAIL ${error.message ?? error}`)
  process.exitCode = 1
})
