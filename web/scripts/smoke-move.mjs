/**
 * 実機での確認 — **スケッチパラメータパネルの後継**が、本物のブラウザで端から端まで動く。
 *
 *   node scripts/smoke-move.mjs [--port 8975] [--http-port 8976]
 *
 * 2026-08-30に右サイドバーの `SketchPanel.svelte`(数値入力欄・モード表示・「正面から見る」・
 * 「抜ける」)を廃止し、位置の打ち直しを**3Dの直接選択 + move verb**へ置き換えた。
 * この置き換えが本当に成立していることを、pytestでは判定できない部分について見る:
 *
 *   1. **マウスの割り当て**: 右ドラッグ=カメラ回転 / 中ドラッグ=パン / 左ドラッグ=ウィンドウ選択
 *   2. **ウィンドウ選択**: 矩形に**完全包含**されたものだけが選ばれる(向きは問わない)
 *   3. **Shift と Ctrl がどちらも同じトグル**(選択済みなら外す・未選択なら足す)
 *   4. **move_sketch_entities**: 点を掴んで動かす / 図形ごと複製する
 *   5. **move_body**: 面をクリックしてボディを動かす / 複製する
 *   6. **モード操作の移設**: `#sketch-mode` / `#sketch-look` / `#sketch-exit` が
 *      **コマンドツールバーの中**にあり、スケッチモード中に機能する
 *   7. **右サイドバーの開閉**: `#sidebar-toggle` で `<aside class="sidebar">` が出入りし、
 *      中身は `AiPanel` だけ(= 完全にAI専用)。`.viewport` のサイズ指定は変わらない
 *
 * サーバはこのスクリプトが空の文書で起動し、**形は全部UIから作る**(この検査の主題が
 * 「人が手で操作できるか」なので、MCPで下ごしらえしない)。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 */
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { findBrowser, launchBrowser, frameAll, withoutBrowserPanel } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const wsPort = options.port ?? '8975'
const httpPort = options['http-port'] ?? '8976'
const timeout = 60000
const workdir = mkdtempSync(join(tmpdir(), 'duocad-move-'))

/** 板(領域を押し出してボディを作る)。 */
const PLATE = { width: 40, height: 30, depth: 12 }
/** 動かす線分(板から離れた所に置く。ウィンドウ選択で板を巻き込まないため)。 */
const LINE = { x0: 60, y0: 0, x1: 80, y1: 0 }

