/**
 * v1の受入 — **18操作を人間側とAI側の両方で実際に動かす**。
 *
 * v1の定義:
 *
 * > v1 = いま実装済みのverbすべてを、人間とAIの両方が満足に使える状態。
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
 * 結果はJSONで stdout の末尾へ出す(`ACCEPTANCE.md` はそれを見て人が書く)。
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
 * 到達表の18操作(実装計画書§0)。**この並びのまま ACCEPTANCE.md の表になる**。
 *
 * 2026-08-27に19から18へ減った —— 参照ボディから編集用ソリッドを作る複製の操作を
 * 削除し、STEP読み込み(`import_step`)が読んだ時点で編集可能なボディまで作るように
 * 統合したため。**減ったのはこの1つだけ**であること(他を削って通していないこと)が
 * v1到達の根拠を保つ条件である。
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

const results = { human: {}, ai: {}, interleaved: null, physical: null }

function record(side, verb, ok, note) {
  results[side][verb] = { ok, note }
  console.log(`  ${ok ? 'OK  ' : 'NG  '} ${side.padEnd(5)} ${verb.padEnd(20)} ${note}`)
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
  const server = await startServer({ wsPort, httpPort })
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
    await page.click('#sketch-look')

    await attempt('human', 'add_line', async () => {
      await page.click('#sketch-line')
      await clickPlane(70, 0)
      await clickPlane(70, 30)
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
      await clickPlane(-20, 20)
      await typeInto('#sketch-radius', 10)
      await page.keyboard.press('Enter')
      await waitFor(
        () => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return Boolean(active && active.entities.some((entity) => entity.kind === 'circle'))
        },
        '円が置けない',
      )
      return '中心をクリック → 半径10を打つ → Enter'
    })

    await attempt('human', 'add_arc', async () => {
      await page.click('#sketch-arc')
      await clickPlane(-20, -10)
      await typeInto('#sketch-arc-radius', 8)
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
      await clickPlane(-20, 45)
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
    await frameAll(page)
    const bodyId = (await look()).parts[0].id

    await attempt('human', 'extrude_face', async () => {
      const before = (await look()).parts[0].volume
      const face = await page.evaluate((id) => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const geometry = window.__duocad.getGeometry(id)
        const planes = [...geometry.faces].filter((item) => item.kind === 'plane')
        for (const item of planes) {
          const position = window.__duocad.projectFace(id, item.index)
          if (position) return { index: item.index, x: position.x + rect.left, y: position.y + rect.top }
        }
        return null
      }, bodyId)
      if (!face) throw new Error('押し出せる面が画面から指せない')
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
      await typeInto('#distance', 2)
      await waitFor(() => window.__duocad.previewing === true, 'プレビューが出ない')
      await page.keyboard.press('Enter')
      await waitFor(
        (previous) => Math.abs(window.__duocad.parts[0].volume - previous) > 1e-9,
        '押し出しても体積が変わらない',
        before,
      )
      const after = (await look()).parts[0].volume
      return `面をクリック → 距離2 → プレビューが出てからEnter(体積 ${before.toFixed(2)} → ${after.toFixed(2)})`
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
      await page.click('#file-export-step')
      await typeInto('#file-path', humanStep)
      await page.keyboard.press('Enter')
      await waitFor(() => window.__duocad.lastFile?.action === 'export_step', '応答が返らない')
      const file = (await look()).lastFile
      return `パスを打ってEnter(${file.bytes} B)`
    })

    await attempt('human', 'export_stl', async () => {
      await page.click('#file-export-stl')
      await typeInto('#file-path', humanStl)
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
      await page.click('#file-import-step')
      await typeInto('#file-path', humanStep)
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
      await mcp.callTool('propose_selection', { refs: [mine[0]] })
      await mcp.callTool('commit_selection')
      await mcp.callTool('chamfer', { distance: 1 })
      return '述語で円い辺を選び、長さ1で面取りした'
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

    // ================================================================ 副次: 実物相当
    //
    // **別プロセス(空の文書)で作る**。`join` の押し出しは「対象が無ければ新しいソリッドを
    // 作る」verbなので、既に何ボディも居るこの受入用の文書では既存へ融合してしまい、
    // 独立した円筒が作れない(1回目の実行で実際に踏んだ)。
    console.log('\n--- 副次: 手だけで実物相当(全周円筒+円錐。空の文書で) ---')
    const solo = await startServer({ wsPort: '8887', httpPort: '8888' })
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
    console.log('\n=== RESULT_JSON ===')
    console.log(JSON.stringify(results, null, 2))
  } finally {
    if (browser) await browser.close()
    await server.stop()
  }
}

main().catch((error) => {
  console.error(`acceptance: FAIL ${error.message}`)
  console.log('\n=== RESULT_JSON ===')
  console.log(JSON.stringify(results, null, 2))
  process.exit(1)
})
