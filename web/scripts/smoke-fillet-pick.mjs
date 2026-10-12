/**
 * 実機での確認 — **フィレット・面取りの起動中は面を選ばせず、辺は連なりで選ぶ**。
 *
 *   node scripts/smoke-fillet-pick.mjs
 *
 *   1. 面を選んだ状態でフィレットを起動すると、面が選択から外れる(辺は残す)。辺はアクティブではない
 *      その面のボディのものが出る(面を外してもアクティブボディへ戻らない)
 *   2. 起動中に面をクリックしても選択に入らず、ホバーも面を指さない
 *   3. 接線で続く辺(角を丸めた箱の底の直線→円弧→直線)の1本をクリックすると連なりが全部入り、もう一度で全部外れる。
 *      連なりの一部だけが入った状態で連なりの1本を矩形で囲むと全部入り、もう一度で全部外れる
 *   4. 面取りでも 1 と同じ
 *
 * 形は MCP で作る: 箱を2つ作り、アクティブでない方の縦の辺1本を丸める。一直線上で割れた辺は押し出しの join / union が面ごと
 * 統合して1本にするので、画面から作れる連なりの題材はこちら(一直線の連なりは `tests/test_blend_chains.py`)。
 */
import { assertNoAppErrors, frameAll, openApp, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { clickAt, clickFace, faceAt, screenOf } from './pick.mjs'

runSmoke('smoke-fillet-pick', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  const first = await addExtrudedRect(mcp, { corner: [0, 0], width: 20, height: 10, distance: 5 })
  const second = await addExtrudedRect(mcp, { corner: [40, 0], width: 20, height: 10, distance: 5 })

  const { page, consoleErrors } = await openApp(browser, url, { timeout })
  await frameAll(page)
  const active = await page.evaluate(() => window.__duocad.state?.active_body_id ?? null)
  // 検査するのはアクティブでない方(面を外したあと辺のボディがアクティブへ戻らないことを見るため)。
  const bodyId = active === first.bodyId ? second.bodyId : first.bodyId
  ok(`箱を2つ作った: アクティブ ${active}、検査する方 ${bodyId}`)

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
    '外した面のボディの辺が出ない(アクティブボディへ戻った)',
    [bodyId],
  )
  await waitFrames(page, 10)
  const shown = await page.evaluate(() => window.__duocad.edges?.body_id)
  if (shown !== bodyId) fail(`辺のボディが ${shown} へ変わった(外した面のボディ ${bodyId} であるべき)`)
  ok('面を外しても、辺はその面のボディのものが出ている')
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
  // 検査する方はアクティブではないので、その面を選んでから起動する(面は外れ、辺はそのボディのものが出る)。
  await waitFrames(page, 5)
  await clickFace(page, bodyId, (await visibleFace()).index)
  await waitFor(() => (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'face'), '面が選ばれない(丸めた後)')
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

  // 矩形: 連なりの1本(円弧)だけを囲む。先に連なりの別の1本だけを選んでおく。
  const arc = await page.evaluate((members) => {
    const edge = window.__duocad.edges.edges.find((item) => members.includes(item.index) && item.kind === 'circle')
    return edge ? { index: edge.index, points: Array.from(edge.points) } : null // $state の配列は素では渡らない
  }, chain)
  if (arc === null) fail(`連なり ${chain} に円弧が無い`)
  const other = chain.find((index) => index !== arc.index)
  await mcp.callTool('set_selection', { refs: [{ body: bodyId, kind: 'edge', index: other }] })
  await waitFor((index) => {
    const items = window.__duocad.selection?.items ?? []
    return items.length === 1 && items[0].index === index
  }, '連なりの一部だけを選べない', [other])
  await waitFrames(page, 3)
  /**
   * 円弧を囲む矩形(ページ座標)を引く。選択が変わるとビューポートが動くので毎回射影し直す。
   * ブラウザパネルは畳む(ビューポートの左上に重なり、矩形の始点を吸う)。
   */
  const dragAroundArc = () => withoutBrowserPanel(page, dragAroundArcNow)
  async function dragAroundArcNow() {
    const corners = []
    for (let i = 0; i < arc.points.length; i += 3) {
      const at = await screenOf(page, 'projectPoint', [arc.points[i], arc.points[i + 1], arc.points[i + 2]])
      if (!at) fail('円弧の点が画面に射影できない')
      corners.push(at)
    }
    const pad = 4
    const left = Math.min(...corners.map((c) => c.x)) - pad
    const top = Math.min(...corners.map((c) => c.y)) - pad
    const right = Math.max(...corners.map((c) => c.x)) + pad
    const bottom = Math.max(...corners.map((c) => c.y)) + pad
    await page.mouse.move(left, top)
    await page.mouse.down()
    await page.mouse.move((left + right) / 2, (top + bottom) / 2, { steps: 4 })
    await page.mouse.move(right, bottom, { steps: 4 })
    await page.mouse.up()
  }
  await dragAroundArc()
  await waitFor(
    (members) => {
      const picked = (window.__duocad.selection?.items ?? []).filter((item) => item.kind === 'edge').map((item) => item.index)
      return members.every((index) => picked.includes(index)) && picked.length === members.length
    },
    `一部だけ選んだ連なり ${chain} を矩形で囲んでも全部入らない`,
    [chain],
  )
  ok('一部だけ選んだ連なりの1本を矩形で囲むと、連なりが全部入った')
  await waitFrames(page, 3)
  await dragAroundArc()
  await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, 'もう一度の矩形で連なりが外れない')
  ok('もう一度の矩形で全部外れた')

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
