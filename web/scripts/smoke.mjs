/**
 * 実ブラウザでの検査 — **人が使っていても気づけない壊れ方**だけを見る(何を smoke に書くかは
 * `docs/SMOKE_TESTING.md` の冒頭)。題材は MCP で作る板2枚(原点の角の板と、原点の向こう側 x<0 の板)。
 *
 *   web> node scripts/smoke.mjs
 *
 *   1. 辺はカーソルの近くのボディの分だけ頼む(全ボディの辺を頼むと、使わない折れ線が毎回サーバから届く)
 *   2. 全解除の配信が届く前のクリックを、配信前の選択で判定しない(タイミングでしか出ない)
 *   3. AI が選択を書き換えても、人が起動した即適用のコマンドは走らない(人が指したときだけ走る)
 *   4. 基準平面を指せる間も、原点をはさんでカメラの反対側にある面が取れる(#109)
 *   5. 自動保存が書けなかったら「未保存」の札が出て、Ctrl+S で書き直すと消える
 *   6. 入力欄にフォーカスがある間の Ctrl+Z は、文書を undo しない(欄が空なら undo / redo になる)
 *   7. 押し出しの結果を、距離欄から Tab で進んで Space で選べる
 *   8. F で、向きを変えずに全体表示になる(入力欄の中の F は効かない)
 *   9. ビューキューブの外側の余白を引いても視点は回らない(立方体の上を引くと回る)
 *  10. プレビュー中の全体表示は、隠してある元のボディではなく見えている結果に寄る
 */
import { existsSync, renameSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fail, ok } from './args.mjs'
import {
  assertNoAppErrors,
  frameAll,
  openApp,
  pressUndo,
  sleep,
  viewportCenter,
  waitFrames,
  waitOrFail,
  withoutBrowserPanel,
} from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { clickFace, faceAt, planarFace } from './pick.mjs'

