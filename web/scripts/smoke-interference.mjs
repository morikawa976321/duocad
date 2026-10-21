/**
 * 実機での確認 — **干渉チェック(issue #186)で、重なっている領域が画面上で分かる**。
 *
 *   node scripts/smoke-interference.mjs
 *
 *   1. 重なった2体(+離れた1体)で「干渉チェック」を押すと、結果のイベントに overlap の三角形があり、
 *      ビューポートの層に赤い立体が1つ増える(体積 4000 mm³ = 10×20×20)
 *   2. パネルの見出しは4つで、干渉の行は開き、接触は畳まれている。行を選ぶとその組が強調され、もう一度押すと外れる
 *   3. 隙間の欄に 0.2 を入れて再検査すると、その値が結果に載る(欄は同じ文書の間は残る)
 *   4. 検査の前後で文書の版(適用回数・ログ本数)が変わらず、Ctrl+Z の対象にもならない
 *   5. 検査の最中に文書が変わったら、届いた結果は描かない
 *   6. 閉じると赤い立体が消える
 *   7. 隙間の欄は、起動し直しても同じ文書の間は残り、文書を切り替えると 0 に戻る
 *
 * 形は全部 MCP で作る。**`--models-dir` に使い捨てディレクトリを渡す**(`runSmoke` が既定で渡す)。
 * 子プロセス(`python -m duocad.interference_worker`)の起動に 1〜2 秒かかるので、待ちは長めにしてある。
 */
import { assertNoAppErrors, openApp, sleep, waitOrFail } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'

