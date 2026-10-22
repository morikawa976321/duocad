/**
 * 実機での確認 — **スケッチの投影**(issue #184。`project_to_sketch` / `project_outline`)。
 *
 *   web> node scripts/smoke-project-sketch.mjs [--shots <dir>]
 *
 * 題材(MCP で作る): 40×30×10 の箱に半径 5 の貫通穴(中心 (20, 15))、その横に半径 5・長さ 40 の横倒しの円柱(軸が X、y = 45)。
 * z = 20 のオフセット平面のスケッチ S を編集中にする。
 *
 *   1. 「投影」の起動中はスケッチモードでも箱の上面を指せ、幽霊が出て(プレビューにも編集中のスケッチが届く)、Enter で外周4本と
 *      穴の円が足される
 *   2. Ctrl+Z で写した図形だけが消える
 *   3. 「外形を投影」で箱と円柱を指すと、箱の外形(穴の円を含む)と円柱の長方形が足される
 *   4. 垂直な辺だけを写そうとすると理由付きで断る(辺は上から見ると点なので、AI と同じく選択を MCP で置く)
 *   5. 投影を終えるとスケッチの中身を指す状態に戻る
 *
 * `--shots` を渡すと各段の画面を PNG で残す(報告用。git に載せない場所へ)。
 */
import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, pressUndo, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedCircle, addExtrudedRect } from './mcp-client.mjs'
import { clickFace, planarFace } from './pick.mjs'

const HEIGHT = 20
const REFUSAL = '写すと点に潰れて図形ができない(スケッチの平面に垂直な辺だけを選んでいる)'