runSmoke('smoke', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout, workdir }) => {
  // 原点の角から描くので、手前の壁(法線 −Y)は XZ 基準平面と、底面は XY 基準平面とぴったり重なる。
  const { bodyId } = await addExtrudedRect(mcp, { corner: [0, 0], width: 60, height: 20, distance: 5 })
  // 原点の向こう側(x<0)の板。ホームビューのカメラは x>0 にあるので、上面へのレイは YZ 基準平面を先に通る。
  const far = await addExtrudedRect(mcp, { corner: [-40, 0], width: 20, height: 20, distance: 5 })
  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.parts ?? []).length >= 2,
  })
  await frameAll(page)

  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout })
  const selectionIs = (indexes, what) =>
    waitFor(
      (expected) => {
        const items = window.__duocad.selection?.items ?? []
        return (
          items.length === expected.length &&
          expected.every((index) => items.some((item) => item.kind === 'face' && item.index === index))
        )
      },
      what,
      [indexes],
    )
  const clearSelection = async () => {
    await mcp.callTool('clear_selection')
    await selectionIs([], '選択を空にできない')
  }
  const top = await planarFace(page, bodyId, [0, 0, 1])
  const wall = await planarFace(page, bodyId, [0, -1, 0])

  // ================================================================ 1. 辺はカーソルの近くのボディの分だけ
  const fetched = () => page.evaluate(() => Object.keys(window.__duocad.edges ?? {}))
  const fetchedBefore = await fetched()
  if (fetchedBefore.length !== 0) fail(`カーソルを置く前から辺のジオメトリが届いている: ${JSON.stringify(fetchedBefore)}`)
  const farTop = await planarFace(page, far.bodyId, [0, 0, 1])
  await withoutBrowserPanel(page, async () => {
    const at = await faceAt(page, far.bodyId, farTop)
    if (!at) fail('向こう側の板の上面が画面の中に無い')
    await page.mouse.move(at.x, at.y)
  })
  await waitFor((id) => window.__duocad.edges?.[id] !== undefined, 'カーソルを置いた板の辺が届かない', [far.bodyId])
  await waitFrames(page, 6)
  if ((await fetched()).includes(bodyId)) fail('カーソルから離れた板の辺まで取り寄せている')
  ok('[1] 辺はカーソルを置いたボディの分だけ届き、離れたボディの分は頼まない')

  // ================================================================ 2. 全解除の配信前のクリック
  // 本物の入力では WS の往復に勝てないので、Esc と押下・離しを**ページの1タスクの中で**続けて
  // 起こす(配信はその間に割り込めない)。コアでは「全解除 → クリック」の順に効くので、
  // 配信前の選択([上面])を見て「唯一選ばれている面の再クリック = 解除」と判定すると空になる。
  await clickFace(page, bodyId, top, { hidePanel: true })
  await selectionIs([top], '上面を選べない')
  await page.click('#extrude')
  await waitFor(() => document.querySelector('#command-abort') !== null, '押し出しが起動しない')
  const raced = await withoutBrowserPanel(page, async () => {
    const at = await faceAt(page, bodyId, top)
    if (!at) fail('上面が画面の中に無い')
    return page.evaluate((point) => {
      const epoch = window.__duocad.stateEpoch
      const target = document.elementFromPoint(point.x, point.y)
      if (target?.tagName !== 'CANVAS') return { error: target?.tagName ?? null }
      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      const init = {
        clientX: point.x,
        clientY: point.y,
        button: 0,
        pointerId: 1,
        pointerType: 'mouse',
        isPrimary: true,
        bubbles: true,
      }
      target.dispatchEvent(new PointerEvent('pointerdown', init))
      target.dispatchEvent(new PointerEvent('pointerup', init))
      return { epoch }
    }, at)
  })
  if (raced.error !== undefined) fail(`上面の画素がキャンバスでない: ${raced.error}`)
  // 全解除とクリックの2通が届くまで待つ(届く前の選択は元のままで、見ても区別できない)
  await waitFor((epoch) => window.__duocad.stateEpoch >= epoch + 2, '全解除とクリックの配信が届かない', [raced.epoch])
  await waitFor(() => document.querySelector('#command-abort') === null, 'Escで無起動へ戻らない')
  await selectionIs([top], '全解除の配信前に上面を素でクリックしたのに選ばれていない(配信前の選択で解除と判定した)')
  ok('[2] 全解除の配信前に、唯一選ばれていた面を素でクリックしても、その面が選ばれた')
  await clearSelection()

  // ================================================================ 3. AI の選択では即適用が走らない
  // 即適用(`command.ts` の `needsEnter` が false)の引き金は**人がこの画面で指したこと**でなければ
  // ならない。選択は AI も書き換えられるので、「面が1つ選ばれた」だけで発火させると、人が何も
  // 触っていないのに文書が変わる(`App.svelte` の `selfSelections` を数える `$effect`)。
  const sketchesBefore = await page.evaluate(() => window.__duocad.sketches.length)
  await page.click('#sketch-create') // 起動までは人の1手
  await mcp.callTool('modify_selection', { add: [{ body: bodyId, kind: 'face', index: wall }] })
  await selectionIs([wall], 'AI の選択が画面へ届かない')
  await sleep(1500) // 走るなら「選択の配信 → 適用 → 状態配信」で済む時間
  const afterAi = await page.evaluate(() => {
    const host = document.querySelector('.command-host')
    return {
      sketches: window.__duocad.sketches.length,
      activeSketch: window.__duocad.activeSketch,
      command: host.dataset.command,
      phase: host.dataset.phase,
    }
  })
  if (afterAi.sketches !== sketchesBefore || afterAi.activeSketch !== null) {
    fail('AIが選択を書き換えただけで「スケッチ」が走った')
  }
  // 空振りで緑になっていないこと: コマンドは起動したまま構えている
  if (afterAi.command !== 'sketch_create' || afterAi.phase !== 'collecting') {
    fail(`この検査が空振りしている: ${JSON.stringify(afterAi)}`)
  }
  ok('[3] AIが選択を書き換えても即適用は発火しない(コマンドは構えたまま)')
  await page.click('#sketch-exit')
  await waitFor(() => window.__duocad.mode === 'idle', '貼る先待ちのモードを抜けられない')
  await clearSelection()

  // ================================================================ 4. 原点の向こう側の面
  // 面と基準平面をレイ距離で競わせる実装に戻ると、上面は YZ 平面に、手前の壁は同着で XZ 平面に
  // 取られて赤になる(理由は docs/VIEWPORT.md「ピック」)。
  const cameraX = await page.evaluate(() => window.__duocad.cameraPose().position[0])
  if (!(cameraX > 0)) fail(`この検査の前提が崩れた: カメラが x>0 に無い(x=${cameraX})`)
  for (const [target, index, what] of [
    [far.bodyId, farTop, '原点の向こう側の板の上面'],
    [bodyId, wall, 'XZ 基準平面と重なる手前の壁'],
  ]) {
    await page.click('#sketch-create')
    await waitFor(() => window.__duocad.wantsBasePlanes === true, '「スケッチ」で基準平面が指せる状態にならない')
    await waitFrames(page, 2) // 起動でツールバーの行が変わる。射影は押す直前
    // 基準平面が本当に当たる状態でなければ、どの実装でも面が取れて空振りする
    if ((await page.evaluate(() => window.__duocad.projectBasePlane('XY'))) === null) {
      fail('この検査の前提が崩れた: 「スケッチ」の起動中なのに基準平面 XY を指せる画素が無い')
    }
    // x<0 の板は画面の左寄りに出て、ブラウザパネルに覆われうる
    await withoutBrowserPanel(page, async () => {
      const at = await faceAt(page, target, index)
      if (at === null) fail(`${what}が画面から指せない`)
      await page.mouse.click(at.x, at.y)
    })
    // 即適用は貼ると同時にそのスケッチへ入るので、`sketch` が立つことが手が届いた証拠になる
    await waitFor(() => window.__duocad.sketch !== null, `${what}のクリックでスケッチが貼られない`)
    const plane = await page.evaluate(() => {
      const { kind, body, index } = window.__duocad.sketch.plane
      return { kind, body, index }
    })
    if (plane.kind !== 'face' || plane.body !== target || plane.index !== index) {
      fail(`基準平面を指せる間に${what}をクリックしたのに、貼り先が ${JSON.stringify(plane)} になった`)
    }
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'スケッチを抜けられない')
    await pressUndo(page)
    await waitFor((expected) => window.__duocad.sketches.length === expected, 'スケッチをundoで戻せない', [sketchesBefore])
    await clearSelection()
  }
  ok('[4] 基準平面を指せる間も、原点の向こう側の面と、基準平面と重なる面が面として取れる')

  const pressWithControl = async (key) => {
    await page.keyboard.down('Control')
    await page.keyboard.press(key)
    await page.keyboard.up('Control')
  }

  // ================================================================ 5. 「未保存」の札と Ctrl+S
  // 書けていないのに札が出ないと、保存済みと思って作業を続けてしまう(黙って失われる)。models-dir を同名の
  // 空ファイルに差し替えて次の自動保存を失敗させ(OS を問わず `mkdir` と書き込みが落ちる)、戻して Ctrl+S で書き直す。
  const saveTag = () => page.evaluate(() => document.querySelector('#save-state') !== null)
  const fileName = () => page.evaluate(() => document.querySelector('#current-file')?.textContent ?? '')
  if (await saveTag()) fail('書けているのに「未保存」の札が出ている(この検査が空振りになる)')
  const aside = `${workdir}.aside`
  renameSync(workdir, aside)
  writeFileSync(workdir, '')
  try {
    await addExtrudedRect(mcp, { corner: [60, 40], width: 10, height: 10, distance: 5 })
    await waitFor(() => document.querySelector('#save-state') !== null, '自動保存が書けなかったのに「未保存」の札が出ない')
  } finally {
    rmSync(workdir, { force: true })
    renameSync(aside, workdir)
  }
  await page.evaluate(() => document.activeElement?.blur?.())
  await pressWithControl('KeyS')
  await waitFor(() => document.querySelector('#save-state') === null, 'Ctrl+S で書き直したのに「未保存」の札が消えない')
  const saved = await fileName()
  if (saved === '' || !existsSync(join(workdir, saved))) fail(`Ctrl+S で書いたはずのファイルが無い: ${JSON.stringify(saved)}`)
  // わざと出させた失敗は警告として控えに残る(最後の「エラーなし」の検査から除く)。
  const failedSaves = await page.evaluate(() =>
    [...new Set(JSON.parse(JSON.stringify(window.__duocad.errors)))].filter((message) =>
      message.startsWith('警告 自動保存できなかった'),
    ),
  )
  if (failedSaves.length === 0) fail('自動保存の失敗が警告として控えに残っていない')
  ok('[5] 自動保存が書けなかったら「未保存」の札が出て、Ctrl+S で書き直すと消える')

  // ================================================================ 6. 入力欄の Ctrl+Z
  // フォーカスが入力欄にあるとき、Ctrl+Z はブラウザ標準のテキスト取り消しに任せ、文書の undo を
  // 発火しない(`App.svelte` の `onWindowKeydown`)。
  const history = () =>
    page.evaluate(() => ({ log: window.__duocad.state.log.length, redo: window.__duocad.state.redo_count }))
  await page.click('#extrude')
  await page.waitForSelector('#distance', { timeout })
  await page.click('#distance')
  await page.type('#distance', '9', { delay: 20 })
  const before = await history()
  if (before.log === 0) fail('入力欄Ctrl+Zの検査が意味を持たない(文書の操作履歴が0件)')
  await pressWithControl('KeyZ')
  // 発火していれば WS 往復のあとで反映される。待たずに比べると「まだ届いていないだけ」を緑と読む
  // (実測で undo 一往復に2秒近くかかることがあり、400ms 待ちでは退行を見逃した)。
  await sleep(3000)
  const after = await history()
  if (after.log !== before.log || after.redo !== before.redo) {
    fail(`入力欄にフォーカスがあるのに Ctrl+Z で文書の undo が発火した: ${JSON.stringify({ before, after })}`)
  }
  // 欄が空なら(奪っても失う文字が無い)Ctrl+Z / Ctrl+Y は欄でなく文書のものになる
  await page.click('#distance', { clickCount: 3 })
  await page.keyboard.press('Backspace')
  await pressWithControl('KeyZ')
  await waitFor((log) => window.__duocad.state.log.length === log - 1, '空の距離欄の Ctrl+Z で文書が undo されない', [before.log])
  await pressWithControl('KeyY')
  await waitFor((log) => window.__duocad.state.log.length === log, '空の距離欄の Ctrl+Y で文書がやり直されない', [before.log])
  // 入力欄の外では文書が undo される(上の検査が「そもそも効かない Ctrl+Z」で緑になっていない裏取り)
  await page.evaluate(() => document.activeElement?.blur?.())
  await pressWithControl('KeyZ')
  await waitFor((log) => window.__duocad.state.log.length === log - 1, '入力欄の外の Ctrl+Z で文書が undo されない', [before.log])
  await pressWithControl('KeyY')
  await waitFor((log) => window.__duocad.state.log.length === log, 'Ctrl+Y で戻らない', [before.log])
  // 押しっぱなし(キーの自動反復)では 1 回しか戻らない
  await page.keyboard.down('Control')
  for (let press = 0; press < 3; press += 1) await page.keyboard.down('KeyZ')
  await page.keyboard.up('KeyZ')
  await page.keyboard.up('Control')
  await waitFor((log) => window.__duocad.state.log.length === log - 1, '押しっぱなしの Ctrl+Z で 1 回も戻らない', [before.log])
  await sleep(1500)
  if ((await history()).log !== before.log - 1) fail('Ctrl+Z の押しっぱなしで 2 回以上戻った')
  await pressWithControl('KeyY')
  await waitFor((log) => window.__duocad.state.log.length === log, 'Ctrl+Y で戻らない', [before.log])
  ok('[6] 入力欄にフォーカスがある間の Ctrl+Z は文書を変えず、外では undo される(押しっぱなしは 1 回だけ)')

  // ================================================================ 7. 押し出しの結果をキーボードだけで選べる
  // 選択肢のボタンは DOM で数値欄より前にあり、素の Tab では欄から届かない。
  const activeId = () => page.evaluate(() => document.activeElement?.id ?? '')
  await page.focus('#distance')
  await page.keyboard.press('Tab')
  if ((await activeId()) !== 'extrude-mode-join') fail(`距離欄から Tab で結果のボタンへ進めない: ${await activeId()}`)
  await page.keyboard.press('Space')
  await waitFor(() => document.querySelector('#extrude-mode-join')?.classList.contains('on') === true, 'Space で結果が選ばれない')
  ok('[7] 距離欄から Tab で結果のボタンへ進み、Space で選べる')

  // ================================================================ 8. 全体表示(F キー)
  // 向きを変えずに寄る。入力欄にフォーカスがあるときは効かない(F はその欄に打つ文字)。
  const poseOf = () => page.evaluate(() => window.__duocad.cameraPose())
  const distanceOf = (pose) => Math.hypot(...pose.position.map((value, axis) => value - pose.target[axis]))
  const directionOf = (pose) => {
    const length = distanceOf(pose)
    return pose.position.map((value, axis) => (value - pose.target[axis]) / length)
  }
  const farOut = async () => {
    await page.evaluate(() => document.activeElement?.blur?.())
    const center = await viewportCenter(page)
    await page.mouse.move(center.x, center.y)
    for (let step = 0; step < 6; step += 1) await page.mouse.wheel({ deltaY: 400 })
    await waitFrames(page, 10)
  }
  const fitted = distanceOf(await poseOf())
  await farOut()
  const away = distanceOf(await poseOf())
  if (!(away > fitted * 1.2)) fail(`この検査の前提が崩れた: ホイールで引いても距離が増えない(${fitted} -> ${away})`)
  const directionBefore = directionOf(await poseOf())
  await page.keyboard.press('KeyF')
  await waitFor((limit) => {
    const pose = window.__duocad.cameraPose()
    return Math.hypot(...pose.position.map((value, axis) => value - pose.target[axis])) < limit
  }, 'F を押しても全体表示にならない', [away * 0.8])
  const directionAfter = directionOf(await poseOf())
  if (directionBefore.reduce((sum, value, axis) => sum + value * directionAfter[axis], 0) < 0.999) {
    fail(`全体表示で視点の向きが変わった: ${JSON.stringify({ directionBefore, directionAfter })}`)
  }
  await farOut()
  await page.click('#distance')
  await page.keyboard.press('KeyF')
  await sleep(500)
  if (!(distanceOf(await poseOf()) > away * 0.9)) fail('入力欄にフォーカスがあるのに F で全体表示になった')
  ok('[8] F で向きを変えずに全体表示になり、入力欄の中の F は効かない')

  // ================================================================ 9. ビューキューブの外側の余白は下へ通す
  // 当たりは描いた立方体の外形だけ。四角の隅を引いても視点は回らず、立方体の上を引くと回る。
  await page.click('#command-abort')
  await waitFor(() => document.querySelector('#command-abort') === null, 'コマンドを中断できない')
  const box = await page.evaluate(() => {
    const rect = document.querySelector('.viewport').getBoundingClientRect()
    return { x: rect.right - 12 - 144, y: rect.top + 12 }
  })
  const dragFrom = async (x, y) => {
    await waitFrames(page, 60) // 直前の回転の慣性が止まってから測る
    const before = await poseOf()
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(x - 40, y + 10, { steps: 6 })
    await page.mouse.up()
    await waitFrames(page, 6)
    const after = await poseOf()
    return Math.hypot(...after.position.map((value, axis) => value - before.position[axis]))
  }
  const turned = await dragFrom(box.x + 72, box.y + 72)
  if (!(turned > 1)) fail('この検査の前提が崩れた: 立方体の上を引いても視点が回らない')
  // 慣性の残りは回転量の1%に届かない(実測: 回転 296 に対し余白では 0.002)
  if ((await dragFrom(box.x + 4, box.y + 4)) > turned * 0.01) fail('立方体の外の余白を引いたのに視点が回った')
  ok('[9] ビューキューブの外側の余白は下のモデルへ通り、立方体の上だけが視点を回す')

  // ================================================================ 10. プレビュー中の全体表示
  // 見えている結果へ寄る。プレビューの間、元のボディは隠してあるだけでシーンに残っている。離れた所に長さ 2000 mm の棒を
  // 足し、+X の端の面を 1999 mm 削るプレビューにすると、見えているのは幅 1 mm の残りと元からの形だけになる。
  // 棒の元になったスケッチ(同じ長さの長方形)は隠す。見えていれば、それが収まる所へ寄るのが正しい。
  const bar = await addExtrudedRect(mcp, { corner: [200, 0], width: 2000, height: 20, distance: 5 })
  await mcp.callTool('set_sketch_visible', { sketch_id: bar.sketchId, visible: false })
  await waitFor(
    (count, sketchId) =>
      (window.__duocad?.parts ?? []).length >= count &&
      (window.__duocad.sketches ?? []).find((item) => item.sketch_id === sketchId)?.visible !== true,
    '足した棒が画面に届かない、またはそのスケッチが隠れない',
    [3, bar.sketchId],
  )
  const fitDistance = async () => {
    await farOut()
    await page.keyboard.press('KeyF')
    await waitFrames(page, 10)
    return distanceOf(await poseOf())
  }
  const whole = await fitDistance()
  const endFace = await planarFace(page, bar.bodyId, [1, 0, 0])
  await mcp.callTool('set_selection', { refs: [{ body: bar.bodyId, kind: 'face', index: endFace }] })
  await selectionIs([endFace], '棒の端の面を選べない')
  await page.click('#extrude')
  await page.waitForSelector('#distance', { timeout })
  await page.click('#extrude-mode-cut')
  await page.click('#distance', { clickCount: 3 })
  await page.type('#distance', '-1999', { delay: 20 })
  await waitFor(() => window.__duocad.previewing === true, '端の面を削るプレビューが出ない')
  const shown = await fitDistance()
  if (!(shown < whole * 0.5)) {
    fail(`プレビュー中の全体表示が、隠してある元のボディの大きさで引いている: ${JSON.stringify({ whole, shown })}`)
  }
  await page.click('#command-abort')
  await waitFor(() => document.querySelector('#command-abort') === null, 'コマンドを中断できない')
  ok('[10] プレビュー中の全体表示は、隠してある元のボディではなく見えている結果に寄る')

  // ================================================================ 11. ビューキューブの回転の途中の全体表示
  // 立方体を押すと 300ms かけて視点が回る。その間に F を押しても、回り終えた向きで全体表示の距離になる
  // (全体表示の距離は外接球で決めるので、向きに依らない)。
  const settled = await fitDistance()
  await farOut()
  const startPose = await poseOf()
  if (!(distanceOf(startPose) > settled * 1.2)) fail('この検査の前提が崩れた: ホイールで引いても距離が増えない')
  await page.mouse.click(box.x + 72, box.y + 72)
  await page.keyboard.press('KeyF')
  await sleep(600)
  const midTurn = await poseOf()
  if (directionOf(startPose).reduce((sum, value, axis) => sum + value * directionOf(midTurn)[axis], 0) > 0.9999) {
    fail('立方体を押した向きへ回っていない(この検査の前提が崩れたか、全体表示が回転を途中で止めた)')
  }
  if (Math.abs(distanceOf(midTurn) - settled) > settled * 0.01) {
    fail(`視点の回転の途中の全体表示が、合わせた距離を保っていない: ${JSON.stringify({ settled, got: distanceOf(midTurn) })}`)
  }
  ok('[11] ビューキューブの回転の途中に F を押しても、回り終えた向きで全体表示になる')

  await assertNoAppErrors(page, consoleErrors, { allow: failedSaves })
})
