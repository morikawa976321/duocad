/**
 * 実ブラウザでの形状最適化の**実行**検査 —— 条件がそろう → 走らせる → 結果が画面に出る。
 *
 *   web> node scripts/smoke-optimize.mjs
 *
 * `smoke-optimize-mode.mjs` は「条件を**置ける**」までを見る。ここはその続きで、
 * **本物のSIMPを端から端まで回して、結果のメッシュボディが画面に出るところまで**を見る。
 * **薄くしない** —— 要素数を落として一瞬で終わらせると、通っているのは配線だけで最適化ではなくなる。
 *
 * 見るもの(荷重の上書き規則・解像度の目盛り・中断で文書が変わらないこと・表現できない保持の検出・
 * STL への書き出し・編集 verb の拒否は `tests/test_optimize_*.py` / `tests/test_mesh_body.py` が持つ):
 *
 *   1. **パネルが最適化モードのあいだだけ出る**。条件が足りないうちは実行が押せず、
 *      **足りない理由が画面に文で出ている**(コンソールではなく)。
 *   2. 荷重の verb を起動したとき、**既にある荷重が選択状態で現れない**(現れると、次の確定で
 *      既存の荷重まで黙って書き換わる)。
 *   3. **走らせると進捗が画面に出て、中断できる。** 実行中はモードから抜けられない。
 *   4. **走り切ると結果のメッシュボディが画面に出る**(部品一覧・viewportの三角形・投影)。
 *   5. **メッシュボディをクリックするとボディとして選ばれる**(面参照が飛ばない)。
 *      **編集verbは起動しても確定できない**(＝押せるのに断られる、が起きない)。
 */
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, sleep, waitOrFail } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addBoxPair, placeConditions } from './optimize-fixture.mjs'
import { clickFace } from './pick.mjs'

