/**
 * v1の受入(タスクG・2026-08-14) — **19操作を人間側とAI側の両方で実際に動かす**。
 *
 * 実装計画書§0の定義:
 *
 * > v1 = いま実装済みのverbすべてを、人間とAIの両方が満足に使える状態。
 *
 * このスクリプトは**判定の材料を作る**ものであり、合格を作りに行くものではない。
 * 各操作は独立して試し、落ちても止めずに ❌ と理由を残す(推測で ✅ にしない)。
 *
 *   duocad> node web/scripts/acceptance.mjs
 *
 * サーバは**このスクリプトが `python -m duocad.mcp_server` として空の文書で起動する**
 * (`smoke-mcp.mjs` と同じ形。`--no-step` は廃止済み)。**1つのプロセスがMCPとWebSocketの両方を持つ**ので、
 * AIが叩いた結果が同じ文書としてブラウザに届く —— 交互操作の検証はこれが前提である。
 *
 * 見るのは3つ:
 *
 * 1. **到達表**: 19操作 × 人間/AI を実際に動かして ✅ / ❌
 * 2. **交互操作**: AIが述語で辺を提案 → ブラウザに提案として出る → 人がクリックで直す →
 *    AIが読み直して fillet → 形が変わる(**この企画の核心**)
 * 3. **副次**: 手だけで実物相当(全周円筒+円錐)を作る
 *
 * 結果はJSONで stdout の末尾へ出す(`ACCEPTANCE.md` はそれを見て人が書く)。
 */
import { existsSync, mkdtempSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { launch } from 'puppeteer-core'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(HERE, '..', '..')
const PYTHON = resolve(REPO_ROOT, '.venv', 'Scripts', 'python.exe')

const options = parseArgs(process.argv.slice(2))
const wsPort = options.port ?? '8885'
const httpPort = options['http-port'] ?? '8886'
const timeout = Number.parseInt(options.timeout ?? '120000', 10)
const workdir = options.workdir ?? mkdtempSync(join(tmpdir(), 'duocad-g-'))

function parseArgs(argv) {
  const parsed = {}
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i]
    if (!item.startsWith('--')) continue
    const key = item.slice(2)
    const next = argv[i + 1]
    if (next && !next.startsWith('--')) {
      parsed[key] = next
      i += 1
    } else {
      parsed[key] = 'true'
    }
  }
  return parsed
}

const CANDIDATE_BROWSERS = [
  process.env.DUOCAD_BROWSER,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
].filter(Boolean)

/** 到達表の19操作(実装計画書§0)。**この並びのまま ACCEPTANCE.md の表になる**。 */
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
  'make_editable_copy',
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

class McpClient {
  constructor(child) {
    this.child = child
    this.nextId = 1
    this.pending = new Map()
    this.buffer = ''
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk) => this.onData(chunk))
  }

  onData(chunk) {
    this.buffer += chunk
    let index = this.buffer.indexOf('\n')
    while (index >= 0) {
      const line = this.buffer.slice(0, index).trim()
      this.buffer = this.buffer.slice(index + 1)
      if (line) {
        // **JSON以外の行は捨てずに控える**。stdoutはJSON-RPC専用なので、混ざったら
        // それ自体が不具合(OCCTがC++から実fdへ吐く統計がこれ)。ここで例外にすると
        // 受入検証そのものが止まってしまうので、記録して続ける。
        let message
        try {
          message = JSON.parse(line)
        } catch {
          results.stdoutNoise = [...(results.stdoutNoise ?? []), line.slice(0, 120)]
          continue
        }
        const resolver = this.pending.get(message.id)
        if (resolver) {
          this.pending.delete(message.id)
          resolver(message)
        }
      }
      index = this.buffer.indexOf('\n')
    }
  }

  send(method, params) {
    const id = this.nextId
    this.nextId += 1
    const promise = new Promise((resolvePending, rejectPending) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        rejectPending(new Error(`MCPの応答が来ない: ${method}`))
      }, timeout)
      this.pending.set(id, (message) => {
        clearTimeout(timer)
        resolvePending(message)
      })
    })
    this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`)
    return promise
  }

  notify(method, params) {
    this.child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method, params })}\n`)
  }

  async call(name, args) {
    const response = await this.send('tools/call', { name, arguments: args ?? {} })
    if (response.error) throw new Error(`${name}: ${JSON.stringify(response.error)}`)
    if (response.result.isError) throw new Error(`${name}: ${response.result.content[0].text}`)
    return JSON.parse(response.result.content[0].text)
  }
}

