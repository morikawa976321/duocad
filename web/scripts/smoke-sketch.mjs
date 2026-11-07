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
 *   3. 線分の始点と同じ場所をもう一度押しても点にならず、続けて終点を打てば線分ができる
 *   4. スケッチ A で選んだ図形は、B へ直接移った時点で選択から外れる(外れないと、B で動かした
 *      つもりで A の図形が動く)
 *   5. 長方形の幅と高さを両方、負の値で打つと、マウスが反対側にあっても負の側へ伸びる
 *   6. 打ちかけの点は、空の数値欄にフォーカスがあっても Backspace と Ctrl+Z で1点ずつ戻り、文書は変わらない
 *   7. 引数の行が開いても、ツールバーとビューポートの高さは変わらない
 *   8. 平面を真横から見ていても、既存の点は始点・終点になり、何も無い所と線の途中は断られる
 *
 * 落ちる先の規則そのもの(円の半径ちょうど・傾いた辺の上)は `tests/eval_sketch_snap.mjs` が持つ。
 * ここが見るのは、本物のクリックと幽霊がその規則を通っていること。
 */
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, openApp, sleep, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { clickEntityHandle, sketchPointAt } from './pick.mjs'

/**
 * 視点を、XY 平面を真横から見る向き(視線と平面の角が `MIN_PLANE_SIGHT_DEG` 未満)へ、右ドラッグの回転で倒す。
 * 回転量は画面の高さに比例する(OrbitControls)。減衰で止まるまで待ってから姿勢を読み、残りを打ち直す。
 */
