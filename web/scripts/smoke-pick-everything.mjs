/**
 * 実機での確認 — **見えるものはすべてさわれる**(docs/DESIGN_DECISIONS.md「見えるものはすべてさわれる」)。
 *
 *   web> node scripts/smoke-pick-everything.mjs
 *
 * 題材(MCP で作る): 20×20×10 の箱(元のスケッチは表示のまま)、箱の横の 8×8 の四角(スケッチ B。押し出さない)、
 * 箱の真下に隠れる円(スケッチ C)、反復の複製を持つ円(スケッチ D)。視点はホーム(+X・−Y・上から)を少し引いたもの。
 *
 *   1. コマンドを起動していなければ、通常モードで全種類を指せる(`pickKinds`)
 *   2. 編集していないスケッチ B の点・線・領域を、ホバーで光らせてからクリックで選べる
 *   3. 箱の面・見えている辺・見えている頂点をクリックで選べる(辺・頂点はカーソルの近くのボディから届く)
 *   4. 立体の裏のものは取れない: 箱に隠れた円 C の中心を押すと箱の面が取れ、隠れた辺の上を押してもその辺は取れない
 *   5. 反復の複製は独立した図形で、クリックでその複製だけが選べる
 *   6. 矩形選択は全種類を集める(B を囲むと図形と領域が入る)
 *   7. コマンド起動中は受け付けるものだけ: フィレット中は B の領域を押しても選択が変わらない
 *   8. スケッチモードでは編集中のスケッチの中身だけ: B を編集中は D の線を押しても選べない。抜けても B の図形の選択は残る
 *   9. 通常モードでも領域を押し出せる(B の領域で新規ボディ)
 */
import { fail, ok } from './args.mjs'
import {
  assertNoAppErrors,
  frameAll,
  openApp,
  typeInto,
  viewportCenter,
  waitFrames,
  waitOrFail,
  withoutBrowserPanel,
} from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { clickAt, faceAt, planarFace, screenOf, sketchPointAt } from './pick.mjs'

const BOX = 20
const HEIGHT = 10
/** スケッチ B の四角(左下の角と一辺)。 */
const FREE = { u: 26, v: 0, size: 8 }

