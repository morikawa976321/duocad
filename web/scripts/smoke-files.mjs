/**
 * 実ブラウザでのパネルとファイル操作の検査。
 *
 * `smoke-features.mjs` と同じく**空の文書**から始めて、人が手でやる順序を
 * 本物のクリックとキー入力で通す。見るのは5つ:
 *
 *   1. **穴の径を1箇所編集して、反復で作った全部の穴が追随する**(M3の存在理由)
 *   2. **保存は1操作ごとに自動**(issue #5): 無題の文書は最初の変更で `untitled-<n>.json` が
 *      付き、名前欄で改名する。複製 → undo → 複製を開く で undo 前の形が戻る(操作ログの
 *      再生)。**編集中でないファイルは1バイトも書き換わらない**(既にある名前への改名は拒否)
 *   3. STEPを読み込む(その時点で編集可能なボディができる)
 *   4. STEP/STL を書き出し、**STEPは読み戻して体積が一致**(読み戻しは pytest 側)
 *   5. undo/redoでやり直せる件数と有効な操作数が実際に動く
 *   6. **一覧の行から別名で複製・ディスクから削除**できる(issue #17)。どちらも編集中の文書を
 *      変えない。複製先が既にあれば拒否(1バイトも変わらない)、編集中のファイルの削除は拒否
 *
 *   web> node scripts/smoke-files.mjs [--workdir <書き込めるディレクトリ>]
 *
 * **サーバはこのスクリプトが自分で立てる**(`web/scripts/server.mjs`)。
 * 既に立っているものへ繋ぐときだけ `--url` を渡す。
 *
 * **サーバは毎回起動し直す**(文書は接続より長生きするので、前回の形が残っていると
 * 「最初のソリッドが生まれる」経路を通れない)。
 */
