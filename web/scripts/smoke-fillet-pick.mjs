/**
 * 実機での確認 — **フィレット・面取りの起動中は面を選ばせず、辺は連なりで選ぶ**。
 *
 *   node scripts/smoke-fillet-pick.mjs
 *
 *   1. 面を選んだ状態でフィレットを起動すると、面が選択から外れる(辺は残す)
 *   2. 起動中は辺だけを指せる(`pickKinds`)。面をクリックしても選択に入らず、ホバーも面を指さない
 *   3. 接線で続く辺(角を丸めた箱の底の直線→円弧→直線)の1本をクリックすると連なりが全部入り、もう一度で全部外れる。
 *      連なりの一部だけが入った状態で連なりの1本を矩形で囲むと全部入り、もう一度で全部外れる
 *   4. 面取りでも 1 と同じ
 *
 * 形は MCP で作る: 箱を2つ作り、アクティブでない方の対角の縦の辺2本を丸める。一直線上で割れた辺は押し出しの join / union が面ごと
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
  // 辺はカーソルの近くのボディの分が届く(面を押したカーソルはそのボディの上にある)
  await waitFor(
    (id) => (window.__duocad.edges?.[id]?.edges ?? []).length > 0,
    '面を外したボディの辺が届かない',
    [bodyId],
  )
  if (JSON.stringify(await page.evaluate(() => window.__duocad.pickKinds)) !== '["edge"]') {
    fail(`フィレット中に辺以外も指せる: ${JSON.stringify(await page.evaluate(() => window.__duocad.pickKinds))}`)
  }
  ok('起動中は辺だけを指せ、面を外したボディの辺が届いている')
  // 面の重心が辺の近くだと辺が当たるので、辺に当たらない面の点を探す(面に当たらないことはどの点でも見る)。
  // `projectFace` は使えない: 起動中は面が取れないので、取れる画素を返す約束どおり null になる。
  const centroids = await page.evaluate((id) => (window.__duocad.getGeometry(id)?.faces ?? []).map((face) => [...face.centroid]), bodyId)
  let open = null
  for (let index = 1; index <= centroids.length && open === null; index += 1) {
    const at = await screenOf(page, 'projectPoint', centroids[index - 1])
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
  // 対角の縦の辺2本を丸める: 上面の輪郭に「直線→円弧→直線」の連なりが2つでき、どちらの円弧も上から見える
  // (裏に回った円弧は指せないので、連なりの届く前のクリックを見る円弧が上面に要る)
  const verticals = await page.evaluate((id) => {
    const same = (values) => values.every((value) => Math.abs(value - values[0]) < 1e-6)
    const found = window.__duocad.edges[id].edges
      .filter((item) => same(item.points.filter((_, i) => i % 3 === 0)) && same(item.points.filter((_, i) => i % 3 === 1)))
      .map((item) => ({ index: item.index, x: item.points[0], y: item.points[1] }))
    for (const a of found) {
      const b = found.find((other) => Math.abs(other.x - a.x) > 1e-6 && Math.abs(other.y - a.y) > 1e-6)
      if (b) return [a.index, b.index]
    }
    return null
  }, bodyId)
  if (verticals === null) fail('対角の縦の辺が見つからない')
  await page.keyboard.press('Escape')
  const revisionOf = (id) => (window.__duocad.state?.bodies ?? []).find((body) => body.body_id === id)?.revision
  const before = await page.evaluate(revisionOf, bodyId)
  await mcp.callTool('set_selection', { refs: verticals.map((index) => ({ body: bodyId, kind: 'edge', index })) })
  await mcp.callTool('fillet', { radius: 2 })
  await waitFor(
    (id, previous) => (window.__duocad.state?.bodies ?? []).find((body) => body.body_id === id)?.revision !== previous,
    '角の丸めが画面に届かない',
    [bodyId, before],
  )
  const filleted = await page.evaluate(revisionOf, bodyId)
  // 検査する方はアクティブではないので、その面を選んでから起動する(面は外れ、辺はそのボディのものが出る)。
  await waitFrames(page, 5)
  await clickFace(page, bodyId, (await visibleFace()).index)
  await waitFor(() => (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'face'), '面が選ばれない(丸めた後)')
  await page.click('#fillet')
  // 丸めた形の辺(丸めた後の版以降)。版はその後も粗さで上がりうるが、形が同じなら添字はそのまま使える(`sameShape`)
  await waitFor(
    (id, revision) => (window.__duocad.edges?.[id]?.revision ?? -1) >= revision,
    '丸めた後の辺が届かない',
    [bodyId, filleted],
  )
  // 連なりは辺の折れ線に入っておらず、ホバーした辺の分だけ届く(`get_edge_chains`)。円弧に乗せて取り寄せさせる。
  const arcs = await page.evaluate(
    (id) => window.__duocad.edges[id].edges.filter((edge) => edge.kind === 'circle').map((edge) => edge.index),
    bodyId,
  )
  let chain = null
  for (const index of arcs) {
    const at = await screenOf(page, 'projectEdge', bodyId, index)
    if (!at) continue
    await page.mouse.move(at.x, at.y)
    await page.waitForFunction((id, i) => window.__duocad.edgeChain(id, i) !== null, { timeout: 15000, polling: 'raf' }, bodyId, index).catch(() => {})
    const members = await page.evaluate((id, i) => window.__duocad.edgeChain(id, i), bodyId, index)
    if (members?.length === 3) {
      chain = members
      break
    }
  }
  if (chain === null) fail('直線→円弧→直線の連なり(3本)が届いていない(kernel_chains.BlendChains)')
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
  const arc = await page.evaluate((id, members) => {
    const edge = window.__duocad.edges[id].edges.find((item) => members.includes(item.index) && item.kind === 'circle')
    return edge ? { index: edge.index, points: Array.from(edge.points) } : null // $state の配列は素では渡らない
  }, bodyId, chain)
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

  // 連なりがまだ届いていない辺のクリック(ホバーより先に押した)は、連なりが届くまで待ってから全部入れる。
  // 押す・離すを1回の evaluate で送り、間にホバーの更新(毎フレーム)を挟ませない。
  const unknownArc = await page.evaluate(
    (id, members) =>
      window.__duocad.edges[id].edges.find(
        (edge) =>
          edge.kind === 'circle' &&
          !members.includes(edge.index) &&
          window.__duocad.edgeChain(id, edge.index) === null &&
          window.__duocad.projectEdge(id, edge.index) !== null,
      )?.index ?? null,
    bodyId,
    chain,
  )
  if (unknownArc === null) fail('連なりの届いていない見えている円弧が無い(もう一方の角の上面の輪郭)')
  const unknownAt = await screenOf(page, 'projectEdge', bodyId, unknownArc)
  if (!unknownAt) fail(`円弧 ${unknownArc} が画面で指せない`)
  await page.evaluate((at) => {
    const canvas = document.querySelector('canvas')
    for (const type of ['pointerdown', 'pointerup']) {
      canvas.dispatchEvent(new PointerEvent(type, { clientX: at.x, clientY: at.y, button: 0, pointerId: 1, isPrimary: true, bubbles: true }))
    }
  }, unknownAt)
  await waitFor(
    (id, index) => {
      const members = window.__duocad.edgeChain(id, index)
      const picked = (window.__duocad.selection?.items ?? []).filter((item) => item.kind === 'edge').map((item) => item.index)
      return members !== null && members.length === 3 && picked.length === 3 && members.every((member) => picked.includes(member))
    },
    `連なりの届く前にクリックした円弧 ${unknownArc} の連なりが全部入らない`,
    [bodyId, unknownArc],
  )
  ok('連なりの届く前のクリックでも、届いてから連なりが全部入った')

  // ---------------------------------------------------------------- 4. 面取りでも面が外れる
  await page.keyboard.press('Escape')
  await waitFor(() => window.__duocad.command === null, 'Esc で降りない')
  await waitFrames(page, 5)
  await clickFace(page, bodyId, (await visibleFace()).index) // 角を丸めたので面の添字は付け直し
  await waitFor(() => (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'face'), '面が選ばれない(面取りの前)')
  await page.click('#chamfer')
  await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, '面取りの起動で面が外れない')
  ok('面を選んだまま面取りを起動したら、面が外れた')

  await assertNoAppErrors(page, consoleErrors)
})