runSmoke('smoke-pick-everything', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  const { bodyId } = await addExtrudedRect(mcp, { corner: [0, 0], width: BOX, height: BOX, distance: HEIGHT })
  const free = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  const freeRect = (
    await mcp.callTool('add_rectangle', { sketch_id: free, corner: [FREE.u, FREE.v], width: FREE.size, height: FREE.size })
  ).entity_id
  const hidden = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  await mcp.callTool('add_circle', { sketch_id: hidden, center: [10, 10], diameter: 4 })
  const patterned = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  const source = (await mcp.callTool('add_circle', { sketch_id: patterned, center: [2, -10], diameter: 3 })).entity_id
  const copies = (
    await mcp.callTool('pattern_rectangular', { sketch_id: patterned, entities: [source], dx: 8, dy: 0, nx: 3, ny: 1 })
  ).created
  await mcp.callTool('leave_sketch')
  await mcp.callTool('clear_selection')

  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.sketches ?? []).length >= 4 && window.__duocad.mode === 'idle',
  })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  const items = () => page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.selection?.items ?? [])))
  const clear = async () => {
    await mcp.callTool('clear_selection')
    await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, '選択が空にならない')
  }
  /**
   * 画素へカーソルを置き、ホバーが落ち着くまで待ってから押す(押す前の予告を返す)。`where` は画素を返す関数で、
   * ブラウザパネルを畳んだ後に射影する(パネルの下の画素は `project*` が null を返す)。
   */
  const hoverAndClick = async (where, what, { modifier = null } = {}) =>
    withoutBrowserPanel(page, async () => {
      const at = await where()
      if (!at) {
        const pose = await page.evaluate(() => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          return JSON.parse(JSON.stringify({ camera: window.__duocad.cameraPose(), size: [rect.width, rect.height] }))
        })
        fail(`${what} が画面の中に無い(${JSON.stringify(pose)})`)
      }
      await page.mouse.move(at.x, at.y)
      await waitFrames(page, 4)
      const hovered = await page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.hoveredRef())))
      await clickAt(page, at, { modifier })
      return hovered
    })
  const onSketch = (sketchId, u, v) => () => sketchPointAt(page, u, v, { sketchId, settle: true })
  await frameAll(page)
  // 視点合わせはボディだけに合うので、横に置いたスケッチまで画面に入るよう少し引く
  const center = await viewportCenter(page)
  await page.mouse.move(center.x, center.y)
  for (let step = 0; step < 3; step += 1) {
    await page.mouse.wheel({ deltaY: 200 })
    await waitFrames(page, 3)
  }
  await waitFrames(page, 20)

  // ================================================================ 1. 無起動は全種類
  const kinds = await page.evaluate(() => window.__duocad.pickKinds)
  for (const kind of ['face', 'edge', 'vertex', 'region', 'sketchPoint', 'sketchLine']) {
    if (!kinds.includes(kind)) fail(`無起動の通常モードで ${kind} を指せない: ${JSON.stringify(kinds)}`)
  }
  ok('pick: 無起動の通常モードは面・辺・頂点・領域・スケッチの点と線を全部指せる')

  // ================================================================ 2. 編集していないスケッチの点・線・領域
  let hovered = await hoverAndClick(onSketch(free, FREE.u + FREE.size / 2, FREE.v), 'スケッチ B の辺')
  if (hovered.sketchEntity?.entityId !== freeRect || hovered.sketchEntity?.point !== null) {
    fail(`スケッチ B の辺にホバーしても光らない: ${JSON.stringify(hovered)}`)
  }
  await waitFor(
    (sketch, entity) =>
      (window.__duocad.selection?.items ?? []).some((item) => item.sketch === sketch && item.entity === entity && item.point === null),
    '編集していないスケッチの辺をクリックしても選べない',
    [free, freeRect],
  )
  ok('pick: 編集していないスケッチの辺はホバーで光り、クリックで選べる')

  hovered = await hoverAndClick(onSketch(free, FREE.u, FREE.v), 'スケッチ B の角')
  if (hovered.sketchEntity?.point !== 'x') fail(`スケッチ B の点にホバーしても光らない: ${JSON.stringify(hovered)}`)
  await waitFor(
    (sketch) => (window.__duocad.selection?.items ?? []).some((item) => item.sketch === sketch && item.point === 'x'),
    '編集していないスケッチの点をクリックしても選べない',
    [free],
  )
  ok('pick: 編集していないスケッチの点はホバーで光り、クリックで選べる')

  const freeInside = onSketch(free, FREE.u + FREE.size / 2, FREE.v + FREE.size / 2)
  hovered = await hoverAndClick(freeInside, 'スケッチ B の領域')
  if (hovered.region?.sketchId !== free) fail(`スケッチ B の領域にホバーしても光らない: ${JSON.stringify(hovered)}`)
  await waitFor(
    (sketch) => (window.__duocad.selection?.items ?? []).some((item) => item.sketch === sketch && item.entity === undefined),
    '通常モードで領域をクリックしても選べない',
    [free],
  )
  ok('pick: 通常モードで領域がホバーで光り、クリックで選べる')

  // ================================================================ 3. 面・辺・頂点
  const top = await planarFace(page, bodyId, [0, 0, 1], { timeout })
  await hoverAndClick(() => faceAt(page, bodyId, top), '箱の上面')
  await waitFor(
    (id, index) => (window.__duocad.selection?.items ?? []).some((item) => item.body === id && item.kind === 'face' && item.index === index),
    '箱の上面をクリックしても選べない',
    [bodyId, top],
  )
  await waitFor((id) => window.__duocad.edges?.[id] !== undefined, 'カーソルを箱に置いても辺が届かない', [bodyId])
  ok('pick: 箱の上面をクリックで選べ、カーソルを置いた箱の辺が届く')

  const edgeCount = await page.evaluate((id) => window.__duocad.edges[id].edges.length, bodyId)
  let edgeIndex = null
  for (let index = 1; index <= edgeCount && edgeIndex === null; index += 1) {
    if ((await screenOf(page, 'projectEdge', bodyId, index)) !== null) edgeIndex = index
  }
  if (edgeIndex === null) fail('見えている辺が1本も指せない')
  hovered = await hoverAndClick(() => screenOf(page, 'projectEdge', bodyId, edgeIndex), `辺 ${edgeIndex}`)
  if (hovered.edge?.edgeIndex !== edgeIndex) fail(`辺にホバーしても光らない: ${JSON.stringify(hovered)}`)
  await waitFor(
    (id, index) => (window.__duocad.selection?.items ?? []).some((item) => item.body === id && item.kind === 'edge' && item.index === index),
    '見えている辺をクリックしても選べない',
    [bodyId, edgeIndex],
  )
  ok(`pick: 見えている辺(${edgeIndex})はホバーで光り、クリックで選べる`)

  let vertexIndex = null
  for (let index = 1; index <= 8 && vertexIndex === null; index += 1) {
    if ((await screenOf(page, 'projectVertex', bodyId, index)) !== null) vertexIndex = index
  }
  if (vertexIndex === null) fail('見えている頂点が1つも指せない')
  hovered = await hoverAndClick(() => screenOf(page, 'projectVertex', bodyId, vertexIndex), `頂点 ${vertexIndex}`)
  if (hovered.vertex?.vertexIndex !== vertexIndex) fail(`頂点にホバーしても光らない: ${JSON.stringify(hovered)}`)
  await waitFor(
    (id, index) => (window.__duocad.selection?.items ?? []).some((item) => item.body === id && item.kind === 'vertex' && item.index === index),
    '見えている頂点をクリックしても選べない',
    [bodyId, vertexIndex],
  )
  ok(`pick: 見えている頂点(${vertexIndex})はホバーで光り、クリックで選べる`)

  // ================================================================ 4. 立体の裏は取れない
  // 円の中心はホーム視点では手前の縦の辺の真後ろに来る(どちらも視線の水平成分に沿った対角線の上)ので、取れるのは
  // その辺か手前の面。どちらでもよく、隠れた円・領域が取れないことだけを見る。
  await clear()
  hovered = await hoverAndClick(onSketch(hidden, 10, 10), '箱に隠れた円の中心')
  if (hovered.sketchEntity !== null || hovered.region !== null) fail(`箱に隠れた円がホバーで光る: ${JSON.stringify(hovered)}`)
  await waitFor(() => (window.__duocad.selection?.items ?? []).length === 1, '箱に隠れた円の中心を押しても何も選ばれない')
  const underPicked = await items()
  if (underPicked.some((item) => item.sketch === hidden) || underPicked[0].body !== bodyId) {
    fail(`箱に隠れた円の中心を押すと、手前の箱ではなく ${JSON.stringify(underPicked)} が選ばれる`)
  }
  ok(`pick: 箱に隠れたスケッチの図形は取れず、手前の箱(${underPicked[0].kind})が取れる`)

  // 裏の辺(x=0・y=20 の縦の辺)はホーム視点では箱に隠れる
  const backEdge = await page.evaluate((id) => {
    const near = (a, b) => Math.abs(a - b) < 1e-6
    for (const edge of window.__duocad.edges[id].edges) {
      const p = edge.points
      let all = true
      for (let at = 0; at < p.length; at += 3) all = all && near(p[at], 0) && near(p[at + 1], 20)
      if (all) return { index: edge.index, mid: [0, 20, (p[2] + p[p.length - 1]) / 2] }
    }
    return null
  }, bodyId)
  if (backEdge === null) fail('箱の裏の縦の辺が見つからない')
  if ((await screenOf(page, 'projectEdge', bodyId, backEdge.index)) !== null) fail('箱に隠れた裏の辺を指せる画素がある')
  await clear()
  await hoverAndClick(() => screenOf(page, 'projectPoint', backEdge.mid), '裏の辺の中点')
  await waitFrames(page, 6)
  if ((await items()).some((item) => item.kind === 'edge' && item.index === backEdge.index)) {
    fail('箱に隠れた裏の辺の上を押すとその辺が選ばれる')
  }
  ok('pick: 箱に隠れた裏の辺は取れない')

  // ================================================================ 5. 反復の複製
  await clear()
  hovered = await hoverAndClick(onSketch(patterned, 10, -8.5), '反復の1つ目の複製の円周')
  if (hovered.sketchEntity?.entityId !== copies[0]) fail(`反復の複製にホバーしても光らない: ${JSON.stringify(hovered)}`)
  await waitFor(
    (entity) => {
      const selected = window.__duocad.selection?.items ?? []
      return selected.length === 1 && selected[0].entity === entity
    },
    '反復の複製をクリックしてもその複製だけが選ばれない',
    [copies[0]],
  )
  ok('pick: 反復の複製は独立した図形で、クリックでその複製だけが選べる')

  // ================================================================ 6. 矩形選択は全種類
  await clear()
  await withoutBrowserPanel(page, async () => {
    // 四角の4隅の画面の外接矩形(斜めから見るので、対角の2隅だけでは四角を囲めない)
    const corners = []
    for (const [u, v] of [[0, 0], [1, 0], [1, 1], [0, 1]]) {
      const at = await sketchPointAt(page, FREE.u + u * FREE.size, FREE.v + v * FREE.size, { sketchId: free, settle: corners.length === 0 })
      if (!at) fail('スケッチ B の隅が画面の中に無い')
      corners.push(at)
    }
    const pad = 12
    const from = { x: Math.min(...corners.map((c) => c.x)) - pad, y: Math.min(...corners.map((c) => c.y)) - pad }
    const to = { x: Math.max(...corners.map((c) => c.x)) + pad, y: Math.max(...corners.map((c) => c.y)) + pad }
    await page.mouse.move(from.x, from.y)
    await page.mouse.down()
    await page.mouse.move((from.x + to.x) / 2, (from.y + to.y) / 2, { steps: 4 })
    await page.mouse.move(to.x, to.y, { steps: 4 })
    await page.mouse.up()
  })
  await waitFor(
    (sketch, entity) => {
      const selected = window.__duocad.selection?.items ?? []
      return (
        selected.some((item) => item.sketch === sketch && item.entity === entity && item.point === null) &&
        selected.some((item) => item.sketch === sketch && item.entity === undefined)
      )
    },
    '矩形選択でスケッチ B の図形と領域の両方が入らない',
    [free, freeRect],
  )
  ok('pick: 矩形選択は図形と領域の全種類を集める')

  // ================================================================ 7. コマンド起動中は受け付けるものだけ
  await clear()
  await page.click('#fillet')
  await waitFor(() => JSON.stringify(window.__duocad.pickKinds) === '["edge"]', 'フィレット中に辺だけになっていない')
  await hoverAndClick(freeInside, 'スケッチ B の領域(フィレット中)')
  await waitFrames(page, 6)
  if ((await items()).length !== 0) fail(`フィレット中に領域が選べてしまう: ${JSON.stringify(await items())}`)
  ok('pick: フィレット中は辺だけが指せ、領域を押しても選択は変わらない')
  await page.keyboard.press('Escape')
  await waitFor(() => window.__duocad.command === null, 'Esc でフィレットが降りない')

  // ================================================================ 8. スケッチモードは編集中のスケッチだけ
  await mcp.callTool('enter_sketch', { sketch_id: free })
  await waitFor((id) => window.__duocad.activeSketch === id, 'スケッチ B に入れない', [free])
  // スケッチに入ると視点がそのスケッチ(中心 (30, 4))へ寄るので、寄り終わってから全体へ戻して少し引く
  await waitFor(
    () => {
      const target = window.__duocad.cameraPose().target
      return Math.abs(target[0] - 30) < 1e-3 && Math.abs(target[1] - 4) < 1e-3
    },
    'スケッチ B に入っても視点が寄らない',
  )
  await frameAll(page)
  const sketchCenter = await viewportCenter(page)
  await page.mouse.move(sketchCenter.x, sketchCenter.y)
  for (let step = 0; step < 3; step += 1) {
    await page.mouse.wheel({ deltaY: 200 })
    await waitFrames(page, 3)
  }
  await waitFrames(page, 20)
  await hoverAndClick(onSketch(patterned, 10, -8.5), '別のスケッチ D の円周(スケッチモード)')
  await waitFrames(page, 6)
  if ((await items()).some((item) => item.sketch === patterned)) fail('スケッチモードで別のスケッチの線が選べる')
  await hoverAndClick(onSketch(free, FREE.u + FREE.size / 2, FREE.v), 'スケッチ B の辺(スケッチモード)')
  await waitFor(
    (sketch) => (window.__duocad.selection?.items ?? []).some((item) => item.sketch === sketch && item.entity !== undefined),
    'スケッチモードで編集中のスケッチの辺が選べない',
    [free],
  )
  await mcp.callTool('leave_sketch')
  await waitFor(() => window.__duocad.mode === 'idle', 'スケッチ B から抜けられない')
  await waitFrames(page, 4)
  if (!(await items()).some((item) => item.sketch === free && item.entity === freeRect)) {
    fail('スケッチを抜けたら、そのスケッチの図形の選択が消えた')
  }
  ok('pick: スケッチモードでは編集中のスケッチだけを指し、抜けても図形の選択は残る')

  // ================================================================ 9. 通常モードの領域押し出し
  await clear()
  await hoverAndClick(freeInside, 'スケッチ B の領域(押し出し)')
  await waitFor(
    (sketch) => (window.__duocad.selection?.items ?? []).some((item) => item.sketch === sketch && item.entity === undefined),
    '押し出す領域が選べない',
    [free],
  )
  const before = (await mcp.callTool('describe_document', {})).bodies.length
  await page.click('#extrude')
  await page.waitForSelector('#extrude-mode-new', { timeout })
  await typeInto(page, '#distance', '5')
  await page.click('#extrude-mode-new')
  await page.keyboard.press('Enter')
  await waitFor(() => window.__duocad.lastApplied?.verb === 'extrude', '通常モードで領域を押し出せない')
  const after = (await mcp.callTool('describe_document', {})).bodies
  const volume = FREE.size * FREE.size * 5
  if (after.length !== before + 1 || !after.some((body) => Math.abs(body.volume - volume) < 1e-6)) {
    fail(`通常モードの領域押し出しで ${FREE.size}×${FREE.size}×5 の新規ボディができない: ${JSON.stringify(after.map((body) => body.volume))}`)
  }
  ok('pick: 通常モードでも領域を押し出せる')

  await assertNoAppErrors(page, consoleErrors)
})