runSmoke('smoke-project-sketch', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout, options }) => {
  const { bodyId: box } = await addExtrudedRect(mcp, { corner: [0, 0], width: 40, height: 30, distance: 10 })
  await addExtrudedCircle(mcp, { center: [20, 15], radius: 5, distance: 10, mode: 'cut' })
  const { bodyId: rod } = await addExtrudedCircle(mcp, { center: [45, 5], radius: 5, distance: 40, basePlane: 'YZ' })
  const sketch = (await mcp.callTool('sketch_create_offset', { distance: HEIGHT, base_plane: 'XY' })).sketch_id

  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => window.__duocad?.mode === 'sketch' && (window.__duocad?.parts ?? []).length >= 2,
  })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  const shotsDir = options.shots ?? null
  if (shotsDir !== null) mkdirSync(shotsDir, { recursive: true })
  const shot = async (name) => {
    if (shotsDir === null) return
    const file = path.join(shotsDir, `${name}.png`)
    await withoutBrowserPanel(page, async () => {
      await waitFrames(page, 8)
      await page.screenshot({ path: file })
    })
    console.log(`  shot: ${file}`)
  }
  const entityKinds = () =>
    page.evaluate(() => (window.__duocad.sketch?.entities ?? []).map((entity) => entity.kind).sort())
  const selected = () => page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.selection?.items ?? [])))

  await frameAll(page)
  await waitFrames(page, 10)
  await shot('184-0-before')

  // ================================================================ 1. 上面を投影
  await page.click('#sketch-project')
  await waitFor(() => window.__duocad.pickKinds.includes('face'), '投影の起動中に面を指せない')
  await waitFrames(page, 4) // ソリッドを指す切り替え(`setSolidPickInSketch`)は種類の後の effect で届く
  const top = await planarFace(page, box, [0, 0, 1], { at: { axis: 2, value: 10 } })
  await withoutBrowserPanel(page, () => clickFace(page, box, top))
  await waitFor(
    (id, index) => (window.__duocad.selection?.items ?? []).some((item) => item.body === id && item.kind === 'face' && item.index === index),
    '投影の起動中に箱の上面を押しても面が選ばれない(スケッチモードでソリッドを指せていない)',
    [box, top],
  )
  await waitFor(() => window.__duocad.previewing === true, '投影の幽霊が出ない(プレビューに sketch_id が届いていない)')
  await shot('184-1-preview')
  await page.keyboard.press('Enter')
  await waitFor(() => (window.__duocad.sketch?.entities ?? []).length === 5, '投影の確定で図形が5つ(外周4本と穴の円)にならない')
  const projected = await entityKinds()
  if (JSON.stringify(projected) !== JSON.stringify(['circle', 'line', 'line', 'line', 'line'])) {
    fail(`上面の投影が外周4本と円1つでない: ${projected.join(',')}`)
  }
  await shot('184-2-after')
  ok('project: スケッチモードで箱の上面を指し、外周4本と貫通穴の円を写せる')

  // ================================================================ 2. Ctrl+Z
  await pressUndo(page)
  await waitFor(() => (window.__duocad.sketch?.entities ?? []).length === 0, 'Ctrl+Z で写した図形が消えない')
  ok('project: Ctrl+Z で写した図形だけが消える')

  // ================================================================ 2b. 投影の起動中の矩形選択はソリッドを集める
  await page.click('#sketch-project')
  await waitFor(() => window.__duocad.pickKinds.includes('face'), '投影の起動中に面を指せない(2回目)')
  await waitFrames(page, 4)
  await withoutBrowserPanel(page, async () => {
    const corners = await page.evaluate(() => {
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      const points = []
      for (const x of [0, 40]) for (const y of [0, 30]) for (const z of [0, 10]) {
        const at = window.__duocad.projectPoint([x, y, z])
        if (at) points.push({ x: at.x + rect.left, y: at.y + rect.top })
      }
      return points
    })
    const xs = corners.map((point) => point.x)
    const ys = corners.map((point) => point.y)
    await page.mouse.move(Math.min(...xs) - 20, Math.min(...ys) - 20)
    await page.mouse.down()
    await page.mouse.move(Math.max(...xs) + 20, Math.max(...ys) + 20, { steps: 8 })
    await page.mouse.up()
    // パネルを畳んだまま待つ: 辺の届いていないボディがあると、届いてから同じ矩形で集め直し、そのとき視点の寸法が
    // 引いたときと違えば捨てる(`App.svelte` の `pendingBox`)
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'face' || item.kind === 'edge'),
      '投影の起動中に矩形で囲んでもソリッドの面・辺が選ばれない',
    )
  })
  const boxed = await selected()
  if (boxed.some((item) => item.sketch !== undefined)) fail(`投影の起動中の矩形選択にスケッチのものが混ざる: ${JSON.stringify(boxed)}`)
  ok(`project: 投影の起動中の矩形選択はソリッドの面・辺を集める(${boxed.length} 件)`)
  await page.keyboard.press('Escape')
  await waitFor(() => window.__duocad.command === null || window.__duocad.command?.verb !== 'project_to_sketch', '投影を中断できない')

  // ================================================================ 3. 外形を投影
  await page.click('#sketch-project-outline')
  await waitFor(() => window.__duocad.pickKinds.includes('face'), '外形の投影の起動中に面を指せない')
  await waitFrames(page, 4)
  await withoutBrowserPanel(page, () => clickFace(page, box, top))
  const rodTop = await planarFace(page, rod, [1, 0, 0])
  await withoutBrowserPanel(page, () => clickFace(page, rod, rodTop))
  await waitFor(
    (a, b) => {
      const items = window.__duocad.selection?.items ?? []
      return [a, b].every((id) => items.some((item) => item.body === id && item.kind === 'body'))
    },
    '外形の投影で面を押してもボディが選ばれない',
    [box, rod],
  )
  await waitFor(() => window.__duocad.previewing === true, '外形の投影の幽霊が出ない')
  await shot('184-3-outline-preview')
  await page.keyboard.press('Enter')
  await waitFor(() => (window.__duocad.sketch?.entities ?? []).length === 9, '外形の確定で図形が9つ(箱4本・穴の円・円柱4本)にならない')
  await shot('184-4-outline-after')
  ok(`project: 外形の投影で箱(穴を含む)と横倒しの円柱の長方形を写せる: ${(await entityKinds()).join(',')}`)

  // ================================================================ 4. 垂直な辺だけは断る
  await page.click('#sketch-project')
  await waitFor(() => window.__duocad.pickKinds.includes('edge'), '投影の起動中に辺を指せない')
  await mcp.callTool('set_selection', { predicate: { kind: 'edge', body: box, where: [{ op: 'parallel_to', axis: 'Z' }] } })
  await waitFor(() => (window.__duocad.selection?.items ?? []).length >= 4, '垂直な辺が選択に入らない')
  await page.keyboard.press('Enter')
  await waitFor((text) => (window.__duocad.errors ?? []).some((line) => line.includes(text)), '垂直な辺だけの投影が断られない', [REFUSAL])
  const refusal = await page.evaluate((text) => (window.__duocad.errors ?? []).find((line) => line.includes(text)), REFUSAL)
  if ((await page.evaluate(() => (window.__duocad.sketch?.entities ?? []).length)) !== 9) fail('断ったのに図形の数が変わった')
  await shot('184-5-refused')
  ok('project: 垂直な辺だけを写そうとすると理由付きで断り、何も足さない')

  // ================================================================ 5. 終えるとスケッチの中身に戻る
  await page.keyboard.press('Escape')
  await waitFor(() => !window.__duocad.pickKinds.includes('edge') || window.__duocad.command === null, '投影を終えても起動中のまま')
  if ((await selected()).some((item) => item.kind === 'edge')) {
    // 起動中に置いた選択は終わると外れる(`onCommandEnd`)
    await waitFor(() => !(window.__duocad.selection?.items ?? []).some((item) => item.kind === 'edge'), '投影を終えても辺が選択に残る')
  }
  ok('project: Esc で投影を終える')

  await assertNoAppErrors(page, consoleErrors, { allow: [refusal] })
})
