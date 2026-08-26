/**
 * 実機での確認 — 「AIが操作すると人間の画面が変わる」ことを
 * 本物のMCPと本物のブラウザで見る。**このマイルストーンの意義そのものを機械検査する。**
 *
 *   node scripts/smoke-mcp.mjs [--step <STEPのパス>] [--port 8865] [--http-port 8866]
 *
 * `smoke.mjs` との違いは**操作の出どころ**である。あちらは人間がブラウザを触る経路、
 * こちらは**AIがMCP経由でコアを叩く経路**で、ブラウザには一切触らずに画面が変わることを見る。
 *
 * 検査する4つ:
 *   1. MCP経由の `describe_document` が **27件のBody** を返す
 *   2. MCP経由で `set_visible(false)` にすると **ブラウザの表示が実際に変わる**
 *      (= コアが変更イベントを発火し、アプリ層が購読して描き直す上り方向が動いている)
 *   3. その状態で **奥の面が本物のマウスクリックで選べる**
 *   4. **AIが積んだ操作をブラウザのCtrl+Zで取り消せる**(同じ操作ログを共有している)。
 *      あわせて、**その同じCtrl+Zで可視性は戻らない**(`set_visible` は操作ログに載らない)
 *
 * サーバはこのスクリプトが `python -m duocad.mcp_server` として**空の文書で**起動する。
 * STEPはMCPの `import_step_editable` で読む。
 * CLIの `--step <path>` は「MCPで読み込むファイル」であり、サーバへは渡さない。
 * **別のサーバを先に立てておく必要はない**(むしろポートがぶつかる)。
 * ブラウザはインストール済みのEdge/Chromeを使う(`smoke.mjs` と同じ。`DUOCAD_BROWSER` で明示可)。
 */
import { existsSync } from 'node:fs'
import { findBrowser, launchBrowser, frameAll } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'
import { PC_CASE_STEP, PC_CASE_SOLIDS } from './models.mjs'


const options = parseArgs(process.argv.slice(2))
const stepPath = options.step ?? PC_CASE_STEP
// 既定のポート(8765/8766)をわざと避ける: 本人が実機で開いているサーバを壊さないため。
const wsPort = options.port ?? '8865'
const httpPort = options['http-port'] ?? '8866'
const timeout = Number.parseInt(options.timeout ?? '180000', 10)
const expectedBodies = Number.parseInt(options['expect-bodies'] ?? String(PC_CASE_SOLIDS), 10)

/** 3Dハイライトの色(`viewport.ts` の `HIGHLIGHT_COLOR` / `PROPOSED_COLOR`)。
    **選択パネルを廃した後、提案と確定を区別する唯一の手段**なので、値で検査する。 */
const USER_HIGHLIGHT = 0xffb454
const PROPOSED_HIGHLIGHT = 0x36d1ff




