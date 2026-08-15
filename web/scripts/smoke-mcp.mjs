/**
 * 実機での確認(M4の最小の先取り・2026-08-11) — 「AIが操作すると人間の画面が変わる」ことを
 * 本物のMCPと本物のブラウザで見る。**このマイルストーンの意義そのものを機械検査する。**
 *
 *   node scripts/smoke-mcp.mjs [--step <STEPのパス>] [--port 8865] [--http-port 8866]
 *
 * `smoke.mjs`(M1/M3.5)との違いは**操作の出どころ**である。あちらは人間がブラウザを触る経路、
 * こちらは**AIがMCP経由でコアを叩く経路**で、ブラウザには一切触らずに画面が変わることを見る。
 *
 * 検査する4つ(タスクファイルの「検証(実機で)」):
 *   1. MCP経由の `describe_document` が **27件のBody** を返す
 *   2. MCP経由で `set_visible(false)` にすると **ブラウザの表示が実際に変わる**
 *      (= コアが変更イベントを発火し、アプリ層が購読して描き直す上り方向が動いている)
 *   3. その状態で **奥の面が本物のマウスクリックで選べる**
 *   4. **undo で可視性が戻る**(ブラウザのCtrl+Z。AIの操作と人間の操作が同じ操作ログに載る)
 *
 * サーバはこのスクリプトが `python -m duocad.mcp_server` として**空の文書で**起動する
 * (`--step` 起動オプションは廃止済み)。STEPはMCPの `import_step_editable` で読む。
 * CLIの `--step <path>` は「MCPで読み込むファイル」であり、サーバへは渡さない。
 * **先に `duocad.server` を立てておく必要はない**(むしろポートがぶつかる)。
 * ブラウザはインストール済みのEdge/Chromeを使う(`smoke.mjs` と同じ。`DUOCAD_BROWSER` で明示可)。
 */
import { existsSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { launch } from 'puppeteer-core'
import { PC_CASE_STEP, PC_CASE_SOLIDS } from './models.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO_ROOT = resolve(HERE, '..', '..')
const PYTHON = resolve(REPO_ROOT, '.venv', 'Scripts', 'python.exe')

const options = parseArgs(process.argv.slice(2))
const stepPath = options.step ?? PC_CASE_STEP
// 既定のポート(8765/8766)をわざと避ける: 本人が実機で開いているサーバを壊さないため。
const wsPort = options.port ?? '8865'
const httpPort = options['http-port'] ?? '8866'
const timeout = Number.parseInt(options.timeout ?? '180000', 10)
const expectedBodies = Number.parseInt(options['expect-bodies'] ?? String(PC_CASE_SOLIDS), 10)

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

function ok(message) {
  console.log(`  ok   ${message}`)
}

function fail(message) {
  throw new Error(message)
}

/**
 * MCPクライアント(stdio・JSON-RPC 2.0)。**Claude Code がやることと同じ**ことを最小限だけやる。
 * 行区切りのJSONで喋り、idでレスポンスを突き合わせる。
 */
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
      if (line) this.onLine(line)
      index = this.buffer.indexOf('\n')
    }
  }

  onLine(line) {
    let message
    try {
      message = JSON.parse(line)
    } catch (error) {
      // stdoutにJSON-RPC以外が出ていたら接続は壊れている。**黙って読み飛ばさない**。
      fail(`MCPサーバのstdoutにJSON-RPC以外の行が出た: ${line}`)
      return
    }
    const resolver = this.pending.get(message.id)
    if (!resolver) return
    this.pending.delete(message.id)
    resolver(message)
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

  /** ツールを呼んで、返ってきたJSONテキストを object で返す(isErrorなら失敗)。 */
  async callTool(name, args) {
    const response = await this.send('tools/call', { name, arguments: args ?? {} })
    if (response.error) fail(`${name} がプロトコルエラー: ${JSON.stringify(response.error)}`)
    if (response.result.isError) fail(`${name} が失敗: ${response.result.content[0].text}`)
    return JSON.parse(response.result.content[0].text)
  }
}