async function main() {
  const executablePath = CANDIDATE_BROWSERS.find((candidate) => existsSync(candidate))
  if (!executablePath) throw new Error(`ブラウザが見つからない: ${CANDIDATE_BROWSERS.join(', ')}`)
  if (!existsSync(PYTHON)) throw new Error(`venvのpythonが無い: ${PYTHON}`)

  console.log(`acceptance: ws=${wsPort} http=${httpPort} workdir=${workdir}`)
  const child = spawn(
    PYTHON,
    ['-m', 'duocad.mcp_server', '--port', String(wsPort), '--http-port', String(httpPort)],
    { cwd: REPO_ROOT, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } },
  )
  const serverLog = []
  child.stderr.setEncoding('utf8')
  child.stderr.on('data', (chunk) => serverLog.push(chunk))

  const mcp = new McpClient(child)
  let browser = null

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

    browser = await launch({
      executablePath,
      headless: true,
      args: [
        '--headless=new',
        '--use-gl=angle',
        '--use-angle=swiftshader',
        '--enable-unsafe-swiftshader',
        '--disable-gpu-sandbox',
        '--window-size=1280,900',
      ],
    })
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

    // ================================================================ 人間側の19操作
    console.log('\n--- 人間側(ブラウザを本物のクリックとキーで操作) ---')

    await attempt('human', 'sketch_create', async () => {
      await page.click('#sketch-new-XY')
      await waitFor(() => window.__duocad.activeSketch !== null, 'スケッチに入れない')
      return 'XY平面にスケッチを作り、そのままモードへ入った'
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
      await page.keyboard.press('Enter')
      await waitFor(
        () => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return Boolean(active && active.entities.some((entity) => entity.kind === 'line'))
        },
        '線分が置けない',
      )
      return '2点をクリック(ゴムひもで確定前の線が見える)'
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
      await page.click('#undo') // この後の受入で邪魔になるので戻す
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
      await page.click('#undo')
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
    await page.click('#view-fit')
    const bodyId = (await look()).parts[0].id

    await attempt('human', 'extrude_face', async () => {
      const before = (await look()).parts[0].volume
      const face = await page.evaluate((id) => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const geometry = window.__duocad.bodyGeometry[id]
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
      await page.click('#undo')
      await waitFor(
        (previous) => window.__duocad.parts[0].face_count < previous,
        'undoで戻らない',
        before,
      )
      return `undoボタンでフィレットが戻った(面 ${before} → ${(await look()).parts[0].faces})`
    })

    await attempt('human', 'redo', async () => {
      const before = (await look()).parts[0].faces
      await page.click('#redo')
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
      const entry = await page.evaluate(() => {
        const entries = Object.entries(window.__duocad.parameters)
        const hit = entries.find(([, value]) => Math.abs(value - 60) < 1e-9) // 長方形の幅
        return hit ? { name: hit[0], value: hit[1] } : null
      })
      if (!entry) throw new Error('長方形の幅がパラメータ一覧に出ていない')
      await typeInto(`[id="parameter-${entry.name}"]`, 50)
      await page.keyboard.press('Enter')
      await waitFor(
        (previous) => Math.abs(window.__duocad.parts[0].volume - previous) > 1e-6,
        '打ち直しても形が追随しない',
        before,
      )
      const after = (await look()).parts[0].volume
      return `パラメータ一覧で ${entry.name} を 60 → 50 に打ち直した(体積 ${before.toFixed(2)} → ${after.toFixed(2)})`
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
      const before = (await look()).bodies.length
      await page.click('#file-import-step')
      await typeInto('#file-path', humanStep)
      await page.keyboard.press('Enter')
      await waitFor(
        (previous) => window.__duocad.state.bodies.length > previous,
        'STEPを読み込めない',
        before,
      )
      return '自分で書き出したSTEPを読み込んだ(そのまま編集できるボディができる)'
    })

    await attempt('human', 'make_editable_copy', async () => {
      await page.click('#parts-hide-all')
      await waitFor(() => window.__duocad.parts.every((body) => !body.visible), '編集用を隠せない')
      // 参照は隠れていて指せない。直前の import で増えた編集用ボディ(parts の末尾)を表示して面を指す。
      const importedId = await page.evaluate(
        () => window.__duocad.parts[window.__duocad.parts.length - 1].body_id,
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
      const point = await page.evaluate((id) => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        for (let index = 1; index <= 30; index += 1) {
          const position = window.__duocad.projectFace(id, index)
          if (position) return { x: position.x + rect.left, y: position.y + rect.top, index }
        }
        return null
      }, importedId)
      if (!point) throw new Error('読み込んだボディの面が画面から指せない')
      await page.mouse.click(point.x, point.y)
      await waitFor(
        (id) =>
          (window.__duocad.selection?.items ?? []).some(
            (item) => item.body === id && item.kind === 'face',
          ),
        '読み込んだボディの面を選べない',
        importedId,
      )
      const before = (await look()).parts.length
      await page.click('#file-make-editable')
      await page.keyboard.press('Enter')
      await waitFor(
        (previous) => window.__duocad.parts.length > previous,
        '編集可能なコピーができない',
        before,
      )
      await page.click('#parts-show-all')
      return '面を選んで編集可能なコピーを作った(複製の経路)'
    })

    // ================================================================ AI側の19操作
    console.log('\n--- AI側(MCPツールを本物のJSON-RPCで呼ぶ) ---')
    // **同じサーバ・同じ文書**を触る(このスクリプトが起動したのは mcp_server 1つだけ)。
    let aiSketch = null
    let aiCircle = null

    await attempt('ai', 'sketch_create', async () => {
      const result = await mcp.call('sketch_create', { base_plane: 'XZ' })
      aiSketch = result.sketch_id
      return `XZ平面に作った(${aiSketch})`
    })

    await attempt('ai', 'add_circle', async () => {
      const result = await mcp.call('add_circle', { sketch_id: aiSketch, center: [0, 0], radius: 12 })
      aiCircle = result.entity_id
      return `半径12の円(${aiCircle})`
    })

    await attempt('ai', 'add_line', async () => {
      await mcp.call('add_line', { sketch_id: aiSketch, p0: [40, 0], p1: [60, 0] })
      return '線分を1本'
    })

    await attempt('ai', 'add_rectangle', async () => {
      await mcp.call('add_rectangle', { sketch_id: aiSketch, corner: [40, 20], width: 10, height: 6 })
      return '長方形を1つ'
    })

    await attempt('ai', 'add_arc', async () => {
      await mcp.call('add_arc', {
        sketch_id: aiSketch,
        center: [-40, 0],
        radius: 6,
        start_deg: 0,
        end_deg: 90,
      })
      return '円弧を1つ'
    })

    await attempt('ai', 'pattern_rectangular', async () => {
      const rectangle = await mcp.call('add_rectangle', {
        sketch_id: aiSketch,
        corner: [60, 40],
        width: 4,
        height: 4,
      })
      await mcp.call('pattern_rectangular', {
        sketch_id: aiSketch,
        entities: [rectangle.entity_id],
        dx: 6,
        dy: 0,
        nx: 3,
        ny: 1,
      })
      await mcp.call('undo')
      await mcp.call('undo')
      return '3×1に反復して確認し、undoで戻した(図形も消した)'
    })

    await attempt('ai', 'pattern_circular', async () => {
      const dot = await mcp.call('add_circle', { sketch_id: aiSketch, center: [70, 0], radius: 2 })
      await mcp.call('pattern_circular', {
        sketch_id: aiSketch,
        entities: [dot.entity_id],
        center: [80, 0],
        count: 4,
        total_angle: 360,
      })
      await mcp.call('undo')
      await mcp.call('undo')
      return '全周4個に反復して確認し、undoで戻した'
    })

    await attempt('ai', 'extrude', async () => {
      // **AIから見える領域は「何個あるか」だけ**(`describe_document` は `region_count`)。
      // どれが目的の領域かは `propose_selection` → `measure` の往復で確かめる ——
      // これは提案を確定前に検算する経路そのもの(§0のAI側の条件)。
      const document = await mcp.call('describe_document', { include_regions: true })
      const sketch = document.sketches.find((item) => item.sketch_id === aiSketch)
      let best = null
      for (let index = 1; index <= sketch.region_count; index += 1) {
        await mcp.call('propose_selection', { refs: [{ sketch: aiSketch, index }] })
        const measured = await mcp.call('measure', {})
        const area = measured.items[0]?.area ?? 0
        if (!best || area > best.area) best = { index, area }
      }
      if (!best) throw new Error(`領域が1つも無い(region_count=${sketch.region_count})`)
      await mcp.call('propose_selection', { refs: [{ sketch: aiSketch, index: best.index }] })
      await mcp.call('commit_selection')
      const result = await mcp.call('extrude', { distance: 20, mode: 'join' })
      return `${sketch.region_count}個の領域を measure で確かめて最大(面積 ${best.area.toFixed(1)})を選び、距離20で押し出した(seq ${result.seq})`
    })

    let aiBody = null
    await attempt('ai', 'extrude_face', async () => {
      const document = await mcp.call('describe_document', {})
      aiBody = document.bodies.filter((body) => body.kind === 'solid').slice(-1)[0].body_id
      const proposed = await mcp.call('propose_selection', {
        predicate: { kind: 'face', where: [{ op: 'kind_is', value: 'plane' }] },
      })
      const face = proposed.selection.items.find((item) => item.body === aiBody)
      if (!face) throw new Error(`このボディの平面が述語で1枚も選べない: ${JSON.stringify(proposed.selection.items.slice(0, 3))}`)
      await mcp.call('propose_selection', { refs: [face] })
      await mcp.call('commit_selection')
      const result = await mcp.call('extrude_face', { distance: 3 })
      return `述語で平面を選び直し、3mm押し出した(seq ${result.seq})`
    })

    await attempt('ai', 'fillet', async () => {
      const proposed = await mcp.call('propose_selection', {
        predicate: { kind: 'edge', where: [{ op: 'kind_is', value: 'circle' }] },
      })
      const mine = proposed.selection.items.filter((item) => item.body === aiBody)
      if (mine.length === 0) throw new Error('円い辺が述語で選べない')
      await mcp.call('propose_selection', { refs: [mine[0]] })
      await mcp.call('commit_selection')
      const result = await mcp.call('fillet', { radius: 1 })
      return `述語で円い辺を選び、半径1で丸めた(面 ${result.face_count ?? '?'})`
    })

    await attempt('ai', 'chamfer', async () => {
      const proposed = await mcp.call('propose_selection', {
        predicate: { kind: 'edge', where: [{ op: 'kind_is', value: 'circle' }] },
      })
      const mine = proposed.selection.items.filter((item) => item.body === aiBody)
      if (mine.length === 0) throw new Error('円い辺が述語で選べない')
      await mcp.call('propose_selection', { refs: [mine[0]] })
      await mcp.call('commit_selection')
      await mcp.call('chamfer', { distance: 1 })
      return '述語で円い辺を選び、長さ1で面取りした'
    })

    await attempt('ai', 'set_parameter', async () => {
      const document = await mcp.call('describe_document', {})
      const sketch = document.sketches.find((item) => item.sketch_id === aiSketch)
      const circle = sketch.entities.find((item) => item.entity_id === aiCircle)
      const name = circle.parameters.radius
      await mcp.call('set_parameter', { name, value: 14 })
      return `${name} を 12 → 14 にした(describe_document の parameters から辿れる)`
    })

    await attempt('ai', 'set_visible', async () => {
      await mcp.call('set_visible', { body_id: aiBody, visible: false })
      await page.waitForFunction(
        (id) => {
          const body = window.__duocad.state.bodies.find((item) => item.body_id === id)
          return body && body.visible === false
        },
        { timeout: 30000, polling: 50 },
        aiBody,
      )
      await mcp.call('set_visible', { body_id: aiBody, visible: true })
      return 'AIが隠すとブラウザの表示も変わった(上り方向)'
    })

    const aiStep = join(workdir, 'ai.step')
    await attempt('ai', 'export_step', async () => {
      const result = await mcp.call('export_step', { path: aiStep })
      return `${result.bytes} B`
    })

    await attempt('ai', 'export_stl', async () => {
      const result = await mcp.call('export_stl', { path: join(workdir, 'ai.stl'), deviation: 0.05 })
      return `${result.bytes} B(偏差0.05)`
    })

    let aiReference = null
    await attempt('ai', 'import_step', async () => {
      const result = await mcp.call('import_step', { path: aiStep })
      aiReference = result.body_ids[0]
      return `編集可能なボディ ${result.body_ids.length} 個`
    })

    await attempt('ai', 'make_editable_copy', async () => {
      const result = await mcp.call('make_editable_copy', { source_body_id: aiReference })
      return `編集用ボディ ${result.body_id} を作った`
    })

    await attempt('ai', 'undo', async () => {
      const result = await mcp.call('undo')
      return `${result.undone} を取り消した`
    })

    await attempt('ai', 'redo', async () => {
      const result = await mcp.call('redo')
      return `${result.redone} をやり直した`
    })

    // ================================================================ 交互操作(核心)
    console.log('\n--- 交互操作(AIが提案 → 画面に出る → 人が直す → AIが適用) ---')
    try {
      // 1. AIが述語で辺の集合を提案する
      const proposed = await mcp.call('propose_selection', {
        predicate: { kind: 'edge', where: [{ op: 'parallel_to', axis: 'Y' }] },
      })
      const proposedItems = proposed.selection.items
      if (proposedItems.length === 0) throw new Error('述語で辺が1本も選べない')

      // 2. **ブラウザに「提案」として出る**(確定選択と違う色・選択パネルにバッジ)
      await page.waitForFunction(
        (count) => {
          const selection = window.__duocad.selection
          return Boolean(selection && selection.origin === 'proposed' && selection.count === count)
        },
        { timeout: 30000, polling: 50 },
        proposedItems.length,
      )
      const badge = await page.$eval('#selection-summary', (element) => ({
        origin: element.dataset.origin,
        text: element.textContent.trim(),
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
      await page.click('#view-fit') // 提案されたボディが画面の外にあることがある
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
      const readBack = (await mcp.call('get_selection', {})).selection
      const targetBody = readBack.items[0].body
      const facesBefore = (await mcp.call('describe_shape', { body_id: targetBody })).face_count
      const applied = await mcp.call('fillet', { radius: 1.5 })
      const facesAfter = (await mcp.call('describe_shape', { body_id: targetBody })).face_count

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
    const solo = spawn(
      PYTHON,
      ['-m', 'duocad.mcp_server', '--port', '8887', '--http-port', '8888'],
      { cwd: REPO_ROOT, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } },
    )
    solo.stderr.resume()
    const soloMcp = new McpClient(solo)
    try {
      await soloMcp.send('initialize', {
        protocolVersion: '2025-06-18',
        capabilities: {},
        clientInfo: { name: 'duocad-acceptance-solo', version: '0.0.1' },
      })
      soloMcp.notify('notifications/initialized', {})
      const sketch = await soloMcp.call('sketch_create', { base_plane: 'XY' })
      await soloMcp.call('add_circle', { sketch_id: sketch.sketch_id, center: [0, 0], radius: 12.5 })
      await soloMcp.call('propose_selection', { refs: [{ sketch: sketch.sketch_id, index: 1 }] })
      await soloMcp.call('commit_selection')
      await soloMcp.call('extrude', { distance: 22, mode: 'join' })
      const body = (await soloMcp.call('describe_document', {})).bodies[0].body_id
      const cylinder = await soloMcp.call('describe_shape', { body_id: body })
      const proposed = await soloMcp.call('propose_selection', {
        predicate: { kind: 'edge', where: [{ op: 'kind_is', value: 'circle' }] },
      })
      await soloMcp.call('propose_selection', { refs: [proposed.selection.items[0]] })
      await soloMcp.call('commit_selection')
      await soloMcp.call('chamfer', { distance: 2 })
      const after = await soloMcp.call('describe_shape', { body_id: body })
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
      solo.kill()
    }

    results.consoleErrors = consoleErrors.slice(0, 5)
    results.screenMessages = (await look()).messages
    console.log('\n=== RESULT_JSON ===')
    console.log(JSON.stringify(results, null, 2))
  } finally {
    if (browser) await browser.close()
    child.kill()
  }
}

main().catch((error) => {
  console.error(`acceptance: FAIL ${error.message}`)
  console.log('\n=== RESULT_JSON ===')
  console.log(JSON.stringify(results, null, 2))
  process.exit(1)
})
