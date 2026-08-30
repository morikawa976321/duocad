/**
 * 受入チェック — **18操作を人間側とAI側の両方で実際に動かす**。
 *
 * 「使える」の定義:
 *
 * > いま実装済みのverbすべてを、人間とAIの両方が満足に使える状態。
 *
 * このスクリプトは**判定の材料を作る**ものであり、合格を作りに行くものではない。
 * 各操作は独立して試し、落ちても止めずに ❌ と理由を残す(推測で ✅ にしない)。
 *
 *   duocad> node web/scripts/acceptance.mjs
 *
 * サーバは**このスクリプトが `python -m duocad.mcp_server` として空の文書で起動する**
 * (`smoke-mcp.mjs` と同じ形)。**1つのプロセスがMCPとWebSocketの両方を持つ**ので、
 * AIが叩いた結果が同じ文書としてブラウザに届く —— 交互操作の検証はこれが前提である。
 *
 * 見るのは3つ:
 *
 * 1. **到達表**: 18操作 × 人間/AI を実際に動かして ✅ / ❌
 * 2. **交互操作**: AIが述語で辺を提案 → ブラウザに提案として出る → 人がクリックで直す →
 *    AIが読み直して fillet → 形が変わる(**この企画の核心**)
 * 3. **副次**: 手だけで実物相当(全周円筒+円錐)を作る
 *
 * 結果はJSONで stdout の末尾へ出す。
 */
import { existsSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { findBrowser, launchBrowser, withoutBrowserPanel, pressUndo, pressRedo, frameAll } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'


const options = parseArgs(process.argv.slice(2))
const wsPort = options.port ?? '8885'
const httpPort = options['http-port'] ?? '8886'
const timeout = Number.parseInt(options.timeout ?? '120000', 10)
const workdir = options.workdir ?? mkdtempSync(join(tmpdir(), 'duocad-g-'))



/**
 * 到達表の18操作(実装計画書§0)。
 *
 * 2026-08-27に19から18へ減った —— 参照ボディから編集用ソリッドを作る複製の操作を
 * 削除し、STEP読み込み(`import_step`)が読んだ時点で編集可能なボディまで作るように
 * 統合したため。**減ったのはこの1つだけ**であること(他を削って通していないこと)が
 * 到達の根拠を保つ条件である。
 */
const VERBS = [
  'sketch_create',
  'add_line',
  'add_circle',
  'add_rectangle',
  'add_arc',
  'extrude',
  'extrude_face',
  'fillet',
  'chamfer',
  'pattern_rectangular',
  'pattern_circular',
  'set_parameter',
  'set_visible',
  'import_step',
  'export_step',
  'export_stl',
  'undo',
  'redo',
]

const results = { human: {}, ai: {}, interleaved: null, physical: null, fov: null, zup: null }

function record(side, verb, ok, note) {
  results[side][verb] = { ok, note }
  console.log(`  ${ok ? 'OK  ' : 'NG  '} ${side.padEnd(5)} ${verb.padEnd(20)} ${note}`)
}

/**
 * ❌ を1つでも残したまま終わったなら、その名前を全部並べる(空なら全部通っている)。
 *
 * **このスクリプトは落ちても止めない設計**(冒頭のdocstring「各操作は独立して試し、
 * 落ちても止めずに ❌ と理由を残す」)なので、合否は走り終わってから1回だけ判定する。
 * **判定をここに置かないと、❌ が並んでいても終了コードが0のまま**になり、
 * 呼び出し側(人・cron・drain)からは全部通ったのと区別が付かない —— 2026-08-28まで
 * 実際にそうなっていた。**出力は1行も減らさず、終了コードだけを変える。**
 */
function failedLabels(collected) {
  const failed = []
  for (const side of ['human', 'ai']) {
    for (const verb of VERBS) {
      const entry = collected[side][verb]
      // **未実行も失敗として数える。** 黙って飛ばされた操作は ❌ より始末が悪い。
      if (entry === undefined) failed.push(`${side}/${verb}(未実行)`)
      else if (!entry.ok) failed.push(`${side}/${verb}`)
    }
  }
  for (const name of ['interleaved', 'physical', 'fov', 'zup']) {
    const entry = collected[name]
    if (entry === null || entry === undefined) failed.push(`${name}(未実行)`)
    else if (!entry.ok) failed.push(name)
  }
  return failed
}

/** 1操作を試す。**落ちても止めない**(❌ と理由を残して次へ)。 */
async function attempt(side, verb, fn) {
  try {
    const note = await fn()
    record(side, verb, true, note ?? '')
  } catch (error) {
    record(side, verb, false, String(error.message ?? error).slice(0, 300))
  }
}

async function main() {
  const executablePath = findBrowser()

  console.log(`acceptance: ws=${wsPort} http=${httpPort} workdir=${workdir}`)
  // **`modelsDir: workdir`** —— 渡さないとリポジトリ直下の実 `models/` を受入が触る
  // (`server.mjs` の `startServer` 参照)。使い捨てdirは既に作ってあるのに、
  // ここだけ渡し忘れていた(2026-08-28に発見。他の実機検査5本は元から渡している)。
  const server = await startServer({ wsPort, httpPort, modelsDir: workdir })
  // 受入は落ちても最後まで走る作りなので、混入行は**記録して続ける**。
  const mcp = new McpClient(server.child, {
    timeout,
    onNoise: (line) => {
      results.stdoutNoise = [...(results.stdoutNoise ?? []), line.slice(0, 120)]
    },
  })
  let browser = null
  /** 人間側が最初に作ったスケッチのID(`set_parameter` が再開に使う)。 */
  let humanSketchId = null
  /** 最初にスケッチを抜けた直後の `camera.up`(下の「座標系」の節が判定する)。 */
  let upAfterFirstExit = null

  try {
    const initialized = await mcp.send('initialize', {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 'duocad-acceptance', version: '0.0.1' },
    })
    if (initialized.error) throw new Error(`initialize: ${JSON.stringify(initialized.error)}`)
    mcp.notify('notifications/initialized', {})
    const tools = (await mcp.send('tools/list')).result.tools.map((tool) => tool.name)
    console.log(`  MCP: ${tools.length} tools`)

    browser = await launchBrowser(executablePath)
    const page = await browser.newPage()
    await page.setViewport({ width: 1280, height: 900 })
    const consoleErrors = []
    page.on('pageerror', (error) => consoleErrors.push(String(error)))
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    await page.goto(`http://127.0.0.1:${httpPort}/`, { waitUntil: 'load', timeout })
    await page.waitForFunction(() => Boolean(window.__duocad && window.__duocad.ready), {
      timeout,
      polling: 100,
    })

    // ------------------------------------------------------------------ 道具(人間側)
    const look = async () =>
      JSON.parse(
        await page.evaluate(() => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch) ?? null
          return JSON.stringify({
            parts: hook.parts.map((body) => ({
              id: body.body_id,
              volume: body.volume,
              faces: body.face_count,
              visible: body.visible,
            })),
            bodies: hook.state.bodies.map((body) => ({ id: body.body_id, kind: body.kind, visible: body.visible })),
            activeSketch: hook.activeSketch,
            sketches: hook.sketches.length,
            entities: active?.entities?.length ?? 0,
            drawn: active?.drawn?.length ?? 0,
            regions: active?.regions?.length ?? 0,
            selection: hook.selection,
            parameters: hook.parameters,
            log: hook.state.log.length,
            redoCount: hook.redoCount,
            messages: hook.messages,
            // 走行中に出たエラーの控え(`App.svelte` の `errorLog`)。**トーストは8秒で消える**ので、
            // 記録に残すのは画面のDOMではなくこちら(2026-08-22にエラーの表示先が移った)。
            errors: hook.errors,
            lastFile: hook.lastFile,
          })
        }),
      )

    async function waitFor(predicate, what, ...args) {
      try {
        await page.waitForFunction(predicate, { timeout: 30000, polling: 50 }, ...args)
      } catch {
        throw new Error(`${what}: ${JSON.stringify(await look())}`)
      }
    }

    async function typeInto(selector, value) {
      await page.click(selector)
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyA')
      await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await page.type(selector, String(value), { delay: 12 })
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
        if (!at) throw new Error(`平面上の点 (${u}, ${v}) が画面の中に無い`)
        await page.mouse.click(at.x, at.y)
      })
    }

    async function clickRegion(index, shift = false) {
      const at = await page.evaluate((idx) => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const position = window.__duocad.projectRegion(idx)
        return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
      }, index)
      if (!at) throw new Error(`領域 ${index} が画面の中に無い`)
      if (shift) await page.keyboard.down('Shift')
      await page.mouse.click(at.x, at.y)
      if (shift) await page.keyboard.up('Shift')
    }

    async function clickEdge(bodyId, edgeIndex, shift = false) {
      const at = await page.evaluate(
        (id, index) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectEdge(id, index)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        bodyId,
        edgeIndex,
      )
      if (!at) return false
      if (shift) await page.keyboard.down('Shift')
      await page.mouse.click(at.x, at.y)
      if (shift) await page.keyboard.up('Shift')
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
        return true
      } catch {
        return false
      }
    }

    async function waitForEdges(bodyId) {
      await waitFor(
        (id) => {
          const hook = window.__duocad
          const body = hook.parts.find((item) => item.body_id === id)
          return Boolean(
            hook.wantsEdges && hook.edges && body && hook.edges.revision === body.revision,
          )
        },
        '辺が届かない',
        bodyId,
      )
    }

    // ================================================================ 人間側の18操作
    console.log('\n--- 人間側(ブラウザを本物のクリックとキーで操作) ---')

    await attempt('human', 'sketch_create', async () => {
      // **2手**(「スケッチ」を起動 → 貼る先の基準平面グリッドをクリック)。Enterは無い。
      await page.click('#sketch-create')
      const at = await page
        .waitForFunction(
          () => {
            const rect = document.querySelector('.viewport').getBoundingClientRect()
            const position = window.__duocad.projectBasePlane('XY')
            return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
          },
          { timeout, polling: 50 },
        )
        .then((handle) => handle.jsonValue())
      await page.mouse.click(at.x, at.y)
      await waitFor(() => window.__duocad.activeSketch !== null, 'スケッチに入れない')
      // **後段の `set_parameter` が使う**: このスケッチは `#sketch-exit` の後に一度
      // 抜けるので、SketchPanelで図形を打ち直すには「再開」(=BrowserPanelの名前クリック)で
      // 戻る必要がある(ParametersPanel廃止に伴う受け入れた損失)。
      humanSketchId = await page.evaluate(() => window.__duocad.activeSketch)
      return 'XY平面のグリッドをクリックしてスケッチを作り、そのままモードへ入った'
    })

    await attempt('human', 'add_rectangle', async () => {
      await page.click('#sketch-rectangle')
      await clickPlane(0, 0)
      await typeInto('#sketch-width', 60)
      await typeInto('#sketch-height', 40)
      await page.keyboard.press('Enter')
      await waitFor(
        () => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return Boolean(active && active.entities.some((entity) => entity.kind === 'rectangle'))
        },
        '長方形が置けない',
      )
      return '左下をクリック → 幅60・高さ40を打つ → Enter'
    })

    // **描いたものに視点を合わせてから続ける**。空のスケッチに入った直後のカメラは
    // 何も無い場面に合わせてあるので、原点から離れた点は画面の外になる(実測で踏んだ)。
    //
    // **矩形(u[0,60]・v[0,40])の外へ逃がす場所を、横(u<0/u>60)から下(v<0)へ変えた**
    // (2026-08-26、ファイル一覧が左サイドバーへ戻ってビューポートが240px狭まったため)。
    // 横方向は実測でu=70やu=-20が軒並み画面の外になった一方、縦方向(v)は
    // ウィンドウの幅を削っても変わらないFOVで決まるので、矩形の下端(v=0)からv=-10までの
    // 帯は今も見える(実測)。**この帯の中に置くのは、矩形と交差させないため** ——
    // `duocad/region.py` の領域計算は `BRepAlgoAPI_Splitter` で面をスケッチの辺すべてで
    // 割るので、閉じていない線分・円弧でも矩形の内側を横切らせると分割線になり、
    // 実際に「join produced an invalid shape」で押し出しが壊れることを実測で確認した。
    // 矩形の外に完全に離してあれば、線分・円弧は閉じていないので新しい領域を作らず、
    // 円だけが独立した小さな領域になる(以前の u=70/-20 配置と同じ理屈)。
    await page.click('#sketch-look')

    await attempt('human', 'add_line', async () => {
      await page.click('#sketch-line')
      await clickPlane(5, -8)
      await clickPlane(25, -8)
      // **Enterは押さない**(2026-08-22)。線分は対象が点2つ・howの入力が無いので
      // 2点目のクリックで即適用される(`command.ts` の `needsEnter`)。
      await waitFor(
        () => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return Boolean(active && active.entities.some((entity) => entity.kind === 'line'))
        },
        '線分が置けない',
      )
      return '2点をクリック(**2点目で即適用**。ゴムひもで確定前の線が見える)'
    })

    await attempt('human', 'add_circle', async () => {
      await page.click('#sketch-circle')
      await clickPlane(35, -5)
      await typeInto('#sketch-radius', 4)
      await page.keyboard.press('Enter')
      await waitFor(
        () => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return Boolean(active && active.entities.some((entity) => entity.kind === 'circle'))
        },
        '円が置けない',
      )
      return '中心をクリック → 半径4を打つ → Enter'
    })

    await attempt('human', 'add_arc', async () => {
      await page.click('#sketch-arc')
      await clickPlane(50, -5)
      await typeInto('#sketch-arc-radius', 3)
      await typeInto('#sketch-arc-start', 0)
      await typeInto('#sketch-arc-end', 120)
      await page.keyboard.press('Enter')
      await waitFor(
        () => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return Boolean(active && active.entities.some((entity) => entity.kind === 'arc'))
        },
        '円弧が置けない',
      )
      return '中心をクリック → 半径・開始角・終了角を打つ'
    })

    // 反復(スケッチモードのまま)
    await attempt('human', 'pattern_rectangular', async () => {
      await page.keyboard.press('Escape')
      const circleId = await page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return active.entities.find((entity) => entity.kind === 'circle').entity_id
      })
      await page.click(`[id="entity-${circleId}"]`)
      await waitFor(
        (id) => (window.__duocad.selectedEntities ?? []).includes(id),
        '図形を反復の対象に選べない',
        circleId,
      )
      await page.click('#pattern-rectangular')
      await typeInto('#pattern-dx', 0)
      await typeInto('#pattern-dy', -12)
      await typeInto('#pattern-nx', 1)
      await typeInto('#pattern-ny', 2)
      await page.keyboard.press('Enter')
      await waitFor(
        () => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return Boolean(active && active.drawn.length > active.entities.length)
        },
        '矩形反復が展開されない',
      )
      await pressUndo(page) // この後の受入で邪魔になるので戻す
      await waitFor(
        () => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return Boolean(active && active.drawn.length === active.entities.length)
        },
        '反復のundoが効かない',
      )
      return '図形を一覧から選ぶ → 間隔と個数を打つ(1×2で展開を確認し、undoで戻した)'
    })

    await attempt('human', 'pattern_circular', async () => {
      const circleId = await page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return active.entities.find((entity) => entity.kind === 'circle').entity_id
      })
      const picked = await page.evaluate(
        (id) => (window.__duocad.selectedEntities ?? []).includes(id),
        circleId,
      )
      if (!picked) await page.click(`[id="entity-${circleId}"]`)
      await page.click('#pattern-circular')
      await clickPlane(20, 45)
      await typeInto('#pattern-count', 4)
      await typeInto('#pattern-angle', 360)
      await page.keyboard.press('Enter')
      await waitFor(
        () => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return Boolean(active && active.drawn.length >= active.entities.length + 3)
        },
        '円形反復が展開されない',
      )
      await pressUndo(page)
      await waitFor(
        () => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return Boolean(active && active.drawn.length === active.entities.length)
        },
        '反復のundoが効かない',
      )
      return '回転中心をクリック → 個数4・全周360を打つ(展開を確認し、undoで戻した)'
    })

    // 領域押し出し(長方形の領域)
    await attempt('human', 'extrude', async () => {
      await page.keyboard.press('Escape')
      await page.click('#sketch-look')
      const target = await page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        // 長方形の領域(面積が最大のもの)を狙う
        return [...active.regions].sort((a, b) => b.area - a.area)[0].index
      })
      await clickRegion(target)
      await waitFor(
        () => (window.__duocad.selection?.items ?? []).some((item) => 'sketch' in item),
        '領域を選べない',
      )
      await page.click('#extrude-regions')
      await typeInto('#regions-distance', 10)
      await page.click('#extrude-mode-join')
      await page.keyboard.press('Enter')
      await waitFor(() => window.__duocad.parts.length === 1, '領域押し出しでソリッドができない')
      const volume = (await look()).parts[0].volume
      return `領域を選んで距離10・結合で押し出した(体積 ${volume.toFixed(2)})`
    })

    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, '最初のスケッチを抜けられない')
    // **抜けた直後の上方向を控える**(判定は下の「座標系」の節。完了条件5)。
    //
    // **ここが `setActiveSketch(null)` の「既定へ戻す」書き込みが効く唯一の経路**である。
    // 最初の板はこのスケッチの中で生まれた(=入った時点では何も無かった)ので、
    // `cameraBeforeSketch.framedAtEntry` が false になり「入る前の視点」への復帰は
    // 走らない —— 上方向を決めるのは既定へ戻す1行だけになる。2回目以降の出入りでは
    // 控えた up が復元されるので、**そちらで見てもこの1行の不具合は隠れる**。
    //
    // **`frameAll` より前に読む。** `frameAll` も up を+Zへ直すので、後で読むと隠れる。
    upAfterFirstExit = await page.evaluate(() => window.__duocad.cameraPose().up)
    await frameAll(page)
    const bodyId = (await look()).parts[0].id

    await attempt('human', 'extrude_face', async () => {
      const before = (await look()).parts[0].volume
      // **押す面は法線で名指しする**(手前=−Y の壁)。以前は「平面のうち最初に射影できた
      // もの」だったが、それだと**どの面を押したかが視点で変わる** —— 2026-08-28に視点を
      // Z-up の front-top-right へ変えたとき、それまで当たっていた +Y の壁が裏向きになり、
      // 別の壁(40×10)が選ばれて後続の形が丸ごと変わった(AI側の `fillet` が、できあがった
      // 形の都合で OCCT に IsDone=false で断られるところまで波及した)。**受入の筋書きは
      // カメラの向きに依存させない。**
      const face = await page.evaluate((id) => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const geometry = window.__duocad.getGeometry(id)
        const wall = geometry.faces.find((item) => item.kind === 'plane' && item.normal[1] < -0.9)
        if (wall === undefined) return null
        const position = window.__duocad.projectFace(id, wall.index)
        return position === null
          ? null
          : { index: wall.index, x: position.x + rect.left, y: position.y + rect.top }
      }, bodyId)
      if (!face) throw new Error('手前(−Y)の壁が画面から指せない')
      await page.mouse.click(face.x, face.y)
      await waitFor(
        (id, index) =>
          (window.__duocad.selection?.items ?? []).some(
            (item) => item.body === id && item.kind === 'face' && item.index === index,
          ),
        '面を選べない',
        bodyId,
        face.index,
      )
      // **通常モードの既定は無起動なので、ここで明示的に「押し出す」を起動する**
      // (2026-08-28。以前は`extrude_face`が最初から構えたままで、この1手が要らなかった)。
      // 面は選択済みのまま(起動は選択を触らない)なので、#distanceだけがここで出る。
      await page.click('#extrude')
      await waitFor(() => document.querySelector('#distance') !== null, '#extrudeを押しても#distanceが出ない')
      await typeInto('#distance', 2)
      await waitFor(() => window.__duocad.previewing === true, 'プレビューが出ない')
      await page.keyboard.press('Enter')
      await waitFor(
        (previous) => Math.abs(window.__duocad.parts[0].volume - previous) > 1e-9,
        '押し出しても体積が変わらない',
        before,
      )
      const after = (await look()).parts[0].volume
      return (
        `面をクリック →「押し出す」を起動 → 距離2 → プレビューが出てからEnter` +
        `(体積 ${before.toFixed(2)} → ${after.toFixed(2)})`
      )
    })

    await attempt('human', 'fillet', async () => {
      await page.click('#fillet')
      await waitForEdges(bodyId)
      const edges = await page.evaluate(() => window.__duocad.edges.edges.map((edge) => edge.index))
      let picked = null
      for (const index of edges) {
        if (await clickEdge(bodyId, index)) {
          picked = index
          break
        }
      }
      if (picked === null) throw new Error('辺を1本も指せない')
      const before = (await look()).parts[0].faces
      await typeInto('#fillet-radius', 2)
      await page.keyboard.press('Enter')
      await waitFor(
        (previous) => window.__duocad.parts[0].face_count > previous,
        'フィレットで面が増えない',
        before,
      )
      return `辺 ${picked} を本物のクリックで選び、半径2で丸めた(面 ${before} → ${(await look()).parts[0].faces})`
    })

    await attempt('human', 'undo', async () => {
      const before = (await look()).parts[0].faces
      await pressUndo(page)
      await waitFor(
        (previous) => window.__duocad.parts[0].face_count < previous,
        'undoで戻らない',
        before,
      )
      return `undoボタンでフィレットが戻った(面 ${before} → ${(await look()).parts[0].faces})`
    })

    await attempt('human', 'redo', async () => {
      const before = (await look()).parts[0].faces
      await pressRedo(page)
      await waitFor(
        (previous) => window.__duocad.parts[0].face_count > previous,
        'redoで戻らない',
        before,
      )
      return `redoボタンでフィレットがやり直された(面 ${before} → ${(await look()).parts[0].faces})`
    })

    await attempt('human', 'chamfer', async () => {
      await page.click('#chamfer')
      await waitForEdges(bodyId)
      const edges = await page.evaluate(() => window.__duocad.edges.edges.map((edge) => edge.index))
      let picked = null
      for (const index of edges) {
        if (await clickEdge(bodyId, index)) {
          picked = index
          break
        }
      }
      if (picked === null) throw new Error('辺を1本も指せない')
      const before = (await look()).parts[0].faces
      await typeInto('#chamfer-distance', 1)
      await page.keyboard.press('Enter')
      await waitFor(
        (previous) => window.__duocad.parts[0].face_count > previous,
        '面取りで面が増えない',
        before,
      )
      return `辺 ${picked} を選び、長さ1で面取りした(面 ${before} → ${(await look()).parts[0].faces})`
    })

    await attempt('human', 'set_parameter', async () => {
      const before = (await look()).parts[0].volume
      // **打ち直すのはSketchPanel**(ParametersPanel廃止)。長方形はもうスケッチモードの
      // 外にいる図形なので、「再開」(BrowserPanelの名前クリック)でそのスケッチへ戻ってから
      // 図形の一覧の入力欄を打つ —— 人間が実際にやる手順そのもの。
      await page.click(`[data-sketch-id="${humanSketchId}"] .sketch-name`)
      await waitFor(
        (id) => window.__duocad.activeSketch === id,
        '長方形のスケッチへ再入場できない',
        humanSketchId,
      )
      const target = await page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        for (const entity of active.entities) {
          for (const [key, value] of Object.entries(entity.params)) {
            if (Math.abs(value - 60) < 1e-9) return { entityId: entity.entity_id, key } // 長方形の幅
          }
        }
        return null
      })
      if (!target) throw new Error('長方形の幅がSketchPanelの図形一覧に出ていない')
      await typeInto(`[id="param-${target.entityId}-${target.key}"]`, 50)
      await page.keyboard.press('Enter')
      await waitFor(
        (previous) => Math.abs(window.__duocad.parts[0].volume - previous) > 1e-6,
        '打ち直しても形が追随しない',
        before,
      )
      const after = (await look()).parts[0].volume
      await page.click('#sketch-exit')
      await waitFor(() => window.__duocad.activeSketch === null, '長方形のスケッチを抜けられない')
      return `SketchPanelで再開して ${target.key} を 60 → 50 に打ち直した(体積 ${before.toFixed(2)} → ${after.toFixed(2)})`
    })

    await attempt('human', 'set_visible', async () => {
      await page.click(`.parts-row[data-body-id="${bodyId}"] input.visible-toggle`)
      await waitFor(
        (id) => window.__duocad.parts.find((body) => body.body_id === id).visible === false,
        '非表示にできない',
        bodyId,
      )
      await page.click('#parts-show-all')
      await waitFor(
        (id) => window.__duocad.parts.find((body) => body.body_id === id).visible === true,
        '表示に戻せない',
        bodyId,
      )
      return 'パーツ一覧のチェックボックスで非表示 → 「全表示」で戻した'
    })

    const humanStep = join(workdir, 'human.step')
    const humanStl = join(workdir, 'human.stl')

    await attempt('human', 'export_step', async () => {
      // **左サイドバーのファイルパネル**(2026-08-27にツールバーから移設)。欄へ打って
      // Enter(その行のボタンと同じ)。
      await typeInto('#file-export-step-path', humanStep)
      await page.keyboard.press('Enter')
      await waitFor(() => window.__duocad.lastFile?.action === 'export_step', '応答が返らない')
      const file = (await look()).lastFile
      return `パスを打ってEnter(${file.bytes} B)`
    })

    await attempt('human', 'export_stl', async () => {
      await typeInto('#file-export-stl-path', humanStl)
      await typeInto('#file-deviation', 0.05)
      await page.keyboard.press('Enter')
      await waitFor(() => window.__duocad.lastFile?.action === 'export_stl', '応答が返らない')
      const file = (await look()).lastFile
      return `偏差0.05を打って書き出した(${file.bytes} B)`
    })

    await attempt('human', 'import_step', async () => {
      // **読んだ時点で編集できることまで見る。** `parts` は `kind === "solid"` だけを
      // 並べるので(参照ボディは隠されて出ない)、ここが増えていれば編集用ボディができている。
      // `bodies` の側で数えると、参照ボディだけが増えた実装でも緑になってしまう。
      const before = (await look()).parts.length
      await typeInto('#file-import-path', humanStep)
      await page.keyboard.press('Enter')
      await waitFor(
        (previous) => window.__duocad.parts.length > previous,
        'STEPを読み込んでも編集用ボディが増えない',
        before,
      )
      const added = (await look()).parts.length - before
      return `自分で書き出したSTEPを読み込んだ(そのまま編集できるボディが ${added} 個できる)`
    })

    // ================================================================ AI側の18操作
    console.log('\n--- AI側(MCPツールを本物のJSON-RPCで呼ぶ) ---')
    // **同じサーバ・同じ文書**を触る(このスクリプトが起動したのは mcp_server 1つだけ)。
    let aiSketch = null
    let aiCircle = null

    await attempt('ai', 'sketch_create', async () => {
      const result = await mcp.callTool('sketch_create', { base_plane: 'XZ' })
      aiSketch = result.sketch_id
      return `XZ平面に作った(${aiSketch})`
    })

    await attempt('ai', 'add_circle', async () => {
      const result = await mcp.callTool('add_circle', { sketch_id: aiSketch, center: [0, 0], radius: 12 })
      aiCircle = result.entity_id
      return `半径12の円(${aiCircle})`
    })

    await attempt('ai', 'add_line', async () => {
      await mcp.callTool('add_line', { sketch_id: aiSketch, p0: [40, 0], p1: [60, 0] })
      return '線分を1本'
    })

    await attempt('ai', 'add_rectangle', async () => {
      await mcp.callTool('add_rectangle', { sketch_id: aiSketch, corner: [40, 20], width: 10, height: 6 })
      return '長方形を1つ'
    })

    await attempt('ai', 'add_arc', async () => {
      await mcp.callTool('add_arc', {
        sketch_id: aiSketch,
        center: [-40, 0],
        radius: 6,
        start_deg: 0,
        end_deg: 90,
      })
      return '円弧を1つ'
    })

    await attempt('ai', 'pattern_rectangular', async () => {
      const rectangle = await mcp.callTool('add_rectangle', {
        sketch_id: aiSketch,
        corner: [60, 40],
        width: 4,
        height: 4,
      })
      await mcp.callTool('pattern_rectangular', {
        sketch_id: aiSketch,
        entities: [rectangle.entity_id],
        dx: 6,
        dy: 0,
        nx: 3,
        ny: 1,
      })
      await mcp.callTool('undo')
      await mcp.callTool('undo')
      return '3×1に反復して確認し、undoで戻した(図形も消した)'
    })

    await attempt('ai', 'pattern_circular', async () => {
      const dot = await mcp.callTool('add_circle', { sketch_id: aiSketch, center: [70, 0], radius: 2 })
      await mcp.callTool('pattern_circular', {
        sketch_id: aiSketch,
        entities: [dot.entity_id],
        center: [80, 0],
        count: 4,
        total_angle: 360,
      })
      await mcp.callTool('undo')
      await mcp.callTool('undo')
      return '全周4個に反復して確認し、undoで戻した'
    })

    await attempt('ai', 'extrude', async () => {
      // **AIから見える領域は「何個あるか」だけ**(`describe_document` は `region_count`)。
      // どれが目的の領域かは `propose_selection` → `measure` の往復で確かめる ——
      // これは提案を確定前に検算する経路そのもの(§0のAI側の条件)。
      const document = await mcp.callTool('describe_document', { include_regions: true })
      const sketch = document.sketches.find((item) => item.sketch_id === aiSketch)
      let best = null
      for (let index = 1; index <= sketch.region_count; index += 1) {
        await mcp.callTool('propose_selection', { refs: [{ sketch: aiSketch, index }] })
        const measured = await mcp.callTool('measure', {})
        const area = measured.items[0]?.area ?? 0
        if (!best || area > best.area) best = { index, area }
      }
      if (!best) throw new Error(`領域が1つも無い(region_count=${sketch.region_count})`)
      await mcp.callTool('propose_selection', { refs: [{ sketch: aiSketch, index: best.index }] })
      await mcp.callTool('commit_selection')
      const result = await mcp.callTool('extrude', { distance: 20, mode: 'join' })
      return `${sketch.region_count}個の領域を measure で確かめて最大(面積 ${best.area.toFixed(1)})を選び、距離20で押し出した(seq ${result.seq})`
    })

    let aiBody = null
    await attempt('ai', 'extrude_face', async () => {
      const document = await mcp.callTool('describe_document', {})
      aiBody = document.bodies.filter((body) => body.kind === 'solid').slice(-1)[0].body_id
      const proposed = await mcp.callTool('propose_selection', {
        predicate: { kind: 'face', where: [{ op: 'kind_is', value: 'plane' }] },
      })
      const face = proposed.selection.items.find((item) => item.body === aiBody)
      if (!face) throw new Error(`このボディの平面が述語で1枚も選べない: ${JSON.stringify(proposed.selection.items.slice(0, 3))}`)
      await mcp.callTool('propose_selection', { refs: [face] })
      await mcp.callTool('commit_selection')
      const result = await mcp.callTool('extrude_face', { distance: 3 })
      return `述語で平面を選び直し、3mm押し出した(seq ${result.seq})`
    })

    await attempt('ai', 'fillet', async () => {
      const proposed = await mcp.callTool('propose_selection', {
        predicate: { kind: 'edge', where: [{ op: 'kind_is', value: 'circle' }] },
      })
      const mine = proposed.selection.items.filter((item) => item.body === aiBody)
      if (mine.length === 0) throw new Error('円い辺が述語で選べない')
      await mcp.callTool('propose_selection', { refs: [mine[0]] })
      await mcp.callTool('commit_selection')
      const result = await mcp.callTool('fillet', { radius: 1 })
      return `述語で円い辺を選び、半径1で丸めた(面 ${result.face_count ?? '?'})`
    })

    await attempt('ai', 'chamfer', async () => {
      const proposed = await mcp.callTool('propose_selection', {
        predicate: { kind: 'edge', where: [{ op: 'kind_is', value: 'circle' }] },
      })
      const mine = proposed.selection.items.filter((item) => item.body === aiBody)
      if (mine.length === 0) throw new Error('円い辺が述語で選べない')
      // **`measure` で長さを確かめてから一番長い円い辺を選ぶ**(`ai/extrude` と同じ、
      // 提案を確定前に検算する経路)。**先頭を黙って使わない** —— 直前の `fillet` が
      // 作る角の小さな円弧(長さ1.57mm = 半径1の四半円)も述語では同じ「円い辺」なので、
      // 添字の順で取ると**長さ1の面取りが入る余地のない辺**を掴む。2026-08-28に視点を
      // Z-up へ変えて形が少し変わった際、実際にそこへ落ちて OCCT に
      // 「no suitable edges」で断られた(それまでは偶然通っていた)。
      const candidates = []
      for (const ref of mine) {
        await mcp.callTool('propose_selection', { refs: [ref] })
        const measured = await mcp.callTool('measure', {})
        candidates.push({ ref, length: measured.items[0]?.length ?? 0 })
      }
      candidates.sort((a, b) => b.length - a.length)
      const target = candidates[0]
      await mcp.callTool('propose_selection', { refs: [target.ref] })
      await mcp.callTool('commit_selection')
      await mcp.callTool('chamfer', { distance: 1 })
      return `述語で円い辺を${candidates.length}本集め、measure で一番長いもの(辺${target.ref.index}・${target.length.toFixed(1)}mm)を長さ1で面取りした`
    })

    await attempt('ai', 'set_parameter', async () => {
      const document = await mcp.callTool('describe_document', {})
      const sketch = document.sketches.find((item) => item.sketch_id === aiSketch)
      const circle = sketch.entities.find((item) => item.entity_id === aiCircle)
      const name = circle.parameters.radius
      await mcp.callTool('set_parameter', { name, value: 14 })
      return `${name} を 12 → 14 にした(describe_document の parameters から辿れる)`
    })

    await attempt('ai', 'set_visible', async () => {
      await mcp.callTool('set_visible', { body_id: aiBody, visible: false })
      await page.waitForFunction(
        (id) => {
          const body = window.__duocad.state.bodies.find((item) => item.body_id === id)
          return body && body.visible === false
        },
        { timeout: 30000, polling: 50 },
        aiBody,
      )
      await mcp.callTool('set_visible', { body_id: aiBody, visible: true })
      return 'AIが隠すとブラウザの表示も変わった(上り方向)'
    })

    const aiStep = join(workdir, 'ai.step')
    await attempt('ai', 'export_step', async () => {
      const result = await mcp.callTool('export_step', { path: aiStep })
      return `${result.bytes} B`
    })

    await attempt('ai', 'export_stl', async () => {
      const result = await mcp.callTool('export_stl', { path: join(workdir, 'ai.stl'), deviation: 0.05 })
      return `${result.bytes} B(偏差0.05)`
    })

    await attempt('ai', 'import_step', async () => {
      // **STEP読み込みのツールは1つだけ**(ツール名 == ログのverb名)。読んだ時点で
      // 編集可能なボディができ、原本の参照ボディは隠したまま同じ1操作で入る。
      const result = await mcp.callTool('import_step', { path: aiStep })
      if (!Array.isArray(result.body_ids) || result.body_ids.length === 0) {
        throw new Error(`編集可能なボディが返らない: ${JSON.stringify(result.body_ids)}`)
      }
      if (result.reference_ids.length !== result.body_ids.length) {
        throw new Error(
          `参照と編集用が対になっていない: ${result.reference_ids.length} / ${result.body_ids.length}`,
        )
      }
      return `編集可能なボディ ${result.body_ids.length} 個(原本 ${result.reference_ids.length} 個は隠したまま)`
    })

    await attempt('ai', 'undo', async () => {
      const result = await mcp.callTool('undo')
      return `${result.undone} を取り消した`
    })

    await attempt('ai', 'redo', async () => {
      const result = await mcp.callTool('redo')
      return `${result.redone} をやり直した`
    })

    // ================================================================ 交互操作(核心)
    console.log('\n--- 交互操作(AIが提案 → 画面に出る → 人が直す → AIが適用) ---')
    try {
      // 1. AIが述語で辺の集合を提案する
      const proposed = await mcp.callTool('propose_selection', {
        predicate: { kind: 'edge', where: [{ op: 'parallel_to', axis: 'Y' }] },
      })
      const proposedItems = proposed.selection.items
      if (proposedItems.length === 0) throw new Error('述語で辺が1本も選べない')

      // 2. **ブラウザに「提案」として出る**(確定選択と**違う色**で3Dに出る。
      //    2026-08-23に選択パネルを廃したので、区別を伝えるのはこの色だけになった)
      await page.waitForFunction(
        (count) => {
          const selection = window.__duocad.selection
          return Boolean(selection && selection.origin === 'proposed' && selection.count === count)
        },
        { timeout: 30000, polling: 50 },
        proposedItems.length,
      )
      const badge = await page.evaluate(() => ({
        origin: window.__duocad.selection.origin,
        text: `${window.__duocad.selection.count} 件`,
      }))
      const promptText = await page.$eval('#command-prompt', (element) => element.textContent.trim())

      // 3. 人がクリックで過不足を直す(**触ること自体が承認**)。
      //
      //    **先にフィレットを起動する** —— 辺が画面に出るのは辺を要求しているコマンドが
      //    起動している間だけで、これは「見えているものと指せるものを一致させる」ための
      //    設計(D)。人の手順としても「これから丸めるから辺を見せてくれ」で自然である。
      //    提案は複数のボディにまたがりうるので、**人が1本に絞る**(素のクリック=置き換え)。
      const target = proposedItems[0]
      await page.click('#fillet')
      await frameAll(page) // 提案されたボディが画面の外にあることがある
      await waitForEdges(target.body)
      // **提案のうち、実際に指せる1本を人が選ぶ**。裏に回っている辺は指せないので、
      // 同じボディの候補を順に試す(人がやることと同じ —— 見えている辺をクリックする)。
      let picked = null
      for (const item of proposedItems.filter((entry) => entry.body === target.body)) {
        if (await clickEdge(item.body, item.index)) {
          picked = item
          break
        }
      }
      if (picked === null) throw new Error('提案された辺を画面から1本も指せない')
      // **触ること自体が承認**(A1の決定3)。クリックが本当に効いたことは
      // `origin` が `proposed` → `user` へ変わることでしか分からない ——
      // 提案に既に入っている辺をクリックしても件数は動かないので、件数で見てはいけない。
      await page.waitForFunction(
        () => window.__duocad.selection?.origin === 'user',
        { timeout: 30000, polling: 50 },
      )
      const afterHuman = await page.evaluate(() => ({
        origin: window.__duocad.selection.origin,
        count: window.__duocad.selection.count,
      }))

      // 4. AIが読み直して適用する
      const readBack = (await mcp.callTool('get_selection', {})).selection
      const targetBody = readBack.items[0].body
      const facesBefore = (await mcp.callTool('describe_shape', { body_id: targetBody })).face_count
      const applied = await mcp.callTool('fillet', { radius: 1.5 })
      const facesAfter = (await mcp.callTool('describe_shape', { body_id: targetBody })).face_count

      // 5. 形が変わり、画面にも届く
      await page.waitForFunction(
        (id, expected) => {
          const body = window.__duocad.state.bodies.find((item) => item.body_id === id)
          return body && body.face_count === expected
        },
        { timeout: 30000, polling: 50 },
        targetBody,
        facesAfter,
      )

      results.interleaved = {
        ok: true,
        proposedCount: proposedItems.length,
        proposedOrigin: badge.origin,
        selectionSummary: badge.text,
        prompt: promptText,
        humanPicked: picked,
        afterHuman,
        readBackCount: readBack.count,
        readBackOrigin: readBack.origin,
        facesBefore,
        facesAfter,
        appliedVerb: applied.verb,
      }
      console.log(`  OK   交互操作: 提案${proposedItems.length}本(origin=${badge.origin}) → 人が直して${afterHuman.count}本(origin=${afterHuman.origin}) → AIがfillet(面 ${facesBefore}→${facesAfter})`)
    } catch (error) {
      results.interleaved = { ok: false, error: String(error.message ?? error) }
      console.log(`  NG   交互操作: ${results.interleaved.error}`)
    }

    // ================================================================ 設定: 画角(FOV)
    //
    // **2026-08-28にFOVをUIから可変にした**ので、受入に数手だけ足す(実機検査のファイルは
    // 増やさない)。同日に**完全正投影(0度)**を足したので、見るのは3点:
    //
    //   1. **どの画角でも `frameAll` が図形を画面に収める**(0でも30でも90でもシーンが
    //      消えない —— near/far のクリップに当たらない)
    //   2. **画角を変えても注視点付近の見かけの大きさが変わらない**(距離補正)。
    //      端の点はパース差で動くのが正しい挙動なので、**動かないことを見るのは画面中心に
    //      近い点**、**動くことを見るのは一番外の点**にする。両方見ないと検出力が無い ——
    //      `setFov` が丸ごと効いていない実装でも「中心が動かない」だけなら緑になる。
    //   3. **0度が本物の正投影であること**(下の(4)。「とても小さい画角の透視」では
    //      通らない検査にしてある)と、**正投影でも寄り引きと `frameAll` が噛み合うこと**
    //      (下の(5))
    //
    // **ダイアログは各手の前に必ず閉じる。** `showModal()` は背後を inert にするので、
    // 開いたままだとビューポートのクリックが全部死ぬ。
    console.log('\n--- 設定: 画角(FOV。距離補正と frameAll) ---')

    /** 設定ダイアログの開閉(DOMに反映されるまで待つ)。 */
    async function setSettingsOpen(open) {
      const now = await page.evaluate(() => document.querySelector('#settings-dialog')?.open === true)
      if (now === open) return
      await page.click(open ? '#settings-open' : '#settings-close')
      await page.waitForFunction(
        (want) => document.querySelector('#settings-dialog')?.open === want,
        { timeout: 30000, polling: 20 },
        open,
      )
    }

    /**
     * スライダーを**本物のキー操作で** `target` まで動かす(range の step は 1 なので
     * 矢印キー1回が1度)。値を直接代入しないのは、`oninput` を経由しない代入では
     * 「スライダーを動かすとその場で効く」が検証にならないため。
     */
    async function dragFovTo(target) {
      await setSettingsOpen(true)
      await page.focus('#settings-fov')
      for (let guard = 0; guard < 100; guard += 1) {
        const now = await page.$eval('#settings-fov', (element) => Number(element.value))
        if (now === target) break
        const step = now < target ? 'ArrowRight' : 'ArrowLeft'
        for (let i = 0; i < Math.min(Math.abs(target - now), 10); i += 1) {
          await page.keyboard.press(step)
        }
      }
      await page.waitForFunction(
        (want) => window.__duocad.fov === want,
        { timeout: 30000, polling: 20 },
        target,
      )
      const shown = await page.$eval('#settings-fov-value', (element) => element.textContent.trim())
      await setSettingsOpen(false)
      return shown
    }

    /** 完全正投影(0度)の入切。**スライダーとは別の口**(`#settings-ortho`)。 */
    async function setOrtho(on) {
      await setSettingsOpen(true)
      const now = await page.$eval('#settings-ortho', (element) => element.checked)
      if (now !== on) await page.click('#settings-ortho')
      await page.waitForFunction(
        (want) => (window.__duocad.fov === 0) === want,
        { timeout: 30000, polling: 20 },
        on,
      )
      const shown = await page.$eval('#settings-fov-value', (element) => element.textContent.trim())
      await setSettingsOpen(false)
      return shown
    }

    /**
     * 画角を `value` にする。**0は正投影のチェックボックス、それ以外はスライダー**。
     * 正投影のあいだスライダーは disabled なので、角度へ戻すときは先にチェックを外す。
     */
    async function setFovTo(value) {
      if (value === 0) return await setOrtho(true)
      await setOrtho(false)
      return await dragFovTo(value)
    }

    /**
     * 注視点の手前(-1)・注視点上(0)・奥(+1)に**同じ長さの世界の線分**を視線と直角に置き、
     * それぞれ画面で何画素に写るかを測る(`ratio` は手前÷奥)。
     *
     * **これが正投影の検査の要**である。透視投影ではこの比は `(d+L)/(d-L)` ——
     * **距離だけで決まり画角に依らない**ので、画角をいくら絞っても1にならない。
     * つまり「比が1」は近似では作れず、本物の平行投影であることの証拠になる
     * (面の重心のような図形依存の点ではなく、視点から作った世界座標を射影するので、
     * どんなシーンでも同じように測れる)。
     *
     * **`scale` を渡すと線分の長さ(＝物差し)を世界座標で固定する。** 渡さないと注視点までの
     * 距離から作るので、カメラが前後した瞬間に物差しごと変わり、別の時点の測定値と
     * 比べられない(返り値の `scale` をそのまま次へ渡すこと)。
     *
     * **前後のずらしだけは固定しない。** こちらを世界座標で固定すると、カメラが遠ざかる
     * 実装(画角を絞って正投影を真似る等)では前後の差が距離に対して相対的に消え、比が1へ
     * 近づく —— 検査の検出力がカメラの遠さで薄まる。2026-08-28、画角1度の偽物で実測して
     * 比 1.0127(しきい値1.01のすぐ外)しか出ず、もう一声絞られたら通ってしまうことが
     * 分かったので、**常に今の距離から作る**(本物の透視なら距離によらず約1.86)。
     */
    async function depthProbe(scale = null) {
      return await page.evaluate((given) => {
        const hook = window.__duocad
        const pose = hook.cameraPose()
        if (pose === null) return null
        const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
        const len = (v) => Math.hypot(v[0], v[1], v[2])
        const unit = (v) => {
          const l = len(v)
          return [v[0] / l, v[1] / l, v[2] / l]
        }
        const cross = (a, b) => [
          a[1] * b[2] - a[2] * b[1],
          a[2] * b[0] - a[0] * b[2],
          a[0] * b[1] - a[1] * b[0],
        ]
        const toTarget = sub(pose.target, pose.position)
        const distance = len(toTarget)
        if (!(distance > 0)) return null
        const dir = unit(toTarget)
        const right = unit(cross(dir, pose.up))
        if (!right.every((value) => Number.isFinite(value))) return null
        const half = given === null ? distance * 0.05 : given.half // 線分の半分の長さ(物差し)
        const depth = distance * 0.3 // 注視点からの前後のずらし(常に今の距離から作る)
        const widthAt = (k) => {
          const center = [0, 1, 2].map((i) => pose.target[i] + dir[i] * depth * k)
          const a = hook.projectPoint(center.map((value, i) => value + right[i] * half))
          const b = hook.projectPoint(center.map((value, i) => value - right[i] * half))
          if (a === null || b === null) return null
          return Math.hypot(a.x - b.x, a.y - b.y)
        }
        const near = widthAt(-1)
        const at = widthAt(0)
        const far = widthAt(1)
        if (near === null || at === null || far === null) return null
        return { near, at, far, ratio: near / far, scale: { half } }
      }, scale)
    }

    /**
     * 今この瞬間に画面へ射影できる面を全部集める(ビューポート左上を原点とする画素)。
     * `fromCenter` はビューポート中心からの距離 —— **中心付近か外周かで期待する挙動が
     * 逆になる**ので、判定はこの値で振り分ける。
     */
    async function faceScreenPoints() {
      return await page.evaluate(() => {
        const hook = window.__duocad
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const center = { x: rect.width / 2, y: rect.height / 2 }
        const points = []
        for (const part of hook.parts) {
          const geometry = hook.getGeometry(part.body_id)
          if (geometry === null) continue
          for (const face of geometry.faces) {
            // **面の重心を、固定した世界座標として`projectPoint`で射影する**
            // (`projectFace`は使わない)。`projectFace`は「面の三角形を順に走査して
            // 最初にクリックが通る画素」を返す実装で、代表点ではなく**たまたま条件を
            // 満たした三角形の重心**を返す。視点・画角を変えると勝ち残る三角形が入れ替わり、
            // 同じ面でも画面座標が数十px跳ぶ(engine
            // `projected-face-point-jumps-between-triangles`。2026-08-28実測: 中心から
            // 211pxの面で27.3px対3.5pxの差)。ここは「視点・画角の違いを比べる」検査なので、
            // 固定した世界座標(面の重心)をそのまま射影する`projectPoint`を使う
            // (`projectFace`が要るのは「本当にそこを押せるか」を見る手だけ)。
            const at = hook.projectPoint(face.centroid)
            if (at === null) continue
            if (at.x < 0 || at.y < 0 || at.x > rect.width || at.y > rect.height) continue
            points.push({
              key: `${part.body_id}#${face.index}`,
              x: at.x,
              y: at.y,
              fromCenter: Math.hypot(at.x - center.x, at.y - center.y),
            })
          }
        }
        return { points, width: rect.width, height: rect.height }
      })
    }

    try {
      await setSettingsOpen(false)
      await setFovTo(45)
      await frameAll(page)

      // (1) 30 と 90 のどちらでも、frameAll のあとに図形が画面の中にある(完了条件8・9)。
      //
      //     **「1面でも射影できた」では検出力が無い。** `frameToContent` が画角を見ずに
      //     距離を決めていても、中央付近の面はどのみち画面に残るので緑になってしまう
      //     (実際に距離式を `diagonal * 1.2` へ戻して確かめた)。そこで**画面に占める
      //     大きさ**を見る —— `fitDistance` は画角によらず同じ外接球を画面いっぱいに
      //     収めるので、射影された面の広がりは画角を変えてもほぼ変わらないはずである。
      //     画角を見ない距離式なら、90度では 3.7 倍ほど小さく写る(=シーンが豆粒になる)。
      const framed = {}
      const captured = {}
      // **0(正投影)も同じ枠で見る。** 正投影は距離では収まらず視錐台で収めるので、
      // `frameToContent` が透視のときの距離式だけを直していると、ここで豆粒になるか
      // 画面からはみ出す。
      for (const angle of [45, 30, 90, 0]) {
        const shown = await setFovTo(angle)
        await frameAll(page)
        const { points } = await faceScreenPoints()
        if (points.length === 0) {
          throw new Error(`画角${angle}で frameAll しても面が1つも画面の中に無い`)
        }
        captured[angle] = new Map(points.map((point) => [point.key, point]))
        framed[angle] = { shown, faces: points.length }
      }
      // **広がりは同じ面の集合で測る。** 射影できる面は画角によって増えたり減ったりするので、
      // 拾えた面の顔ぶれの違いを広がりの差と取り違えない。
      const commonKeys = [...captured[45].keys()].filter(
        (key) => captured[30].has(key) && captured[90].has(key) && captured[0].has(key),
      )
      if (commonKeys.length < 3) {
        throw new Error(`0/30/45/90 のどれでも射影できる面が ${commonKeys.length} 個しか無い(広がりを比べられない)`)
      }
      const spreadOf = (angle) => {
        const xs = commonKeys.map((key) => captured[angle].get(key).x)
        const ys = commonKeys.map((key) => captured[angle].get(key).y)
        return Math.hypot(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys))
      }
      for (const angle of [45, 30, 90, 0]) framed[angle].spread = Number(spreadOf(angle).toFixed(1))
      for (const angle of [30, 90, 0]) {
        const ratio = framed[angle].spread / framed[45].spread
        framed[angle].ratio = Number(ratio.toFixed(3))
        if (ratio < 0.6 || ratio > 1.5) {
          throw new Error(
            `画角${angle}で frameAll した図形の広がりが45度の ${ratio.toFixed(2)} 倍` +
              `(${framed[angle].spread} / ${framed[45].spread} 画素。` +
              '収める大きさが投影に追随していない)',
          )
        }
      }

      // (2) 距離補正。**frameAll を挟まずに**画角だけを 45 → 60 へ変え、同じ面の画面座標を比べる。
      const backTo45 = await setFovTo(45)
      await frameAll(page)
      const before = await faceScreenPoints()
      await setFovTo(60)
      const after = await faceScreenPoints()

      const afterByKey = new Map(after.points.map((point) => [point.key, point]))
      const moved = []
      for (const point of before.points) {
        const now = afterByKey.get(point.key)
        // 画角を広げると外周のものは画面から出ていく(正しい挙動)。比べられる面だけ見る。
        if (now === undefined) continue
        moved.push({ ...point, shift: Math.hypot(now.x - point.x, now.y - point.y) })
      }
      if (moved.length === 0) throw new Error('画角45と60の両方で射影できる面が1つも無い')
      moved.sort((a, b) => a.fromCenter - b.fromCenter)

      // **中心にごく近い点だけを見ても検出力が足りない** —— 距離補正が丸ごと無い実装は
      // 中心を不動点にした一様な拡大になるので、中心の点は補正の有無に関わらず動かない。
      // だから「中心付近は数画素以内」に加えて、**外周寄りの点が拡大率どおりには動いて
      // いないこと**を見る。45→60の拡大率は tan(30°)/tan(22.5°) ≒ 1.394 なので、
      // 補正が無ければ中心から r の点は 0.394r 動く。
      //
      // **しきい値は実測から作った(2026-08-28再校正)。** 測定点を`projectFace`(最初に
      // ピックできた三角形の重心。視点で数十px跳ぶ)から`projectPoint(face.centroid)`
      // (固定した世界座標)へ差し替えた際、旧しきい値(6px / 0.25r)は跳びを吸収するための
      // 値のまま残っていて、跳びの無い新しい測り方には桁で緩かった(実測0.68px/3.38pxに対し
      // 6px/46.6pxは約9倍・14倍の余裕)。この文書と同じ場面(板1枚・穴3つのシーン)で
      // 実測した値は次のとおり(45→60固定・5回連続で誤差なく再現。`projectPoint`は
      // 三角形選択に依存しないため決定論的):
      //   correct(このコード)          near 0.68px / outer 3.38px(r=186.5)
      //   setFovの補正を半分の強さに   near 1.99px / outer 27.6px
      //   setFovの補正を20%弱める      near 0.47px / outer  9.8px
      //   setFovの補正を丸ごと外す     near 4.10px / outer 52.7px
      // **near単独では「補正を弱める」系の劣化を確実には拾えない**(相対距離があまり
      // 動かない点なので、半分の強さでも1.99pxとしきい値2pxの内側に収まりうる)。
      // 検出の主力はouter(補正の量そのものが線形に出る)で、near は「補正が丸ごと無い」
      // 極端な壊れ方を確実に落とす保険という位置づけ(元設計と同じ役割分担)。
      // **near 2px(実測の約2.9倍)・outer 0.04r(実測比の約2.2倍。r=186.5なら7.46px)**
      // に置いた —— 補正半分の劣化はouterが27.6px対7.46pxで明白に落とし、20%弱めた
      // 劣化も9.8px対7.46pxで落とす。10%弱めた劣化(outer 3.35px)は正常値(3.38px)と
      // 同オーダーで測定の分解能を超えており、この検査では区別できない
      // (`projectPoint`を使ってもピクセル単位の測定には限界がある)。
      const near = moved[0]
      // 外周寄りの点。画面の端ぎりぎりだと射影が別の三角形へ移りうるので上限を掛ける。
      const radiusCap = 0.35 * Math.min(before.width, before.height)
      const outer = moved.filter((point) => point.fromCenter <= radiusCap).pop()
      if (outer === undefined || outer.fromCenter < 40) {
        throw new Error('外周寄りの比較点が取れない(中心から40画素以上離れた面が無い)')
      }
      const maxShift = Math.max(...moved.map((point) => point.shift))

      if (near.shift > 2) {
        throw new Error(`画角45→60で中心付近の面が ${near.shift.toFixed(1)} 画素動いた(距離補正が効いていない)`)
      }
      const allowed = 0.04 * outer.fromCenter
      if (outer.shift > allowed) {
        throw new Error(
          `画角45→60で中心から ${outer.fromCenter.toFixed(0)} 画素の面が ${outer.shift.toFixed(1)} 画素動いた` +
            `(距離補正が無いときの目安 ${(0.394 * outer.fromCenter).toFixed(1)} 画素に近い。許容 ${allowed.toFixed(1)})`,
        )
      }
      // (3) 画角そのものが本当に変わっていること(`setFov` が丸ごと効いていない実装を落とす)。
      //     **45→90で見る** —— 45→60のパース差はこのシーンの薄さでは10画素に届かず、
      //     境目を引ける幅が無い。45→90なら拡大率が 2.414 倍になり、注視点から奥行きの
      //     ずれた面は数十画素動く。
      await setFovTo(45)
      await frameAll(page)
      const wideBefore = await faceScreenPoints()
      await setFovTo(90)
      const wideAfter = await faceScreenPoints()
      const wideByKey = new Map(wideAfter.points.map((point) => [point.key, point]))
      const wideShifts = wideBefore.points
        .filter((point) => wideByKey.has(point.key))
        .map((point) => {
          const now = wideByKey.get(point.key)
          return Math.hypot(now.x - point.x, now.y - point.y)
        })
      if (wideShifts.length === 0) throw new Error('画角45と90の両方で射影できる面が1つも無い')
      const wideMaxShift = Math.max(...wideShifts)
      if (wideMaxShift < 20) {
        throw new Error(`画角45→90でどの面も ${wideMaxShift.toFixed(1)} 画素しか動かない(画角が実際には変わっていない)`)
      }

      // (4) **完全正投影(0度)。** 見るのは2点 —— (a) 手前と奥で見かけの大きさが変わらない
      //     こと(`depthProbe` の説明のとおり、これは「画角のとても小さい透視」では作れない)、
      //     (b) 切り替えても注視点の見かけの大きさが変わらないこと(30〜90の距離補正と同じ
      //     約束を、投影の切り替えでも守る)。
      await setFovTo(45)
      await frameAll(page)
      const perspProbe = await depthProbe()
      if (perspProbe === null) {
        throw new Error('透視での前後の比が測れない(線分が画面の外か、視線が上方向と平行)')
      }
      if (perspProbe.ratio < 1.5) {
        throw new Error(
          `画角45なのに手前と奥の見かけが ${perspProbe.ratio.toFixed(3)} 倍しか違わない` +
            '(この検査自体が効いていない。想定は約1.86倍)',
        )
      }
      const scale = perspProbe.scale // 以降は同じ物差しで測る(カメラが前後しても比べられる)
      /**
       * **見かけの大きさが動かなくなるまで測り続ける。** ホイールの効きは次の rAF まで
       * 出ないので、送った直後に1回測るだけでは寄る前の値を読む。「変わるまで待つ」では
       * なく「動かなくなるまで待つ」にしてあるのは、1回の操作が複数のイベントに分かれて
       * 届いたときに途中の値を掴まないため(**この分割が実際に起きるかは確かめていない**。
       * 上限200回で、正常時は数回で抜ける)。
       */
      async function settledProbe() {
        let previous = null
        let stable = 0
        for (let guard = 0; guard < 200 && stable < 3; guard += 1) {
          const now = await depthProbe(scale)
          if (now === null) return null
          if (previous !== null && Math.abs(now.at - previous.at) < 0.01) stable += 1
          else stable = 0
          previous = now
        }
        return previous
      }
      const orthoShown = await setFovTo(0)
      if (orthoShown !== '0°') throw new Error(`正投影の表示が「${orthoShown}」になっている`)
      // スライダーは正投影のあいだ効かない(押せるのに効かない口を出さない)。
      await setSettingsOpen(true)
      const sliderDisabled = await page.$eval('#settings-fov', (element) => element.disabled)
      await setSettingsOpen(false)
      if (!sliderDisabled) throw new Error('正投影なのに画角のスライダーが効いたままになっている')
      const orthoProbe = await depthProbe(scale)
      if (orthoProbe === null) throw new Error('正投影での前後の比が測れない')
      if (Math.abs(orthoProbe.ratio - 1) > 0.01) {
        throw new Error(
          `正投影なのに手前と奥の見かけが ${orthoProbe.ratio.toFixed(4)} 倍違う` +
            `(手前 ${orthoProbe.near.toFixed(1)}px / 奥 ${orthoProbe.far.toFixed(1)}px。パースが残っている)`,
        )
      }
      const orthoKeptSize = orthoProbe.at / perspProbe.at
      if (Math.abs(orthoKeptSize - 1) > 0.02) {
        throw new Error(
          `正投影へ切り替えると注視点の見かけの大きさが ${orthoKeptSize.toFixed(3)} 倍になる` +
            '(切り替えがズームになっている)',
        )
      }

      // (5) **正投影の寄り引きと、そこからの `frameAll`。** 正投影のドリーは `zoom` を
      //     動かすだけで**カメラを動かさない**ので、`frameAll` が位置しか直していないと
      //     寄った倍率のまま居座る。上の(1)の枠ではこれを検出できない —— 画角を切り替えた
      //     直後の見かけの大きさは `setFov` 側が保つので、`frameAll` が何もしなくても
      //     たまたま正しい値になっているためである。ここは**先にホイールで倍率を崩してから**
      //     `frameAll` を呼ぶので、視錐台を作り直していない実装は戻れない。
      const zoomedIn = await withoutBrowserPanel(page, async () => {
        const box = await page.evaluate(() => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
        })
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
        await page.mouse.wheel({ deltaY: -600 })
        return await settledProbe()
      })
      if (zoomedIn === null || !(zoomedIn.at > orthoProbe.at * 1.2)) {
        throw new Error(
          `正投影でホイールが効かない(見かけ ${zoomedIn === null ? 'null' : zoomedIn.at.toFixed(1)}px /` +
            ` 寄る前 ${orthoProbe.at.toFixed(1)}px)`,
        )
      }
      // 寄っても平行投影のままであること(`zoom` の道でパースが混ざらない)。
      if (Math.abs(zoomedIn.ratio - 1) > 0.01) {
        throw new Error(`正投影で寄ったら手前と奥の見かけが ${zoomedIn.ratio.toFixed(4)} 倍違う`)
      }
      await frameAll(page)
      const refit = await settledProbe()
      if (refit === null) throw new Error('正投影の frameAll 後に前後の比が測れない')
      const refitRatio = refit.at / orthoProbe.at
      if (Math.abs(refitRatio - 1) > 0.05) {
        throw new Error(
          `正投影で寄ったあと frameAll しても倍率が戻らない(戻り ${refitRatio.toFixed(3)} 倍。` +
            `${refit.at.toFixed(1)}px / ${orthoProbe.at.toFixed(1)}px)`,
        )
      }

      await setFovTo(45)
      await frameAll(page)

      results.fov = {
        ok: true,
        framed,
        backTo45,
        orthoShown,
        orthoRatio: Number(orthoProbe.ratio.toFixed(4)),
        perspRatio: Number(perspProbe.ratio.toFixed(4)),
        orthoKeptSize: Number(orthoKeptSize.toFixed(4)),
        orthoZoomedIn: Number((zoomedIn.at / orthoProbe.at).toFixed(3)),
        orthoRefit: Number(refitRatio.toFixed(4)),
        compared: moved.length,
        nearShift: Number(near.shift.toFixed(2)),
        nearFromCenter: Number(near.fromCenter.toFixed(1)),
        outerShift: Number(outer.shift.toFixed(2)),
        outerFromCenter: Number(outer.fromCenter.toFixed(1)),
        outerAllowed: Number(allowed.toFixed(2)),
        maxShift: Number(maxShift.toFixed(2)),
        wideMaxShift: Number(wideMaxShift.toFixed(2)),
      }
      console.log(
        `  OK   画角: frameAll の広がり(共通の${commonKeys.length}面) 45度=${framed[45].spread}px / 30度=${framed[30].spread}px(${framed[30].ratio}倍)` +
          ` / 90度=${framed[90].spread}px(${framed[90].ratio}倍) / 正投影=${framed[0].spread}px(${framed[0].ratio}倍)。` +
          ` 正投影の手前÷奥=${orthoProbe.ratio.toFixed(4)}(透視45度なら ${perspProbe.ratio.toFixed(2)})・` +
          `切り替えでの見かけ ${orthoKeptSize.toFixed(3)}倍・` +
          `ホイールで ${(zoomedIn.at / orthoProbe.at).toFixed(2)}倍に寄って frameAll で ${refitRatio.toFixed(3)}倍へ戻る。` +
          ` 45→60: 中心付近(r=${near.fromCenter.toFixed(0)})は ${near.shift.toFixed(1)}px /` +
          ` 外周(r=${outer.fromCenter.toFixed(0)})は ${outer.shift.toFixed(1)}px(補正無しなら ${(0.394 * outer.fromCenter).toFixed(0)}px) /` +
          ` 最大 ${maxShift.toFixed(1)}px。45→90では最大 ${wideMaxShift.toFixed(1)}px 動く`,
      )
    } catch (error) {
      results.fov = { ok: false, error: String(error.message ?? error) }
      console.log(`  NG   画角: ${results.fov.error}`)
      await setSettingsOpen(false).catch(() => {})
    }


    // ================================================================ 座標系: Z-up 右手系
    //
    // **2026-08-28にビューポートを Z-up(X=右 / Y=奥 / Z=上)へ直した**ので、受入に数手
    // 足す(実機検査のファイルは増やさない)。カーネルは元から Z-up で、**ずれていたのは
    // ビューアだけ**だった。見るのは4点:
    //
    //   1. `camera.up` がワールド+Z、かつ初期視点が front-top-right
    //   2. **+Z が画面で上** —— XY平面から +Z へ押し出した部品の上面が、下面より画面で上に写る
    //   3. **オービットの極が +Z** —— 水平ドラッグでZ軸まわりに回る(Y軸まわりではない)
    //   4. **スケッチに入って出ても Y-up へ戻らない**
    //
    // **上下面の比較に `projectFace` は使えない。** あれは「その画素が本当にその面を
    // 取れる」ことまで確かめて返すので、**上から見えている限り下面は常に null** になり、
    // 比べる相手が存在しない。遮蔽と無関係な投影そのものを問う `projectPoint` を使う。
    //
    // **3 は画面の見た目からは分からない。** `OrbitControls` はコンストラクタで生成時の
    // `object.up` を `_quat` に焼き込み `update()` で再計算しないので、up を後から
    // 書き換えた実装では**画面の上方向だけが正しく、回転の極は+Yのまま**になる。
    // 判定は「水平ドラッグしても注視点からの高さ(`position.z - target.z`)が変わらない」
    // ＋「回転軸上の点の画面位置が動かない」で見る —— 極が+Yなら高さが大きく振れる。
    console.log('\n--- 座標系: Z-up 右手系(上方向・オービットの極・視点) ---')

    /** 減衰(`enableDamping`)が効いているので、カメラが止まってから読む。 */
    async function settledPose() {
      let previous = null
      for (let guard = 0; guard < 200; guard += 1) {
        const pose = await page.evaluate(() => window.__duocad.cameraPose())
        if (
          previous !== null &&
          previous.position.every((value, i) => Math.abs(value - pose.position[i]) < 1e-4)
        ) {
          return pose
        }
        previous = pose
        await new Promise((resolve) => setTimeout(resolve, 30))
      }
      throw new Error('カメラが止まらない(減衰が終わらない)')
    }

    try {
      await setSettingsOpen(false)
      await frameAll(page)
      const pose = await settledPose()

      // (1) 上方向そのもの(完了条件1)。
      const up = pose.up
      if (Math.abs(up[0]) > 1e-6 || Math.abs(up[1]) > 1e-6 || Math.abs(up[2] - 1) > 1e-6) {
        throw new Error(`camera.up が [${up.map((v) => v.toFixed(3)).join(', ')}](+Zでない)`)
      }

      // (2) 初期視点が front-top-right(完了条件4)。**X と Y に等量、Zは控えめ**の
      //     (d, -d, 0.6d) —— `+Y` から見ると背面になるので Y は負である。
      const offset = pose.position.map((value, i) => value - pose.target[i])
      const home = { x: offset[0], y: offset[1], z: offset[2] }
      if (!(home.x > 0 && home.y < 0 && home.z > 0)) {
        throw new Error(
          `初期視点が front-top-right でない(注視点からのオフセット ` +
            `${offset.map((v) => v.toFixed(1)).join(', ')}。+X / −Y / +Z の側から見るはず)`,
        )
      }
      const xyRatio = Math.abs(home.y) / home.x
      const zRatio = home.z / home.x
      if (Math.abs(xyRatio - 1) > 0.05 || Math.abs(zRatio - 0.6) > 0.05) {
        throw new Error(
          `初期視点の比が (1, -1, 0.6) でない(|y|/x=${xyRatio.toFixed(3)} z/x=${zRatio.toFixed(3)})`,
        )
      }

      // (3) **+Z が画面で上**(完了条件2)。まず注視点とその真上40mmの点で見る。
      const axisPoint = [pose.target[0], pose.target[1], pose.target[2] + 40]
      const screen = await page.evaluate(
        (below, above) => ({
          below: window.__duocad.projectPoint(below),
          above: window.__duocad.projectPoint(above),
        }),
        pose.target,
        axisPoint,
      )
      if (screen.below === null || screen.above === null) {
        throw new Error('注視点とその真上40mmの点が画面に射影できない')
      }
      const axisRise = screen.below.y - screen.above.y // 画面yは下向きなので、上なら正
      if (axisRise < 10) {
        throw new Error(
          `+Zへ40mm動かしても画面で ${axisRise.toFixed(1)} 画素しか上がらない(画面の上方向が+Zでない)`,
        )
      }

      // (4) **押し出した部品の上面と下面**(完了条件2の本番)。法線 +Z / −Z の面の重心を
      //     射影して、上面のほうが画面で上(y が小さい)ことを見る。
      const plate = await page.evaluate(() => {
        const hook = window.__duocad
        for (const part of hook.parts) {
          const geometry = hook.getGeometry(part.body_id)
          if (geometry === null) continue
          const top = geometry.faces.find((face) => face.normal[2] > 0.9)
          const bottom = geometry.faces.find((face) => face.normal[2] < -0.9)
          if (top === undefined || bottom === undefined) continue
          return {
            bodyId: part.body_id,
            top: { index: top.index, at: hook.projectPoint(top.centroid), z: top.centroid[2] },
            bottom: {
              index: bottom.index,
              at: hook.projectPoint(bottom.centroid),
              z: bottom.centroid[2],
            },
          }
        }
        return null
      })
      if (plate === null) throw new Error('法線が +Z と −Z の面を両方持つボディが1つも無い')
      if (plate.top.at === null || plate.bottom.at === null) {
        throw new Error('上面・下面の重心が画面に射影できない')
      }
      if (!(plate.top.z > plate.bottom.z)) {
        throw new Error(
          `上面の重心のZ(${plate.top.z})が下面(${plate.bottom.z})より上でない(前提が崩れている)`,
        )
      }
      const faceRise = plate.bottom.at.y - plate.top.at.y
      if (faceRise <= 0) {
        throw new Error(
          `上面(面${plate.top.index})が画面で下面(面${plate.bottom.index})より下にある` +
            `(y ${plate.top.at.y.toFixed(1)} vs ${plate.bottom.at.y.toFixed(1)})`,
        )
      }

      // (5) **オービットの極が +Z**(完了条件3)。ビューポートを水平にドラッグして、
      //     注視点からの高さと、回転軸上の点の画面位置が動かないことを見る。
      const rect = await page.evaluate(() => {
        const box = document.querySelector('.viewport').getBoundingClientRect()
        return { left: box.left, top: box.top, width: box.width, height: box.height }
      })
      const midY = rect.top + rect.height / 2
      const centerX = rect.left + rect.width / 2
      const swing = Math.min(220, rect.width / 3)
      await page.mouse.move(centerX - swing, midY)
      await page.mouse.down()
      for (let i = 1; i <= 10; i += 1) {
        await page.mouse.move(centerX - swing + (2 * swing * i) / 10, midY)
      }
      await page.mouse.up()
      const turned = await settledPose()
      const turnedOffset = turned.position.map((value, i) => value - turned.target[i])
      const azimuthBefore = Math.atan2(offset[1], offset[0])
      const azimuthAfter = Math.atan2(turnedOffset[1], turnedOffset[0])
      let azimuthDelta = Math.abs(azimuthAfter - azimuthBefore)
      if (azimuthDelta > Math.PI) azimuthDelta = 2 * Math.PI - azimuthDelta
      const radius = Math.hypot(...offset)
      if (azimuthDelta < 0.5) {
        throw new Error(
          `水平に${(2 * swing).toFixed(0)}画素ドラッグしても方位が ${azimuthDelta.toFixed(3)} rad しか回らない(オービットが効いていない)`,
        )
      }
      const heightDrift = Math.abs(turnedOffset[2] - offset[2])
      if (heightDrift > 0.01 * radius) {
        throw new Error(
          `水平ドラッグで注視点からの高さが ${offset[2].toFixed(1)} → ${turnedOffset[2].toFixed(1)} へ動いた` +
            `(${heightDrift.toFixed(2)}mm。回転の極が+Zでない ——` +
            ` OrbitControls が生成時の up を焼き込んでいる)`,
        )
      }
      const axisAfter = await page.evaluate(
        (point) => window.__duocad.projectPoint(point),
        axisPoint,
      )
      if (axisAfter === null) throw new Error('回転軸上の点が回した後に射影できない')
      const axisShift = Math.hypot(axisAfter.x - screen.above.x, axisAfter.y - screen.above.y)
      if (axisShift > 3) {
        throw new Error(
          `水平ドラッグで回転軸(注視点の真上)の点が ${axisShift.toFixed(1)} 画素動いた(その軸で回っていない)`,
        )
      }

      // (6) **スケッチに入って出ても Y-up へ戻らない**(完了条件5)。既定へ戻す処理が
      //     `(0,1,0)` を直書きしていたので、1往復するだけで倒れていた。
      //     まず**最初の退出**(復帰の走らない経路。上の `upAfterFirstExit` 参照)、
      //     続いて2回目以降の出入り(控えた up の復元が走る経路)の両方を見る。
      if (upAfterFirstExit === null) throw new Error('最初にスケッチを抜けたときの up を控えていない')
      const firstUp = upAfterFirstExit
      if (
        Math.abs(firstUp[0]) > 1e-6 ||
        Math.abs(firstUp[1]) > 1e-6 ||
        Math.abs(firstUp[2] - 1) > 1e-6
      ) {
        throw new Error(
          `最初にスケッチを抜けた直後の camera.up が [${firstUp.map((v) => v.toFixed(3)).join(', ')}]` +
            `(既定へ戻す処理が Y-up を直書きしている)`,
        )
      }
      await page.click(`[data-sketch-id="${humanSketchId}"] .sketch-name`)
      await waitFor((id) => window.__duocad.activeSketch === id, 'スケッチへ再入場できない', humanSketchId)
      await page.click('#sketch-exit')
      await waitFor(() => window.__duocad.activeSketch === null, 'スケッチを抜けられない')
      const afterSketch = await settledPose()
      const backUp = afterSketch.up
      if (Math.abs(backUp[0]) > 1e-6 || Math.abs(backUp[1]) > 1e-6 || Math.abs(backUp[2] - 1) > 1e-6) {
        throw new Error(
          `スケッチを1往復したら camera.up が [${backUp.map((v) => v.toFixed(3)).join(', ')}] になった(Y-upへ戻っている)`,
        )
      }

      await frameAll(page)

      results.zup = {
        ok: true,
        up,
        home: offset.map((value) => Number(value.toFixed(2))),
        homeRatio: { xy: Number(xyRatio.toFixed(3)), z: Number(zRatio.toFixed(3)) },
        axisRise: Number(axisRise.toFixed(1)),
        plate: {
          bodyId: plate.bodyId,
          topFace: plate.top.index,
          bottomFace: plate.bottom.index,
          rise: Number(faceRise.toFixed(1)),
        },
        orbit: {
          azimuthDelta: Number(azimuthDelta.toFixed(3)),
          heightDrift: Number(heightDrift.toFixed(4)),
          axisShift: Number(axisShift.toFixed(2)),
          radius: Number(radius.toFixed(1)),
        },
        upAfterFirstExit: firstUp,
        upAfterSketchRoundTrip: backUp,
      }
      console.log(
        `  OK   座標系: up=[${up.join(', ')}] / 視点は front-top-right` +
          `(${offset.map((v) => v.toFixed(0)).join(', ')}。|y|/x=${xyRatio.toFixed(2)} z/x=${zRatio.toFixed(2)}) /` +
          ` +Zへ40mmで画面 ${axisRise.toFixed(0)}px 上がる /` +
          ` 上面(面${plate.top.index})は下面(面${plate.bottom.index})より ${faceRise.toFixed(0)}px 上 /` +
          ` 水平ドラッグで方位 ${azimuthDelta.toFixed(2)}rad 回しても高さのずれ ${heightDrift.toFixed(3)}mm・` +
          `軸上の点 ${axisShift.toFixed(1)}px / スケッチは最初の退出も1往復の後も up は +Z`,
      )
    } catch (error) {
      results.zup = { ok: false, error: String(error.message ?? error) }
      console.log(`  NG   座標系: ${results.zup.error}`)
      await setSettingsOpen(false).catch(() => {})
      await page.evaluate(() => window.__duocad.frameAll()).catch(() => {})
    }
    // ================================================================ 副次: 実物相当
    //
    // **別プロセス(空の文書)で作る**。`join` の押し出しは「対象が無ければ新しいソリッドを
    // 作る」verbなので、既に何ボディも居るこの受入用の文書では既存へ融合してしまい、
    // 独立した円筒が作れない(1回目の実行で実際に踏んだ)。
    console.log('\n--- 副次: 手だけで実物相当(全周円筒+円錐。空の文書で) ---')
    // 副次の別プロセスも実 `models/` を向かせない(上の本体と同じ理由)。
    const solo = await startServer({ wsPort: '8887', httpPort: '8888', modelsDir: workdir })
    const soloMcp = new McpClient(solo.child, { timeout })
    try {
      await soloMcp.handshake('duocad-acceptance-solo')
      const sketch = await soloMcp.callTool('sketch_create', { base_plane: 'XY' })
      await soloMcp.callTool('add_circle', { sketch_id: sketch.sketch_id, center: [0, 0], radius: 12.5 })
      await soloMcp.callTool('propose_selection', { refs: [{ sketch: sketch.sketch_id, index: 1 }] })
      await soloMcp.callTool('commit_selection')
      await soloMcp.callTool('extrude', { distance: 22, mode: 'join' })
      const body = (await soloMcp.callTool('describe_document', {})).bodies[0].body_id
      const cylinder = await soloMcp.callTool('describe_shape', { body_id: body })
      const proposed = await soloMcp.callTool('propose_selection', {
        predicate: { kind: 'edge', where: [{ op: 'kind_is', value: 'circle' }] },
      })
      await soloMcp.callTool('propose_selection', { refs: [proposed.selection.items[0]] })
      await soloMcp.callTool('commit_selection')
      await soloMcp.callTool('chamfer', { distance: 2 })
      const after = await soloMcp.callTool('describe_shape', { body_id: body })
      results.physical = {
        ok: true,
        cylinder: { faces: cylinder.face_count, by_type: cylinder.faces_by_type },
        afterChamfer: { faces: after.face_count, by_type: after.faces_by_type },
      }
      console.log(
        `  OK   実物相当: 円筒 ${cylinder.face_count}面 ${JSON.stringify(cylinder.faces_by_type)}` +
          ` → 面取り後 ${after.face_count}面 ${JSON.stringify(after.faces_by_type)}`,
      )
    } catch (error) {
      results.physical = { ok: false, error: String(error.message ?? error) }
      console.log(`  NG   実物相当: ${results.physical.error}`)
    } finally {
      await solo.stop()
    }

    results.consoleErrors = consoleErrors.slice(0, 5)
    const screen = await look()
    results.screenMessages = screen.messages
    // **エラーは別に記録する。** ステータスバーに残るのは情報だけになったので、
    // `screenMessages` だけを受入記録に載せると「エラーは1件も出ていない」ように読める。
    results.screenErrors = screen.errors ?? []
    // **合否そのものも記録に残す**(終了コードだけだと、ログを後から読む人には見えない)。
    results.failed = failedLabels(results)
    console.log('\n=== RESULT_JSON ===')
    console.log(JSON.stringify(results, null, 2))
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

main()
  .then(() => {
    // **後始末(ブラウザとサーバの停止)が終わってから**落とす —— `main()` の中で
    // `process.exit` すると `finally` を飛ばして子プロセスが残る。
    const failed = failedLabels(results)
    if (failed.length > 0) {
      console.error(`\nacceptance: FAIL ${failed.length}件 — ${failed.join(' / ')}`)
      process.exit(1)
    }
    console.log('\nacceptance: PASS')
  })
  .catch((error) => {
    console.error(`acceptance: FAIL ${error.message}`)
    console.log('\n=== RESULT_JSON ===')
    console.log(JSON.stringify(results, null, 2))
    process.exit(1)
  })