async function main() {
  const executablePath = CANDIDATE_BROWSERS.find((candidate) => existsSync(candidate))
  if (!executablePath) fail(`ブラウザが見つからない: ${CANDIDATE_BROWSERS.join(', ')}`)
  if (!existsSync(PYTHON)) fail(`venvのpythonが無い: ${PYTHON}`)
  if (!existsSync(stepPath)) fail(`STEPが無い: ${stepPath}`)

  console.log(`smoke-mcp: ${stepPath}`)
  console.log(`  browser: ${executablePath}`)

  // `--step` 起動オプションは廃止済み。空で立ち上げ、あとで MCP の import_step_editable で読む。
  const child = spawn(
    PYTHON,
    [
      '-m',
      'duocad.mcp_server',
      '--port',
      String(wsPort),
      '--http-port',
      String(httpPort),
    ],
    { cwd: REPO_ROOT, env: { ...process.env, PYTHONIOENCODING: 'utf-8' } },
  )
  const serverLog = []
  child.stderr.setEncoding('utf8')
  child.stderr.on('data', (chunk) => serverLog.push(chunk))
  child.on('exit', (code) => {
    if (code !== 0 && code !== null) {
      console.error(serverLog.join(''))
    }
  })

  const mcp = new McpClient(child)
  let browser = null

  try {
    // ---------------------------------------------------------------- MCPの握手
    const initialized = await mcp.send('initialize', {
      protocolVersion: '2025-06-18',
      capabilities: {},
      clientInfo: { name: 'duocad-smoke-mcp', version: '0.0.1' },
    })
    if (initialized.error) fail(`initialize が失敗: ${JSON.stringify(initialized.error)}`)
    mcp.notify('notifications/initialized', {})
    ok(
      `MCP接続: protocol ${initialized.result.protocolVersion} / ` +
        `${initialized.result.serverInfo.name} ${initialized.result.serverInfo.version}`,
    )

    const tools = (await mcp.send('tools/list')).result.tools.map((tool) => tool.name)
    // **【2026-08-12 F2で退役】** ここは `tools.length !== 3` で「3つだけ」を固定していたが、
    // F（F1+F2）が実装計画書§0の到達表のAI列を埋めてツールは28個になった。顔ぶれの上限を
    // 決める契約は `tests/test_mcp_verbs.py` にひとつだけ置く（同じ上限を複数箇所に書くと、
    // 片方の更新を忘れた瞬間に必ず食い違う——pytest側でも同じ穴を踏んでいる）。
    // ここに残すのは **M4の3つが名前を変えずに生き残っていること** の回帰ガードである。
    const m4Tools = ['describe_document', 'set_visible', 'describe_shape']
    const missing = m4Tools.filter((name) => !tools.includes(name))
    if (missing.length) fail(`M4のツールが消えている: ${missing.join(', ')}`)
    ok(`tools/list: ${tools.length}個（M4の3つを含む）`)

    // ツール名 == ログのverb名(2026-08-15にツールを2つへ割った)。
    const imported = await mcp.callTool('import_step_editable', { path: stepPath })
    if (imported.verb !== 'import_step_editable') {
      fail(`import_step_editable の verb が一致しない: ${imported.verb}`)
    }
    if (imported.body_ids.length !== expectedBodies) {
      fail(`編集可能なボディが ${expectedBodies} 個でない: ${imported.body_ids.length}`)
    }
    if (imported.reference_ids.length !== expectedBodies) {
      fail(`原本の参照ボディが ${expectedBodies} 個でない: ${imported.reference_ids.length}`)
    }
    ok(
      `MCPの import_step: 編集可能なボディ ${imported.body_ids.length} 個(原本 ${imported.reference_ids.length} 個は隠したまま)`,
    )

    // ---------------------------------------------------------------- 検証1: 27件のBody
    const document = await mcp.callTool('describe_document')
    if (document.body_count !== expectedBodies) {
      fail(`describe_document が ${expectedBodies} 件でない: ${document.body_count} 件`)
    }
    ok(
      `describe_document: ${document.body_count} 件のBody` +
        `(参照ボディ ${document.reference_count} 件は既定で出さない) / units=${document.units}`,
    )

    // ---------------------------------------------------------------- ブラウザを開く
    browser = await launch({
      executablePath,
      headless: true,
      args: [
        '--headless=new',
        '--use-gl=angle',
        '--use-angle=swiftshader',
        '--enable-unsafe-swiftshader',
        '--disable-gpu-sandbox',
        '--window-size=1280,860',
      ],
    })
    const page = await browser.newPage()
    await page.setViewport({ width: 1280, height: 860 })
    const consoleErrors = []
    page.on('pageerror', (error) => consoleErrors.push(String(error)))
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })
    page.on('response', (response) => {
      if (response.status() >= 400) consoleErrors.push(`HTTP ${response.status()} ${response.url()}`)
    })

    await page.goto(`http://127.0.0.1:${httpPort}/`, { waitUntil: 'load', timeout })
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        return Boolean(hook && hook.ready && hook.parts && hook.parts.length > 0)
      },
      { timeout, polling: 100 },
    )
    const partCount = await page.evaluate(() => window.__duocad.parts.length)
    if (partCount !== expectedBodies) fail(`画面のパーツ数が合わない: ${partCount}`)
    ok(`ブラウザ接続: パーツ一覧 ${partCount} 件(MCPが見ている文書と同じ)`)

    // 前回の中断に備えて全表示から始める(**MCP経由で**戻す)
    await setAllVisible(mcp, document.bodies, true)
    await page.waitForFunction(() => window.__duocad.parts.every((body) => body.visible), {
      timeout,
      polling: 50,
    })

    // ---------------------------------------------------------------- 検証3の下準備
    // 「奥の面」を持つ部品を選ぶ: **今まさに他のパーツに隠されて選べない面がある**部品。
    // `projectFace` は他Bodyに隠れている面に対して null を返す(viewport.ts)。
    const pickableBefore = await countPickable(page)
    const target = pickableBefore
      .filter((part) => part.pickable < part.total)
      .sort((a, b) => a.pickable / a.total - b.pickable / b.total)[0]
    if (!target) fail('他パーツに隠れている面を持つ部品が1つも無い(この検査が成立しない)')
    ok(
      `対象パーツ「${target.name}」: いま選べる面 ${target.pickable}/${target.total}` +
        `(残りは他の26部品に隠れている)`,
    )

    // ---------------------------------------------------------------- 検証2: 画面が変わる
    // **AIが手前のパーツを隠す**。ブラウザには一切触らない。
    const others = document.bodies.filter((body) => body.body_id !== target.body_id)
    const hideStarted = Date.now()
    for (const body of others) {
      await mcp.callTool('set_visible', { body_id: body.body_id, visible: false })
    }
    await page.waitForFunction(
      (bodyId) => {
        const parts = window.__duocad.parts
        return parts.every((part) => (part.body_id === bodyId ? part.visible : !part.visible))
      },
      { timeout, polling: 50 },
      target.body_id,
    )
    ok(
      `MCPで ${others.length} 部品を非表示 → **ブラウザの表示が変わった**` +
        `(${((Date.now() - hideStarted) / 1000).toFixed(1)}秒。ブラウザは一切触っていない)`,
    )
    // パーツ一覧のチェックボックスにも反映されている(画面の状態がコアと一致している)
    const checkedCount = await page.$$eval(
      '.parts-row input.visible-toggle',
      (elements) => elements.filter((element) => element.checked).length,
    )
    if (checkedCount !== 1) fail(`パーツ一覧のチェックが1件でない: ${checkedCount} 件`)
    ok('パーツ一覧のチェックボックスもAIの操作どおりになった')

    // ---------------------------------------------------------------- 検証3: 奥の面が選べる
    const pickableAfter = (await countPickable(page)).find(
      (part) => part.body_id === target.body_id,
    )
    if (pickableAfter.pickable <= target.pickable) {
      fail(
        `手前を隠したのに選べる面が増えていない: ${target.pickable} -> ${pickableAfter.pickable}`,
      )
    }
    ok(
      `選べる面が増えた: ${target.pickable}/${target.total} -> ` +
        `${pickableAfter.pickable}/${pickableAfter.total}(AIが隠したぶん奥が見えた)`,
    )

    // 「増えた」だけでなく、**さっきまで選べなかった面を本物のマウスでクリック**して確かめる。
    const newlyPickable = await page.evaluate(
      (bodyId, total, known) => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        for (let index = 1; index <= total; index += 1) {
          if (known.includes(index)) continue
          const position = window.__duocad.projectFace(bodyId, index)
          if (position) return { index, x: position.x + rect.left, y: position.y + rect.top }
        }
        return null
      },
      target.body_id,
      target.total,
      target.pickableIndices,
    )
    if (!newlyPickable) fail('新しく選べるようになった面が特定できない')
    await page.mouse.click(newlyPickable.x, newlyPickable.y)
    await page.waitForFunction(
      (bodyId, index) => {
        const selected = window.__duocad.selected
        return Boolean(selected && selected.bodyId === bodyId && selected.faceIndex === index)
      },
      { timeout, polling: 50 },
      target.body_id,
      newlyPickable.index,
    )
    ok(
      `**さっきまで隠れて選べなかった面 ${newlyPickable.index} を本物のクリックで選べた** ` +
        `@ (${newlyPickable.x.toFixed(0)}, ${newlyPickable.y.toFixed(0)})`,
    )

    // ---------------------------------------------------------------- 検証4: undoで戻る
    // AIの `set_visible` は人間の操作と同じ操作ログに載る。**ブラウザのCtrl+Z**で戻る。
    const lastHidden = others[others.length - 1]
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyZ')
    await page.keyboard.up('Control')
    await page.waitForFunction(
      (bodyId) => window.__duocad.parts.find((part) => part.body_id === bodyId).visible === true,
      { timeout, polling: 50 },
      lastHidden.body_id,
    )
    ok(
      `ブラウザのCtrl+Zで、AIが最後に隠した「${lastHidden.name}」が戻った` +
        `(AIの操作と人間の操作が同じ操作ログに載っている)`,
    )

    // 後始末: 全部表示へ戻す(次の実行のため)
    await setAllVisible(mcp, document.bodies, true)
    await page.waitForFunction(() => window.__duocad.parts.every((body) => body.visible), {
      timeout,
      polling: 50,
    })
    ok('全表示へ戻した')

    // ---------------------------------------------------------------- 検証5(B): 交互操作の一巡
    // **AIが述語で辺を提案 → ブラウザに提案として出る → 人がクリックで1本外す →
    //   AIが読むと外れている。** これが通れば、交互操作が人間側から見える形で成立している
    //   (タスクBの検証項番5)。ここまでの4つと違い、**人とAIが同じ集合を順に触る**。
    await mcp.callTool('clear_selection')
    let proposed = await mcp.callTool('propose_selection', {
      predicate: { kind: 'edge', body: target.body_id, where: [{ op: 'parallel_to', axis: 'Z' }] },
    })
    if (proposed.selection.count < 2) {
      // Zに平行な辺が足りない部品なら、その部品の辺全部を提案に使う(件数だけが目的)
      proposed = await mcp.callTool('propose_selection', {
        predicate: { kind: 'edge', body: target.body_id, where: [] },
      })
    }
    const proposedCount = proposed.selection.count
    if (proposedCount < 2) fail(`提案に使える辺が足りない: ${proposedCount} 本`)
    if (proposed.selection.origin !== 'proposed') {
      fail(`propose_selection の origin が proposed でない: ${proposed.selection.origin}`)
    }

    await page.waitForFunction(
      (count) => {
        const selection = window.__duocad.selection
        return selection?.count === count && selection.origin === 'proposed'
      },
      { timeout, polling: 50 },
      proposedCount,
    )
    // **提案は確定選択と見た目が違う**(枠の色と「AIの提案」の札)。承認の操作もここにある。
    const proposedLook = await page.evaluate(() => ({
      marked: Boolean(document.querySelector('.selection-panel.proposed')),
      badge: Boolean(document.querySelector('.selection-panel .badge')),
      commit: Boolean(document.querySelector('#selection-commit')),
      rows: document.querySelectorAll('.selection-row').length,
      summary: document.querySelector('#selection-summary').textContent.trim(),
    }))
    if (!proposedLook.marked || !proposedLook.badge) {
      fail('AIの提案が確定選択と同じ見た目で出ている(区別が付かない)')
    }
    if (!proposedLook.commit) fail('承認(commit_selection)の操作がUIに無い')
    if (proposedLook.rows !== proposedCount) {
      fail(`提案の件数が一覧に出ていない: ${proposedLook.rows} 行 / ${proposedCount} 本`)
    }
    ok(
      `AIが述語で辺 ${proposedCount} 本を提案 → ブラウザに**提案として**出た` +
        `(${proposedLook.summary} / 承認ボタンあり)`,
    )

    // ---------------------------------------------------------------- 人が1本外す
    const droppedIndex = await page.evaluate(() => {
      const ref = document.querySelector('.selection-row').dataset.ref // "edge:<body>:<index>"
      return Number.parseInt(ref.slice(ref.lastIndexOf(':') + 1), 10)
    })
    await page.click('.selection-row .selection-remove')
    await page.waitForFunction(
      (count) => window.__duocad.selection?.count === count,
      { timeout, polling: 50 },
      proposedCount - 1,
    )
    ok(`人がブラウザで辺 ${droppedIndex} を1本外した(${proposedCount} -> ${proposedCount - 1} 本)`)

    // ---------------------------------------------------------------- AIが読むと外れている
    const afterHuman = await mcp.callTool('get_selection')
    if (afterHuman.selection.count !== proposedCount - 1) {
      fail(
        `AIが読んだ選択の件数が合わない: ${afterHuman.selection.count} / ` +
          `期待 ${proposedCount - 1}`,
      )
    }
    if (afterHuman.selection.items.some((item) => item.index === droppedIndex)) {
      fail(`人が外した辺 ${droppedIndex} がAI側にまだ残っている`)
    }
    if (afterHuman.selection.origin !== 'user') {
      fail(`人が触ったのに origin が user でない: ${afterHuman.selection.origin}`)
    }
    ok(
      `**AIが get_selection で読むと外れている**(${afterHuman.selection.count} 本 / ` +
        `origin=user = 人が触ったことが承認になっている)`,
    )
    ok('→ 交互操作(AIが提案 → 人が過不足を直す → AIが読む)が一巡した')

    await mcp.callTool('clear_selection')

    // ---------------------------------------------------------------- C: 手で描いたものがAIにも見える
    // タスクCの検証6。**片方にしか無い状態を作らない** —— 人がブラウザで描いた図形が
    // AIの `describe_document` に出なければ、UIとコアが別の文書を見ていることになる。
    await page.click('#sketch-new-XY')
    await page.waitForFunction(() => window.__duocad.activeSketch !== null, {
      timeout,
      polling: 50,
    })
    await page.click('#sketch-circle')
    const center = await page.evaluate(() => {
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      const position = window.__duocad.projectSketchPoint(0, 0)
      return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
    })
    if (!center) fail('スケッチ平面の原点が画面の中に無い(手で描けない)')
    await page.mouse.click(center.x, center.y)
    await page.click('#sketch-radius')
    await page.type('#sketch-radius', '15', { delay: 20 })
    await page.keyboard.press('Enter')
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === 1)
      },
      { timeout, polling: 50 },
    )
    const drawnSketchId = await page.evaluate(() => window.__duocad.activeSketch)

    // `include_regions` を明示する(既定では領域を数えない=重いため)
    const afterDrawing = await mcp.callTool('describe_document', { include_regions: true })
    const seenByAi = (afterDrawing.sketches ?? []).find((item) => item.sketch_id === drawnSketchId)
    if (!seenByAi) {
      fail(
        `人が描いたスケッチ ${drawnSketchId} がAIから見えない: ` +
          `${JSON.stringify((afterDrawing.sketches ?? []).map((item) => item.sketch_id))}`,
      )
    }
    const drawnCircle = (seenByAi.entities ?? []).find((entity) => entity.kind === 'circle')
    if (!drawnCircle) fail(`AI側に円が無い: ${JSON.stringify(seenByAi.entities)}`)
    if (Math.abs(drawnCircle.params.radius - 15) > 1e-9) {
      fail(`AIが読んだ半径が打った値と違う: ${drawnCircle.params.radius} != 15`)
    }
    if (seenByAi.region_count !== 1) fail(`AI側の領域数が1でない: ${seenByAi.region_count}`)
    ok(
      `**人がブラウザで描いた円が、AIの describe_document にそのまま見えている**` +
        `(半径 ${drawnCircle.params.radius} / 領域 ${seenByAi.region_count} 個)`,
    )

    if (consoleErrors.length > 0) {
      fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    }
    ok('ブラウザのコンソールにエラーなし')

    console.log('smoke-mcp: PASS')
  } finally {
    if (browser) await browser.close()
    child.stdin.end() // EOF = MCPクライアントが切断したときと同じ終わり方
    await new Promise((done) => child.once('exit', done))
  }
}

/** 各パーツについて「いま画面で選べる面の数」を数える(`projectFace` が null なら選べない)。 */
async function countPickable(page) {
  return page.evaluate(() => {
    return window.__duocad.parts.map((part) => {
      const pickableIndices = []
      for (let index = 1; index <= part.face_count; index += 1) {
        if (window.__duocad.projectFace(part.body_id, index)) pickableIndices.push(index)
      }
      return {
        body_id: part.body_id,
        name: part.name,
        total: part.face_count,
        pickable: pickableIndices.length,
        pickableIndices,
      }
    })
  })
}

async function setAllVisible(mcp, bodies, visible) {
  for (const body of bodies) {
    await mcp.callTool('set_visible', { body_id: body.body_id, visible })
  }
}

main().catch((error) => {
  console.error(`smoke-mcp: FAIL — ${error.message}`)
  process.exitCode = 1
})
