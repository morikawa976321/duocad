/**
 * 実ブラウザでの検査 — **人と AI が1つの verb を一緒に進める**(issue #153。起動 → 選択と引数を相互に直す → 誰かが確定)。
 * 画面は起動中の verb と引数を自分で持たず、サーバの作業空間(`state.command`)に従う。ここが壊れると、AI が構えた
 * ものが画面に出ない・人が打った値が AI に見えない・片方の確定でもう片方の構えが残る、のどれかが起きる。
 *
 *   web> node scripts/smoke-workspace.mjs
 *
 *   1. AI が begin_command(fillet) → set_selection(辺) で止めると、画面に「丸める」が起動して辺が見える
 *   2. AI の set_command_args(radius) が人の欄に出る
 *   3. 人が打った値が共有の command に出る(打ち終わってから)
 *   4. 打っている最中の欄は外からの値で奪われず、欄を離れると自分の値が勝つ
 *   5. 人の Enter で確定され、共有の command が畳まれる
 *   6. 人が起動したものを AI が埋めて確定すると、画面の構えも畳まれる
 *   7. 人の Esc で共有の command が畳まれ、選択が空になる
 *   8. 再読み込みしても起動中のコマンドと引数が戻る
 */
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, sleep, typeInto, waitOrFail } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'

const VERTICAL_EDGES = { kind: 'edge', where: [{ op: 'parallel_to', axis: 'Z' }] }

