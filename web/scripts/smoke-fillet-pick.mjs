/**
 * 実機での確認 — **フィレット・面取りの起動中は面を選ばせず、辺は連なりで選ぶ**。
 *
 *   node scripts/smoke-fillet-pick.mjs
 *
 *   1. 面を選んだ状態でフィレットを起動すると、面が選択から外れる(辺は残す)
 *   2. 起動中に面をクリックしても選択に入らず、ホバーも面を指さない
 *   3. 接線で続く辺(角を丸めた箱の底の直線→円弧→直線)の1本をクリックすると連なりが全部入り、もう一度で全部外れる
 *   4. 面取りでも 1 と同じ
 *
 * 形は MCP で作る: 箱を作り、縦の辺1本を丸める。一直線上で割れた辺は押し出しの join / union が面ごと
 * 統合して1本にするので、画面から作れる連なりの題材はこちら(一直線の連なりは `tests/test_blend_chains.py`)。
 */
import { assertNoAppErrors, frameAll, openApp, waitFrames, waitOrFail } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { clickAt, clickFace, faceAt, screenOf } from './pick.mjs'

runSmoke('smoke-fillet-pick', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  const { bodyId } = await addExtrudedRect(mcp, { corner: [0, 0], width: 20, height: 10, distance: 5 })
  ok(`箱を作った: ${bodyId}`)

  const { page, consoleErrors } = await openApp(browser, url, { timeout })
  await frameAll(page)

  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  const selection = () =>
    page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.selection?.items ?? [])))
  const kinds = async () => (await selection()).map((item) => item.kind ?? (item.entity ? 'entity' : 'region'))

  /** 画面に見えている面を1つ(添字と画面座標)。 */
  async function visibleFace() {
    for (let index = 1; index <= 20; index += 1) {
      const at = await faceAt(page, bodyId, index)
      if (at) return { index, at }
    }
    fail('画面に見えている面が無い')
  }

  // ---------------------------------------------------------------- 1. 起動で面が外れる
  const face = await visibleFace()
  await clickFace(page, bodyId, face.index)
  await waitFor(() => (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'face'), '面が選ばれない')
  await page.click('#fillet')
  await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, 'フィレットの起動で面が外れない')
  ok('面を選んだままフィレットを起動したら、面が外れた')

  // ---------------------------------------------------------------- 2. 起動中は面を指せない
  await waitFor(
    (id) => (window.__duocad.edges?.body_id === id) && (window.__duocad.edges?.edges ?? []).length > 0,
    '辺が届かない',
    [bodyId],
  )
  // 面の射影点が辺の近くだと辺が当たるので、辺に当たらない面の点を探す(面に当たらないことはどの点でも見る)。
  let open = null
  for (let index = 1; index <= 20 && open === null; index += 1) {
    const at = await faceAt(page, bodyId, index)
    if (!at) continue
    await page.mouse.move(at.x, at.y)
    await waitFrames(page, 3)
    const hovered = await page.evaluate(() => window.__duocad.hoveredRef())
    if (hovered?.face) fail(`起動中に面 ${index} がホバーされた: ${JSON.stringify(hovered.face)}`)
    if (!hovered?.edge) open = at
  }
  if (open === null) fail('辺に当たらない面の点が見つからない(検査の前提が崩れた)')
  await clickAt(page, open)
  await waitFrames(page, 5)
  if ((await kinds()).includes('face')) fail('起動中のクリックで面が選択に入った')
  ok('起動中は面がホバーもクリックもされない')

  // ---------------------------------------------------------------- 3. 連なりで選ぶ
  const vertical = await page.evaluate(() => {
    const same = (values) => values.every((value) => Math.abs(value - values[0]) < 1e-6)
    const edge = window.__duocad.edges.edges.find(
      (item) => same(item.points.filter((_, i) => i % 3 === 0)) && same(item.points.filter((_, i) => i % 3 === 1)),
    )
    return edge?.index ?? null
  })
  if (vertical === null) fail('縦の辺が見つからない')
  await page.keyboard.press('Escape')
  const revisionOf = (id) => (window.__duocad.state?.bodies ?? []).find((body) => body.body_id === id)?.revision
  const before = await page.evaluate(revisionOf, bodyId)
  await mcp.callTool('set_selection', { refs: [{ body: bodyId, kind: 'edge', index: vertical }] })
  await mcp.callTool('fillet', { radius: 2 })
  await waitFor(
    (id, previous) => (window.__duocad.state?.bodies ?? []).find((body) => body.body_id === id)?.revision !== previous,
    '角の丸めが画面に届かない',
    [bodyId, before],
  )
  await page.click('#fillet')
  await waitFor(
    (id) => {
      const body = (window.__duocad.state?.bodies ?? []).find((item) => item.body_id === id)
      return window.__duocad.edges?.body_id === id && window.__duocad.edges.revision === body?.revision
    },
    '丸めた後の辺が届かない',
    [bodyId],
  )
  const chain = await page.evaluate(() => {
    const groups = new Map()
    for (const edge of window.__duocad.edges.edges) {
      groups.set(edge.chain, [...(groups.get(edge.chain) ?? []), edge.index])
    }
    return [...groups.values()].find((members) => members.length === 3) ?? null
  })
  if (chain === null) fail('直線→円弧→直線の連なり(3本)が届いていない(kernel.blend_chains)')
  /** 連なりのどれかの辺の画面座標。選択が変わるとコマンド欄の行が変わってビューポートが動くので、押す直前に取る。 */
  async function chainTarget() {
    for (const index of chain) {
      const at = await screenOf(page, 'projectEdge', bodyId, index)
      if (at) return at
    }
    fail(`連なり ${chain} のどの辺も画面で指せない`)
  }
  const target = await chainTarget()
  await page.mouse.move(target.x, target.y)
  await waitFrames(page, 3)
  // 連なりのホバーの見た目は数値で読めない(材質は公開していない)ので、目で見るときだけ撮る。
  if (process.env.FILLET_PICK_SHOT) await page.screenshot({ path: process.env.FILLET_PICK_SHOT })
  await clickAt(page, target)
  await waitFor(
    (members) => {
      const picked = (window.__duocad.selection?.items ?? []).filter((item) => item.kind === 'edge').map((item) => item.index)
      return members.every((index) => picked.includes(index)) && picked.length === members.length
    },
    `辺1本のクリックで連なり ${chain} が全部入らない`,
    [chain],
  )
  ok(`辺1本のクリックで連なり ${chain} が全部入った`)
  await waitFrames(page, 3)
  await clickAt(page, await chainTarget())
  await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, 'もう一度のクリックで連なりが外れない')
  ok('もう一度のクリックで全部外れた')

  // ---------------------------------------------------------------- 4. 面取りでも面が外れる
  await page.keyboard.press('Escape')
  await waitFor(() => window.__duocad.edges === null || (window.__duocad.selection?.items ?? []).length === 0, 'Esc で降りない')
  await waitFrames(page, 5)
  await clickFace(page, bodyId, (await visibleFace()).index) // 角を丸めたので面の添字は付け直し
  await waitFor(() => (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'face'), '面が選ばれない(面取りの前)')
  await page.click('#chamfer')
  await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, '面取りの起動で面が外れない')
  ok('面を選んだまま面取りを起動したら、面が外れた')

  await assertNoAppErrors(page, consoleErrors)
})
