/**
 * 実機での確認 — **ホバーで明るくなったものが、そのままクリックしたら選択されるものと一致する**。
 *
 *   node scripts/smoke-hover.mjs [--port 8975] [--http-port 8976]
 *
 * 2026-08-27に「クリックすれば取れるものをホバーで先に見せる」を入れた。この機能の価値は
 * **予告と結果が一致していること**にしかない(食い違うなら、無いほうがましである)。
 * だから見るのはその一致であって、見た目ではない:
 *
 *   1. 面をホバー → `hoveredRef()` がその面を返し、**同じ画素をクリックするとその面が選ばれる**
 *   2. ポインタがビューポートを出たら強調が消える(掴んだままにならない)
 *   3. **選択済みの面にも明度が乗る**(2026-08-27の裁定。色を足さず明度だけで伝えるので、
 *      「選択の色そのまま」になっていないことを色の値で見る)
 *   4. 丸めるを起動して辺をホバー → **辺が返り、同じ画素のクリックでその辺が選ばれる**。
 *      ホバーがクリックと同じゲート(`showEdges` している間だけ辺を指せる)と
 *      同じ優先順位(辺→面)を通っている
 *   5. 領域をホバーしたまま**ポインタを動かさずに**コマンドを解除 → 強調が消える
 *      (指せるものが変わったことを、ポインタの動き以外の経路で知れているか)
 *
 * サーバはこのスクリプトが空の文書で起動し、板はMCP経由で作る。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 */
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { findBrowser, launchBrowser, frameAll, withoutBrowserPanel } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const wsPort = options.port ?? '8975'
const httpPort = options['http-port'] ?? '8976'
const timeout = 60000
const workdir = mkdtempSync(join(tmpdir(), 'duocad-hover-'))

/** ホバーは描画ループ(`tick`)で解決するので、動かしたあと数フレーム待つ。 */
async function settle(page) {
  await page.evaluate(
    () =>
      new Promise((resolve) => {
        let left = 4
        const step = () => (left-- <= 0 ? resolve() : requestAnimationFrame(step))
        requestAnimationFrame(step)
      }),
  )
}

