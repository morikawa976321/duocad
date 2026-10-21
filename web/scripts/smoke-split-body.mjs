/**
 * 実機での確認 — **ボディ分割が、画面から「起動 → 平面を指す → プレビュー → 確定 → Ctrl+Z」と通る**。
 *
 *   web> node scripts/smoke-split-body.mjs
 *
 *   1. ボディの面を選んで「ボディ分割」を起動し、「スケッチを選ぶ」でスケッチの線を1本指すと、図形の参照が入る
 *   2. 残す側(両側)まで揃うとプレビューが出て、文書はまだ1ボディのまま
 *   3. Enter で確定すると、体積が半分ずつの2ボディになる
 *   4. Ctrl+Z で元の1ボディに戻る
 *   5. ボディの底面に接するだけの XY 平面では、理由付きで断って何も変えない
 *
 * `--shots <dir>` を渡すと各段の画面を PNG で残す(報告用。git に載せない場所へ)。確定後は片側を隠して切り口も撮る。
 *
 * 題材は MCP で作る(40×20×20 の直方体と、z=10 のオフセット平面に線分を1本引いたスケッチ)。
 * 線分は直方体の外(x=45..55)に置く: 内側だと面に隠れて指せない。点は指せない(`sketchEntity` のピックは線を待つ)。
 */
import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, pressUndo, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { clickFace, clickSketchPoint, faceAt } from './pick.mjs'

const REFUSAL = '平面が面・辺・頂点で接しているだけ'

runSmoke('smoke-split-body', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout, options }) => {
  const base = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  await mcp.callTool('add_rectangle', { sketch_id: base, corner: [0, 0], width: 40, height: 20 })
  await mcp.callTool('set_selection', { refs: [{ sketch: base, index: 1 }] })
  await mcp.callTool('extrude', { distance: 20, mode: 'join' })
  const plane = (await mcp.callTool('sketch_create_offset', { base_plane: 'XY', distance: 10 })).sketch_id
  await mcp.callTool('add_line', { sketch_id: plane, p0: [45, 5], p1: [55, 15] })
  await mcp.callTool('clear_selection', {})
  await mcp.callTool('leave_sketch', {}) // スケッチを描いた後は画面がそのスケッチの編集に入っていて、面を指せない
  // 面を押すあいだは両方のスケッチを隠す(平面の板が面の画素を覆う)。平面のスケッチは図形を指す前に出し直す。
  for (const id of [base, plane]) await mcp.callTool('set_sketch_visible', { sketch_id: id, visible: false })
  const bodyId = (await mcp.callTool('describe_document', {})).bodies[0].body_id

  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.parts ?? []).length >= 1,
  })
  await frameAll(page).catch(() => {})
  await waitFrames(page, 8)
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 50 })
  const bodies = async () => (await mcp.callTool('describe_document', {})).bodies
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

  // ================================================================ 1. 起動して平面を指す
  /** 画面に見えている面の添字(どの面でも、ボディを選べればよい)。 */
  async function visibleFace() {
    return withoutBrowserPanel(page, async () => {
      for (let index = 1; index <= 6; index += 1) if (await faceAt(page, bodyId, index)) return index
      return fail('画面に見えている面が無い')
    })
  }
  await clickFace(page, bodyId, await visibleFace(), { hidePanel: true })
  await waitFor(() => (window.__duocad.selection?.items ?? []).length >= 1, '面を選べない')
  await page.click('#split-body')
  await waitFor(() => window.__duocad.command?.verb === 'split_body', 'ボディ分割を起動できない')
  await mcp.callTool('set_sketch_visible', { sketch_id: plane, visible: true })
  await page.click('#split-body-sketch')
  await waitFor(() => window.__duocad.wantedPick === 'sketchEntity', '「スケッチを選ぶ」を押しても図形のピックが開かない')
  await clickSketchPoint(page, 50, 10, { sketchId: plane })
  await waitFor(
    () => (document.querySelector('#split-body-sketch-pick-value')?.textContent ?? '').trim() === '図形1つ',
    'スケッチの線を指しても入らない',
  )
  ok('split_body: スケッチの線を1本指すと、切る平面として入る')
  await shot('193-0-plane')

  // ================================================================ 2. プレビュー
  await page.click('#split-body-keep-both')
  await waitFor(() => window.__duocad.previewing === true, '残す側まで揃ってもプレビューが出ない')
  if ((await bodies()).length !== 1) fail('プレビューの間に文書のボディが増えた')
  ok('split_body: プレビューが出て、文書は1ボディのまま')
  await shot('193-1-preview')

  // ================================================================ 3. 確定
  await page.keyboard.press('Enter')
  await waitFor(() => window.__duocad.lastApplied?.verb === 'split_body', '確定しても split_body が適用されない')
  const after = await bodies()
  const volumes = after.map((body) => Math.round(body.volume)).sort()
  if (after.length !== 2 || volumes[0] !== 8000 || volumes[1] !== 8000) {
    fail(`半分ずつの2ボディにならない: ${JSON.stringify(after)}`)
  }
  ok('split_body: 体積が半分ずつ(8000 mm³)の2ボディになった')
  await shot('193-2-after')
  if (shotsDir !== null) {
    // 断面を見る使い方: 新しくできた側(元の番号でない方)を隠すと切り口が見える。可視性はログに載らない
    const other = after.find((body) => body.body_id !== bodyId).body_id
    await mcp.callTool('set_visible', { body_id: other, visible: false })
    await waitFor(
      (id) => (window.__duocad.state?.bodies ?? []).some((body) => body.body_id === id && body.visible === false),
      '片側を隠せない',
      [other],
    )
    await shot('193-3-section')
    await mcp.callTool('set_visible', { body_id: other, visible: true })
    await waitFor(
      (id) => (window.__duocad.state?.bodies ?? []).some((body) => body.body_id === id && body.visible !== false),
      '隠した側を出し直せない',
      [other],
    )
  }

  // ================================================================ 4. Ctrl+Z
  await waitFrames(page, 4)
  await pressUndo(page)
  await waitFor(() => (window.__duocad.parts ?? []).length === 1, 'Ctrl+Z で1ボディに戻らない')
  const restored = await bodies()
  if (restored.length !== 1 || Math.round(restored[0].volume) !== 16000) fail(`元に戻らない: ${JSON.stringify(restored)}`)
  ok('split_body: Ctrl+Z で元の1ボディ(16000 mm³)に戻った')

  // ================================================================ 5. 接するだけの平面は断る
  await clickFace(page, bodyId, await visibleFace(), { hidePanel: true })
  await waitFor(() => (window.__duocad.selection?.items ?? []).length >= 1, '面を選べない(2回目)')
  await page.click('#split-body')
  await waitFor(() => window.__duocad.command?.verb === 'split_body', 'ボディ分割を起動できない(2回目)')
  await page.click('#split-body-xy')
  await page.click('#split-body-keep-both')
  await page.keyboard.press('Enter')
  await waitFor((text) => (window.__duocad.errors ?? []).some((line) => line.includes(text)), '底面に接するだけの平面で断らない', [REFUSAL])
  if ((await bodies()).length !== 1) fail('断ったのにボディの数が変わった')
  await shot('193-4-refused')
  const refusals = await page.evaluate(
    (text) => (window.__duocad.errors ?? []).filter((line) => line.includes(text)),
    REFUSAL,
  )
  ok('split_body: 底面に接するだけの XY 平面は理由付きで断り、何も変えない')

  await assertNoAppErrors(page, consoleErrors, { allow: [...new Set(refusals)] })
})