async function main() {
  const executablePath = findBrowser()
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  let browser = null
  try {
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

    // ---------------------------------------------------------------- 道具
    async function waitFor(check, what, args = []) {
      try {
        await page.waitForFunction(check, { timeout, polling: 50 }, ...args)
      } catch {
        fail(`${what}(タイムアウト)`)
      }
    }

    async function typeInto(selector, value) {
      await page.click(selector)
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyA')
      await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await page.type(selector, String(value), { delay: 15 })
    }

    /** スケッチ平面の (u,v) の画面座標(押す直前に射影し直す)。無ければ null。 */
    async function planeAt(u, v) {
      return page.evaluate(
        (uu, vv) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectSketchPoint(uu, vv)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        u,
        v,
      )
    }

    async function clickPlane(u, v) {
      return withoutBrowserPanel(page, async () => {
        const at = await planeAt(u, v)
        if (!at) fail(`スケッチ平面の点 (${u}, ${v}) が画面の中に無い`)
        await page.mouse.click(at.x, at.y)
      })
    }

    /** スケッチの図形を**その最初の点**のクリックで選ぶ(他の実機検査と同じ道具)。 */
    async function clickEntityHandle(entityId) {
      return withoutBrowserPanel(page, async () => {
        const at = await page.evaluate((id) => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          const entity = active?.entities.find((item) => item.entity_id === id)
          if (!entity) return null
          const p = entity.params
          const uv = 'cx' in p ? [p.cx, p.cy] : 'x0' in p ? [p.x0, p.y0] : [p.x, p.y]
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectSketchPoint(uv[0], uv[1])
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        }, entityId)
        if (!at) fail(`図形 ${entityId} の点が画面の中に無い`)
        await page.mouse.click(at.x, at.y)
      })
    }

    /** スケッチ平面の点を中心にした矩形を左ドラッグで引く(ウィンドウ選択)。 */
    async function dragBox(from, to, key = null) {
      return withoutBrowserPanel(page, async () => {
        const a = await planeAt(from[0], from[1])
        const b = await planeAt(to[0], to[1])
        if (!a || !b) fail(`矩形の角 (${from}) / (${to}) が画面の中に無い`)
        if (key) await page.keyboard.down(key)
        await page.mouse.move(a.x, a.y)
        await page.mouse.down()
        // **途中の点を経由する**(pointermove が1回も飛ばないと矩形が始まらない)
        await page.mouse.move((a.x + b.x) / 2, (a.y + b.y) / 2, { steps: 4 })
        await page.mouse.move(b.x, b.y, { steps: 4 })
        await page.mouse.up()
        if (key) await page.keyboard.up(key)
      })
    }

    /**
     * 今のスケッチの状態(図形と選択)。
     *
     * **JSONへ落としてから返す**。`window.__duocad` が持つのは Svelte の `$state` プロキシで、
     * puppeteer の構造化コピーに素のまま渡すと**空のオブジェクトとして届く**
     * (実測: `selection.count` が undefined になる)。
     */
    async function sketchNow() {
      return page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch) ?? null
        return JSON.parse(
          JSON.stringify({
            activeSketch: hook.activeSketch,
            entities: active ? active.entities : [],
            regions: active ? active.regions.length : 0,
            selection: hook.selection,
          }),
        )
      })
    }

    /** 線分の図形(1本だけある前提)。 */
    async function lineEntity() {
      const state = await sketchNow()
      const found = state.entities.filter((item) => item.kind === 'line')
      if (found.length === 0) fail('線分が見つからない')
      return found
    }

    // ================================================================ 1. マウスの割り当て
    // **右ドラッグで回る / 中ドラッグでパン / 左ドラッグでは回らない**。
    // 判定は `cameraPose()`(実機検査だけが使う穴)の position で行う。
    const poseBefore = await page.evaluate(() => window.__duocad.cameraPose())
    const box = await page.evaluate(() => {
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
    })

    async function drag(button, dx, dy) {
      await page.mouse.move(box.x, box.y)
      await page.mouse.down({ button })
      await page.mouse.move(box.x + dx, box.y + dy, { steps: 8 })
      await page.mouse.up({ button })
      await new Promise((resolve) => setTimeout(resolve, 400)) // 減衰つきの追随を待つ
      return page.evaluate(() => window.__duocad.cameraPose())
    }

    const afterLeft = await drag('left', 120, 60)
    const movedByLeft = distance(poseBefore.position, afterLeft.position)
    if (movedByLeft > 0.5) fail(`左ドラッグでカメラが動いた(${movedByLeft.toFixed(2)}mm)`)
    ok('左ドラッグではカメラが動かない(ウィンドウ選択に明け渡している)')

    const afterRight = await drag('right', 120, 0)
    const movedByRight = distance(afterLeft.position, afterRight.position)
    if (movedByRight < 1) fail(`右ドラッグでカメラが回らない(${movedByRight.toFixed(2)}mm)`)
    // 回転は**注視点を動かさない**(パンとの見分け)
    const targetShiftOnRotate = distance(afterLeft.target, afterRight.target)
    if (targetShiftOnRotate > 0.5) fail(`右ドラッグが回転ではなくパンになっている(注視点が ${targetShiftOnRotate.toFixed(2)}mm 動いた)`)
    ok(`右ドラッグ = カメラ回転(位置が ${movedByRight.toFixed(1)}mm 動き、注視点は動かない)`)

    const afterMiddle = await drag('middle', 80, 0)
    const targetShiftOnPan = distance(afterRight.target, afterMiddle.target)
    if (targetShiftOnPan < 0.5) fail(`中ドラッグでパンしない(注視点が ${targetShiftOnPan.toFixed(2)}mm しか動かない)`)
    ok(`中ドラッグ = パン(注視点が ${targetShiftOnPan.toFixed(1)}mm 動いた)`)
    await frameAll(page)

    // ================================================================ 2. スケッチを描く
    await page.click('#sketch-create')
    {
      const at = await page.evaluate(() => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const position = window.__duocad.projectBasePlane('XY')
        return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
      })
      if (!at) fail('基準平面XYを指せる画素が無い')
      await page.mouse.click(at.x, at.y)
    }
    await waitFor(() => window.__duocad.activeSketch !== null, 'XY平面のスケッチに入れない')

    // ---- 6の一部: モード操作が**ツールバーの中**にある(右サイドバーではない)
    const modeRow = await page.evaluate(() => ({
      inToolbar: Boolean(document.querySelector('.toolbar #sketch-mode')),
      look: Boolean(document.querySelector('.toolbar #sketch-look')),
      exit: Boolean(document.querySelector('.toolbar #sketch-exit')),
      inSidebar: Boolean(document.querySelector('.sidebar #sketch-mode')),
      sketchPanel: Boolean(document.querySelector('.sketch-panel')),
    }))
    if (!modeRow.inToolbar || !modeRow.look || !modeRow.exit) {
      fail(`モード操作がツールバーに無い: ${JSON.stringify(modeRow)}`)
    }
    if (modeRow.inSidebar || modeRow.sketchPanel) {
      fail(`スケッチパネルが右サイドバーに残っている: ${JSON.stringify(modeRow)}`)
    }
    ok('モード表示・正面から見る・抜けるがコマンドツールバー側にある(右サイドバーには無い)')

    await page.click('#sketch-look') // 正面から見る(以後の (u,v) → 画素が安定する)
    await new Promise((resolve) => setTimeout(resolve, 400))

    await page.click('#sketch-rectangle')
    await clickPlane(0, 0)
    await typeInto('#sketch-width', PLATE.width)
    await typeInto('#sketch-height', PLATE.height)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.regions.length === 1)
      },
      '長方形の領域ができない',
    )

    await page.click('#sketch-line')
    await clickPlane(LINE.x0, LINE.y0)
    await clickPlane(LINE.x1, LINE.y1)
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.some((item) => item.kind === 'line'))
      },
      '線分を描けない',
    )
    await page.keyboard.press('Escape') // 「選ぶ」へ戻す(描くコマンドが点を食わないように)
    ok('長方形と線分をUIから描いた')

    // ================================================================ 3. ウィンドウ選択で点を選ぶ
    // **線分の右端だけ**を小さな矩形で囲む(左端も線全体も入らない大きさ)。
    await dragBox([LINE.x1 - 4, LINE.y1 - 4], [LINE.x1 + 4, LINE.y1 + 4])
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).some((item) => item.point === 'x1'),
      'ウィンドウ選択で線分の右端(x1)を選べない',
    )
    const afterBox = await sketchNow()
    if (afterBox.selection.count !== 1) {
      fail(`右端だけを囲んだのに ${afterBox.selection.count} 件選ばれた: ${JSON.stringify(afterBox.selection.items)}`)
    }
    ok('左ドラッグのウィンドウ選択で、矩形に完全包含された点(x1)だけが選ばれた')

    // **向きは問わない**(本人明言)。同じ矩形を右下→左上へ引いても同じ1点が選ばれる。
    await dragBox([LINE.x1 + 4, LINE.y1 + 4], [LINE.x1 - 4, LINE.y1 - 4])
    await waitFor(
      () => {
        const items = window.__duocad.selection?.items ?? []
        return items.length === 1 && items[0].point === 'x1'
      },
      '逆向きに引いた矩形では選べない(ドラッグの向きで判定が変わっている)',
    )
    ok('矩形を逆向き(右下→左上)に引いても同じ点が選ばれた(向きで判定を変えない)')

    // **囲まなかったものは選ばれない**(完全包含の確認): 線分の中ほどだけを囲む
    await dragBox([LINE.x0 + 6, LINE.y0 - 3], [LINE.x1 - 6, LINE.y1 + 3])
    await waitFor(
      () => (window.__duocad.selection?.count ?? -1) === 0,
      '線分の一部だけを囲んだのに何かが選ばれた(交差判定になっている)',
    )
    ok('線分の一部だけを囲んでも何も選ばれない(交差ではなく完全包含)')

    // ================================================================ 4. Shift / Ctrl のトグル
    await dragBox([LINE.x1 - 4, LINE.y1 - 4], [LINE.x1 + 4, LINE.y1 + 4])
    await waitFor(() => (window.__duocad.selection?.count ?? 0) === 1, '右端を選び直せない')
    const rightEnd = await planeAt(LINE.x1, LINE.y1)
    if (!rightEnd) fail('線分の右端が画面の中に無い')

    await withoutBrowserPanel(page, async () => {
      await page.keyboard.down('Shift')
      await page.mouse.click(rightEnd.x, rightEnd.y)
      await page.keyboard.up('Shift')
    })
    await waitFor(
      () => (window.__duocad.selection?.count ?? -1) === 0,
      'Shift+クリックで選択済みの点を外せない(トグルになっていない)',
    )
    ok('Shift+クリックが選択済みの点を外した(= トグル)')

    await withoutBrowserPanel(page, async () => {
      await page.keyboard.down('Control')
      await page.mouse.click(rightEnd.x, rightEnd.y)
      await page.keyboard.up('Control')
    })
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).some((item) => item.point === 'x1'),
      'Ctrl+クリックで未選択の点を足せない(除外専用のままになっている)',
    )
    ok('Ctrl+クリックが未選択の点を足した(Shiftと同一機能)')

    // ================================================================ 5. move_sketch_entities(移動)
    const before = (await lineEntity())[0]
    await page.click('#sketch-move')
    await page.waitForSelector('#sketch-move-move', { timeout })
    await page.click('#sketch-move-move')
    await page.waitForFunction(
      () => document.querySelector('#sketch-move-move')?.classList.contains('on'),
      { timeout, polling: 50 },
    )
    await typeInto('#sketch-move-dx', 0)
    await typeInto('#sketch-move-dy', 10)

    // ---------------------------------------------------------------- 確定前の幽霊
    // **`preview: 'local'`(2026-09-05)** —— 選んだ点を含む図形が、dy を打った時点で
    // 移動先に半透明で出ている(元の図形はその場に残るので、両方が見える)。
    await waitFor(
      () => window.__duocad.ghostInfo().sketch.length === 1,
      '点を動かす前の幽霊が出ない',
    )
    const sketchGhost = await page.evaluate(() => window.__duocad.ghostInfo().sketch[0])
    if (Math.abs(sketchGhost.params.y1 - (before.params.y1 + 10)) > 1e-9) {
      fail(
        `幽霊が dy=10 のぶん動いていない: y1 ${before.params.y1} → ${sketchGhost.params.y1}`,
      )
    }
    // **選んだのは点1つ(`point:'x1'`)なので、反対の端点は動かない。** 図形ごと平行移動
    // する幽霊を描くと、ここで落ちる(コアは選んだ点だけを動かすので、幽霊と結果が食い違う)。
    if (Math.abs(sketchGhost.params.y0 - before.params.y0) > 1e-9) {
      fail(
        `点1つを選んだのに図形ごと動く幽霊になっている: y0 ${before.params.y0} → ` +
          `${sketchGhost.params.y0}`,
      )
    }
    ok('move_sketch_entities: 確定前に移動先の幽霊が出て、選んだ点だけが dy ぶん動く')

    await page.keyboard.press('Enter')
    // **クリックで置いた座標は丸い数字ではない**(画素→(u,v)の逆射影なので 0.0234 のような値)。
    // 判定は絶対値ではなく**移動量**で行う。
    await waitFor(
      (id, y1) => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        const entity = active?.entities.find((item) => item.entity_id === id)
        return Boolean(entity && Math.abs(entity.params.y1 - (y1 + 10)) < 1e-6)
      },
      '点を動かしても y1 が +10 されない',
      [before.entity_id, before.params.y1],
    )
    const moved = (await lineEntity())[0]
    if (Math.abs(moved.params.y0 - before.params.y0) > 1e-6) {
      fail(`点だけを動かしたのに反対の端点(y0)まで動いた: ${moved.params.y0}`)
    }
    ok(
      `move_sketch_entities で選んだ点だけが動いた(y1: ${before.params.y1.toFixed(3)} → ` +
        `${moved.params.y1.toFixed(3)}・y0 は不変)`,
    )

    // **選択は空になる**(他の形状変更verbと同じ既定の契約。2026-08-31に本人が非として却下し変更)
    const afterMoveSelection = await page.evaluate(() =>
      JSON.parse(JSON.stringify(window.__duocad.selection)),
    )
    if (afterMoveSelection.count !== 0) {
      fail(`移動の後も選択が残っている(リセットされていない): ${JSON.stringify(afterMoveSelection)}`)
    }
    ok('移動の後は選択が空になる(次は選び直しになる)')

    // ================================================================ 6. move_sketch_entities(複製)
    // 図形全体を選び直す(線の中ほどをクリック = 点ではなく辺)。
    await withoutBrowserPanel(page, async () => {
      const mid = await planeAt((LINE.x0 + LINE.x1) / 2, moved.params.y0)
      if (!mid) fail('線分の中ほどが画面の中に無い')
      await page.mouse.click(mid.x, mid.y)
    })
    await waitFor(
      () =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.entity !== undefined && item.point === null,
        ),
      '線分そのもの(辺)をクリックで選べない',
    )
    ok('線分そのもの(点ではなく辺)をクリックで選べた')

    const countBeforeCopy = (await sketchNow()).entities.length
    await page.click('#sketch-move')
    await page.click('#sketch-move-copy')
    await typeInto('#sketch-move-dx', 0)
    await typeInto('#sketch-move-dy', -20)
    await page.keyboard.press('Enter')
    await waitFor(
      (n) => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === n + 1)
      },
      '複製で図形が増えない',
      [countBeforeCopy],
    )
    const lines = await lineEntity()
    if (lines.length !== 2) fail(`線分が2本になっていない: ${lines.length}`)
    ok(`copy=true で図形が複製された(図形 ${countBeforeCopy} → ${countBeforeCopy + 1}・元も残っている)`)

    // ================================================================ 6b. スケッチを直接切り替えると選択が残らない
    //
    // **`exitSketch` を通らずにAからBへ移る道がある**(ビューポートに浮かぶブラウザの
    // スケッチ名クリック)。外さないと、Bで動かしたつもりでAの図形が動き、反復verbには
    // **Aの図形IDがBの文脈で**渡る(サーバは entity_id を素直に解決するので黙って通る)。
    const sketchA = await page.evaluate(() => window.__duocad.activeSketch)
    // Aの図形を1つ選んでおく
    await withoutBrowserPanel(page, async () => {
      const mid = await planeAt((LINE.x0 + LINE.x1) / 2, moved.params.y0)
      if (!mid) fail('線分の中ほどが画面の中に無い')
      await page.mouse.click(mid.x, mid.y)
    })
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).some((item) => item.entity !== undefined),
      'Aの図形を選べない',
    )
    // Bを作る(XZ平面。Aとは別のスケッチ)
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'Aを抜けられない')
    await frameAll(page)
    await page.click('#sketch-create')
    {
      const at = await page.evaluate(() => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const position = window.__duocad.projectBasePlane('XZ')
        return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
      })
      if (!at) fail('基準平面XZを指せる画素が無い')
      await page.mouse.click(at.x, at.y)
    }
    await waitFor(
      (a) => window.__duocad.activeSketch !== null && window.__duocad.activeSketch !== a,
      'XZ平面に2枚目のスケッチを作れない',
      [sketchA],
    )
    const sketchB = await page.evaluate(() => window.__duocad.activeSketch)
    // **Aへ戻り、Aの図形を選んでから、ブラウザのスケッチ名クリックで直接Bへ移る**
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'Bを抜けられない')
    await page.click(`[data-sketch-id="${sketchA}"] .sketch-name`)
    await waitFor((a) => window.__duocad.activeSketch === a, 'Aへ再入場できない', [sketchA])
    await page.click('#sketch-look')
    await new Promise((resolve) => setTimeout(resolve, 400))
    await clickEntityHandle(lines[0].entity_id)
    await waitFor(
      (a) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.entity !== undefined && item.sketch === a,
        ),
      'Aの図形を選び直せない',
      [sketchA],
    )
    await page.click(`[data-sketch-id="${sketchB}"] .sketch-name`)
    await waitFor((b) => window.__duocad.activeSketch === b, 'Bへ直接移れない', [sketchB])
    await waitFor(
      () =>
        !(window.__duocad.selection?.items ?? []).some((item) => item.entity !== undefined) &&
        (window.__duocad.selectedEntities ?? []).length === 0,
      'Aの図形の選択がBへ持ち越されている(enterSketchが外していない)',
    )
    ok('スケッチAで選んだ図形は、Bへ直接移った時点で選択から外れる')

    // ================================================================ 7. 領域を押し出してボディを作る
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'Bを抜けられない(2回目)')
    await page.click(`[data-sketch-id="${sketchA}"] .sketch-name`)
    await waitFor((a) => window.__duocad.activeSketch === a, 'Aへ戻れない', [sketchA])
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'ツールバーの「抜ける」でスケッチを抜けられない')
    ok('ツールバーの「抜ける」でスケッチモードを抜けた(移設先が機能している)')
    await frameAll(page)

    const sketchId = await page.evaluate(() => window.__duocad.sketches[0].sketch_id)
    await page.click('#extrude-regions')
    {
      const at = await page.evaluate((id) => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const position = window.__duocad.projectRegion(1, id)
        return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
      }, sketchId)
      if (!at) fail('領域1を指せる画素が無い')
      await page.mouse.click(at.x, at.y)
    }
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).some((item) => item.index === 1 && item.entity === undefined),
      '領域を選べない',
    )
    await page.click('#extrude-mode-new')
    await typeInto('#regions-distance', PLATE.depth)
    await page.keyboard.press('Enter')
    await waitFor(() => (window.__duocad.parts ?? []).length === 1, '領域を押し出してもボディができない')
    // **Escで無起動へ戻す**。確定してもコマンドは構えたままなので(`CommandHost` の契約)、
    // 領域のピックが開いたままだと次の面クリックが**手前に浮いている領域に取られる**。
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Escape')
    await waitFor(() => window.__duocad.wantsRegions === false, '領域のピックが閉じない')
    await frameAll(page)
    ok('領域を押し出してボディを1つ作った')

    // ================================================================ 8. move_body(移動)
    const bodyId = await page.evaluate(() => window.__duocad.parts[0].body_id)
    const faceIndex = await pickAnyFace(page, bodyId)
    await clickFace(page, bodyId, faceIndex)
    await waitFor(
      (id, index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'face' && item.index === index,
        ),
      '面をクリックで選べない',
      [bodyId, faceIndex],
    )
    const volumeBefore = await page.evaluate(
      (id) => window.__duocad.state.bodies.find((item) => item.body_id === id).volume,
      bodyId,
    )
    const geometryBefore = await page.evaluate((id) => window.__duocad.getGeometry(id).bbox.center, bodyId)

    await page.click('#move-body')
    await page.click('#move-body-move')
    await typeInto('#move-body-dx', 0)
    await typeInto('#move-body-dy', 0)
    await typeInto('#move-body-dz', 25)

    // ---------------------------------------------------------------- 確定前の幽霊
    // **`preview: 'local'`(2026-09-05)** —— 届いているメッシュを dx/dy/dz ずらして
    // 半透明で重ねる。**本物は隠さない**(移動前と移動後が並んで見えることが中身)ので、
    // 幽霊が出ている間も `getGeometry` の本物は元の位置のままである。
    await waitFor(
      (id) => {
        const ghosts = window.__duocad.ghostInfo().bodies
        return ghosts.length === 1 && ghosts[0].bodyId === id && ghosts[0].offset[2] === 25
      },
      'move_body の幽霊が dz=25 で出ない',
      [bodyId],
    )
    const stillHome = await page.evaluate(
      (id) => window.__duocad.getGeometry(id).bbox.center[2],
      bodyId,
    )
    if (Math.abs(stillHome - geometryBefore[2]) > 1e-6) {
      fail(`幽霊を出しただけで本物が動いた: ${geometryBefore[2]} → ${stillHome}`)
    }

    // **Escで消えて、文書は一切変わらない**(サーバへ何も送っていない)
    await page.keyboard.press('Escape')
    await waitFor(
      () => window.__duocad.ghostInfo().bodies.length === 0,
      'Escで move_body の幽霊が消えない',
    )
    const volumeAfterEscape = await page.evaluate(
      (id) => window.__duocad.state.bodies.find((item) => item.body_id === id).volume,
      bodyId,
    )
    if (Math.abs(volumeAfterEscape - volumeBefore) > 1e-9) {
      fail(`Escで中断したのに文書が変わった: ${volumeBefore} → ${volumeAfterEscape}`)
    }
    ok('move_body: 確定前に移動先の幽霊が出て、Escで消えて文書は変わらない')

    // 打ち直して今度は確定する(面の選択は Esc では消えない)
    await page.click('#move-body')
    await page.click('#move-body-move')
    await typeInto('#move-body-dx', 0)
    await typeInto('#move-body-dy', 0)
    await typeInto('#move-body-dz', 25)
    await waitFor(
      () => window.__duocad.ghostInfo().bodies.length === 1,
      '打ち直した move_body の幽霊が出ない',
    )
    await page.keyboard.press('Enter')
    await waitFor(
      (id, z) => {
        const geometry = window.__duocad.getGeometry(id)
        return Boolean(geometry && Math.abs(geometry.bbox.center[2] - (z + 25)) < 0.5)
      },
      'move_body でボディがZ方向へ動かない',
      [bodyId, geometryBefore[2]],
    )
    const volumeAfter = await page.evaluate(
      (id) => window.__duocad.state.bodies.find((item) => item.body_id === id).volume,
      bodyId,
    )
    if (Math.abs(volumeAfter - volumeBefore) > 1e-3) {
      fail(`平行移動で体積が変わった: ${volumeBefore} → ${volumeAfter}`)
    }
    ok(`move_body でボディがZ+25動いた(体積は ${volumeAfter.toFixed(1)} のまま)`)

    const selectionAfterBodyMove = await page.evaluate(() =>
      JSON.parse(JSON.stringify(window.__duocad.selection)),
    )
    if (selectionAfterBodyMove.count !== 0) {
      fail(`ボディの移動の後も選択が残っている(リセットされていない): ${JSON.stringify(selectionAfterBodyMove)}`)
    }
    ok('ボディの移動の後は選択が空になる(次は選び直しになる)')

    // ================================================================ 8b. move_body は辺からも踏める
    //
    // `MOVE_BODY` の選択引数は面/辺/頂点の3種を食う(コアの `require_bodies` が
    // `ShapeRef.body` から逆引きする)。**画面側の配線が本当に届いているか**を見る ——
    // `CommandHost` が主たる種別しか要求しないと、辺のピックが開かないまま
    // 「サーバは受け付けるのに画面から選べない」穴になる(2026-08-30に実際そうなっていた)。
    // move_body は確定のたびに選択を空にする(2026-08-31〜)ので、選択は既に空。
    // そのまま move-body を起動すれば辺のピックが開くはずである。
    // **移動直後は1フレーム挟む**(`projectEdge` 等はレンダー済みの射影行列を読むため。
    // 以前はここにCtrl+クリックの往復があり副次的にフレームを進めていたが、
    // その往復自体は無くなったので明示的に呼ぶ)。
    await frameAll(page)
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Escape')
    await page.click('#move-body')
    await waitFor(
      () => window.__duocad.wantsEdges === true,
      'move_body を起動しても辺を要求しない(kinds の配線が届いていない)',
    )
    // **`wantsEdges` が真になっても、辺の実データはまだ無い**(2026-09-03に原因を特定)。
    // あれは `CommandHost` の同期的な `$effect` で即座に立つが、辺そのものは版が違えば
    // `showEdges(null, [])` で空にしてからWS往復で取り直す(`App.svelte`)。その窓では
    // `projectEdge` が `edgeBodyId !== bodyId` の早期returnで**全indexに対しnull**を返し、
    // 下のループが待たずに8本を撃ち切って「1本も選べない」と誤判定していた。**エラーは
    // 1件も出ない**ので原因が遠い(engine `move-body-edge-pick-fails-on-clean-main`)。
    //
    // **2周するのは、ループの最中に足元が抜ける経路があるため** —— カメラを動かすと
    // 可変解像度が180ms後に `set_view_resolution` を送り、サーバは粗さを版の鍵に入れて
    // いるので形が同じでも配信し直す。その配信で `viewport.update()` が表示中の辺を消すため、
    // 全indexが途中でnullへ落ちうる。取り直しを待ってもう1周すれば通る。
    let edgePicked = null
    for (let attempt = 0; attempt < 2 && edgePicked === null; attempt++) {
      await waitFor(
        (id) =>
          [1, 2, 3, 4, 5, 6, 7, 8].some((index) => window.__duocad.projectEdge(id, index) !== null),
        '辺の実データが届かない(move_body 起動後、projectEdge がどのindexでもnullのまま)',
        [bodyId],
      )
      for (const edgeIndex of [1, 2, 3, 4, 5, 6, 7, 8]) {
        const at = await page.evaluate(
          (id, index) => {
            const rect = document.querySelector('.viewport').getBoundingClientRect()
            const position = window.__duocad.projectEdge(id, index)
            return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
          },
          bodyId,
          edgeIndex,
        )
        if (!at) continue
        await page.mouse.click(at.x, at.y)
        try {
          await page.waitForFunction(
            (id, index) =>
              (window.__duocad.selection?.items ?? []).some(
                (item) => item.body === id && item.kind === 'edge' && item.index === index,
              ),
            { timeout: 5000, polling: 50 },
            bodyId,
            edgeIndex,
          )
          edgePicked = edgeIndex
          break
        } catch {
          // その辺は取れなかった(裏に回っている)。次を試す
        }
      }
    }
    if (edgePicked === null) fail('move_body を起動しても辺を1本もクリックで選べない')
    const beforeEdgeMove = await page.evaluate(
      (id) => window.__duocad.getGeometry(id).bbox.center,
      bodyId,
    )
    await page.click('#move-body-move')
    await typeInto('#move-body-dx', 6)
    await typeInto('#move-body-dy', 0)
    await typeInto('#move-body-dz', 0)
    await page.keyboard.press('Enter')
    await waitFor(
      (id, x) => {
        const geometry = window.__duocad.getGeometry(id)
        return Boolean(geometry && Math.abs(geometry.bbox.center[0] - (x + 6)) < 0.5)
      },
      '辺の選択から move_body してもボディが動かない',
      [bodyId, beforeEdgeMove[0]],
    )
    ok(`辺 ${edgePicked} を選んで move_body できた(面だけでなく辺からもボディを指せる)`)

    // 面の選択へ戻す(次の複製は面から踏む)
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Escape')
    await clickFace(page, bodyId, faceIndex)
    await waitFor(
      (id, index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'face' && item.index === index,
        ),
      '面を選び直せない',
      [bodyId, faceIndex],
    )

    // ================================================================ 9. move_body(複製)
    await page.click('#move-body')
    await page.click('#move-body-copy')
    await typeInto('#move-body-dx', 80)
    await typeInto('#move-body-dy', 0)
    await typeInto('#move-body-dz', 0)
    await page.keyboard.press('Enter')
    await waitFor(() => (window.__duocad.parts ?? []).length === 2, 'copy=true でボディが増えない')
    const bodies = await page.evaluate(() => window.__duocad.parts.map((item) => item.body_id))
    const copyId = bodies.find((id) => id !== bodyId)
    await waitFor(
      (id) => Boolean(window.__duocad.getGeometry(id)),
      '複製したボディの形が画面へ届かない',
      [copyId],
    )
    const copyCenter = await page.evaluate((id) => window.__duocad.getGeometry(id).bbox.center, copyId)
    const originalCenter = await page.evaluate((id) => window.__duocad.getGeometry(id).bbox.center, bodyId)
    if (Math.abs(copyCenter[0] - (originalCenter[0] + 80)) > 0.5) {
      fail(`複製の位置がX+80でない: ${JSON.stringify(copyCenter)} / 元 ${JSON.stringify(originalCenter)}`)
    }
    ok(`copy=true でボディが複製された(2個・複製はX+80の位置・元はその場)`)

    // ================================================================ 10. 右サイドバーの開閉
    const sidebarBefore = await page.evaluate(() => ({
      sidebar: Boolean(document.querySelector('.sidebar')),
      ai: Boolean(document.querySelector('.sidebar .ai-panel')),
      children: document.querySelector('.sidebar')?.childElementCount ?? -1,
      viewport: document.querySelector('.viewport').getBoundingClientRect().width,
    }))
    if (!sidebarBefore.sidebar) fail('右サイドバーが出ていない')
    if (sidebarBefore.children !== 1) {
      fail(`右サイドバーの中身が AiPanel 1つでない(${sidebarBefore.children}個)`)
    }
    ok('右サイドバーの中身は AiPanel 1つだけ(= 完全にAI専用)')

    await page.click('#sidebar-toggle')
    await waitFor(() => document.querySelector('.sidebar') === null, '開閉ボタンで右サイドバーを畳めない')
    const closed = await page.evaluate(() => ({
      viewport: document.querySelector('.viewport').getBoundingClientRect().width,
      inlineSize: document.querySelector('.viewport').style.width,
    }))
    if (closed.viewport <= sidebarBefore.viewport) {
      fail(`畳んでもビューポートが広がらない: ${sidebarBefore.viewport} → ${closed.viewport}`)
    }
    if (closed.inlineSize !== '') {
      fail(`.viewport にインラインの寸法が付いた(flexの配分だけを変える契約): ${closed.inlineSize}`)
    }
    ok(`右サイドバーを畳んだ(ビューポート幅 ${Math.round(sidebarBefore.viewport)} → ${Math.round(closed.viewport)}px・.viewport の寸法指定は不変)`)

    await page.click('#sidebar-toggle')
    await waitFor(() => document.querySelector('.sidebar') !== null, '開閉ボタンで右サイドバーを戻せない')
    ok('右サイドバーを開き直した')

    // ---- ヘッダーの**右端**にあること(左端のファイル一覧ボタンより右)
    const ends = await page.evaluate(() => {
      const header = document.querySelector('header').getBoundingClientRect()
      const left = document.querySelector('#data-panel-toggle').getBoundingClientRect()
      const right = document.querySelector('#sidebar-toggle').getBoundingClientRect()
      const settings = document.querySelector('#settings-open').getBoundingClientRect()
      return {
        gapLeft: left.left - header.left,
        gapRight: header.right - right.right,
        afterSettings: right.left - settings.right,
      }
    })
    if (ends.gapRight > ends.gapLeft + 4) {
      fail(`開閉ボタンがヘッダーの右端に無い(右の余白 ${ends.gapRight} / 左の余白 ${ends.gapLeft})`)
    }
    if (ends.afterSettings < 0) fail('開閉ボタンが #settings-open より左にある')
    ok('開閉ボタンはヘッダーの右端(#settings-open より後ろ)にある')

    if (consoleErrors.length) fail(`ブラウザのコンソールにエラー: ${consoleErrors.join(' / ')}`)
    const appErrors = await page.evaluate(() => [...window.__duocad.errors])
    if (appErrors.length) fail(`画面にエラーが出た: ${JSON.stringify(appErrors)}`)
    ok('走行中にエラーは1件も出ていない')
    console.log('\nsmoke-move: PASS')
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

function distance(a, b) {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])
}

/** 画面から指せる面を1つ見つける(見つからなければ失敗)。 */
async function pickAnyFace(page, bodyId) {
  const found = await page.evaluate((id) => {
    const geometry = window.__duocad.getGeometry(id)
    if (!geometry) return null
    for (const face of geometry.faces) {
      if (window.__duocad.projectFace(id, face.index)) return face.index
    }
    return null
  }, bodyId)
  if (found === null) fail('画面から指せる面が1つも無い')
  return found
}

async function clickFace(page, bodyId, faceIndex) {
  const at = await page.evaluate(
    (id, index) => {
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      const position = window.__duocad.projectFace(id, index)
      return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
    },
    bodyId,
    faceIndex,
  )
  if (!at) fail(`面 ${faceIndex} が画面の中に無い`)
  await page.mouse.click(at.x, at.y)
}

main().catch((error) => {
  console.error(`\nsmoke-move: FAIL ${error.message ?? error}`)
  process.exitCode = 1
})