async function main() {
  const executablePath = findBrowser()
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null
  try {
    await mcp.handshake('duocad-smoke-hover')
    const sketch = await mcp.callTool('sketch_create', { base_plane: 'XY' })
    await mcp.callTool('add_rectangle', {
      sketch_id: sketch.sketch_id,
      corner: [0, 0],
      width: 40,
      height: 30,
    })
    await mcp.callTool('propose_selection', { refs: [{ sketch: sketch.sketch_id, index: 1 }] })
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
    await frameAll(page)

    // **使い回さず、使う直前に測り直す**(smoke.mjsの`locateFace`と同じ流儀)。
    // 2026-08-28、通常モードの既定が無起動になったことでツールバーの行数(=高さ)が
    // 状態によって変わるようになった —— コマンドを起動すると引数の行(数値・選択肢・
    // 中断ボタン)が増える分だけ`.viewport`が下へ縮む。1回だけ測って使い回すと、
    // 起動前後で`.viewport`の実際の位置とずれる(実測で、領域ホバーの検査がこれで外した)。
    async function viewportBox() {
      return page.evaluate(() => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        return { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
      })
    }

    // ---------------------------------------------------------------- 1. 面: 予告と結果の一致
    const spot = await withoutBrowserPanel(page, async () => {
      // **指せる面を実際に射影して探す**(`projectFace` は返す前にその画素からのレイが
      // 本当にその面へ当たることを確かめる)。面の並びは形の作り方で変わるので決め打ちしない。
      const found = await page.evaluate(
        (id) => {
          for (let index = 1; index <= 12; index += 1) {
            const at = window.__duocad.projectFace(id, index)
            if (at) return { index, ...at }
          }
          return null
        },
        bodyId,
      )
      if (!found) fail('画面上で指せる面が1つも無い')
      const box = await viewportBox()
      await page.mouse.move(box.left + found.x, box.top + found.y)
      await settle(page)
      const hovered = await page.evaluate(() => window.__duocad.hoveredRef())
      if (hovered === null) fail('hoveredRef が無い(フックが出ていない)')
      if (hovered.face === null) fail(`面をホバーしたのに強調されない: ${JSON.stringify(hovered)}`)
      if (hovered.face.bodyId !== bodyId || hovered.face.faceIndex !== found.index) {
        fail(`ホバーした面が違う: ${JSON.stringify(hovered.face)} (狙いは面${found.index})`)
      }
      if (hovered.edge !== null || hovered.region !== null || hovered.plane !== null) {
        fail(`面のホバーなのに他の種別まで返った: ${JSON.stringify(hovered)}`)
      }
      if (hovered.color === null) fail('ホバーしているのに色が読めない')
      ok(`面${found.index}をホバー: hoveredRef が同じ面を返す(色 #${hovered.color.toString(16)})`)

      // **同じ画素をクリックする**。ここが一致していなければ、この機能は嘘をついている。
      await page.mouse.click(box.left + found.x, box.top + found.y)
      await page.waitForFunction(() => (window.__duocad.selection?.items ?? []).length === 1, {
        timeout,
        polling: 50,
      })
      // **項目ごとに値を書き写して返す**。選択はSvelte 5のproxyなので、そのまま返すと
      // 入れ子のフィールドが落ちて `{}` になる(engine `puppeteer-evaluate-drops-nested-svelte5-proxy-fields`)。
      const picked = await page.evaluate(() => {
        const item = (window.__duocad.selection?.items ?? [])[0]
        return item ? { kind: item.kind, body: item.body, index: item.index } : null
      })
      if (picked === null) fail('クリックしたのに選択が空のまま')
      if (picked.kind !== 'face' || picked.body !== bodyId || picked.index !== found.index) {
        fail(
          `予告と結果が食い違った: ホバーは面${found.index}、クリックで選ばれたのは ` +
            JSON.stringify(picked),
        )
      }
      ok(`**同じ画素をクリックしたら、予告どおり面${found.index}が選ばれた**`)
      return { ...found, hoverColor: hovered.color }
    })

    // ---------------------------------------------------------------- 2. 外へ出たら消える
    await page.mouse.move(8, 8) // ビューポートの外(上部バー)へ抜ける
    await settle(page)
    const afterLeave = await page.evaluate(() => window.__duocad.hoveredRef())
    if (afterLeave.face !== null || afterLeave.color !== null) {
      fail(`ビューポートを出たのに強調が残った: ${JSON.stringify(afterLeave)}`)
    }
    ok('ビューポートの外へ出たら強調が消える(掴んだままにならない)')

    // ---------------------------------------------------------------- 3. 選択済みにも明度が乗る
    await withoutBrowserPanel(page, async () => {
      const box = await viewportBox()
      await page.mouse.move(box.left + spot.x, box.top + spot.y)
      await settle(page)
      const state = await page.evaluate(() => ({
        hovered: window.__duocad.hoveredRef(),
        colors: window.__duocad.highlightColors(),
      }))
      if (state.hovered.face === null) fail('選択済みの面をホバーし直せない')
      if (state.hovered.color === state.colors.face) {
        fail(
          `選択済みの面に明度が乗っていない(選択の色そのまま #${state.colors.face.toString(16)})`,
        )
      }
      if (state.hovered.color === spot.hoverColor) {
        fail('選択の前後でホバー色が同じ(選択の色に明度を足していない)')
      }
      ok(
        `選択済みの面もホバーで明るくなる(選択 #${state.colors.face.toString(16)} → ` +
          `ホバー #${state.hovered.color.toString(16)}。未選択のときは ` +
          `#${spot.hoverColor.toString(16)})`,
      )
    })

    // ---------------------------------------------------------------- 4. ゲートと優先順位の共有
    await page.click('#fillet')
    // `__duocad.edges` は `{revision, edges}`(配列そのものではない)。
    await page.waitForFunction(() => (window.__duocad.edges?.edges ?? []).length > 0, {
      timeout,
      polling: 50,
    })
    await withoutBrowserPanel(page, async () => {
      const found = await page.evaluate((id) => {
        for (const edge of window.__duocad.edges?.edges ?? []) {
          const at = window.__duocad.projectEdge(id, edge.index)
          if (at) return { index: edge.index, ...at }
        }
        return null
      }, bodyId)
      if (!found) fail('丸めるを起動したのに、画面上で指せる辺が1本も無い')
      const box = await viewportBox() // **#fillet起動後の高さで測る**(起動前の`box`は使い回さない)
      await page.mouse.move(box.left + found.x, box.top + found.y)
      await settle(page)
      const hovered = await page.evaluate(() => window.__duocad.hoveredRef())
      if (hovered.edge === null) {
        fail(`辺をホバーしたのに辺が返らない: ${JSON.stringify(hovered)}`)
      }
      if (hovered.edge.edgeIndex !== found.index) {
        fail(`ホバーした辺が違う: ${JSON.stringify(hovered.edge)} (狙いは辺${found.index})`)
      }
      // **辺が面より先**。ホバーがクリックと同じ `resolveHit` を通っている証拠になる。
      if (hovered.face !== null) fail(`辺の上なのに面まで強調された: ${JSON.stringify(hovered)}`)

      // **面と同じく、同じ画素をクリックして結果まで確かめる**(予告だけ見て終わらない)。
      await page.mouse.click(box.left + found.x, box.top + found.y)
      await page.waitForFunction(
        (index) =>
          (window.__duocad.selection?.items ?? []).some(
            (item) => item.kind === 'edge' && item.index === index,
          ),
        { timeout, polling: 50 },
        found.index,
      )
      ok(`辺${found.index}: ホバーの予告どおり、同じ画素のクリックでその辺が選ばれた`)
    })
    await page.keyboard.press('Escape')

    // ---------------------------------------------------------------- 5. ゲートが閉じたら消える
    // **ポインタを動かさずに**コマンドを解除する。ホバーの解決は「ポインタ位置+視点」が
    // 同じなら省くので、**指せるものが変わったことを別途知る仕組み**が無いと、
    // 取れなくなった領域が明るいまま「クリックすれば取れる」と嘘をつく
    // (2026-08-27のQA(cursor/gpt-5.6-terra-xhigh)がこの穴を指摘した)。
    await withoutBrowserPanel(page, async () => {
      await page.click('#extrude-regions')
      await page.waitForFunction(() => window.__duocad.wantsRegions === true, {
        timeout,
        polling: 50,
      })
      const region = await page.evaluate(() => {
        for (const sketch of window.__duocad.sketches ?? []) {
          for (let index = 1; index <= 4; index += 1) {
            const at = window.__duocad.projectRegion(index, sketch.sketch_id)
            if (at) return { sketchId: sketch.sketch_id, index, x: at.x, y: at.y }
          }
        }
        return null
      })
      if (!region) fail('「領域を押し出す」を起動したのに、画面上で指せる領域が無い')
      // **#extrude-regions起動後の高さで測る**(2026-08-28実測: 起動前の`box`を使い回すと、
      // 選択肢の行(結合/切り取り/新規)ぶんツールバーが伸びて`.viewport`が動き、
      // 領域ではなく別の面を掴んだ)。
      const box = await viewportBox()
      await page.mouse.move(box.left + region.x, box.top + region.y)
      await settle(page)
      const hovered = await page.evaluate(() => window.__duocad.hoveredRef())
      if (hovered.region === null || hovered.region.index !== region.index) {
        fail(`領域をホバーしたのに領域が返らない: ${JSON.stringify(hovered)}`)
      }
      ok(`領域${region.index}をホバー: 領域が返る(要求中だけ指せるゲートを共有している)`)

      // **ここでマウスを1画素も動かさない。**
      await page.keyboard.press('Escape')
      await page.waitForFunction(() => window.__duocad.wantsRegions === false, {
        timeout,
        polling: 50,
      })
      await settle(page)
      const after = await page.evaluate(() => window.__duocad.hoveredRef())
      if (after.region !== null) {
        fail(
          `ゲートが閉じたのに領域が明るいまま(ポインタを動かさないと消えない): ${JSON.stringify(after)}`,
        )
      }
      ok('**ポインタを動かさずにコマンドを解除しても、取れなくなった領域の強調は消える**')
    })

    if (consoleErrors.length > 0) fail(`ブラウザにエラーが出た: ${consoleErrors.join(' / ')}`)
    ok('コンソールにエラーなし')
    console.log('\nsmoke-hover: PASS')
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

main().catch((error) => {
  console.error(`\nsmoke-hover: FAIL ${error.message ?? error}`)
  process.exitCode = 1
})
