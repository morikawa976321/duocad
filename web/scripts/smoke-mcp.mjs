/**
 * 実機での確認 — 「AIが操作すると人間の画面が変わる」ことを
 * 本物のMCPと本物のブラウザで見る。**このマイルストーンの意義そのものを機械検査する。**
 *
 *   node scripts/smoke-mcp.mjs [--step <STEPのパス>]
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
 *      読み込みは何部品でも**undo1回**であることも、ここで同時に見る
 *      あわせて、**その同じCtrl+Zで可視性は戻らない**(`set_visible` は操作ログに載らない)
 *
 * サーバはこのスクリプトが `python -m duocad.mcp_server` として**空の文書で**起動する。
 * STEPはMCPの `import_step` で読む(読み込みのツールは1つだけ)。
 * CLIの `--step <path>` は「MCPで読み込むファイル」であり、サーバへは渡さない。
 * **別のサーバを先に立てておく必要はない**(むしろポートがぶつかる)。
 * ブラウザはインストール済みのEdge/Chromeを使う(`smoke.mjs` と同じ。`DUOCAD_BROWSER` で明示可)。
 */
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { assertNoAppErrors, frameAll, openApp, sleep } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { PC_CASE_STEP, PC_CASE_SOLIDS } from './models.mjs'
import { clickSketchPoint, createSketchOnBasePlane, faceAt, screenOf } from './pick.mjs'

/** 3Dの選択色(`viewport.ts` の `HIGHLIGHT_COLOR`)。人が選んでもAIが選んでも1色。 */
const SELECTION_HIGHLIGHT = 0xffb454