async function lookAlongPlane(page) {
  const pose = () => page.evaluate(() => window.__duocad.cameraPose())
  const elevationDeg = async () => {
    const { position, target } = await pose()
    const d = position.map((value, i) => value - target[i])
    return (Math.asin(Math.abs(d[2]) / Math.hypot(...d)) * 180) / Math.PI
  }
  const rect = await page.evaluate(() => {
    const r = document.querySelector('.viewport').getBoundingClientRect()
    return { x: r.left, y: r.top, w: r.width, h: r.height }
  })
  let sign = -1
  for (let i = 0; i < 8; i += 1) {
    const before = await elevationDeg()
    if (before < 0.5) return
    const dy = Math.round(((before * Math.PI) / 180 / (2 * Math.PI)) * rect.h) * sign
    if (dy !== 0) {
      const x = rect.x + rect.w / 2
      const y = rect.y + rect.h / 2
      await page.mouse.move(x, y)
      await page.mouse.down({ button: 'right' })
      await page.mouse.move(x, y + dy, { steps: 6 })
      await page.mouse.up({ button: 'right' })
    }
    let last = -1
    for (let wait = 0; wait < 40; wait += 1) {
      await sleep(100)
      const now = await elevationDeg()
      if (Math.abs(now - last) < 1e-4) break
      last = now
    }
    if ((await elevationDeg()) > before) sign = -sign
  }
  fail(`視点を平面の真横へ倒せない(平面からの高さ角 ${await elevationDeg()} 度)`)
}

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

  // ================================================================ 3. 始点と同じ場所の2クリック目は点にならない
  // 点を入れてしまうと、待っている点が無くなって次のクリックが選択に流れ、図形も作れない。
  {
    const before = (await entities()).length
    await page.click('#sketch-line')
    await withoutBrowserPanel(page, async () => {
      const start = await sketchPointAt(page, 45, 15, { settle: true })
      if (!start) fail('線分の始点が画面上で指せない')
      await page.mouse.click(start.x, start.y)
      const again = await sketchPointAt(page, 45, 15, { settle: true })
      await page.mouse.click(again.x, again.y)
      await waitFrames(page, 4)
      const end = await sketchPointAt(page, 45, 22, { settle: true })
      if (!end) fail('線分の終点が画面上で指せない')
      await page.mouse.click(end.x, end.y)
    })
    await waitFor((count) => window.__duocad.sketch?.entities.length === count, '同じ点を2回押した後、線分が置けない', [before + 1])
    const redone = await lastLine()
    if (JSON.stringify(redone) !== JSON.stringify({ x0: 45, y0: 15, x1: 45, y1: 22 })) {
      fail(`同じ点を2回押した後の線分が始点から引かれていない: ${JSON.stringify(redone)}`)
    }
    ok('[3] 始点と同じ場所の2クリック目は点にならず、続けて終点を打てば線分ができる')
  }

  // ================================================================ 4. スケッチを直接切り替えると選択が残らない
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
  ok('[4] スケッチ A で選んだ図形は、B へ直接移った時点で選択から外れる')

  // ================================================================ 5. 幅と高さを両方打つと符号が向きを決める
  // マウスは右上へ置いたまま「-10」「-6」を打つ。向きがマウス側のままなら右上へ伸びる。
  await enter(sketchA)
  {
    const before = (await entities()).length
    await page.click('#sketch-rectangle')
    await withoutBrowserPanel(page, async () => {
      const corner = await sketchPointAt(page, 45, 40, { settle: true })
      if (!corner) fail('長方形の角が画面上で指せない')
      await page.mouse.click(corner.x, corner.y)
      const away = await sketchPointAt(page, 52, 46, { settle: true })
      if (!away) fail('マウスを置く場所が画面上で指せない')
      await page.mouse.move(away.x, away.y)
      await waitFrames(page, 4)
    })
    await page.keyboard.type('-10')
    await page.keyboard.press('Tab')
    await page.keyboard.type('-6')
    await page.keyboard.press('Enter')
    await waitFor((count) => window.__duocad.sketch?.entities.length === count, '負の寸法の長方形が置けない', [before + 1])
    const rect = (await entities()).filter((item) => item.kind === 'rectangle').at(-1).params
    if (rect.x !== 35 || rect.y !== 34 || rect.width !== 10 || rect.height !== 6) {
      fail(`負の幅と高さが逆向きに伸びていない: ${JSON.stringify(rect)}`)
    }
    ok('[5] 幅と高さを両方負で打つと、マウスが反対側にあっても負の側へ伸びる')
  }

  // ================================================================ 6. 打ちかけの点は Backspace と Ctrl+Z で1点ずつ戻る
  // 打ち間違えた点を直す手が Esc で全部捨てる以外に無いと、やり直しの手数が大きい。数値欄にフォーカスがあっても、欄が空なら
  // キーは欄でなく画面のものになる(コマンドは開いたまま。Ctrl+Z は文書を戻さない)。
  {
    const sharedArgCount = () => page.evaluate(() => Object.keys(window.__duocad.state.command?.args ?? {}).length)
    const log = () => page.evaluate(() => window.__duocad.state.log.length)
    const before = { entities: (await entities()).length, log: await log() }
    await page.click('#sketch-rectangle')
    for (const key of ['Backspace', 'Control+KeyZ']) {
      await withoutBrowserPanel(page, async () => {
        const corner = await sketchPointAt(page, 45, 40, { settle: true })
        if (!corner) fail('長方形の角が画面上で指せない')
        await page.mouse.click(corner.x, corner.y)
      })
      await waitFor(() => Object.keys(window.__duocad.state.command?.args ?? {}).length > 0, '角を打っても共有の引数に載らない')
      if (key === 'Backspace') await page.keyboard.press('Backspace')
      else {
        await page.keyboard.down('Control')
        await page.keyboard.press('KeyZ')
        await page.keyboard.up('Control')
      }
      await waitFor(
        () => Object.keys(window.__duocad.state.command?.args ?? {}).length === 0 && window.__duocad.state.command?.verb === 'add_rectangle',
        `${key} で打った点が戻らない(長方形のコマンドが開いたままのはず)`,
      )
    }
    await sleep(1500) // 文書の取り消しが誤って送られていれば、往復のあとで log が減る
    if ((await log()) !== before.log || (await entities()).length !== before.entities || (await sharedArgCount()) !== 0) {
      fail('点を戻すキーが文書を変えた')
    }
    await page.keyboard.press('Escape')
    ok('[6] 打ちかけの点は、空の数値欄にフォーカスがあっても Backspace と Ctrl+Z で1点戻り、文書は変わらない')
  }

  // ================================================================ 7. 引数の行が開いても、ツールバーとビューポートの寸法は動かない
  // 待ちの1行(#command-prompt)は起動中だけ引数の行(overlay)へ移る。その分の場所を空けておかないと、コマンドを押すたびに
  // ツールバーが縮んでビューポートが伸び、画面の中身が動く。
  {
    const sizes = () =>
      page.evaluate(() => ({
        host: document.querySelector('.command-host').getBoundingClientRect().height,
        viewport: document.querySelector('.viewport').getBoundingClientRect().height,
      }))
    await page.keyboard.press('Escape')
    await waitFrames(page, 4)
    const idle = await sizes()
    await page.click('#sketch-line')
    await waitFor(() => document.querySelector('#command-abort') !== null, 'コマンドが起動しない')
    await waitFrames(page, 4)
    const active = await sizes()
    if (Math.abs(active.host - idle.host) > 0.5 || Math.abs(active.viewport - idle.viewport) > 0.5) {
      fail(`引数の行が開いて寸法が動いた: ${JSON.stringify({ idle, active })}`)
    }
    await page.keyboard.press('Escape')
    ok('[7] 引数の行が開いても、ツールバーとビューポートの高さは変わらない')
  }

  // ================================================================ 8. 平面を真横から見ていても、既存の点は点として置ける
  // 視線が平面に 2 度未満の浅い角度だと点を置くクリックは断られる(`planeSight.ts`)が、座標が視線に依らず決まる当たり
  // (図形の端点・円の中心)まで断ると、線を既存の端点から引けず、代わりにその図形が選択される。
  {
    const sketchC = (await mcp.callTool('sketch_create', { base_plane: 'XY' })).sketch_id
    await mcp.callTool('add_circle', { sketch_id: sketchC, center: [10.5, 7.3], diameter: 6 })
    await mcp.callTool('add_line', { sketch_id: sketchC, p0: [-40, -10], p1: [-10, -10] })
    await waitFor((id) => window.__duocad.activeSketch === id && window.__duocad.sketch?.entities.length === 2, '作ったスケッチへ入れない', [sketchC])
    await waitFrames(page, 4)
    await lookAlongPlane(page)
    const args = () => page.evaluate(() => Object.keys(window.__duocad.state.command?.args ?? {}).length)
    const selected = () => page.evaluate(() => window.__duocad.selection?.count ?? 0)
    const refusals = () =>
      page.evaluate(() => [...document.querySelectorAll('#toasts .toast-info-text')].filter((el) => el.textContent.includes('浅い角度')).length)
    const press = (u, v, what) =>
      withoutBrowserPanel(page, async () => {
        const at = await sketchPointAt(page, u, v, { settle: true })
        if (!at) fail(`${what}が画面上で指せない`)
        await page.mouse.click(at.x, at.y)
        await waitFrames(page, 4)
      })
    await page.click('#sketch-line')
    await withoutBrowserPanel(page, async () => {
      const at = await sketchPointAt(page, 10.5, 7.3, { settle: true })
      if (!at) fail('円の中心が画面上で指せない')
      await page.mouse.move(at.x, at.y)
      await waitFrames(page, 4)
    })
    const ghost = await page.evaluate(() => (window.__duocad.hoverPoint ? [...window.__duocad.hoverPoint] : null))
    if (!ghost || ghost[0] !== 10.5 || ghost[1] !== 7.3) fail(`浅い角度でも既存の点の上の幽霊は出るはず: ${JSON.stringify(ghost)}`)
    await press(10.5, 7.3, '円の中心')
    await waitFor(() => Object.keys(window.__duocad.state.command?.args ?? {}).length > 0, '浅い角度で既存の点を押しても始点にならない')
    if ((await selected()) !== 0 || (await refusals()) !== 0) fail('既存の点を押したのに、点にならず選択に流れた(または断られた)')
    // 何も無い所と、線の途中は断る(視線で決まる座標なので)。選択にも流さない。
    await press(30, 0, '何も無い所')
    await waitFor(
      () => [...document.querySelectorAll('#toasts .toast-info-text')].some((el) => el.textContent.includes('浅い角度')),
      '何も無い所を押しても知らせが出ない',
    )
    await press(-35, -10, '線の途中')
    if ((await args()) !== 1 || (await selected()) !== 0) fail('断られるはずの所で点が入った、または選択に流れた')
    const before = (await entities()).length
    await press(-40, -10, '線の端点')
    await waitFor((count) => window.__duocad.sketch?.entities.length === count, '既存の端点を終点にして線分が置けない', [before + 1])
    const made = await lastLine()
    const corner = (await entities()).find((item) => item.kind === 'line' && item.params.x0 === -40).params
    // 終点はサーバが 3D を経由して書き戻すので、浮動小数の端数(実測: 4e-15)は許す。
    const near = (a, b) => Math.abs(a - b) < 1e-9
    if (made.x0 !== 10.5 || made.y0 !== 7.3 || !near(made.x1, corner.x0) || !near(made.y1, corner.y0)) fail(`端点に一致した線分になっていない: ${JSON.stringify(made)}`)
    ok('[8] 平面を真横から見ていても、既存の点は始点・終点になり、何も無い所と線の途中は断られる')
  }

  await assertNoAppErrors(page, consoleErrors)
})