async function main() {
  const executablePath = findBrowser()
  if (!existsSync(stepPath)) fail(`STEPが無い: ${stepPath}`)

  console.log(`smoke-mcp: ${stepPath}`)
  console.log(`  browser: ${executablePath}`)

  const server = await startServer({ wsPort, httpPort })
  const mcp = new McpClient(server.child, { timeout })
  let browser = null

  try {
    // ---------------------------------------------------------------- MCPの握手
    const info = await mcp.handshake('duocad-smoke-mcp')
    ok(`MCP接続: protocol ${info.protocolVersion} / ${info.serverInfo.name} ${info.serverInfo.version}`)

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
    browser = await launchBrowser(executablePath)
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
    /**
     * **起きないことを見る前の猶予**。適用が走るなら「選択の配信 → 適用 → 状態配信」で
     * 済む時間を過ぎてから数える —— 待たずに数えると、走っていても
     * 「まだ返ってきていないだけ」を緑と読んでしまう。
     */
    const settle = () => new Promise((resolve) => setTimeout(resolve, 1500))

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

    // ------------------------------------------ 検証3b: AIが選択を変えても即適用は走らない
    // 「編集用に複製」はEnterの要らないコマンド(`command.ts` の `needsEnter`)で、
    // **起動した後に引数が揃うと即座に適用される**。その引き金は**人がこの画面で
    // 指したこと**でなければならない —— 選択集合はAIも `modify_selection` で書き換えられる
    // ので、「面が1つ選ばれた」だけで発火させると、**人が何も触っていないのに部品が増える**。
    // ここはその境目を実機で固定する(2026-08-22のQA指摘)。
    await mcp.callTool('clear_selection')
    await page.waitForFunction(() => (window.__duocad.selection?.items ?? []).length === 0, {
      timeout,
      polling: 50,
    })
    const partsBeforeAi = await page.evaluate(() => window.__duocad.parts.length)
    await page.click('#file-make-editable') // 人が起動する(ここまでは人の1手)
    await mcp.callTool('modify_selection', {
      // **AIが面を1つ選ぶ**。`origin` は "user" になる(MCPの `modify_selection` は
      // 提案ではなく確定選択を直す経路)ので、選択の中身だけを見ていると人のクリックと区別が付かない。
      add: [{ body: target.body_id, kind: 'face', index: newlyPickable.index }],
    })
    await page.waitForFunction(
      () => {
        const selection = window.__duocad.selection
        return Boolean(selection && selection.count === 1 && selection.origin === 'user')
      },
      { timeout, polling: 50 },
    )
    await settle()
    const afterAiSelection = await page.evaluate(() => {
      const host = document.querySelector('.command-host')
      return {
        parts: window.__duocad.parts.length,
        command: host.dataset.command,
        phase: host.dataset.phase,
      }
    })
    if (afterAiSelection.parts !== partsBeforeAi) {
      fail(
        `AIが選択を書き換えただけで「編集用に複製」が走った` +
          `(部品 ${partsBeforeAi} -> ${afterAiSelection.parts})`,
      )
    }
    // **空振りで緑になっていないこと**を同時に見る: コマンドは起動したままで、
    // 引数も揃っている(=Enterを押せば適用される状態)。それでも走っていない。
    if (afterAiSelection.command !== 'make_editable_copy' || afterAiSelection.phase !== 'ready') {
      fail(
        `この検査が空振りしている(command=${afterAiSelection.command} / ` +
          `phase=${afterAiSelection.phase})`,
      )
    }
    ok('**AIが選択を書き換えても即適用は発火しない**(引数は揃っていて、Enterなら適用できる状態のまま)')

    // 同じ起動のまま**人が別の面をクリックすると発火する** ——
    // 上の検査が「そもそも発火しない実装」で緑になっていないことの裏取り。
    const otherFace = await page.evaluate(
      (bodyId, total, skip) => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        for (let index = 1; index <= total; index += 1) {
          if (index === skip) continue
          const position = window.__duocad.projectFace(bodyId, index)
          if (position) return { index, x: position.x + rect.left, y: position.y + rect.top }
        }
        return null
      },
      target.body_id,
      target.total,
      newlyPickable.index,
    )
    if (!otherFace) fail('2つ目の面が画面から指せない(この裏取りが成立しない)')
    await page.mouse.click(otherFace.x, otherFace.y)
    await page.waitForFunction(
      (expected) => window.__duocad.parts.length === expected,
      { timeout, polling: 50 },
      partsBeforeAi + 1,
    )
    ok(`**人が面 ${otherFace.index} をクリックしたときだけ**複製が走った(Enterは押していない)`)

    // 後始末: いま作った複製を戻し、既定のコマンドへ降りる
    // (この後の検証4はundoの位置に依存している)
    await mcp.callTool('undo')
    await page.waitForFunction(
      (expected) => window.__duocad.parts.length === expected,
      { timeout, polling: 50 },
      partsBeforeAi,
    )
    await page.keyboard.press('Escape')

    // ------------------------------------------------- 検証4: 同じ操作ログ / 可視性は履歴の外
    // **AIが積んだ操作を、人間がブラウザのCtrl+Zで取り消せる**(同じ操作ログを共有している)。
    // 材料に可視性を使わない —— `set_visible` は操作ログに載らないので(2026-08-26)、
    // **同じCtrl+Zを押しても隠れたまま**である。戻ってしまうと、奥を覗くために手前を隠した
    // だけで直前の編集へ戻れなくなる。1回のCtrl+Zで両方を見る。
    const lastHidden = others[others.length - 1]
    await mcp.callTool('make_editable_copy', { source_body_id: target.body_id })
    await page.waitForFunction(
      (expected) => window.__duocad.parts.length === expected,
      { timeout, polling: 50 },
      partsBeforeAi + 1,
    )
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyZ')
    await page.keyboard.up('Control')
    await page.waitForFunction(
      (expected) => window.__duocad.parts.length === expected,
      { timeout, polling: 50 },
      partsBeforeAi,
    )
    ok('ブラウザのCtrl+Zで、AIが積んだ複製が取り消せた(AIと人間が同じ操作ログを共有している)')

    const stillHidden = await page.evaluate(
      (bodyId) => window.__duocad.parts.find((part) => part.body_id === bodyId).visible,
      lastHidden.body_id,
    )
    if (stillHidden !== false) {
      fail(`Ctrl+Zで「${lastHidden.name}」の非表示まで戻った(可視性は undo/redo の対象外のはず)`)
    }
    ok(`同じCtrl+Zでも「${lastHidden.name}」は隠れたまま(可視性は undo/redo の対象外)`)

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
    // **提案は確定選択と違う色で出る。** 2026-08-23に選択パネルを廃したので、提案(水色)と
    // 確定(橙)の区別を人へ伝えるのは**3Dのハイライト色だけ**になった —— その色そのものを
    // 読んで確かめる(`viewport.highlightColors`)。辺が3Dに出るのは辺を要求している
    // コマンドを起動している間だけなので、人がやるとおり先にフィレットを起動する。
    await page.click('#fillet')
    await frameAll(page)
    await page.waitForFunction(
      (bodyId) => window.__duocad.edges?.body_id === bodyId,
      { timeout, polling: 50 },
      target.body_id,
    )
    const proposedColor = await page.evaluate(() => window.__duocad.highlightColors().edge)
    if (proposedColor !== PROPOSED_HIGHLIGHT) {
      fail(
        `AIの提案が提案の色で出ていない: ${proposedColor?.toString(16)} ` +
          `(期待 ${PROPOSED_HIGHLIGHT.toString(16)})`,
      )
    }
    ok(`AIが述語で辺 ${proposedCount} 本を提案 → ブラウザに**提案の色(水色)で**出た`)

    // ---------------------------------------------------------------- 人が1本外す
    // **Ctrl+クリックで外す**(選択パネルの「外す」ボタンの代わり。2026-08-23)。
    // 裏に回っている辺は指せないので、提案のうち実際に指せる1本を順に試す。
    const proposedIndices = proposed.selection.items
      .filter((item) => item.body === target.body_id)
      .map((item) => item.index)
    let droppedIndex = null
    for (const index of proposedIndices) {
      const at = await page.evaluate(
        (id, edgeIndex) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectEdge(id, edgeIndex)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        target.body_id,
        index,
      )
      if (!at) continue
      await page.keyboard.down('Control')
      await page.mouse.click(at.x, at.y)
      await page.keyboard.up('Control')
      try {
        await page.waitForFunction(
          (count) => window.__duocad.selection?.count === count,
          { timeout: 5000, polling: 50 },
          proposedCount - 1,
        )
        droppedIndex = index
        break
      } catch {
        // その辺は画面から指せなかった(裏に回っている)。次を試す
      }
    }
    if (droppedIndex === null) fail('提案された辺を本物のクリックで1本も外せない')
    // **触った瞬間に色が確定側(橙)へ変わる** —— 承認されたことが色で人に伝わる。
    const userColor = await page.evaluate(() => window.__duocad.highlightColors().edge)
    if (userColor !== USER_HIGHLIGHT) {
      fail(`人が触ったのに確定の色へ変わらない: ${userColor?.toString(16)}`)
    }
    await page.keyboard.press('Escape') // フィレットは起動しただけ(形は変えない)
    ok(`人がブラウザで辺 ${droppedIndex} をCtrl+クリックで外した(${proposedCount} -> ${proposedCount - 1} 本・色は橙へ)`)

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
    // **先に部品を全部隠す。** 基準平面のグリッドは原点の周り±100mmで、PCケースは
    // その中を丸ごと覆っている —— 面と平面はレイ距離で競う(通常のオクルージョン)ので、
    // 部品が見えている限りグリッドは1画素も指せない。人がやることも同じ(隠してから指す)。
    await page.click('#parts-hide-all')
    await page.waitForFunction(() => window.__duocad.parts.every((item) => !item.visible), {
      timeout,
      polling: 50,
    })

    // **スケッチを作るのは2手**(「スケッチ」を起動 → 貼る先のグリッドをクリック)。
    // Enterは無い —— 対象が1つに定まり how の入力が無いコマンドは即適用になる。
    await page.click('#sketch-create')
    const basePlane = await page
      .waitForFunction(
        () => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectBasePlane('XY')
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        { timeout, polling: 50 },
      )
      .then((handle) => handle.jsonValue())
      .catch(() => null)
    if (!basePlane) fail('基準平面XYを指せる画素が無い(「スケッチ」が起動しているか)')
    await page.mouse.click(basePlane.x, basePlane.y)
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
    await server.stop() // stdinを閉じる = MCPクライアントが切断したときと同じ終わり方
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