runSmoke('smoke-workspace', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  const near = await addExtrudedRect(mcp, { corner: [0, 0], width: 60, height: 20, distance: 5 })
  const far = await addExtrudedRect(mcp, { corner: [-40, 0], width: 20, height: 20, distance: 5 })
  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.parts ?? []).length >= 2,
  })
  await frameAll(page)
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout })
  const hostCommand = () => page.evaluate(() => document.querySelector('.command-host')?.dataset.command ?? null)

  // ================================================================ 1. AI が起動して辺まで選ぶ
  await mcp.callTool('set_visible', { body_id: far.bodyId, visible: false }) // 述語がボディをまたがないように
  await mcp.callTool('begin_command', { verb: 'fillet' })
  await waitFor(
    () => document.querySelector('.command-host')?.dataset.command === 'fillet' && window.__duocad.wantsEdges === true,
    'AI の begin_command で画面に「丸める」が起動しない(辺を要求していない)',
  )
  await mcp.callTool('set_selection', { predicate: VERTICAL_EDGES })
  await waitFor(
    () => (window.__duocad.selection?.count ?? 0) === 4 && (window.__duocad.edges?.edges?.length ?? 0) > 0,
    'AI の辺の選択が画面に届かない(辺のジオメトリが無い)',
  )
  ok('[1] AI が begin_command(fillet) → set_selection(辺) で止めると、画面に「丸める」が起動して辺が見える')

  // ================================================================ 2. AI の引数が欄に出る
  await mcp.callTool('set_command_args', { args: { radius: 2 } })
  await waitFor(() => document.querySelector('#fillet-radius')?.value === '2', 'AI の set_command_args が欄に写らない')
  ok('[2] AI の set_command_args(radius) が人の欄に出る')

  // ================================================================ 3. 人の入力が共有の command に出る
  await typeInto(page, '#fillet-radius', '3')
  await waitFor(() => window.__duocad.command?.args?.radius === 3, '人が打った半径がサーバの作業空間へ出ない')
  ok('[3] 人が打った値が共有の command に出る')

  // ================================================================ 4. 打鍵中の欄は奪われない
  await page.focus('#fillet-radius')
  await mcp.callTool('set_command_args', { args: { radius: 4 } })
  await waitFor(() => window.__duocad.command?.args?.radius === 4, 'AI の変更がサーバへ入らない')
  await sleep(300)
  const kept = await page.$eval('#fillet-radius', (el) => el.value)
  if (kept !== '3') fail(`打鍵中の欄が外からの値で奪われた: ${kept}`)
  await page.evaluate(() => document.querySelector('#fillet-radius').blur())
  await waitFor(() => window.__duocad.command?.args?.radius === 3, '欄を離れても自分の値がサーバへ出ない')
  ok('[4] 打っている最中の欄は外からの値で奪われず、欄を離れると自分の値が勝つ')

  // ================================================================ 5. 人の Enter で確定
  const logBefore = await page.evaluate(() => window.__duocad.state.log.length)
  await page.focus('#fillet-radius')
  await page.keyboard.press('Enter')
  await waitFor(
    (n) => window.__duocad.state.log.length === n + 1 && window.__duocad.command === null,
    '人の Enter で確定されない / 共有の command が畳まれない',
    [logBefore],
  )
  if ((await hostCommand()) !== null) fail('確定の後も画面の構えが残っている')
  ok('[5] 人の Enter で確定され、共有の command が畳まれる')

  // ================================================================ 6. 人が起動し、AI が埋めて確定する
  await mcp.callTool('set_visible', { body_id: near.bodyId, visible: false }) // 丸めた板の縁は再び丸められないので、向こうの板で
  await mcp.callTool('set_visible', { body_id: far.bodyId, visible: true })
  await page.click('#fillet')
  await waitFor(() => window.__duocad.command?.verb === 'fillet', '人の起動が共有の command に出ない')
  await mcp.callTool('set_selection', { predicate: VERTICAL_EDGES })
  await waitFor(() => (window.__duocad.selection?.count ?? 0) === 4, 'AI の辺の選択が画面に届かない')
  await mcp.callTool('set_command_args', { args: { radius: 1 } })
  await mcp.callTool('apply_command')
  await waitFor(
    (n) => window.__duocad.state.log.length === n + 2 && window.__duocad.command === null,
    'AI の apply_command で確定されない / 共有の command が畳まれない',
    [logBefore],
  )
  if ((await hostCommand()) !== null) fail('AI の確定の後も画面の構えが残っている')
  ok('[6] 人が起動したものを AI が埋めて確定すると、画面の構えも畳まれる')

  // ================================================================ 7. 人の Esc
  await page.click('#fillet')
  await waitFor(() => window.__duocad.command?.verb === 'fillet', '人の起動が共有の command に出ない')
  await mcp.callTool('set_selection', { predicate: VERTICAL_EDGES })
  await waitFor(() => (window.__duocad.selection?.count ?? 0) > 0, 'AI の辺の選択が画面に届かない')
  await page.keyboard.press('Escape')
  await waitFor(
    () => window.__duocad.command === null && (window.__duocad.selection?.count ?? 0) === 0,
    'Esc で共有の command が畳まれない / 選択が残る',
  )
  ok('[7] 人の Esc で共有の command が畳まれ、選択が空になる')

  // ================================================================ 8. 再読み込みで戻る
  await page.click('#fillet')
  await waitFor(() => window.__duocad.command?.verb === 'fillet', '人の起動が共有の command に出ない')
  await typeInto(page, '#fillet-radius', '2.5')
  await waitFor(() => window.__duocad.command?.args?.radius === 2.5, '人が打った半径がサーバの作業空間へ出ない')
  await page.reload({ waitUntil: 'load' })
  await waitFor(() => window.__duocad?.ready === true && (window.__duocad.parts ?? []).length >= 2, '再読み込み後に画面が立ち上がらない')
  await waitFor(
    () => document.querySelector('.command-host')?.dataset.command === 'fillet' && document.querySelector('#fillet-radius')?.value === '2.5',
    '再読み込みで起動中のコマンドと引数が戻らない',
  )
  ok('[8] 再読み込みしても起動中のコマンドと引数が戻る')
  await page.keyboard.press('Escape')
  await waitFor(() => window.__duocad.command === null, 'Esc で畳まれない')

  await assertNoAppErrors(page, consoleErrors)
})