import { existsSync, mkdtempSync, readFileSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { findBrowser, launchBrowser, withoutBrowserPanel, pressUndo, pressRedo, frameAll } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
// `--url` を渡したときだけ既に立っているサーバへ繋ぐ(既定は自分で立てる)。
const attachUrl = options.url ?? null
const wsPort = options.port ?? '8795'
const httpPort = options['http-port'] ?? '8796'
const timeout = Number.parseInt(options.timeout ?? '120000', 10)
const workdir = options.workdir ?? mkdtempSync(join(tmpdir(), 'duocad-e-'))


/** 板と穴の寸法(mm)。体積の検算に使うので丸い数字。 */
const PLATE = { width: 60, height: 20, thickness: 5 }
const HOLE = { u: 10, v: 10, radius: 2, dx: 15, count: 3 }



async function main() {
  const executablePath = findBrowser()
  console.log(`smoke-files: ${executablePath}`)
  console.log(`  workdir: ${workdir}`)

  // **`modelsDir: workdir`** —— 保存/開くは `duocad/models/` 配下のファイル名だけを
  // 受け取る。渡さないとリポジトリ直下の実 `models/` を検査が書き換えてしまうので、
  // 既存の使い捨てworkdirをそのまま流用する(STEP/STL書き出しの `join(workdir, ...)` と
  // 同じディレクトリで、実在確認が楽になる)。
  const server = attachUrl === null ? await startServer({ wsPort, httpPort, modelsDir: workdir }) : null
  // **MCPの口**。数値の打ち直し(`set_parameter`)は人間側のUIを持たないので、
  // その1手だけを同じプロセスのMCP側から打つ。`--attach` で外のサーバへ
  // 繋いだときは子プロセスを持たないので `null` になり、使う直前に落とす(黙って飛ばさない)。
  const mcp = server === null ? null : new McpClient(server.child, { timeout })
  const url = server?.url ?? attachUrl
  console.log(`  server : ${url}${server === null ? ' (既に立っているサーバへ接続)' : ''}`)

  const browser = await launchBrowser(executablePath)

  try {
    const page = await browser.newPage()
    await page.setViewport({ width: 1280, height: 900 })
    const consoleErrors = []
    /**
     * **わざと出させたエラーのトースト**(最後の「エラーは1枚も出ていない」から除く)。
     *
     * 検証2bの(c)は「編集中でないファイルへの保存が**拒否される**」ことを見るので、
     * トーストが1枚出るのが正しい姿である。除外は**実際に受け取った文字列そのもの**を
     * 積んで突き合わせる —— 種別や前方一致で緩めると、別の本物のエラーまで一緒に
     * 見逃す(この検査は「失敗しているのに緑」を止める最後の砦なので緩めない)。
     */
    const expectedErrors = []
    page.on('pageerror', (error) => consoleErrors.push(String(error)))
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    page.on('response', (response) => {
      if (response.status() >= 400) consoleErrors.push(`HTTP ${response.status()} ${response.url()}`)
    })

    await page.goto(url, { waitUntil: 'load', timeout })
    await page.waitForFunction(() => Boolean(window.__duocad && window.__duocad.ready), {
      timeout,
      polling: 100,
    })
    if ((await page.evaluate(() => window.__duocad.parts.length)) !== 0) {
      fail('空の文書で起動していない(サーバを起動し直す)')
    }
    ok('空の文書で起動した')

    // ------------------------------------------------------------------ 道具
    const look = async () =>
      JSON.parse(
        await page.evaluate(() => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch) ?? null
          return JSON.stringify({
            bodies: hook.parts.map((body) => ({
              id: body.body_id,
              volume: body.volume,
              faces: body.face_count,
              kind: body.kind,
              visible: body.visible,
            })),
            activeSketch: hook.activeSketch,
            // 文書にあるスケッチの件数(即適用の検査が「増えていないこと」を数える)
            sketches: hook.sketches.length,
            entities: active?.entities?.length ?? 0,
            drawn: active?.drawn?.length ?? 0,
            regions: active?.regions?.length ?? 0,
            parameters: hook.parameters,
            redoCount: hook.redoCount,
            lastFile: hook.lastFile,
            documentList: hook.documentList,
            currentDocumentName: hook.currentDocumentName,
            log: hook.state.log.length,
            messages: hook.messages,
            // 走行中に出たエラーの控え(`App.svelte` の `errorLog`)。**トーストは8秒で消える**ので、
            // 最後に一度だけ見る下の検査は、画面のDOMではなくこちらを見る必要がある。
            errors: hook.errors,
            toasts: Array.from(document.querySelectorAll('#toasts .toast-text')).map((node) =>
              node.textContent.trim(),
            ),
          })
        }),
      )

    async function waitFor(predicate, what, ...args) {
      try {
        await page.waitForFunction(predicate, { timeout, polling: 50 }, ...args)
      } catch {
        fail(`${what}: ${JSON.stringify(await look())}`)
      }
    }

    /**
     * **何も無い所を素でクリックして全部外す**(`App.svelte` の `onPick`)。
     *
     * 人が「全解除」する手段はこれ1つである。
     * どの画素が空いているかは視点と部品の形で変わるので、四隅を順に試して
     * **実際に0件になった時点で成功**とする(取れた画素を決め打ちしない)。
     */
    async function clearSelectionByClickingNothing() {
      const rect = await page.evaluate(() => {
        const box = document.querySelector('.viewport').getBoundingClientRect()
        return { left: box.left, top: box.top, width: box.width, height: box.height }
      })
      // **ビューキューブの矩形を候補から外す**。キューブは右上のクリックを吸うので
      // `[0.97, 0.03]` がそのまま矩形の内側に入る。矩形は右上アンカーで**ビューポートの寸法が
      // 変わると動く**ので、座標を書き換えるのではなく毎回 `viewCubeRect()` に聞いて弾く
      // (座標を決め打ちで動かすと [[viewport-resize-silently-changes-what-a-click-grabs]] を踏む)。
      const cube = await page.evaluate(() => window.__duocad.viewCubeRect?.() ?? null)
      const insideCube = (x, y) =>
        cube !== null && x >= cube.x && x < cube.x + cube.w && y >= cube.y && y < cube.y + cube.h
      const candidates = [[0.97, 0.97], [0.03, 0.97], [0.97, 0.03], [0.5, 0.97]].filter(
        ([fx, fy]) => !insideCube(rect.width * fx, rect.height * fy),
      )
      if (candidates.length === 0) fail('空白クリックの候補が全部ビューキューブの矩形の下だった')
      for (const [fx, fy] of candidates) {
        await page.mouse.click(rect.left + rect.width * fx, rect.top + rect.height * fy)
        try {
          await page.waitForFunction(
            () => (window.__duocad.selection?.items ?? []).length === 0,
            { timeout: 2000, polling: 50 },
          )
          return
        } catch {
          // そこには部品が居た。次の隅を試す
        }
      }
      fail(`何も無い所のクリックで全解除できない: ${JSON.stringify(await look())}`)
    }

    /**
     * **起きないことを見る前の猶予**。適用が走るなら「クリック → WS往復 → 状態配信」で
     * 済む時間(実測で数十〜数百ms)を過ぎてから数える —— 待たずに数えると、
     * 走っていても「まだ返ってきていないだけ」を緑と読んでしまう。
     */
    const settle = () => new Promise((resolve) => setTimeout(resolve, 1000))

    async function typeInto(selector, value) {
      await page.click(selector)
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyA')
      await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await page.type(selector, String(value), { delay: 15 })
    }

    /**
     * **スケッチを作る2手**: ツールバーの「スケッチ」を起動 → 貼る先をクリック。
     *
     * Enterは無い(`command.ts` の `needsEnter`: 対象1つ・howの入力なし → 即適用)。
     * 座標は**クリックの直前に射影し直す**(スケッチ平面の点と同じ流儀)。
     * `projectBasePlane` は「その画素からのレイが本当にその平面に当たり、手前に面が
     * 無い」ことを確かめてから返すので、部品に隠れている所を押すことはない。
     */
    async function createSketchOnBase(name = 'XY') {
      await page.click('#sketch-create')
      let at = null
      try {
        const handle = await page.waitForFunction(
          (plane) => {
            const rect = document.querySelector('.viewport').getBoundingClientRect()
            const position = window.__duocad.projectBasePlane(plane)
            return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
          },
          { timeout, polling: 50 },
          name,
        )
        at = await handle.jsonValue()
      } catch {
        fail(`基準平面 ${name} を指せる画素が無い(「スケッチ」が起動しているか)`)
      }
      await page.mouse.click(at.x, at.y)
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
    async function clickPlane(u, v) {
      // **浮いているブラウザを畳んでから**押す —— スケッチ平面の (u,v) は画面の1画素しかなく、
      // そこがパネルの下に来ると逃げ場が無い(理由は `browser.mjs` の `withoutBrowserPanel`)。
      return withoutBrowserPanel(page, async () => {
        const at = await page.evaluate(
          (uu, vv) => {
            const rect = document.querySelector('.viewport').getBoundingClientRect()
            const position = window.__duocad.projectSketchPoint(uu, vv)
            return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
          },
          u,
          v,
        )
        if (!at) fail(`スケッチ平面の点 (${u}, ${v}) が画面の中に無い`)
        await page.mouse.click(at.x, at.y)
      })
    }

    async function clickRegion(index) {
      const at = await page.evaluate((idx) => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const position = window.__duocad.projectRegion(idx)
        return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
      }, index)
      if (!at) fail(`領域 ${index} が画面の中に無い`)
      await page.mouse.click(at.x, at.y)
    }

    /** 領域を全部選ぶ(反復した穴をまとめて切り取るため)。 */
    async function selectAllRegions(count) {
      await clickRegion(1)
      for (let index = 2; index <= count; index += 1) {
        const at = await page.evaluate((idx) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectRegion(idx)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        }, index)
        if (!at) fail(`領域 ${index} が画面の中に無い`)
        await page.keyboard.down('Shift')
        await page.mouse.click(at.x, at.y)
        await page.keyboard.up('Shift')
      }
      await waitFor(
        (expected) =>
          // **領域だけ**(`sketch_entity` も `sketch` を持つ)
          (window.__duocad.selection?.items ?? []).filter(
            (item) => 'sketch' in item && !('entity' in item),
          ).length === expected,
        `領域を${count}個選べない`,
        count,
      )
    }

    // ================================================================ 板を作る
    await createSketchOnBase('XY')
    await waitFor(() => window.__duocad.activeSketch !== null, 'XY平面のスケッチに入れない')
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
      '板の領域ができない',
    )
    await page.keyboard.press('Escape')
    await page.click('#sketch-look')
    await clickRegion(1)
    await page.click('#extrude-regions')
    await typeInto('#regions-distance', PLATE.thickness)
    await page.click('#extrude-mode-join')
    await page.keyboard.press('Enter')
    await waitFor(() => window.__duocad.parts.length === 1, '板ができない')
    await page.click('#sketch-exit')
    await frameAll(page)
    const plateVolume = (await look()).bodies[0].volume
    ok(`板を作った(体積 ${plateVolume})`)

    // ================================================================ 検証1: 追随
    // 穴を1つ描いて矩形反復で3つにし、まとめて切り取ってから**径を1箇所だけ**打ち直す。
    await createSketchOnBase('XY')
    await waitFor(() => window.__duocad.sketches.length === 2, '穴のスケッチに入れない')
    // **後段の径打ち直しが使う**: このスケッチは押し出し後に一度抜けるので、SketchPanelで
    // 打ち直すには「再開」(BrowserPanelの名前クリック)で戻る必要がある
    // (ParametersPanel廃止に伴う受け入れた損失)。
    const holeSketchId = await page.evaluate(() => window.__duocad.activeSketch)
    await page.click('#sketch-circle')
    await clickPlane(HOLE.u, HOLE.v)
    await typeInto('#sketch-radius', HOLE.radius)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === 1)
      },
      '穴の図形が置けない',
    )
    await page.keyboard.press('Escape')

    const entityId = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      return active.entities[0].entity_id
    })
    // **反復の対象は3Dのクリックで選ぶ**。スケッチの図形そのものが選択の一種
    // (`sketch_entity`)である —— 反復の `entities` 引数はその選択から導かれる。
    await clickEntityHandle(entityId)
    await page.click('#pattern-rectangular')
    await typeInto('#pattern-dx', HOLE.dx)
    await typeInto('#pattern-dy', 0)
    await typeInto('#pattern-nx', HOLE.count)
    await typeInto('#pattern-ny', 1)
    await page.keyboard.press('Enter')
    await waitFor(
      (expected) => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.regions.length === expected)
      },
      '反復した穴の領域が3つにならない',
      HOLE.count,
    )
    await page.click('#sketch-look')
    await selectAllRegions(HOLE.count)
    await page.click('#extrude-regions')
    await typeInto('#regions-distance', PLATE.thickness)
    await page.click('#extrude-mode-cut')
    await page.keyboard.press('Enter')
    const holesVolume = Math.PI * HOLE.radius ** 2 * PLATE.thickness * HOLE.count
    await waitFor(
      (before, expected) => {
        const body = window.__duocad.parts[0]
        return Boolean(body && Math.abs(before - body.volume - expected) < expected * 1e-3)
      },
      '3つの穴が開かない',
      plateVolume,
      holesVolume,
    )
    await page.click('#sketch-exit')
    await frameAll(page)
    const withHoles = (await look()).bodies[0]
    ok(`反復した穴を3つまとめて開けた(体積 ${plateVolume} -> ${withHoles.volume.toFixed(2)})`)

    // **SketchPanelで径を1箇所だけ打ち直す**(ParametersPanel廃止。図形は押し出しの後で
    // スケッチの外に出ているので、「再開」でそのスケッチへ戻ってから図形の一覧を打つ)。
    await page.click(`[data-sketch-id="${holeSketchId}"] .sketch-name`)
    await waitFor(
      (id) => window.__duocad.activeSketch === id,
      '穴のスケッチへ再入場できない',
      holeSketchId,
    )
    // **径の打ち直しはAI(MCP)の口だけである**。
    // 見たいのは「1箇所直すと反復した穴が全部追随する」で、それはverbの性質であって
    // 入力欄の性質ではないので、同じ文書へMCPから打ち込んで確かめる。
    const radiusName = await page.evaluate((id) => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      return active.entities.find((entity) => entity.entity_id === id).refs.radius
    }, entityId)
    if (!radiusName) fail('穴の半径に名前つきの値が付いていない')
    if (mcp === null) fail('--attach では径の打ち直しを打てない(MCPの口が要る)')
    await mcp.handshake('duocad-smoke-files')
    await mcp.callTool('set_parameter', { name: radiusName, value: 3 })
    const biggerHoles = Math.PI * 3 ** 2 * PLATE.thickness * HOLE.count
    await waitFor(
      (before, expected) => {
        const body = window.__duocad.parts[0]
        return Boolean(body && Math.abs(before - body.volume - expected) < expected * 1e-3)
      },
      '径を打ち直しても穴が太くならない(追随していない)',
      plateVolume,
      biggerHoles,
    )
    const afterResize = (await look()).bodies[0]
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, '穴のスケッチを抜けられない')
    ok(
      `**径を1箇所だけ打ち直したら、反復した穴が全部追随した**(打ち込みはMCP経由)` +
        `(半径 ${HOLE.radius}→3 で体積 ${withHoles.volume.toFixed(2)} -> ${afterResize.volume.toFixed(2)})`,
    )

    // ================================================================ 検証5: undo/redoで位置が動く
    const beforeUndo = await look()
    await pressUndo(page)
    await waitFor(
      (expected) => window.__duocad.redoCount === expected,
      'undoしてもやり直せる件数が増えない',
      beforeUndo.redoCount + 1,
    )
    const undone = await look()
    if (undone.log !== beforeUndo.log - 1) {
      fail(`undoで履歴の有効件数が減っていない: ${beforeUndo.log} -> ${undone.log}`)
    }
    ok(`undoでやり直せる件数が増えた(${beforeUndo.redoCount} -> ${undone.redoCount})`)

    await pressRedo(page)
    await waitFor(
      (expected) => window.__duocad.redoCount === expected,
      'redoで位置が戻らない',
      beforeUndo.redoCount,
    )
    const redone = await look()
    if (redone.log !== beforeUndo.log) {
      fail(`redoで履歴の有効件数が戻っていない: ${beforeUndo.log} -> ${redone.log}`)
    }
    ok(`redoで戻った(有効件数 ${redone.log})`)

    // ================================================================ 検証2: 保存と再現
    // **保存は1操作ごとに自動で走る**(issue #5、2026-09-16)。名前を付ける前の文書は最初の
    // 変更で `untitled-<n>.json` が自動で付き、人はパネルの名前欄(`#file-name-input` +
    // `#file-rename`)で**改名**する。明示の保存ボタンは無く、ヘッダの `#save-state` は
    // 自動保存が書けなかったときだけ「未保存」を出す(普段は札そのものが無い)。
    // 保存/開くは `duocad/models/` 配下の**ファイル名の一覧**で、開くのは一覧の行
    // (`.files-row[data-name] .files-open`)。STEP読み込みはツールバーの `#file-import-step`、
    // STEP/STL書き出しは `#file-export-step-path` / `#file-export-stl-path`。
    const untitledName = 'untitled-1.json'
    const firstName = 'model.json'
    const firstPath = join(workdir, firstName) // startServer に modelsDir=workdir を渡してある
    await waitFor(
      (expected) => window.__duocad.currentDocumentName === expected,
      '最初の変更で無題の文書に名前が自動で付かない',
      untitledName,
    )
    if (!existsSync(join(workdir, untitledName))) fail(`自動で付いた名前のファイルが無い: ${untitledName}`)
    ok(`**最初の変更で無題の文書が ${untitledName} として自動保存された**`)

    // 明示の保存ボタンは無く、「未保存」の札も出ていない
    if ((await page.$('#file-save')) !== null) fail('明示の保存ボタン(#file-save)が残っている')
    const expectNoSaveTag = async (where) => {
      await settle()
      if ((await page.$('#save-state')) !== null) {
        fail(`${where}: 自動保存されているのにヘッダに「未保存」の札が出ている`)
      }
    }
    await expectNoSaveTag('起動して図形を作った直後')
    ok('明示の保存ボタンは無く、「未保存」の札も出ていない')

    // **一覧の操作数が今の文書に追いつくのを待つ**(自動保存が本当に書いたことの証拠。
    // 一覧は文書が変わるたびに取り直される —— `App.svelte` の `scheduleDocumentRefresh`)。
    const waitListMatchesDocument = (where, name) =>
      waitFor(
        (expected) => {
          const file = (window.__duocad.documentList ?? []).find((item) => item.name === expected)
          return Boolean(file && file.operations === window.__duocad.state.log.length)
        },
        `${where}: 一覧の操作数が今の文書に追いつかない(自動保存が書いていない)`,
        name,
      )
    await waitListMatchesDocument('自動保存の直後', untitledName)

    // ---- 改名: 名前欄に打って Enter(自動で付いた名前のファイルは移る = 残らない)
    await typeInto('#file-name-input', firstName)
    await page.keyboard.press('Enter')
    await waitFor(() => window.__duocad.lastFile?.action === 'rename', '改名の応答が返らない')
    await waitFor(
      (expected) => window.__duocad.currentDocumentName === expected,
      '改名後に現在のファイル名が更新されない',
      firstName,
    )
    if (!existsSync(firstPath)) fail(`改名したはずのファイルが無い: ${firstPath}`)
    if (existsSync(join(workdir, untitledName))) fail(`改名したのに元の ${untitledName} が残っている`)
    const headerName = await page.$eval('#current-file', (el) => el.textContent.trim())
    if (headerName !== firstName) fail(`ヘッダのファイル名が違う: "${headerName}"`)
    ok(`**名前欄で ${untitledName} → ${firstName} へ改名できた**(ヘッダ "${headerName}"。元の名前のファイルは残らない)`)

    const shapeBefore = (await look()).bodies[0]

    // **アプリを再読込しても編集中のファイル名が戻る**(名前を持っているのはサーバ)。
    await page.reload({ waitUntil: 'load', timeout })
    await page.waitForFunction(() => Boolean(window.__duocad && window.__duocad.ready), {
      timeout,
      polling: 100,
    })
    await waitFor(
      (expected) => window.__duocad.currentDocumentName === expected,
      '再読込するとヘッダから編集中のファイル名が消える',
      firstName,
    )
    ok(`**再読込しても編集中のファイル名が残った**("${firstName}" —— サーバが持っている)`)

    // ---- 開くと形が戻る(操作ログの再生)。**自動保存の下では編集中のファイルが常に今の文書の
    // 鏡なので、開き直しても違いが出ない**。そこで先に**別名で複製**してから undo し、複製の
    // 側を開いて元の形が戻ることを見る(複製の中身が「undo前の文書」の証拠になる)。
    // 再接続で一覧が届くのを先に待つ(`App.svelte` が `onStatus('open')` で取り直す)。
    const secondName = 'spare.json'
    const secondPath = join(workdir, secondName)
    await waitFor(
      (expected) => (window.__duocad.documentList ?? []).some((file) => file.name === expected),
      '再接続後にファイル一覧が届かない',
      firstName,
    )
    await page.click(`.files-row[data-name="${firstName}"] .files-copy`)
    await waitFor(() => document.activeElement?.id === 'file-copy-name', '複製の名前欄が出ない')
    await typeInto('#file-copy-name', secondName)
    await page.keyboard.press('Enter')
    await waitFor(
      (expected) => (window.__duocad.documentList ?? []).some((file) => file.name === expected),
      '複製したファイルが一覧に出ない',
      secondName,
    )
    // **undo は自動保存の引き金**: model.json だけが短くなり、spare.json は undo 前のまま。
    await pressUndo(page)
    await waitFor(
      (before) => Math.abs((window.__duocad.parts[0]?.volume ?? 0) - before) > 1e-6,
      'undoしても形が変わらない(開く検査の前提が作れない)',
      shapeBefore.volume,
    )
    await waitListMatchesDocument('undoの直後', firstName)
    await expectNoSaveTag('undoの直後')
    ok('**undo も自動保存され**(一覧の操作数が追随)、「未保存」の札は出ない')

    await page.click(`.files-row[data-name="${secondName}"] .files-open`)
    await waitFor(
      (expectedVolume) => {
        const body = window.__duocad.parts[0]
        return Boolean(body && Math.abs(body.volume - expectedVolume) < 1e-6)
      },
      '複製を開いても undo 前の形にならない',
      shapeBefore.volume,
    )
    const reopened = (await look()).bodies[0]
    if (reopened.faces !== shapeBefore.faces) {
      fail(`開いた形の面数が違う: ${shapeBefore.faces} -> ${reopened.faces}`)
    }
    await waitFor(
      (expected) => window.__duocad.currentDocumentName === expected,
      '開いた後に現在のファイル名が更新されない',
      secondName,
    )
    ok(
      `**複製 → undo → 複製を開く で undo 前の形が戻った**(体積 ${reopened.volume.toFixed(4)} / 面 ${reopened.faces}` +
        ' — 操作ログから再生された。確認ダイアログは挟んでいない)',
    )
    await expectNoSaveTag('開いた直後')

    // 以降の検査は spare.json を編集中、model.json を編集中でないファイルとして使う。
    const documentName = secondName
    const documentPath = secondPath
    const spareName = firstName
    const sparePath = firstPath

    // ================================================================ 検証2b: 上書きの口は無い
    // (a) Ctrl+S はブラウザの「ページを保存」ダイアログを止める(`preventDefault()`)。
    //     保存そのものは自動で済んでいるので、押しても一覧の操作数は変わらない。
    // (b) **編集中でないファイルは1バイトも書き換わらない** —— 名前欄は改名であり、
    //     既にある名前への改名はサーバが拒否する。
    //
    // **`preventDefault()` が効いていることを直に見る。** puppeteerが合成したキーで
    // ネイティブの「ページを保存」ダイアログが必ず開くとは限らないので、
    // 「先へ進めたから止まっている」では検査にならない —— `window` のバブル段階へ
    // **後から**足した聞き手は `App.svelte` の `onWindowKeydown` の後に走るので、
    // そこで `defaultPrevented` を読めば止めたかどうかが確実に分かる。
    await page.evaluate(() => {
      window.__ctrlS = null
      window.addEventListener('keydown', (event) => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
          window.__ctrlS = { prevented: event.defaultPrevented }
        }
      })
    })
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyS')
    await page.keyboard.up('Control')
    await waitFor(() => window.__ctrlS !== null, 'Ctrl+S のキーイベントが window まで届いていない(検査が空振りしている)')
    const ctrlS = await page.evaluate(() => window.__ctrlS)
    if (!ctrlS.prevented) fail('Ctrl+S を preventDefault() で止めていない(ブラウザの保存ダイアログが開く)')
    await waitListMatchesDocument('Ctrl+Sの直後', documentName)
    ok('Ctrl+S はブラウザの保存ダイアログを止め、文書とファイルは一致したまま')

    const spareBytes = readFileSync(sparePath)
    const errorsBefore = (await look()).errors.length
    await typeInto('#file-name-input', spareName)
    await page.keyboard.press('Enter')
    await waitFor(
      (before) => window.__duocad.errors.length > before,
      '既にある名前への改名が拒否されない(トーストが1枚も出ない)',
      errorsBefore,
    )
    const refusal = (await look()).errors[0]
    expectedErrors.push(refusal)
    if (!refusal.includes(spareName)) fail(`拒否のメッセージが対象ファイル名を言わない: ${refusal}`)
    if (!refusal.includes('既にある')) fail(`拒否のメッセージが理由を言わない: ${refusal}`)
    await settle() // 書き込みが起きるなら、この間に必ず落ちている
    if (!readFileSync(sparePath).equals(spareBytes)) {
      fail(`編集中でないファイルが書き換わった: ${sparePath}`)
    }
    const headerDuringRefusal = await page.$eval('#current-file', (el) => el.textContent.trim())
    if (headerDuringRefusal !== documentName) {
      fail(`拒否されたのに編集中のファイルが移った: "${headerDuringRefusal}"`)
    }
    ok(
      `**既にある ${spareName} への改名は拒否され、1バイトも書き換わらなかった**` +
        `(拒否 "${refusal}" / ヘッダは "${headerDuringRefusal}" のまま)`,
    )

    // ================================================================ 検証2c: 新規
    // 「新規」(`#file-new`)で**空の文書へ戻る**。確認ダイアログは無い(開くと同じ扱い。
    // 編集中の文書は自動保存されていて失うものが無い)。**元の形は一覧から開き直して
    // 取り戻す** —— この後の書き出しの検査が今の形を使う。
    const beforeNew = (await look()).bodies[0]
    await page.click('#file-new')
    await waitFor(
      () => window.__duocad.parts.length === 0 && window.__duocad.currentDocumentName === null,
      '「新規」で空の文書にならない',
    )
    const headerAfterNew = await page.$eval('#current-file', (el) => el.textContent.trim())
    if (headerAfterNew === documentName) fail(`新規にしてもヘッダに前のファイル名が残る: "${headerAfterNew}"`)
    // 無題で何も変えていない文書は書くものが無いので「未保存」ではない。
    await expectNoSaveTag('「新規」の直後')
    ok(`**「新規」で空の文書に戻った**(部品0 / ヘッダ "${headerAfterNew}"。確認ダイアログも「未保存」の札も無い)`)

    await page.click(`.files-row[data-name="${documentName}"] .files-open`)
    await waitFor(
      (expectedVolume) => {
        const body = window.__duocad.parts[0]
        return Boolean(body && Math.abs(body.volume - expectedVolume) < 1e-6)
      },
      '新規にした後、一覧から開き直しても同じ形にならない',
      beforeNew.volume,
    )
    ok('新規にした後も一覧から開き直せた(以降の書き出しの検査は元の形で続く)')

    // ================================================================ 検証2d: 一覧からの複製と削除(issue #17)
    // **どちらも文書を変えない**(ログ長・編集中のファイル名は動かない)。複製先は行内の欄に
    // `<元>-copy.json` が先埋めされ Enter で通る。削除は行内の2段階(「削除」→「本当に削除」)。
    // 拒否はどちらもサーバが言い、トースト1枚になる(`expectedErrors` へ積む)。
    const logBeforeFiles = (await look()).log
    const copyName = spareName.replace(/\.json$/, '-copy.json')
    const copyPath = join(workdir, copyName)
    await page.click(`.files-row[data-name="${spareName}"] .files-copy`)
    await waitFor(() => document.activeElement?.id === 'file-copy-name', '複製の名前欄が出ない(フォーカスが来ない)')
    const suggested = await page.$eval('#file-copy-name', (el) => el.value)
    if (suggested !== copyName) fail(`複製先の先埋めが違う: "${suggested}"`)
    await page.keyboard.press('Enter')
    await waitFor(
      (name) => (window.__duocad.documentList ?? []).some((file) => file.name === name),
      '複製したファイルが一覧に出ない',
      copyName,
    )
    if (!readFileSync(copyPath).equals(readFileSync(sparePath))) fail('複製の中身が元と違う')
    const afterCopy = await look()
    if (afterCopy.currentDocumentName !== documentName || afterCopy.log !== logBeforeFiles) {
      fail(`複製で編集中の文書が動いた: ${JSON.stringify({ name: afterCopy.currentDocumentName, log: afterCopy.log })}`)
    }
    ok(`**一覧の行から別名で複製できた**(先埋め "${suggested}" を Enter。中身は同一・編集中の文書は不変)`)

    // ---- 既にある名前への複製は拒否(1バイトも変わらない)
    const modelBytesBefore = readFileSync(documentPath)
    const errorsBeforeCopy = (await look()).errors.length
    await page.click(`.files-row[data-name="${spareName}"] .files-copy`)
    await waitFor(() => document.activeElement?.id === 'file-copy-name', '複製の名前欄が出ない(2回目)')
    await typeInto('#file-copy-name', documentName)
    await page.keyboard.press('Enter')
    await waitFor(
      (before) => window.__duocad.errors.length > before,
      '既にある名前への複製が拒否されない(トーストが出ない)',
      errorsBeforeCopy,
    )
    const copyRefusal = (await look()).errors[0]
    expectedErrors.push(copyRefusal)
    if (!copyRefusal.includes(documentName) || !copyRefusal.includes('既にある')) {
      fail(`複製の拒否文が対象と理由を言わない: ${copyRefusal}`)
    }
    await settle()
    if (!readFileSync(documentPath).equals(modelBytesBefore)) fail('既にあるファイルが複製で書き換わった')
    ok(`既にある名前への複製は拒否され、そのファイルは1バイトも変わらなかった("${copyRefusal}")`)

    // ---- 削除は行内の2段階
    await page.click(`.files-row[data-name="${copyName}"] .files-delete`)
    await page.waitForSelector(`.files-row[data-name="${copyName}"] .files-delete-confirm`, { timeout })
    if (!existsSync(copyPath)) fail('1段目の「削除」でもう消えている(確認を挟んでいない)')
    await page.click(`.files-row[data-name="${copyName}"] .files-delete-confirm`)
    await waitFor(
      (name) => !(window.__duocad.documentList ?? []).some((file) => file.name === name),
      '削除したファイルが一覧から消えない',
      copyName,
    )
    if (existsSync(copyPath)) fail('一覧から消えたのにディスクに残っている')
    const afterDelete = await look()
    if (afterDelete.currentDocumentName !== documentName || afterDelete.log !== logBeforeFiles) {
      fail('削除で編集中の文書が動いた')
    }
    ok('**一覧の行から削除できた**(行内の2段階。ディスクからも消え、編集中の文書は不変)')

    // ---- 編集中のファイルは削除できない(拒否の文がどうすればできるかを言う)
    const errorsBeforeDelete = (await look()).errors.length
    await page.click(`.files-row[data-name="${documentName}"] .files-delete`)
    await page.waitForSelector(`.files-row[data-name="${documentName}"] .files-delete-confirm`, { timeout })
    await page.click(`.files-row[data-name="${documentName}"] .files-delete-confirm`)
    await waitFor(
      (before) => window.__duocad.errors.length > before,
      '編集中のファイルの削除が拒否されない(トーストが出ない)',
      errorsBeforeDelete,
    )
    const deleteRefusal = (await look()).errors[0]
    expectedErrors.push(deleteRefusal)
    if (!deleteRefusal.includes(documentName) || !deleteRefusal.includes('編集中')) {
      fail(`削除の拒否文が対象と理由を言わない: ${deleteRefusal}`)
    }
    if (!/開く|新規/.test(deleteRefusal)) fail(`削除の拒否文がどうすればできるかを言わない: ${deleteRefusal}`)
    await settle()
    if (!existsSync(documentPath)) fail('編集中のファイルが消えた')
    ok(`**編集中のファイルは削除できない**("${deleteRefusal}")`)

    // ================================================================ 検証4: 書き出し
    const stepPath = join(workdir, 'out.step')
    await typeInto('#file-export-step-path', stepPath)
    await page.click('#file-export-step')
    await waitFor(
      () => window.__duocad.lastFile?.action === 'export_step',
      'STEP書き出しの応答が返らない',
    )
    if (!existsSync(stepPath) || statSync(stepPath).size === 0) fail('STEPが書けていない')
    ok(`STEPを書き出した(${statSync(stepPath).size} B)`)

    const stlPath = join(workdir, 'out.stl')
    await typeInto('#file-export-stl-path', stlPath)
    await typeInto('#file-deviation', 0.05)
    await page.click('#file-export-stl')
    await waitFor(
      () => window.__duocad.lastFile?.action === 'export_stl',
      'STL書き出しの応答が返らない',
    )
    const stl = (await look()).lastFile
    if (stl.deviation !== 0.05) fail(`偏差が渡っていない: ${stl.deviation}`)
    ok(`STLを書き出した(${statSync(stlPath).size} B / 偏差 ${stl.deviation}mm)`)

    // ================================================================ 検証3: 読み込み
    // 読み込んだ時点で編集可能なボディができる。
    const originalVolume = (await look()).bodies[0].volume
    const idsBefore = await page.evaluate(() =>
      window.__duocad.parts.map((body) => body.body_id),
    )
    const partsBefore = idsBefore.length
    // **STEP読み込みはツールバーのコマンド**。起動して初めてパス欄が現れるので
    // **押す→打つ→Enter**の順になる(`commands/files.ts` の `IMPORT_STEP`)。
    await page.click('#file-import-step')
    await typeInto('#file-import-path', stepPath)
    await page.keyboard.press('Enter')
    await waitFor(
      (previous) => window.__duocad.parts.length === previous + 1,
      'STEPを読み込めない',
      partsBefore,
    )
    const importedId = await page.evaluate((before) => {
      const added = window.__duocad.parts.filter((body) => !before.includes(body.body_id))
      return added[0]?.body_id ?? null
    }, idsBefore)
    if (!importedId) fail('読み込んでできた編集用ボディが特定できない')

    // **UIから出したSTEPを読み戻して体積が一致する**(タスクEの検証4の「UIから出しても同じ」)。
    // 許容差は相対 1e-6 —— §7の訂正どおり `==` では成立しない(再読込で数値積分の順序が変わる)。
    const importedVolume = await page.evaluate(
      (id) => window.__duocad.parts.find((body) => body.body_id === id).volume,
      importedId,
    )
    if (Math.abs(importedVolume - originalVolume) > Math.abs(originalVolume) * 1e-6) {
      fail(`STEP往復で体積が変わった: ${originalVolume} -> ${importedVolume}`)
    }
    ok(
      `STEPを読み込んだ(読み込んだボディはそのまま編集できる)。**読み戻した体積が一致**` +
        `(${originalVolume.toFixed(6)} vs ${importedVolume.toFixed(6)})`,
    )

    // **この後の検証5(表示中のみ書き出し)と即適用の検査が使う下ごしらえ。**
    // **先に他を隠して対象だけ表示に戻す** —— 読み込んだのは自分を書き出したSTEPなので、
    // 元のソリッドと同じ場所に重なっている(「手前を隠すと奥が選べる」= M3.5の可視性)。
    // そのうえで**読み込んだボディの面を1枚選ぶ**(読んだ時点で編集できることの実地確認でもある)。
    await page.click('#parts-hide-all')
    await waitFor(
      () => window.__duocad.parts.every((body) => !body.visible),
      '編集用ソリッドを隠せない',
    )
    await page.click(`.parts-row[data-body-id="${importedId}"] input.visible-toggle`)
    await waitFor(
      (id) => {
        const body = window.__duocad.parts.find((item) => item.body_id === id)
        if (!body?.visible) return false
        for (let index = 1; index <= 30; index += 1) {
          if (window.__duocad.projectFace(id, index)) return true
        }
        return false
      },
      '読み込んだボディの面が画面から指せない',
      importedId,
    )
    const facePoint = await page.evaluate((id) => {
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      for (let index = 1; index <= 30; index += 1) {
        const position = window.__duocad.projectFace(id, index)
        if (position) return { x: position.x + rect.left, y: position.y + rect.top, index }
      }
      return null
    }, importedId)
    if (!facePoint) fail('読み込んだボディの面が画面から指せない')
    await page.mouse.click(facePoint.x, facePoint.y)
    await waitFor(
      (id) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'face',
        ),
      '読み込んだボディの面を選べない',
      importedId,
    )

    // ================================================================ 検証5: 表示中のみ書き出し
    // **いまの状態を使い回す**(元の板 = 非表示、読み込んだボディ = 可視の1個だけ)。
    // 「表示中(visible)のBodyだけを書き出し、対象を選ぶ引数は無い」契約を実地で踏む。
    // 除外1件は「元の板」だけになるはず。
    const hiddenPlateId = idsBefore[0]
    const visibleOnlyStepPath = join(workdir, 'visible-only.step')
    await typeInto('#file-export-step-path', visibleOnlyStepPath)
    await page.click('#file-export-step')
    await waitFor(
      (expected) => window.__duocad.lastFile?.path === expected,
      '表示中のみのSTEP書き出しの応答が返らない',
      visibleOnlyStepPath,
    )
    const visibleOnlyStep = (await look()).lastFile
    if (JSON.stringify(visibleOnlyStep.body_ids) !== JSON.stringify([importedId])) {
      fail(`表示中のBodyだけが書き出されていない: body_ids=${JSON.stringify(visibleOnlyStep.body_ids)}`)
    }
    if (JSON.stringify(visibleOnlyStep.skipped_hidden) !== JSON.stringify([hiddenPlateId])) {
      fail(
        `非表示で除外したBodyが応答と一致しない: skipped_hidden=` +
          `${JSON.stringify(visibleOnlyStep.skipped_hidden)}(期待 [${hiddenPlateId}])`,
      )
    }
    if (!existsSync(visibleOnlyStepPath) || statSync(visibleOnlyStepPath).size === 0) {
      fail('表示中のみのSTEPが書けていない')
    }

    const visibleOnlyStlPath = join(workdir, 'visible-only.stl')
    await typeInto('#file-export-stl-path', visibleOnlyStlPath)
    await typeInto('#file-deviation', 0.1)
    await page.click('#file-export-stl')
    await waitFor(
      (expected) => window.__duocad.lastFile?.path === expected,
      '表示中のみのSTL書き出しの応答が返らない',
      visibleOnlyStlPath,
    )
    const visibleOnlyStl = (await look()).lastFile
    if (JSON.stringify(visibleOnlyStl.body_ids) !== JSON.stringify([importedId])) {
      fail(`STLも表示中のBodyだけになっていない: body_ids=${JSON.stringify(visibleOnlyStl.body_ids)}`)
    }
    if (JSON.stringify(visibleOnlyStl.skipped_hidden) !== JSON.stringify([hiddenPlateId])) {
      fail(`STLの除外Bodyが応答と一致しない: skipped_hidden=${JSON.stringify(visibleOnlyStl.skipped_hidden)}`)
    }

    // トーストに「書き出した個数」と「非表示で除外した個数」が出ることを実地で見る
    // (`App.svelte` の `handleFile`。除外0件のときは文言そのものを出さないので、
    // ここでは除外がある場合の文言を固定する)。
    const visibleOnlyMessage = (await look()).messages[0] ?? ''
    if (!visibleOnlyMessage.includes('1個') || !visibleOnlyMessage.includes('除外')) {
      fail(`トーストに除外件数が出ていない: ${JSON.stringify((await look()).messages)}`)
    }
    ok(
      `**表示中のBodyだけ**がSTEP/STLへ書き出された(非表示の1個は除外)。` +
        `トースト: "${visibleOnlyMessage}"`,
    )

    // ---------------------------------------------------------- 即適用
    // Enterの要らないコマンド(`command.ts` の `needsEnter` が false)の骨格を固定する。
    // 材料は**面に貼る `sketch_create`**(対象は貼る先1つ・howの入力なし)。
    //   (a) 面が選ばれた状態で**起動しても発火しない**
    //   (b) **起動した後の面クリック**で発火する(Enterを押さない)
    //   (c) 起動 → Esc のあとは発火しない
    // (a)(c) は「起きないこと」の検査なので、往復ぶんの猶予を与えてから見る。
    //
    // **1点だけ弱くなったことを書いておく**: 旧コマンドは対象(面)が起動より前から揃って
    // いられたので、「揃っていても即適用は発火せず、Enterが唯一の適用手段として残る」まで
    // 見られた。`sketch_create` の貼る先は `App.svelte` の `onPick` からしか埋まらない
    // (`useSelectedFaceAsPlane`)ので、起動前から揃っている状態を作れる即適用コマンドは
    // いま1つも無い。(a) は「起動しただけで走らない」に絞られる。
    const sketchesBefore = (await look()).sketches
    // **数えるものが数値で来ることを先に確かめる。** ここが `undefined` に化けると
    // 下の3つの比較が `undefined !== undefined` で全部素通りし、**検査が黙って死ぬ**
    // (実際に踏んだ: `look()` が返すのは件数であって配列ではない)。
    if (typeof sketchesBefore !== 'number') {
      fail(`スケッチの件数が数値で来ない(検査が黙って死ぬ): ${JSON.stringify(sketchesBefore)}`)
    }
    await page.click('#sketch-create')
    await settle()
    if ((await look()).sketches !== sketchesBefore) {
      fail('(a) 起動しただけでスケッチが走った(即適用が起動時に発火している)')
    }
    ok('(a) 面が選ばれた状態で起動しても発火しない(起動即適用の事故が構造的に無い)')

    // (c) 起動してすぐEscで降りたら、その後の面クリックでは発火しない
    await page.keyboard.press('Escape')
    await clearSelectionByClickingNothing()
    await page.click('#sketch-create')
    await page.keyboard.press('Escape')
    await page.mouse.click(facePoint.x, facePoint.y)
    await settle()
    if ((await look()).sketches !== sketchesBefore) {
      fail('(c) Escで降りた後の面クリックでスケッチが走った')
    }
    ok('(c) 起動 → Esc のあとの面クリックでは発火しない')

    // (b) 起動した後に面をクリックすると、Enterを押さずに適用される
    await clearSelectionByClickingNothing()
    await page.click('#sketch-create')
    await page.mouse.click(facePoint.x, facePoint.y)
    await waitFor(
      (expected) => window.__duocad.sketches.length === expected,
      '(b) 起動後の面クリックで即適用されない',
      sketchesBefore + 1,
    )
    // **面を起点に貼れたことまで見る** —— 基準平面のグリッドに取られていると
    // `setBasePlane` の同期経路になり、ここで見たい「選択が届いてから送る」待ち受け
    // (`CommandHost` の `selfSelections` を数える `$effect`)を踏んでいない。
    // **`JSON.stringify` はブラウザの中でやる** —— スケッチの `plane` はSvelteの
    // リアクティブなプロキシで、そのまま返すとCDPの直列化が空オブジェクトに潰す(実測)。
    const immediatePlane = JSON.parse(
      await page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return JSON.stringify(active?.plane ?? null)
      }),
    )
    if (immediatePlane?.kind !== 'face') {
      fail(`(b) 面ではなく ${JSON.stringify(immediatePlane)} に貼られた(即適用の経路が違う)`)
    }
    ok('(b) **起動 → 面をクリック**だけで面にスケッチを貼れた(Enterを押していない)')
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'スケッチから抜けられない')

    // ---------------------------------------------------------- (d) 同一平面の同着
    // **基準平面に描いて押し出した面は、その基準平面とぴったり重なる。** `resolveHit` は
    // 面と基準平面を「レイ距離が近い方」で決めるので、この重なりでは2つの距離が同着になり、
    // 素の `<` では**勝敗が浮動小数の最後の桁で決まる**。実際、`fitDistance` を数学的に
    // 同値な式へ書き換えただけ(カメラ位置の差 7e-14)で、上の(b)が面ではなくXZ基準平面を
    // 取って赤くなった —— 見た目も `projectFace` が返す画素も同じままで、である。
    //
    // **(b)を1回通すだけでは検出力が無い**(同着はどちらに転んでもおかしくないので、
    // たまたま面が勝っていれば緑になる)。そこで**画角を振ってカメラを視線上で動かし**、
    // 丸め方の違う位置を何通りも作って、そのすべてで面が勝つことを見る。画角を変えても
    // 見かけの大きさは保たれる(`viewport.ts` の `setFov`)ので、同じ面を指し続けられる。
    // **面の画素は毎回測り直す** —— カメラが動くと同じ面でも画素が変わる。
    //
    // **実測**: 同着に決着を付けない実装で、この6通りのうち**51度と55度**が
    // XZ基準平面を取った(45/47/49/53度は面。同じ実装で上の(b)も平面を取っている ——
    // (b)も画角45なのに(d)の45度は面で、**同じ画角でもクリックごとに転ぶ**)。決着を
    // 付けた後は6通りとも面である。**ここは丸めの当たり外れなので、通ったからといって
    // 角度の本数を減らさないこと。**
    const tieAngles = [45, 47, 49, 51, 53, 55]
    const tieResults = []
    for (const angle of tieAngles) {
      await page.evaluate((deg) => window.__duocad.setFov(deg), angle)
      await waitFor((deg) => window.__duocad.fov === deg, '画角が変わらない', angle)
      // **件数はこの回の開始時から数える。** (b)が貼ったスケッチが残っているので、
      // `sketchesBefore + 1` を待つと**クリックの前に条件が満たされている**(実測で踏んだ)。
      const before = await page.evaluate(() => window.__duocad.sketches.length)
      await clearSelectionByClickingNothing()
      await page.click('#sketch-create')
      // **使う直前に測り直す**(カメラが動いているので、前の画角の画素は使えない)。
      const at = await page.evaluate((id, index) => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const position = window.__duocad.projectFace(id, index)
        return position === null ? null : { x: position.x + rect.left, y: position.y + rect.top }
      }, importedId, facePoint.index)
      if (at === null) fail(`(d) 画角${angle}で面${facePoint.index}が画面から指せない`)
      await page.mouse.click(at.x, at.y)
      // **「増えた」ではなく「入った」で待つ** —— 即適用は貼ると同時にそのスケッチへ入るので、
      // `activeSketch` が立つことがこの手が届いた証拠になる。
      await waitFor(() => window.__duocad.activeSketch !== null, `(d) 画角${angle}で面クリックが届かない`)
      const kind = JSON.parse(
        await page.evaluate(() => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return JSON.stringify(active?.plane ?? null)
        }),
      )?.kind
      tieResults.push(`${angle}:${kind}`)
      await page.click('#sketch-exit')
      await waitFor(() => window.__duocad.activeSketch === null, `(d) 画角${angle}で抜けられない`)
      // **1本ずつ戻す**。残すと以降の件数の比較が全部ずれる。
      await pressUndo(page)
      await waitFor(
        (expected) => window.__duocad.sketches.length === expected,
        `(d) 画角${angle}のスケッチをundoで戻せない`,
        before,
      )
    }
    await page.evaluate(() => window.__duocad.setFov(45))
    await waitFor(() => window.__duocad.fov === 45, '画角を45へ戻せない')
    const tieBad = tieResults.filter((entry) => !entry.endsWith(':face'))
    if (tieBad.length > 0) {
      fail(
        `(d) 基準平面と同一平面の面が、画角によっては平面に取られる: ${tieBad.join(' / ')}` +
          `(全結果 ${tieResults.join(' / ')})`,
      )
    }
    ok(`(d) **基準平面と重なった面は、カメラを動かしても面が取れる**(画角 ${tieAngles.join('/')} の6通り)`)

    // **画面に出たエラーを見落とさない**。サーバのエラーはトーストへ出るだけで、
    // ブラウザのコンソールには出ない —— ここを見ないと「失敗しているのに緑」が起きる
    // (open_document で実際に起きた)。
    // **札は8秒で消える**ので最後に一度だけ見るこの検査は控え(`errors`)を見る —— 画面のDOMを
    // 見ると、途中で出て消えたエラーを取りこぼして緑になる(通知欄より弱くなる)。
    const screen = await look()
    // **控えが配列で来ることを先に確かめる。** ここが `undefined` や object に化けると
    // 下の `length > 0` が永久に偽になり、**この検査が赤くならないまま死ぬ**
    // (穴を1つ足すたびに起きうる形なので、握らずに落とす)。
    if (!Array.isArray(screen.errors)) {
      fail(`__duocad.errors が配列で来ない(検査が黙って死ぬ): ${JSON.stringify(screen.errors)}`)
    }
    // **わざと出させた拒否のトーストだけを除く**(`expectedErrors`。上のdocstring)。
    // 除いた分が本当に出ていたことも見る —— 出ていないのに除外だけ積むと、
    // 「拒否されたはず」の検査が空振りしていても緑になる。
    const missing = expectedErrors.filter((message) => !screen.errors.includes(message))
    if (missing.length > 0) {
      fail(`出るはずの拒否のトーストが控えに無い: ${missing.join(' | ')}`)
    }
    const errors = screen.errors.filter((message) => !expectedErrors.includes(message))
    if (errors.length > 0) fail(`画面にエラーが出た: ${errors.join(' | ')}`)
    ok(`走行中に予期しないエラーのトーストは1枚も出ていない(わざと出させた拒否 ${expectedErrors.length} 枚を除く)`)

    if (consoleErrors.length > 0) {
      fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    }
    ok('ブラウザのコンソールにエラーなし')

    console.log('')
    console.log('smoke-files: PASS')
  } finally {
    await browser.close()
    if (server !== null) await server.stop()
  }
}

main().catch((error) => {
  console.error(`smoke-files: FAIL ${error.message}`)
  process.exit(1)
})
