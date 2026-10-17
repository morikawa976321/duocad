/**
 * 実ブラウザでの検査(スケッチ編集) — **人が使っていても気づけない壊れ方**だけを見る
 * (何を smoke に書くかは `docs/SMOKE_TESTING.md` の冒頭)。題材は MCP で描くスケッチ2枚。
 *
 *   web> node scripts/smoke-sketch.mjs
 *
 *   1. 何にも当たっていない場所へ置いた点は 1mm の格子へ丸まり、幽霊も同じ座標に乗る
 *      (丸めが効かなくても見た目は変わらず、座標だけが 73.4 のような値になる)
 *   2. 既存の点(格子から外れた円の中心)の上へ数画素ずらして置いた点は、その点と**完全に一致する**
 *      (一致しなくても見た目は吸い付いて見える)
 *   3. スケッチ A で選んだ図形は、B へ直接移った時点で選択から外れる(外れないと、B で動かした
 *      つもりで A の図形が動く)
 *
 * 落ちる先の規則そのもの(円の半径ちょうど・傾いた辺の上)は `tests/eval_sketch_snap.mjs` が持つ。
 * ここが見るのは、本物のクリックと幽霊がその規則を通っていること。
 */
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, openApp, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { clickEntityHandle, sketchPointAt } from './pick.mjs'

runSmoke('smoke-sketch', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  // 長方形(x: 0-40)・円(x: 50-70)・傾いた長方形(x: 79-105)。点を置く空き地は円と傾いた長方形の
  // あいだ(x: 70-79)と、長方形と円のあいだ(x: 40-50) —— 輪郭から数mm以内はスナップに吸われる。
  const sketchA = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
  await mcp.callTool('add_rectangle', { sketch_id: sketchA, corner: [0, 0], width: 40, height: 30 })
  // 円の中心は**わざと格子から外す** —— 格子の上の点では、点へのスナップが効かなくても 1mm の丸めが
  // 同じ座標を返し、2 の検査が素通りする(実測: 角が (0,0) の長方形で試して、壊しても緑だった)。
  await mcp.callTool('add_circle', { sketch_id: sketchA, center: [60.4, 10.3], diameter: 20 })
  await mcp.callTool('add_rectangle', { sketch_id: sketchA, corner: [85, 0], width: 24, height: 10, angle: 35 })
  const sketchB = (await mcp.callTool('sketch_create', { base_plane: 'XZ' })).sketch_id

  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.sketches ?? []).length >= 2,
  })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout })
  const enter = async (sketchId) => {
    await page.click(`[data-sketch-id="${sketchId}"] .sketch-name`)
    await waitFor((id) => window.__duocad.activeSketch === id, 'スケッチへ入れない', [sketchId])
    await waitFrames(page, 4) // 入った直後の視点合わせ(`lookAtSketch`)は数フレーム遅れる
  }
  /** 今のスケッチの図形(`params` は `$state` のプロキシなのでスプレッドで写す)。 */
  const entities = () =>
    page.evaluate(() =>
      window.__duocad.sketch.entities.map((item) => ({ id: item.entity_id, kind: item.kind, params: { ...item.params } })),
    )
  const lastLine = async () => (await entities()).filter((item) => item.kind === 'line').at(-1).params
  /**
   * 線分を2点で引き、1点目を押す直前の幽霊の座標(`hoverPoint`)を返す。`nudge` は1点目を画素で
   * ずらす量(スナップが効いていれば置かれる座標は変わらない)。
   */
  async function drawLine(from, to, nudge = 0) {
    const before = (await entities()).length
    await page.click('#sketch-line')
    const ghost = await withoutBrowserPanel(page, async () => {
      const start = await sketchPointAt(page, from[0], from[1], { settle: true })
      if (!start) fail(`線分の始点 ${JSON.stringify(from)} が画面上で指せない`)
      await page.mouse.move(start.x + nudge, start.y + nudge)
      await waitFrames(page, 4)
      const hover = await page.evaluate(() => (window.__duocad.hoverPoint ? [...window.__duocad.hoverPoint] : null))
      await page.mouse.click(start.x + nudge, start.y + nudge)
      // 1点目が埋まるとツールバーの行が変わるので、2点目は射影し直す
      const end = await sketchPointAt(page, to[0], to[1], { settle: true })
      if (!end) fail(`線分の終点 ${JSON.stringify(to)} が画面上で指せない`)
      await page.mouse.click(end.x, end.y)
      return hover
    })
    await waitFor((count) => window.__duocad.sketch?.entities.length === count, '線分が置けない', [before + 1])
    return ghost
  }

  await enter(sketchA)
  const circle = (await entities()).find((item) => item.kind === 'circle')

  // ================================================================ 1. 1mm グリッドへの丸め
  // **わざと格子から外した点を狙う**(ちょうど交点を叩くと丸めの有無を区別できない)。
  const gridGhost = await drawLine([73.4, 5.35], [75.6, 27.8])
  if (!gridGhost || gridGhost[0] !== 73 || gridGhost[1] !== 5) {
    fail(`何にも当たっていない幽霊が 1mm へ丸まっていない: ${JSON.stringify(gridGhost)}`)
  }
  const onGrid = await lastLine()
  if (JSON.stringify(onGrid) !== JSON.stringify({ x0: 73, y0: 5, x1: 76, y1: 28 })) {
    fail(`何にも当たっていない場所へ置いた線分が 1mm へ丸まっていない: ${JSON.stringify(onGrid)}`)
  }
  ok('[1] 何にも当たっていない場所の点は 1mm 格子へ丸まり、幽霊も同じ座標に乗る')

  // ================================================================ 2. 点の上は完全一致
  await drawLine([circle.params.cx, circle.params.cy], [45, 15], 2)
  const onPoint = await lastLine()
  if (onPoint.x0 !== circle.params.cx || onPoint.y0 !== circle.params.cy) {
    fail(`点の上に置いたのに座標が完全一致しない: (${onPoint.x0}, ${onPoint.y0})`)
  }
  ok('[2] 既存の点の上へ2画素ずらして置いた始点が、その点と完全に一致した')

  // ================================================================ 3. スケッチを直接切り替えると選択が残らない
  // `exitSketch` を通らずに A から B へ移る道(ブラウザのスケッチ名クリック)。外さないと、反復の
  // verb には A の図形 ID が B の文脈で渡る(サーバは entity_id を素直に解決するので黙って通る)。
  await page.keyboard.press('Escape')
  await clickEntityHandle(page, circle.id)
  await waitFor(
    (id) => (window.__duocad.selection?.items ?? []).some((item) => item.entity === id),
    'A の図形を選べない',
    [circle.id],
  )
  await enter(sketchB)
  await waitFor(
    () =>
      !(window.__duocad.selection?.items ?? []).some((item) => item.entity !== undefined) &&
      (window.__duocad.selectedEntities ?? []).length === 0,
    'A の図形の選択が B へ持ち越されている',
  )
  ok('[3] スケッチ A で選んだ図形は、B へ直接移った時点で選択から外れる')

  await assertNoAppErrors(page, consoleErrors)
})
