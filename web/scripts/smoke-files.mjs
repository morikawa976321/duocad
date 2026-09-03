/**
 * 実ブラウザでのパネルとファイル操作の検査。
 *
 * `smoke-features.mjs` と同じく**空の文書**から始めて、人が手でやる順序を
 * 本物のクリックとキー入力で通す。見るのは5つ:
 *
 *   1. **穴の径を1箇所編集して、反復で作った全部の穴が追随する**(M3の存在理由)
 *   2. **保存 → 再読込 → 開く → 同じ形**(操作ログから再生される)。上書きは
 *      **ヘッダの保存ボタン(`#file-save`)と Ctrl+S だけ**で、行き先は常に
 *      「いま編集しているファイル」——**編集中でないファイルは1バイトも書き換わらない**
 *      (2026-08-30。一覧の行にあった「上書き」ボタンは撤去した)
 *   3. STEPを読み込む(その時点で編集可能なボディができる)
 *   4. STEP/STL を書き出し、**STEPは読み戻して体積が一致**(読み戻しは pytest 側)
 *   5. undo/redoでやり直せる件数と有効な操作数が実際に動く
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
  // 受け取るようになった(2026-08-22)。渡さないとリポジトリ直下の実 `models/` を
  // 検査が書き換えてしまうので、既存の使い捨てworkdirをそのまま流用する
  // (STEP/STL書き出しの `join(workdir, ...)` と同じディレクトリで、実在確認が楽になる)。
  const server = attachUrl === null ? await startServer({ wsPort, httpPort, modelsDir: workdir }) : null
  // **MCPの口**(2026-08-30)。数値の打ち直し(`set_parameter`)は人間側のUIを持たなく
  // なったので、その1手だけを同じプロセスのMCP側から打つ。`--attach` で外のサーバへ
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
     * 2026-08-23に選択パネルを廃したので、人が「全解除」する手段はこれ1つになった。
     * どの画素が空いているかは視点と部品の形で変わるので、四隅を順に試して
     * **実際に0件になった時点で成功**とする(取れた画素を決め打ちしない)。
     */
    async function clearSelectionByClickingNothing() {
      const rect = await page.evaluate(() => {
        const box = document.querySelector('.viewport').getBoundingClientRect()
        return { left: box.left, top: box.top, width: box.width, height: box.height }
      })
      // **ビューキューブの矩形を候補から外す**(2026-09-03)。キューブは右上のクリックを吸うので
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

    /**
     * スケッチの図形を**3Dのクリックで選ぶ**(2026-08-30。旧「図形一覧のボタン」の後継)。
     *
     * 図形の**最初の点**(円・円弧は中心、線分・ベジエは始点、長方形は左下)を狙う ——
     * 点は必ずその図形の上にあり、当たり判定も点が最優先なので確実に掴める。
     * 反復の `entities` 引数はこの選択から導かれる(`App.svelte` の `selectedEntities`)。
     */
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
    // **反復の対象は3Dのクリックで選ぶ**(2026-08-30)。図形一覧のボタン(旧
    // `#entity-<id>`)はSketchPanelごと廃止し、スケッチの図形そのものが選択の一種
    // (`sketch_entity`)になった —— 反復の `entities` 引数はその選択から導かれる。
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
    // **径の打ち直しはAI(MCP)の口だけになった**(2026-08-30に数値入力欄を廃止)。
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
    // 2026-08-22: 保存/開くは `duocad/models/` 配下の**ファイル名の一覧**へ変わった
    // (engine `duocad-save-location-fixed-folder`)。パスを打つ `#file-path` は
    // もう save/open には無い —— `FilesPanel.svelte` の新規保存欄(`#file-name-input`
    // + `#file-save-new`)と、一覧の行(`.files-row[data-name] .files-open`)を使う。
    // **2026-08-27にSTEP/STLの入出力も同じパネルへ移設**され、共用だった `#file-path` は
    // 廃止(`#file-import-path` / `#file-export-step-path` /
    // `#file-export-stl-path` の3つに分かれた)。
    // **2026-08-30に一覧の行から `.files-overwrite` を撤去**した —— 上書きの口は
    // ヘッダの保存ボタン(`#file-save`)と Ctrl+S だけになり、行き先は常に
    // 「いま編集しているファイル」1つに固定された(検証2bを参照)。
    const documentName = 'model.json'
    const documentPath = join(workdir, documentName) // startServer に modelsDir=workdir を渡してある
    await typeInto('#file-name-input', documentName)
    await page.click('#file-save-new')
    await waitFor(
      () => window.__duocad.lastFile?.action === 'save',
      '保存の応答が返らない',
    )
    const saved = (await look()).lastFile
    if (!existsSync(documentPath)) fail(`保存したはずのファイルが無い: ${documentPath}`)
    ok(`保存した(${saved.path} / ${saved.bytes} B / 操作 ${saved.operations} 件)`)

    // **現在のファイル名が画面に出る**(完了条件4)。ヘッダとパネルの両方が同じ名前を映す。
    await waitFor(
      (expected) => window.__duocad.currentDocumentName === expected,
      '保存後に現在のファイル名が更新されない',
      documentName,
    )
    const headerName = await page.$eval('#current-file', (el) => el.textContent.trim())
    if (headerName !== documentName) fail(`ヘッダのファイル名が違う: "${headerName}"`)
    ok(`現在のファイル名が画面に出た("${headerName}")`)

    // **保存状態はファイル名とは別の札**(`#save-state`)。名前が「どれを触っているか」で、
    // こちらは「それがファイルに載っているか」——書けた直後は「保存済み」でなければならない。
    const expectSaveState = (expected, where) =>
      waitFor(
        (want) => document.querySelector('#save-state')?.textContent.trim() === want,
        `${where}: ヘッダの保存状態が "${expected}" にならない`,
        expected,
      )
    await expectSaveState('保存済み', '新規保存の直後')
    ok('保存した直後のヘッダが「保存済み」になった')

    const shapeBefore = (await look()).bodies[0]

    // **アプリを再読込してから開く**(タスクEの検証2「再起動 → 開く → 同じ形状」)。
    // 画面の状態は全部捨てられ、形は操作ログの再生だけから戻ることになる。
    await page.reload({ waitUntil: 'load', timeout })
    await page.waitForFunction(() => Boolean(window.__duocad && window.__duocad.ready), {
      timeout,
      polling: 100,
    })

    // **再読込しても編集中のファイル名が戻る**(2026-08-30)。名前を持っているのはサーバ
    // (`state.current_name`)で、画面はその写しを出すだけ —— ブラウザ側だけが持っていた頃は、
    // 文書はサーバに残るのに名前だけ失われ、**ヘッダが「(無題)」なのに保存だけは通る**という
    // 説明のつかない画面になっていた。
    await waitFor(
      (expected) => window.__duocad.currentDocumentName === expected,
      '再読込するとヘッダから編集中のファイル名が消える',
      documentName,
    )
    const headerAfterReload = await page.$eval('#current-file', (el) => el.textContent.trim())
    if (headerAfterReload !== documentName) fail(`再読込後のヘッダが違う: "${headerAfterReload}"`)
    ok(`**再読込しても編集中のファイル名が残った**("${headerAfterReload}" —— サーバが持っている)`)

    // **開く前に形を変えておく**。サーバは長寿命で、再読込しても文書はそのまま残る ——
    // 保存した形と同じままで「開く」を押すと、**開くのが失敗しても体積が一致してしまい
    // 検査が素通りする**(2026-08-14に実際に踏んだ: open_document が例外で落ちていたのに
    // このチェックが緑だった)。undoで形を変えてから開き、戻ることを見る。
    await pressUndo(page)
    await waitFor(
      (before) => Math.abs((window.__duocad.parts[0]?.volume ?? 0) - before) > 1e-6,
      'undoしても形が変わらない(開く検査の前提が作れない)',
      shapeBefore.volume,
    )
    ok('開く前に undo で形を変えた(開いて戻ることを見るため)')

    // **再接続で一覧が届く**(`App.svelte` が `onStatus('open')` で `refreshDocuments()` を
    // 呼ぶ)。一覧に出ていることを先に見てから、クリックで開く —— **確認ダイアログは無い**
    // (README「省けるクリックは徹底的に省く」)。
    await waitFor(
      (expected) => (window.__duocad.documentList ?? []).some((file) => file.name === expected),
      '再接続後にファイル一覧が届かない',
      documentName,
    )
    ok('再接続後、一覧に保存したファイルが出ている')

    await page.click(`.files-row[data-name="${documentName}"] .files-open`)
    await waitFor(
      (expectedVolume) => {
        const body = window.__duocad.parts[0]
        return Boolean(body && Math.abs(body.volume - expectedVolume) < 1e-6)
      },
      '開いても同じ形にならない',
      shapeBefore.volume,
    )
    const reopened = (await look()).bodies[0]
    if (reopened.faces !== shapeBefore.faces) {
      fail(`開いた形の面数が違う: ${shapeBefore.faces} -> ${reopened.faces}`)
    }
    ok(
      `**保存 → 再読込 → 一覧から開く で同じ形が戻った**(体積 ${reopened.volume.toFixed(4)} / 面 ${reopened.faces}` +
        ' — 操作ログから再生された。確認ダイアログは挟んでいない)',
    )
    await waitFor(
      (expected) => window.__duocad.currentDocumentName === expected,
      '開いた後に現在のファイル名が更新されない',
      documentName,
    )
    ok('開いた後も現在のファイル名が画面に出ている')
    // 開いた直後の文書は**ファイルの中身そのもの**なので「保存済み」から始まる
    // (再読込で `savedAt` は捨てられているから、これは開いたことで入り直した値である)。
    await expectSaveState('保存済み', '開いた直後')
    ok('開いた直後のヘッダが「保存済み」になった')

    // ================================================================ 検証2b: 編集中のファイルへ上書き
    // **一覧の行の「上書き」は2026-08-30に撤去した**(engine
    // `2026-08-30-duocad-save-becomes-a-header-button`)—— A を編集中に B の行を押せば
    // 確認も無く B が今の文書で潰れ、操作ログが唯一の正である以上それは復旧できなかった。
    // 上書きの口は**ヘッダの保存ボタン(`#file-save`)と Ctrl+S だけ**になり、行き先は常に
    // 「いま編集しているファイル」1つに固定されている。ここで見るのは3つ:
    //   (a) ヘッダの保存ボタンで、編集中のファイルへ書ける
    //   (b) Ctrl+S が同じ操作(ブラウザの保存ダイアログは出ない)
    //   (c) **編集中でないファイルは1バイトも書き換わらない**(このタスクの存在理由)
    //
    // 「本当に書き直った」は**一覧の操作数が今の文書に追いついたか**で見る ——
    // `lastFile.action === 'save'` は直前の新規保存で既に真なので待てない。
    const waitListMatchesDocument = (where) =>
      waitFor(
        (expected) => {
          const file = (window.__duocad.documentList ?? []).find((item) => item.name === expected)
          return Boolean(file && file.operations === window.__duocad.state.log.length)
        },
        `${where}: 一覧の操作数が今の文書に追いつかない(書き直っていない)`,
        documentName,
      )
    const listedOperations = async () =>
      (await look()).documentList.find((file) => file.name === documentName)?.operations ?? null

    const beforeOverwrite = (await look()).documentList.find((file) => file.name === documentName)
    if (!beforeOverwrite) fail('上書き前の一覧に対象ファイルが無い')
    await pressUndo(page) // 形を変える(上書きが実際に効いたかを見分けるため)
    await waitFor(
      (before) => Math.abs((window.__duocad.parts[0]?.volume ?? 0) - before) > 1e-6,
      'undoしても形が変わらない(上書き検査の前提が作れない)',
      reopened.volume,
    )
    // **開いた後に文書を動かしたので「未保存」へ落ちる**。ここは書き出しではなく編集なので、
    // ファイルの中身と今の文書が食い違った状態そのものである。
    await expectSaveState('未保存', '開いた後にundoした直後')
    ok('開いた文書を編集したらヘッダが「未保存」へ落ちた')

    // ---- (a) ヘッダの保存ボタン
    await page.click('#file-save')
    await waitListMatchesDocument('ヘッダの保存ボタン')
    await expectSaveState('保存済み', 'ヘッダの保存ボタンの直後')
    ok(
      `**ヘッダの保存ボタン(#file-save)で編集中のファイルへ上書きできた**` +
        `(操作 ${beforeOverwrite.operations} -> ${await listedOperations()} 件)`,
    )

    // ---- (b) Ctrl+S
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
    await pressRedo(page)
    await waitFor(
      (before) => Math.abs((window.__duocad.parts[0]?.volume ?? 0) - before) < 1e-6,
      'redoで形が戻らない(Ctrl+Sの検査の前提が作れない)',
      reopened.volume,
    )
    await expectSaveState('未保存', 'redoで形を戻した直後')
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyS')
    await page.keyboard.up('Control')
    await waitListMatchesDocument('Ctrl+S')
    await expectSaveState('保存済み', 'Ctrl+Sの直後')
    const ctrlS = await page.evaluate(() => window.__ctrlS)
    if (ctrlS === null) fail('Ctrl+S のキーイベントが window まで届いていない(検査が空振りしている)')
    if (!ctrlS.prevented) fail('Ctrl+S を preventDefault() で止めていない(ブラウザの保存ダイアログが開く)')
    ok(
      `**Ctrl+Sも同じ上書きになった**(操作 ${await listedOperations()} 件。` +
        'ブラウザの保存ダイアログは preventDefault() で止まっている)',
    )

    // ---- (c) 編集中でないファイルは1バイトも書き換わらない
    // もう1枚ファイルを作り、`model.json` へ戻ってから、その名前で「新規保存」を試みる。
    // **中身の検査はNode側でファイルを読んで行う** —— 拒否されると一覧は取り直されない
    // (`App.svelte` の `handleFile` は save の応答でしか `refreshDocuments()` を呼ばない)
    // ので、画面の写しを見比べても「変わっていないように見える」だけになる。
    const spareName = 'spare.json'
    const sparePath = join(workdir, spareName)
    await typeInto('#file-name-input', spareName)
    await page.click('#file-save-new')
    await waitFor(
      (expected) => window.__duocad.currentDocumentName === expected,
      '新しい名前で保存できない',
      spareName,
    )
    if (!existsSync(sparePath)) fail(`新しい名前で保存したファイルが無い: ${sparePath}`)
    const spareBytes = readFileSync(sparePath)

    // `model.json` を開き直す(= `spare.json` が「編集中ではない」ファイルになる)
    await page.click(`.files-row[data-name="${documentName}"] .files-open`)
    await waitFor(
      (expected) => window.__duocad.currentDocumentName === expected,
      '一覧から model.json へ戻れない',
      documentName,
    )
    // **今の文書を spare.json の中身と違う長さにする** —— 同じままだと、
    // 誤って上書きされても中身が変わらず、この検査が何も守らない。
    await pressUndo(page)
    await expectSaveState('未保存', '上書き拒否の検査に入る直前')

    const errorsBefore = (await look()).errors.length
    await typeInto('#file-name-input', spareName)
    await page.click('#file-save-new')
    await waitFor(
      (before) => window.__duocad.errors.length > before,
      '編集中でないファイルへの保存が拒否されない(トーストが1枚も出ない)',
      errorsBefore,
    )
    const refusal = (await look()).errors[0]
    expectedErrors.push(refusal)
    if (!refusal.includes(spareName)) fail(`拒否のメッセージが対象ファイル名を言わない: ${refusal}`)
    if (!refusal.includes('開け')) fail(`拒否のメッセージがどうすればできるかを言わない: ${refusal}`)
    await settle() // 書き込みが起きるなら、この間に必ず落ちている
    if (!readFileSync(sparePath).equals(spareBytes)) {
      fail(`編集中でないファイルが書き換わった: ${sparePath}`)
    }
    const headerDuringRefusal = await page.$eval('#current-file', (el) => el.textContent.trim())
    if (headerDuringRefusal !== documentName) {
      fail(`拒否されたのに編集中のファイルが移った: "${headerDuringRefusal}"`)
    }
    await expectSaveState('未保存', '上書きを拒否された直後')
    ok(
      `**編集中でない ${spareName} は1バイトも書き換わらなかった**` +
        `(拒否 "${refusal}" / ヘッダは "${headerDuringRefusal}" のまま)`,
    )

    // **この後の検査のために、ファイルと今の文書を揃え直す**(検証2cは
    // 「新規 → 一覧から開き直して同じ形」を見るので、食い違ったままにできない)。
    await page.click('#file-save')
    await waitListMatchesDocument('拒否の後の保存')
    await expectSaveState('保存済み', '拒否の後に保存し直した直後')

    // ================================================================ 検証2c: 新規
    // 「新規」(`#file-new`)で**空の文書へ戻る**。確認ダイアログは無い(開くと同じ扱い)。
    // **元の形は一覧から開き直して取り戻す** —— この後の書き出しの検査が今の形を使うので、
    // ここで空のままにできない。捨てた文書がundoでは戻らないことは pytest 側が固定する。
    const beforeNew = (await look()).bodies[0]
    await page.click('#file-new')
    await waitFor(
      () => window.__duocad.parts.length === 0 && window.__duocad.currentDocumentName === null,
      '「新規」で空の文書にならない',
    )
    const headerAfterNew = await page.$eval('#current-file', (el) => el.textContent.trim())
    if (headerAfterNew === documentName) fail(`新規にしてもヘッダに前のファイル名が残る: "${headerAfterNew}"`)
    ok(`**「新規」で空の文書に戻った**(部品0 / ヘッダ "${headerAfterNew}"。確認ダイアログは挟んでいない)`)
    // 新規の文書は**どこにも書かれていない**ので、名前が無いのと同じ理由で「未保存」。
    await expectSaveState('未保存', '「新規」の直後')
    ok('「新規」の直後のヘッダが「未保存」へ戻った')

    // ---- 編集中のファイルが無いときの保存ボタン(2026-08-30本人決定)
    // **`disabled` にしない** —— なぜ押せないかを言わないまま死ぬ札になり、新規の文書では
    // 必ず通る道だから。押したら**ファイルパネルを開いて名前欄へフォーカスを置く**
    // (「保存したい」への答えは「名前を付けろ」で、その場所まで連れて行く)。
    // **エラーにもしない**(トーストは出ない。出ていたら最後の検査が赤くなる)。
    await page.click('#data-panel-toggle') // わざと畳んでおく(開き直すことまで見る)
    await waitFor(() => document.querySelector('.data-panel') === null, 'ファイルパネルを畳めない')
    const saveDisabled = await page.$eval('#file-save', (el) => el.disabled)
    if (saveDisabled) fail('編集中のファイルが無いだけで保存ボタンが死んでいる(disabledにしない)')
    await page.click('#file-save')
    await waitFor(
      () => document.activeElement?.id === 'file-name-input',
      '編集中のファイルが無いのに、保存ボタンが名前欄へ連れて行かない',
    )
    ok('**編集中のファイルが無いときの保存ボタンは、ファイルパネルを開いて名前欄へ連れて行った**')

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

    // ================================================================ 検証3: 参照部品
    // 読み込んだ時点で編集可能なボディができ、参照ボディは隠れて画面から指せない
    // (隠したBodyは形状が配信されないので `projectFace` が null を返す)。
    const originalVolume = (await look()).bodies[0].volume
    const idsBefore = await page.evaluate(() =>
      window.__duocad.parts.map((body) => body.body_id),
    )
    const partsBefore = idsBefore.length
    await typeInto('#file-import-path', stepPath)
    await page.click('#file-import-step')
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

    const references = await page.evaluate(() => {
      const refs = window.__duocad.state.bodies.filter((body) => body.kind === 'reference')
      return {
        count: refs.length,
        hidden: refs.filter((body) => body.visible === false).length,
      }
    })
    if (references.count < 1) fail('参照ボディが増えていない')
    if (references.hidden !== references.count) {
      fail(`参照ボディが隠れていない: ${references.count} 個中 非表示 ${references.hidden}`)
    }
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
    // そのうえで**読み込んだボディの面を1枚選ぶ**(読んだ時点で編集できることの実地確認でもある
    // —— 参照ボディはパーツ一覧に出ず、面も指せない)。
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
    // 「表示中(visible)のBodyだけを書き出し、対象を選ぶ引数は無い」契約を実地で踏む
    // (2026-08-27 export-visible-only)。参照ボディはそもそも候補にならない
    // (kind==="solid"の候補があるので参照へのフォールバックは起きない)ので、
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

    // ---------------------------------------------------------- 即適用(2026-08-22)
    // Enterの要らないコマンド(`command.ts` の `needsEnter` が false)の骨格を固定する。
    // 材料は**面に貼る `sketch_create`**(対象は貼る先1つ・howの入力なし)。2026-08-27まで
    // ここは「編集用に複製」で見ていたが、そのコマンドが消えたので同じ配線を踏む方へ移した。
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
    // (2026-08-27に実際に踏んだ: `look()` が返すのは件数であって配列ではない)。
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

    // ---------------------------------------------------------- (d) 同一平面の同着(2026-08-28)
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
    // **実測(2026-08-28)**: 同着に決着を付けない実装で、この6通りのうち**51度と55度**が
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
    // (2026-08-14に open_document で実際に起きた)。
    // 2026-08-22にエラーの表示先がステータスバーからトーストへ移った。**札は8秒で消える**ので
    // 最後に一度だけ見るこの検査は控え(`errors`)を見る —— 画面のDOMを見ると、
    // 途中で出て消えたエラーを取りこぼして緑になる(通知欄より弱くなる)。
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