runSmoke('smoke-optimize', { timeout: 60000, mcp: true }, async ({ options, mcp, browser, url, timeout }) => {
  /** 最適化1回ぶんの待ち。**既定の60秒では足りない**(実測 約60秒 + 機械の熱状態で1.7倍)。 */
  const runTimeout = Number.parseInt(options['run-timeout'] ?? '420000', 10)

  const bodies = await addBoxPair(mcp)
  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.parts ?? []).length >= 2,
  })
  await frameAll(page)

  const waitFor = (check, what, args = [], limit = timeout) =>
    waitOrFail(page, check, what, { args, timeout: limit, polling: 100 })
  const text = (selector) =>
    page.evaluate((sel) => document.querySelector(sel)?.textContent?.trim() ?? null, selector)
  const disabled = (selector) =>
    page.evaluate((sel) => {
      const element = document.querySelector(sel)
      return element === null ? null : element.disabled === true
    }, selector)

  // ================================================================== 1. パネルとモード
  console.log('\n[1] 最適化モードに入るとパネルが出る / 条件が足りないうちは実行が押せない')
  if (await page.$('#optimize-panel')) fail('通常モードなのに実行パネルが出ている')
  await page.click('#optimize-enter')
  await waitFor(() => window.__duocad.optimizeMode === true, 'モードに入らない')
  await waitFor(() => document.querySelector('#optimize-panel') !== null, 'パネルが出ない')
  await waitFor(() => window.__duocad.optimizeConditions !== null, '条件の点検が返ってこない')
  if ((await disabled('#optimize-run')) !== true) fail('条件が空なのに実行ボタンが押せる')
  const emptyProblems = await text('#optimize-conditions')
  if (!emptyProblems || !emptyProblems.includes('保持ソリッド')) {
    fail(`足りない条件が画面に出ていない: ${emptyProblems}`)
  }
  ok(`[1] パネルが出て、足りない条件が画面に文で出ている(「${emptyProblems.slice(0, 34)}…」)`)

  await placeConditions(mcp, page, bodies)
  await waitFor(() => window.__duocad.optimizeConditions?.ready === true, '4条件を置いても条件がそろわない')
  if ((await disabled('#optimize-run')) !== false) fail('条件がそろったのに実行ボタンが押せない')

  // ================================================================== 2. 荷重の verb は既存の荷重を巻き込まない
  // `OPTIMIZE_PRESET_ROLE` に `set_loads` を載せていない理由。載っていると、2枚目を置いて Enter を
  // 押した瞬間に1枚目の大きさまで黙って書き換わる。
  await page.click('#set-loads')
  await waitFor(() => document.querySelector('#load-magnitude') !== null, '荷重の verb が起動しない')
  const preselected = await page.evaluate(() => (window.__duocad.selection?.items ?? []).length)
  if (preselected !== 0) {
    fail(`荷重verbの起動時に ${preselected} 個が選択されている(既存の荷重を巻き込んで上書きする)`)
  }
  await page.keyboard.press('Escape')
  ok('[2] 荷重の verb を起動しても、既にある荷重は選択状態で現れない')

  // ================================================================== 3. 走らせて中断する
  console.log('\n[3] 実行 → 進捗が画面に出る → 中断')
  await waitFor(() => Boolean(window.__duocad.optimizeResolution?.steps?.length), '解像度の目盛りが用意されない')
  const partsBefore = await page.evaluate(() => window.__duocad.parts.length)
  await page.click('#optimize-run')
  await waitFor(() => window.__duocad.optimizeRunning === true, '実行を押しても走り出さない')
  if ((await disabled('#optimize-exit')) !== true) fail('実行中なのにモードから抜けられる')
  await waitFor(() => window.__duocad.optimizeProgress !== null, '進捗が1件も届かない', [], runTimeout)
  const progressText = await text('#optimize-progress')
  if (!progressText) fail('進捗が画面に出ていない')
  await page.click('#optimize-cancel')
  await waitFor(() => window.__duocad.optimizeRunning === false, '中断しても止まらない', [], runTimeout)
  const cancelNotice = await text('#optimize-notice')
  if (!cancelNotice || !cancelNotice.includes('中断')) {
    fail(`中断したことが画面に残っていない: ${cancelNotice}`)
  }
  if ((await page.evaluate(() => window.__duocad.parts.length)) !== partsBefore) {
    fail('中断したのにボディが増えている')
  }
  ok(`[3] 進捗が出た(「${progressText.slice(0, 40)}…」)→ 中断できた`)

  // ================================================================== 4. 走り切る
  // **既定の段(いちばん粗い 12mm)では結果が出ない** —— 12mm の立方体が1要素にしかならず、
  // 全材料でも剛体運動を拘束できないと判定されて断られる。画面の指示どおり解像度を1段上げる
  // (実測: 12mm は結果なし、6mm は約8秒で結果が出る)。
  console.log('\n[4] 解像度を1段上げて、最後まで走らせる')
  await page.focus('#optimize-resolution')
  await page.keyboard.press('ArrowRight')
  await waitFor(() => window.__duocad.optimizeStepIndex === 1, 'スライダーを右へ動かしても段が変わらない')
  const started = Date.now()
  await page.click('#optimize-run')
  await waitFor(() => window.__duocad.optimizeRunning === true, '2回目が走り出さない')
  await waitFor(() => window.__duocad.optimizeRunning === false, '最適化が終わらない', [], runTimeout)
  await waitFor(
    () => (window.__duocad.parts ?? []).some((body) => body.kind === 'mesh'),
    '結果のメッシュボディが届かない',
    [],
    runTimeout,
  )
  const elapsed = ((Date.now() - started) / 1000).toFixed(0)
  const resultNotice = await text('#optimize-notice')
  if (!resultNotice || !resultNotice.includes('三角形')) {
    fail(`結果が画面に出ていない: ${resultNotice}`)
  }
  const mesh = await page.evaluate(() => {
    const body = window.__duocad.parts.find((item) => item.kind === 'mesh')
    const geometry = window.__duocad.getGeometry(body.body_id)
    return {
      bodyId: body.body_id,
      name: body.name,
      // 部品一覧は種別で濾していない(メッシュボディも行として出る)
      listed: document.querySelector(`.parts-row[data-body-id="${body.body_id}"]`) !== null,
      faceCount: geometry === null ? null : geometry.faces.length,
      triangles: geometry === null ? null : geometry.faces[0].indices.length / 3,
    }
  })
  if (!mesh.listed) fail('結果のメッシュボディが部品一覧に行として出ていない')
  if (mesh.faceCount !== 1) fail(`メッシュは面1枚として配る約束: ${mesh.faceCount}`)
  if (!(mesh.triangles > 100)) {
    fail(`三角形が少なすぎる(本物の最適化が回っていない疑い): ${mesh.triangles}`)
  }
  await frameAll(page)
  const projected = await page.evaluate((bodyId) => window.__duocad.projectFace(bodyId, 1), mesh.bodyId)
  if (projected === null) fail('結果のメッシュが画面へ投影できない(描かれていない)')
  ok(`[4] ${elapsed}秒で終わり、結果「${mesh.name}」が画面に出た(三角形 ${mesh.triangles}枚)`)

  // ================================================================== 5. メッシュのピック
  console.log('\n[5] メッシュをクリックすると「ボディ」として選ばれる(面参照が飛ばない)')
  await page.click('#optimize-exit')
  await waitFor(() => window.__duocad.mode === 'idle', '最適化モードから抜けられない')
  await frameAll(page)
  await clickFace(page, mesh.bodyId, 1)
  await waitFor(
    (id) =>
      (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'body' && item.body === id),
    'メッシュをクリックしてもボディとして選ばれない',
    [mesh.bodyId],
  )
  const faceRefs = await page.evaluate(
    (id) =>
      (window.__duocad.selection?.items ?? []).filter((item) => item.kind === 'face' && item.body === id)
        .length,
    mesh.bodyId,
  )
  if (faceRefs !== 0) fail(`メッシュに面参照が飛んでいる(${faceRefs}件)`)
  ok('[5] メッシュはボディ参照になる(`select_face` の断りを踏まない)')

  // **編集verbは確定できない** —— 起動はできるが、ボディ参照ではどの編集verbの
  // 選択要件も満たせないので、Enterがサーバへ届かない(押せるのに断られる、が起きない)。
  const opsBefore = await page.evaluate(() => window.__duocad.state.applied_count)
  await page.click('#move-body')
  await page.keyboard.press('Enter')
  await sleep(400)
  const opsAfter = await page.evaluate(() => window.__duocad.state.applied_count)
  if (opsAfter !== opsBefore) fail('メッシュボディに move_body が通ってしまった')
  const prompt = await text('#command-prompt')
  if (!prompt || !prompt.includes('選ぶ')) {
    fail(`編集verbが「何が足りないか」を言っていない: ${prompt}`)
  }
  ok(`[5] 編集verbは確定できず、足りないものを言う(「${prompt.slice(0, 30)}…」)`)

  await assertNoAppErrors(page, consoleErrors)
})
