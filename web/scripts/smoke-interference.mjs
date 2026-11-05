/**
 * 実機での確認 — **干渉チェック(issue #186)の最中に文書が変わったら、届いた結果を描かない**。
 *
 *   node scripts/smoke-interference.mjs
 *
 * 計算はワーカーで走り、その間も人と AI は文書を変えられる。古い結果を描くと、今の形に無い重なりが赤く出る
 * (見た目では古いと分からない)。捨てたことは札で知らせる。
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
  await addExtrudedRect(mcp, { corner: [115, 0], width: 20, height: 20, distance: 20 })

  const { page, consoleErrors } = await openApp(browser, url, { timeout })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  await waitFor(() => window.__duocad.parts.length === 4, '4つのボディが届く')

  // パネルを開く(1回目の検査。結果が届いてから「再検査」を押せる)
  await page.click('#interference')
  await waitFor(() => window.__duocad.interference.result !== null, '干渉チェックの結果が届く')
  await waitFor(() => window.__duocad.interference.solids === 2, 'ビューポートの層に赤い立体が2つ載る')

  await page.click('#interference-run')
  // モードを変えない変更で文書の版を進める(箱を足すと画面がスケッチへ入り、モードの切り替えで先に畳まれる)
  const renamed = (await mcp.callTool('describe_document', {})).bodies[0].body_id
  await mcp.callTool('rename', { target: renamed, name: 'renamed-during-check' })
  await waitFor(() => window.__duocad.interference.running === false, '検査が終わる(結果は捨てられる)')
  await sleep(300)
  const stale = await page.evaluate(() => ({
    result: window.__duocad.interference.result,
    notice: document.querySelector('#interference-notice')?.textContent ?? '',
  }))
  if (stale.result !== null) fail('文書が変わった後に届いた結果が描かれた')
  if (!stale.notice.includes('文書が変わった')) fail(`捨てた旨の表示が無い: ${stale.notice}`)
  await waitFor(() => window.__duocad.interference.solids === 0, '古い赤い立体が残らない')
  ok('検査の最中に文書が変わったら、届いた結果は描かない')

  await assertNoAppErrors(page, consoleErrors)
})