// 握手はこの本が自分で打つ(`handshake: false`) —— 応答の中身(protocol / serverInfo)を検査に使う。
runSmoke('smoke-mcp', { timeout: 180000, mcp: true, handshake: false }, async ({ options, workdir, mcp, browser, url, timeout }) => {
  const stepPath = options.step ?? PC_CASE_STEP
  const expectedBodies = Number.parseInt(options['expect-bodies'] ?? String(PC_CASE_SOLIDS), 10)
  if (!existsSync(stepPath)) fail(`STEPが無い: ${stepPath}`)
  console.log(`smoke-mcp: ${stepPath}`)
  {
    // ---------------------------------------------------------------- MCPの握手
    const info = await mcp.handshake('duocad-smoke-mcp')
    ok(`MCP接続: protocol ${info.protocolVersion} / ${info.serverInfo.name} ${info.serverInfo.version}`)

    const tools = (await mcp.send('tools/list')).result.tools.map((tool) => tool.name)
    // ツールの顔ぶれの上限を決める契約は `tests/test_mcp_verbs.py` にひとつだけ置く
    // (同じ上限を複数箇所に書くと、片方の更新を忘れた瞬間に必ず食い違う)。
    // ここに残すのは **M4の3つが名前を変えずに生き残っていること** の回帰ガードである。
    const m4Tools = ['describe_document', 'set_visible', 'describe_shape']
    const missing = m4Tools.filter((name) => !tools.includes(name))
    if (missing.length) fail(`M4のツールが消えている: ${missing.join(', ')}`)
    ok(`tools/list: ${tools.length}個（M4の3つを含む）`)

    // ツール名 == ログのverb名。**STEP読み込みのツールは1つだけ**。
    const imported = await mcp.callTool('import_step', { path: stepPath })
    if (imported.verb !== 'import_step') {
      fail(`import_step の verb が一致しない: ${imported.verb}`)
    }
    if (imported.body_ids.length !== expectedBodies) {
      fail(`編集可能なボディが ${expectedBodies} 個でない: ${imported.body_ids.length}`)
    }
    ok(`MCPの import_step: 編集可能なボディ ${imported.body_ids.length} 個`)

    // ---------------------------------------------------------------- 検証1: 27件のBody
    const document = await mcp.callTool('describe_document')
    if (document.body_count !== expectedBodies) {
      fail(`describe_document が ${expectedBodies} 件でない: ${document.body_count} 件`)
    }
    ok(`describe_document: ${document.body_count} 件のBody / units=${document.units}`)

    // ---------------------------------------------------------------- ブラウザを開く
    const { page, consoleErrors } = await openApp(browser, url, {
      timeout,
      until: () => (window.__duocad.parts ?? []).length > 0,
    })
    /**
     * **起きないことを見る前の猶予**。適用が走るなら「選択の配信 → 適用 → 状態配信」で
     * 済む時間を過ぎてから数える —— 待たずに数えると、走っていても
     * 「まだ返ってきていないだけ」を緑と読んでしまう。
     */
    const graceForNothingToHappen = () => sleep(1500)

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
    // **即適用**(`command.ts` の `needsEnter` が false になるコマンド)は、起動した後に
    // 引数が揃うと即座に適用される。その引き金は**人がこの画面で指したこと**でなければ
    // ならない —— 選択集合はAIも `modify_selection` で書き換えられるので、「面が1つ選ばれた」
    // だけで発火させると、**人が何も触っていないのに文書が変わる**。ここはその境目を実機で
    // 固定する。
    //
    // 材料は**面に貼る `sketch_create`**である。踏む配線は同じ1本 ——
    // `CommandHost` の `appliesOnSelection` が構え、
    // `App.svelte` が「自分が出した」と確かめた選択変更の数(`selfSelections`)が増えたときだけ
    // `commit()` する `$effect` である。
    //
    // **旧コマンドより1段弱いことを明記しておく**: 面1つで揃う旧コマンドは、AIが選択を
    // 書き換えた時点で `phase="ready"`(引数が揃った状態)に到達できたので、「揃っているのに
    // 発火しない」まで示せた。`sketch_create` の貼る先は `App.svelte` の `onPick` からしか
    // 埋まらない(`useSelectedFaceAsPlane`)ため、AIの選択だけでは `ready` にならない。
    // いま固定できるのは「**コアの選択が人のクリック後と同じ状態でも**、AI由来なら
    // 貼る先は埋まらず、コマンドは構えたまま何も起きない」までである。
    await mcp.callTool('clear_selection')
    await page.waitForFunction(() => (window.__duocad.selection?.items ?? []).length === 0, {
      timeout,
      polling: 50,
    })
    const sketchesBeforeAi = await page.evaluate(() => window.__duocad.sketches.length)
    await page.click('#sketch-create') // 人が起動する(ここまでは人の1手)
    await mcp.callTool('modify_selection', {
      // **AIが面を1つ選ぶ**。
      add: [{ body: target.body_id, kind: 'face', index: newlyPickable.index }],
    })
    await page.waitForFunction(
      () => {
        const selection = window.__duocad.selection
        return Boolean(selection && selection.count === 1)
      },
      { timeout, polling: 50 },
    )
    await graceForNothingToHappen()
    const afterAiSelection = await page.evaluate(() => {
      const host = document.querySelector('.command-host')
      return {
        sketches: window.__duocad.sketches.length,
        activeSketch: window.__duocad.activeSketch,
        command: host.dataset.command,
        phase: host.dataset.phase,
      }
    })
    if (afterAiSelection.sketches !== sketchesBeforeAi || afterAiSelection.activeSketch !== null) {
      fail(
        `AIが選択を書き換えただけで「スケッチ」が走った` +
          `(スケッチ ${sketchesBeforeAi} -> ${afterAiSelection.sketches})`,
      )
    }
    // **空振りで緑になっていないこと**を同時に見る: コマンドは起動したまま構えている。
    // (`phase` は `collecting` のまま —— 上のコメントのとおり、貼る先は人のクリックでしか埋まらない)
    if (afterAiSelection.command !== 'sketch_create' || afterAiSelection.phase !== 'collecting') {
      fail(
        `この検査が空振りしている(command=${afterAiSelection.command} / ` +
          `phase=${afterAiSelection.phase})`,
      )
    }
    ok('**AIが選択を書き換えても即適用は発火しない**(コマンドは構えたまま、貼る先は埋まらない)')

    // 同じ画面で**人が面をクリックすると発火する** ——
    // 上の検査が「そもそも発火しない実装」で緑になっていないことの裏取り。**Enterは押さない**。
    //
    // 貼れるのは**平面状の面**だけなので候補を絞り、さらに「押した画素が基準平面のグリッドに
    // 取られた」場合(面より手前にグリッドが来ることがある)はその試行を捨てて次の面を試す。
    // **面を起点に貼れたことまで確かめる**(グリッドで貼ると `setBasePlane` の同期経路になり、
    // ここで見たい `selfSelections` の待ち受けを踏まない)。
    const planarCandidates = await page.evaluate(
      (bodyId, total, skip) => {
        const geometry = window.__duocad.getGeometry(bodyId)
        const planar = new Set(
          (geometry?.faces ?? []).filter((face) => face.kind === 'plane').map((face) => face.index),
        )
        const found = []
        for (let index = 1; index <= total; index += 1) {
          if (index === skip || !planar.has(index)) continue
          if (window.__duocad.projectFace(bodyId, index)) found.push(index)
          if (found.length >= 6) break
        }
        return found
      },
      target.body_id,
      target.total,
      newlyPickable.index,
    )
    if (planarCandidates.length === 0) fail('平面状の面が画面から1枚も指せない(この裏取りが成立しない)')

    let sketchedOnFace = null
    const attemptLog = []
    for (const index of planarCandidates) {
      // **起動してから射影する**(順序が逆だと当たらない): コマンドを起動するとコマンド行の
      // 文言が変わって行が伸び縮みし、`.viewport` の高さ=射影が動く(smoke.mjs で実測)。
      if (await page.evaluate(() => document.querySelector('.command-host').dataset.command !== 'sketch_create')) {
        await page.click('#sketch-create')
      }
      const at = await faceAt(page, target.body_id, index)
      if (!at) {
        attemptLog.push(`${index}: 射影できない`)
        continue
      }
      await page.mouse.click(at.x, at.y)
      const created = await page
        .waitForFunction(() => window.__duocad.activeSketch !== null, { timeout: 15000, polling: 50 })
        .then(() => true)
        .catch(() => false)
      if (!created) {
        const toast = await page.evaluate(
          () => document.querySelector('#toasts .toast-text')?.textContent?.trim() ?? null,
        )
        attemptLog.push(`${index}: スケッチができない(${toast ?? 'トーストなし'})`)
        await page.keyboard.press('Escape')
        continue
      }
      // **`JSON.stringify` はブラウザの中でやる** —— スケッチの `plane` はSvelteの
      // リアクティブなプロキシで、そのまま返すとCDPの直列化が空オブジェクトに潰す(実測)。
      const plane = JSON.parse(
        await page.evaluate(() => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return JSON.stringify(active?.plane ?? null)
        }),
      )
      // 抜けてから判定する(この後の検証4はスケッチモードの外を前提にしている)
      await page.click('#sketch-exit')
      await page.waitForFunction(() => window.__duocad.activeSketch === null, { timeout, polling: 50 })
      attemptLog.push(`${index}: plane=${JSON.stringify(plane)}`)
      if (plane?.kind === 'face') {
        sketchedOnFace = index
        break
      }
      // 手前の基準平面グリッドに取られた。作られてしまったスケッチを戻して次の面を試す。
      await mcp.callTool('undo')
      await page.waitForFunction(
        (expected) => window.__duocad.sketches.length === expected,
        { timeout, polling: 50 },
        sketchesBeforeAi,
      )
    }
    if (sketchedOnFace === null) {
      fail(
        `人が面をクリックしても面に貼れなかった(即適用の裏取りが成立していない): ` +
          `候補=${JSON.stringify(planarCandidates)} / ${attemptLog.join(' | ')}`,
      )
    }
    ok(`**人が面 ${sketchedOnFace} をクリックしたときだけ**スケッチが貼られた(Enterは押していない)`)

    // 後始末: いま作ったスケッチを戻し、既定のコマンドへ降りる
    // (この後の検証4はundoの位置に依存している)
    await mcp.callTool('undo')
    await page.waitForFunction(
      (expected) => window.__duocad.sketches.length === expected,
      { timeout, polling: 50 },
      sketchesBeforeAi,
    )
    await page.keyboard.press('Escape')

    // ------------------------------------------------- 検証4: 同じ操作ログ / 可視性は履歴の外
    // **AIが積んだ操作を、人間がブラウザのCtrl+Zで取り消せる**(同じ操作ログを共有している)。
    // 材料に可視性を使わない —— `set_visible` は操作ログに載らないので、
    // **同じCtrl+Zを押しても隠れたまま**である。戻ってしまうと、奥を覗くために手前を隠した
    // だけで直前の編集へ戻れなくなる。1回のCtrl+Zで両方を見る。
    //
    // 積む操作は **`import_step`**。いま表示中は対象の1部品だけなので、AIに書き出させた
    // STEPをそのまま読み直せば軽い1操作になる。**読み込みは何部品でもログ1行・undo1回**
    // という粒度を、実機のCtrl+Z 1回で裏取りする形でもある。
    const lastHidden = others[others.length - 1]
    const roundTripStep = join(workdir, 'round-trip.step')
    await mcp.callTool('export_step', { path: roundTripStep })
    const partsBeforeImport = await page.evaluate(() => window.__duocad.parts.length)
    const reimported = await mcp.callTool('import_step', { path: roundTripStep })
    if (reimported.verb !== 'import_step') {
      fail(`import_step の verb が一致しない: ${reimported.verb}`)
    }
    if (reimported.body_ids.length < 1) {
      fail(`読み込みが編集可能なボディを1つも作らない: ${reimported.body_ids.length}`)
    }
    await page.waitForFunction(
      (expected) => window.__duocad.parts.length === expected,
      { timeout, polling: 50 },
      partsBeforeImport + reimported.body_ids.length,
    )
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyZ')
    await page.keyboard.up('Control')
    await page.waitForFunction(
      (expected) => window.__duocad.parts.length === expected,
      { timeout, polling: 50 },
      partsBeforeImport,
    )
    ok(
      `ブラウザのCtrl+Z **1回**で、AIが読み込んだ ${reimported.body_ids.length} 部品が` +
        `まとめて消えた(AIと人間が同じ操作ログを共有していて、読み込みはundo1回)`,
    )

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
    // **AIが述語で辺を選ぶ → ブラウザに出る → 人がクリックで1本外す →
    //   AIが読むと外れている。** これが通れば、交互操作が人間側から見える形で成立している
    //   (タスクBの検証項番5)。ここまでの4つと違い、**人とAIが同じ集合を順に触る**。
    await mcp.callTool('clear_selection')
    let proposed = await mcp.callTool('set_selection', {
      predicate: { kind: 'edge', body: target.body_id, where: [{ op: 'parallel_to', axis: 'Z' }] },
    })
    if (proposed.selection.count < 2) {
      // Zに平行な辺が足りない部品なら、その部品の辺全部を使う(件数だけが目的)
      proposed = await mcp.callTool('set_selection', {
        predicate: { kind: 'edge', body: target.body_id, where: [] },
      })
    }
    const proposedCount = proposed.selection.count
    if (proposedCount < 2) fail(`選べる辺が足りない: ${proposedCount} 本`)

    await page.waitForFunction(
      (count) => {
        const selection = window.__duocad.selection
        return selection?.count === count
      },
      { timeout, polling: 50 },
      proposedCount,
    )
    // **AIが選んだものも人が選んだものと同じ色で出る。** その色そのものを
    // 読んで確かめる(`viewport.highlightColors`)。辺が3Dに出るのは辺を要求している
    // コマンドを起動している間だけなので、人がやるとおり先にフィレットを起動する。
    await page.click('#fillet')
    await frameAll(page)
    await page.waitForFunction(
      (bodyId) => window.__duocad.edges?.body_id === bodyId,
      { timeout, polling: 50 },
      target.body_id,
    )
    const aiColor = await page.evaluate(() => window.__duocad.highlightColors().edge)
    if (aiColor !== SELECTION_HIGHLIGHT) {
      fail(`AIが選んだ辺が選択色で出ていない: ${aiColor?.toString(16)} (期待 ${SELECTION_HIGHLIGHT.toString(16)})`)
    }
    ok(`AIが述語で辺 ${proposedCount} 本を選んだ → ブラウザに選択色(橙)で出た`)

    // ---------------------------------------------------------------- 人が1本外す
    // **Ctrl+クリックで外す**(選択パネルの「外す」ボタンの代わり)。
    // 裏に回っている辺は指せないので、選ばれたうち実際に指せる1本を順に試す。
    const proposedIndices = proposed.selection.items
      .filter((item) => item.body === target.body_id)
      .map((item) => item.index)
    let droppedIndex = null
    for (const index of proposedIndices) {
      const at = await screenOf(page, 'projectEdge', target.body_id, index)
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
    if (droppedIndex === null) fail('AIが選んだ辺を本物のクリックで1本も外せない')
    ok(`人がブラウザで辺 ${droppedIndex} をCtrl+クリックで外した(${proposedCount} -> ${proposedCount - 1} 本)`)

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
    ok(`**AIが get_selection で読むと外れている**(${afterHuman.selection.count} 本)`)
    ok('→ 交互操作(AIが選ぶ → 人が過不足を直す → AIが読む)が一巡した')
    // フィレットは起動しただけ(形は変えない)。**AIが読んだ後で**中断する —— 中断すると選択は空になる。
    await page.keyboard.press('Escape')
    await page.waitForFunction(() => window.__duocad.selection?.count === 0, { timeout, polling: 50 })

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
    await createSketchOnBasePlane(page, 'XY', { timeout })
    await page.click('#sketch-circle')
    await clickSketchPoint(page, 0, 0)
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

    await assertNoAppErrors(page, consoleErrors)
  }
})

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
