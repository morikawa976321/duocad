/**
 * 実機での確認 — **「面の削除」と「オフセット」が、画面から「起動 → 面を指す → プレビュー → 確定 → Ctrl+Z」と通る**。
 *
 *   web> node scripts/smoke-face-edit.mjs
 *
 *   1. 「面の削除」を起動して上面4辺の丸めの1枚を指すと、つながった丸め4枚とも選択に入る(確定の前に消える範囲が見える)
 *   2. プレビューが出て、文書はまだ丸めのまま
 *   3. Enter で確定すると元の直方体(24000 mm³)に戻り、Ctrl+Z で丸めに戻る
 *   4. 上面を選んで「オフセット」を起動し、距離 2 で確定すると太り、Ctrl+Z で戻る
 *
 * `--shots <dir>` を渡すと各段の画面を PNG で残す(報告用。git に載せない場所へ)。
 * 題材は MCP で作る(40×30×20 の直方体の上面4辺を半径 4 で丸める)。
 */
import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, pressUndo, typeInto, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { clickFace, faceAt } from './pick.mjs'

const BOX = 40 * 30 * 20

runSmoke('smoke-face-edit', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout, options }) => {
  const sketch = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  await mcp.callTool('add_rectangle', { sketch_id: sketch, corner: [0, 0], width: 40, height: 30 })
  await mcp.callTool('set_selection', { refs: [{ sketch, index: 1 }] })
  await mcp.callTool('extrude', { distance: 20, mode: 'join' })
  await mcp.callTool('leave_sketch', {})
  await mcp.callTool('set_sketch_visible', { sketch_id: sketch, visible: false })
  const UP = { kind: 'face', where: [{ op: 'normal_dot_gt', axis: 'Z', value: 0.99 }] }
  const indicesOf = async () => (await mcp.callTool('get_selection', {})).selection.items.map((item) => item.index).sort((a, b) => a - b)
  await mcp.callTool('set_selection', { predicate: UP })
  const [top] = await indicesOf()
  await mcp.callTool('set_selection', { predicate: { kind: 'edge', where: [{ op: 'on_boundary_of_face', face: top }] } })
  await mcp.callTool('fillet', { radius: 4 })
  await mcp.callTool('set_selection', { predicate: { kind: 'face', where: [{ op: 'kind_is', value: 'cylinder_partial' }] } })
  const blends = await indicesOf()
  if (blends.length !== 4) fail(`丸めの面が4枚でない: ${JSON.stringify(blends)}`)
  await mcp.callTool('set_selection', { predicate: UP })
  const [roundedTop] = await indicesOf()
  await mcp.callTool('clear_selection', {})
  const bodyId = (await mcp.callTool('describe_document', {})).bodies[0].body_id
  const volume = async () => (await mcp.callTool('describe_document', {})).bodies[0].volume
  const rounded = await volume()
  /** undo は画面から送るので、文書に届くまで少し待つ(届かなければ最後に読んだ値を返す)。 */
  const volumeBecomes = async (expected) => {
    for (let i = 0; i < 100; i += 1) {
      const value = await volume()
      if (Math.abs(value - expected) < 1e-6) return value
      await new Promise((resolve) => setTimeout(resolve, 50))
    }
    return volume()
  }

  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.parts ?? []).length >= 1,
  })
  await frameAll(page).catch(() => {})
  await waitFrames(page, 8)
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  const selectedFaces = () =>
    page.evaluate(() =>
      (window.__duocad.selection?.items ?? []).filter((item) => item.kind === 'face').map((item) => item.index).sort((a, b) => a - b),
    )
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

  // ================================================================ 1. 丸めの1クリックで連なり全体
  await page.click('#delete-faces')
  await waitFor(() => window.__duocad.command?.verb === 'delete_faces', '「面の削除」を起動できない')
  const visibleBlend = await withoutBrowserPanel(page, async () => {
    for (const index of blends) if (await faceAt(page, bodyId, index)) return index
    return fail('画面に見えている丸めの面が無い')
  })
  await clickFace(page, bodyId, visibleBlend, { hidePanel: true })
  await waitFor((n) => (window.__duocad.selection?.items ?? []).length === n, '丸めの1クリックで4枚とも選ばれない', [blends.length])
  const chosen = await selectedFaces()
  if (JSON.stringify(chosen) !== JSON.stringify(blends)) fail(`選ばれた面が丸めの連なりでない: ${JSON.stringify(chosen)} / ${JSON.stringify(blends)}`)
  ok(`delete_faces: 丸めの面 ${visibleBlend} の1クリックで、つながった丸め ${JSON.stringify(blends)} が選ばれた`)
  await shot('face-edit-0-chain')

  // ================================================================ 2. プレビュー
  await waitFor(() => window.__duocad.previewing === true, '面を選んでもプレビューが出ない')
  if (Math.abs((await volume()) - rounded) > 1e-6) fail('プレビューの間に文書の形が変わった')
  ok('delete_faces: プレビューが出て、文書は丸めのまま')
  await shot('face-edit-1-preview')

  // ================================================================ 3. 確定と Ctrl+Z
  await page.keyboard.press('Enter')
  await waitFor(() => window.__duocad.lastApplied?.verb === 'delete_faces', '確定しても delete_faces が適用されない')
  const deleted = await volume()
  if (Math.abs(deleted - BOX) > 1e-3) fail(`元の直方体に戻らない: ${deleted}`)
  ok(`delete_faces: 確定で元の直方体(${Math.round(deleted)} mm³)に戻った`)
  await shot('face-edit-2-deleted')
  await waitFrames(page, 4)
  await pressUndo(page)
  const undone = await volumeBecomes(rounded)
  if (Math.abs(undone - rounded) > 1e-6) fail(`Ctrl+Z で丸めに戻らない: ${undone} / ${rounded}`)
  ok('delete_faces: Ctrl+Z で丸めの形に戻った')

  // ================================================================ 4. オフセット
  await page.keyboard.press('Escape')
  await waitFor(() => window.__duocad.command === null || window.__duocad.command === undefined, '起動中のコマンドを閉じられない')
  await waitFrames(page, 8) // Ctrl+Z の形が描き直されるまで待つ(待たずに押すと「面が画面の中に無い」で落ちた)
  await clickFace(page, bodyId, roundedTop, { hidePanel: true })
  await waitFor(() => (window.__duocad.selection?.items ?? []).length === 1, '上面を選べない')
  await page.click('#offset-faces')
  await waitFor(() => window.__duocad.command?.verb === 'offset_faces', '「オフセット」を起動できない')
  if ((await selectedFaces()).length !== 1) fail('オフセットで上面が丸めの連なりへ広がった(広げるのは面の削除だけ)')
  await typeInto(page, '#offset-faces-distance', '2')
  await waitFor(() => window.__duocad.previewing === true, '距離を打ってもプレビューが出ない')
  await shot('face-edit-3-offset-preview')
  await page.keyboard.press('Enter')
  await waitFor(() => window.__duocad.lastApplied?.verb === 'offset_faces', '確定しても offset_faces が適用されない')
  const thicker = await volume()
  if (!(thicker > rounded + 1000)) fail(`上面を 2 上げても太らない: ${thicker} / ${rounded}`)
  ok(`offset_faces: 上面を 2 上げると ${Math.round(rounded)} → ${Math.round(thicker)} mm³`)
  await shot('face-edit-4-offset')
  await waitFrames(page, 4)
  await pressUndo(page)
  if (Math.abs((await volumeBecomes(rounded)) - rounded) > 1e-6) fail('Ctrl+Z でオフセットの前に戻らない')
  ok('offset_faces: Ctrl+Z で戻った')

  await assertNoAppErrors(page, consoleErrors)
})
