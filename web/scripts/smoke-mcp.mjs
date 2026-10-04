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
 *   1. MCP経由で `set_visible(false)` にすると **ブラウザの表示が実際に変わる**
 *      (= コアが変更イベントを発火し、アプリ層が購読して描き直す上り方向が動いている)
 *   2. その状態で **奥の面が本物のマウスクリックで選べる**
 *   3. **AIが選択を書き換えても、起動中の即適用コマンドは走らない**(人が指したときだけ走る側は
 *      `smoke-files.mjs` の即適用)
 *   4. **交互操作の一巡**: AIが述語で辺を選ぶ → 画面に選択色で出る → 人が1本外す → AIが読むと外れている
 *
 * ツールの顔ぶれ・読み込みがログ1行であること・可視性が履歴に載らないことは pytest が持つ。
 *
 * サーバはこのスクリプトが `python -m duocad.mcp_server` として**空の文書で**起動する。
 * STEPはMCPの `import_step` で読む(読み込みのツールは1つだけ)。
 * CLIの `--step <path>` は「MCPで読み込むファイル」であり、サーバへは渡さない。
 * **別のサーバを先に立てておく必要はない**(むしろポートがぶつかる)。
 * ブラウザはインストール済みのEdge/Chromeを使う(`smoke.mjs` と同じ。`DUOCAD_BROWSER` で明示可)。
 */
import { existsSync } from 'node:fs'
import { assertNoAppErrors, frameAll, openApp, sleep } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { PC_CASE_STEP, PC_CASE_SOLIDS } from './models.mjs'
import { screenOf } from './pick.mjs'

/** 3Dの選択色(`viewport.ts` の `HIGHLIGHT_COLOR`)。人が選んでもAIが選んでも1色。 */
const SELECTION_HIGHLIGHT = 0xffb454

// 握手はこの本が自分で打つ(`handshake: false`) —— 応答の中身(protocol / serverInfo)を検査に使う。
runSmoke('smoke-mcp', { timeout: 180000, mcp: true, handshake: false }, async ({ options, mcp, browser, url, timeout }) => {
  const stepPath = options.step ?? PC_CASE_STEP
  const expectedBodies = Number.parseInt(options['expect-bodies'] ?? String(PC_CASE_SOLIDS), 10)
  if (!existsSync(stepPath)) fail(`STEPが無い: ${stepPath}`)
  console.log(`smoke-mcp: ${stepPath}`)
  {
    // ---------------------------------------------------------------- MCPの握手
    const info = await mcp.handshake('duocad-smoke-mcp')
    ok(`MCP接続: protocol ${info.protocolVersion} / ${info.serverInfo.name} ${info.serverInfo.version}`)

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
    // 貼る先待ちのモードを抜ける(verb の行が返ってくる)
    await page.click('#sketch-exit')
    await page.waitForFunction(() => window.__duocad.mode === 'idle', { timeout, polling: 50 })

    // ---------------------------------------------------------------- 検証5(B): 交互操作の一巡
    // **AIが述語で辺を選ぶ → ブラウザに出る → 人がクリックで1本外す →
    //   AIが読むと外れている。** これが通れば、交互操作が人間側から見える形で成立している
    //   **人とAIが同じ集合を順に触る**。
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