runSmoke('smoke-interference', { timeout: 120000, mcp: true }, async ({ browser, url, timeout, mcp }) => {
  await addExtrudedRect(mcp, { corner: [0, 0], width: 20, height: 20, distance: 20 })
  await addExtrudedRect(mcp, { corner: [10, 0], width: 20, height: 20, distance: 20 })
  await addExtrudedRect(mcp, { corner: [100, 0], width: 20, height: 20, distance: 20 })

  const { page, consoleErrors } = await openApp(browser, url, { timeout })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  const hook = (read) => page.evaluate(read)

  await waitFor(() => window.__duocad.parts.length === 3, '3つのボディが届く')
  const before = await hook(() => ({
    applied: window.__duocad.state.applied_count,
    log: window.__duocad.state.log.length,
    canUndo: window.__duocad.state.can_undo,
  }))

  // ---------------------------------------------------------------- 1. 押す → 結果 → 赤い立体
  await page.click('#interference')
  await waitFor(() => window.__duocad.interference.result !== null, '干渉チェックの結果が届く')
  const result = await hook(() => JSON.parse(JSON.stringify(window.__duocad.interference.result)))
  const overlaps = result.pairs.filter((pair) => pair.relation === 'overlap')
  if (overlaps.length !== 1) fail(`重なっている組が1つのはずが ${overlaps.length}: ${JSON.stringify(result.counts)}`)
  if (Math.abs(overlaps[0].volume - 4000) > 1e-3) fail(`重なりの体積が 4000 のはずが ${overlaps[0].volume}`)
  if (!(overlaps[0].render?.solid?.indices?.length > 0)) fail('結果のイベントに重なりの三角形が無い')
  if (result.target.mode !== 'visible_all' || result.target.pairs !== 3) fail(`対象が違う: ${JSON.stringify(result.target)}`)
  ok('結果のイベントに overlap の三角形があり、体積が 4000 mm³')
  await waitFor(() => window.__duocad.interference.solids === 1, 'ビューポートの層に赤い立体が1つ載る')
  ok('ビューポートの層に赤い立体が1つ増える')

  // ---------------------------------------------------------------- 2. パネル
  const headings = await hook(() => [...document.querySelectorAll('#interference-panel details')].map((d) => ({
    group: d.dataset.group, open: d.open, rows: d.querySelectorAll('[data-pair]').length,
  })))
  const expected = [['overlap', true, 1], ['short', true, 0], ['unknown', true, 0], ['contact', false, 0]]
  if (JSON.stringify(headings.map((h) => [h.group, h.open, h.rows])) !== JSON.stringify(expected)) {
    fail(`見出しが違う: ${JSON.stringify(headings)}`)
  }
  ok('見出しは干渉・隙間不足・未確定・接触の4つで、接触だけ畳まれている')
  await page.click('[data-pair]')
  await waitFor(() => document.querySelector('[data-pair].picked') !== null, '行を選ぶと強調される')
  await waitFor(() => window.__duocad.interference.solids === 1, '選んだ組の立体が出る')
  await page.click('[data-pair]')
  await waitFor(() => document.querySelector('[data-pair].picked') === null, 'もう一度押すと外れる')
  ok('行を選ぶとその組が強調され、もう一度押すと外れる')

  // ---------------------------------------------------------------- 3. 隙間の欄
  await page.click('#interference-clearance')
  await page.keyboard.down('Control')
  await page.keyboard.press('KeyA')
  await page.keyboard.up('Control')
  await page.keyboard.type('0.2')
  const firstOp = result.client_op
  await page.click('#interference-run')
  await waitFor((op) => {
    const next = window.__duocad.interference.result
    return next !== null && next.client_op !== op && next.clearance === 0.2
  }, '隙間 0.2 の結果が届く', [firstOp])
  ok('隙間の欄の値が結果に載る')
  await page.click('#interference')
  await waitFor((op) => {
    const next = window.__duocad.interference.result
    return next !== null && next.client_op > op
  }, '押し直した結果が届く', [firstOp + 1])
  if ((await hook(() => document.querySelector('#interference-clearance').value)) !== '0.2') fail(`起動し直すと隙間の欄が変わった: ${await hook(() => document.querySelector('#interference-clearance').value)}`)
  ok('隙間の欄は同じ文書のあいだ直前の値のまま')

  // ---------------------------------------------------------------- 4. 文書は変わらない
  const after = await hook(() => ({
    applied: window.__duocad.state.applied_count,
    log: window.__duocad.state.log.length,
    canUndo: window.__duocad.state.can_undo,
  }))
  if (JSON.stringify(before) !== JSON.stringify(after)) fail(`検査で文書が変わった: ${JSON.stringify(before)} → ${JSON.stringify(after)}`)
  ok('検査の前後で文書の版もログも変わらない')

  // ---------------------------------------------------------------- 5. 検査の最中に文書が変わる
  await page.click('#interference-run')
  await addExtrudedRect(mcp, { corner: [200, 0], width: 5, height: 5, distance: 5 })
  await waitFor(() => window.__duocad.interference.running === false, '検査が終わる(結果は捨てられる)')
  await sleep(300)
  const stale = await hook(() => ({
    result: window.__duocad.interference.result,
    notice: document.querySelector('#interference-notice')?.textContent ?? '',
  }))
  if (stale.result !== null) fail('文書が変わった後に届いた結果が描かれた')
  if (!stale.notice.includes('文書が変わった')) fail(`捨てた旨の表示が無い: ${stale.notice}`)
  await waitFor(() => window.__duocad.interference.solids === 0, '古い赤い立体が残らない')
  ok('検査の最中に文書が変わったら、届いた結果は描かない')

  // ---------------------------------------------------------------- 6. 閉じる
  await page.click('#interference')
  await waitFor(() => window.__duocad.interference.result !== null, '押し直すと新しい結果が届く')
  await page.click('#interference-close')
  await waitFor(() => window.__duocad.interference.solids === 0 && document.querySelector('#interference-panel') === null, '閉じると赤い立体が消える')
  ok('閉じると赤い立体もパネルも消える')

  await assertNoAppErrors(page, consoleErrors)

  // ---------------------------------------------------------------- 7. 文書を切り替えると隙間の欄は 0 に戻る
  // 新しい文書は空なので、押すと「組が作れない」と断られるが、パネルは開いて欄の値が見える(断りの札は検査の対象外)。
  await page.click('#file-new')
  await waitFor(() => document.querySelector('#interference-panel') === null, '文書を切り替えるとパネルが畳まれる')
  await page.click('#interference')
  await waitFor(() => document.querySelector('#interference-clearance') !== null, 'パネルが開く')
  if ((await hook(() => document.querySelector('#interference-clearance').value)) !== '0') fail('文書を切り替えても隙間の欄が 0 に戻らない')
  ok('文書を切り替えると隙間の欄は 0 に戻る')
})
