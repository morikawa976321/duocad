/**
 * 実機での確認 — **鏡映複製の基準(スケッチの線分 / ソリッドの面)をマウスだけで指せる**。
 *
 *   node scripts/smoke-mirror.mjs
 *
 * `mirror_sketch_entities` の軸と `mirror_body` の `plane_face` は、対象の確定選択とは
 * 別チャンネル(`web/src/commands/command.ts` の `PickArgSpec`)で受け取る。pytestでは
 * 判定できないのは**その2つのチャンネルが画面上で混ざらないこと**なので、ここでは
 * 両方向を見る:
 *
 *   1. **基準に指したものが対象の選択へ入らない**(入ると、軸の線分や鏡に使った面の
 *      ボディまで鏡映される)
 *   2. **基準を待っている間も対象を選び直せる**(Shift/Ctrl+クリックはピックへ行かない)
 *   3. スケッチ: 図形を選ぶ → 鏡映複製 → 既存の線分をクリック → 複製が軸の向こう側に出る
 *   4. ソリッド: 面でボディを選ぶ → 鏡映複製 → 「面を選ぶ」→ 平らな面をクリック →
 *      その面の平面で鏡映した複製が出る(XY/XZ/YZ の3択も壊れていない)
 *
 * サーバはこのスクリプトが空の文書で起動し、**形は全部UIから作る**。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 *
 * **スケッチの図形はスケッチへ入った直後に全部描く。** `#sketch-look` が合わせる視野は
 * そのとき見えているものに依るので、後から離れた場所を押そうとすると画面の外になる
 * (実測: 板だけがある状態で `#sketch-look` した後、原点付近の (0,-20) が画面の外だった)。
 */
import { assertNoAppErrors, frameAll, openApp, sleep, typeInto, waitOrFail } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { clickFace, clickSketchPoint, createSketchOnBasePlane, sketchPointAt } from './pick.mjs'

/** 鏡映軸に使う線分(u=0 の縦線)。 */
const AXIS = { x: 0, y0: -20, y1: 20 }
/** 鏡映する円(軸から右に20mm)と、対象の選び直しに使うもう1つの円。 */
const TARGET = { cx: 20, cy: 0, r: 5 }
const OTHER = { cx: 20, cy: 15, r: 3 }
/** 押し出してボディにする長方形(鏡映する円と重ならない位置)。 */
const PLATE = { x: 40, y: -20, width: 40, height: 30, depth: 12 }

runSmoke('smoke-mirror', { timeout: 60000 }, async ({ browser, url, timeout }) => {
  {
    const { page, consoleErrors } = await openApp(browser, url, { timeout })
    await frameAll(page).catch(() => {})

    // ---------------------------------------------------------------- 道具
    const waitFor = (check, what, args = []) =>
      waitOrFail(page, check, what, { args, timeout, polling: 50 })
    const planeAt = (u, v) => sketchPointAt(page, u, v)
    /** スケッチ平面の (u,v) をクリックする(`key` を渡すとその修飾キーを押しながら)。 */
    const clickPlane = (u, v, key = null) => clickSketchPoint(page, u, v, { modifier: key })

    /** いまのスケッチ(図形と選択)。**JSONへ落としてから返す**(Svelteの`$state`は素で渡らない)。 */
    async function sketchNow() {
      return page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch) ?? null
        return JSON.parse(
          JSON.stringify({
            activeSketch: hook.activeSketch,
            entities: active ? active.entities : [],
            regions: active ? active.regions : [],
            selection: hook.selection,
          }),
        )
      })
    }

    /** 選択に入っている図形のID(重複なし)。 */
    async function selectedEntityIds() {
      return page.evaluate(() => [
        ...new Set(
          (window.__duocad.selection?.items ?? [])
            .filter((item) => item.entity !== undefined)
            .map((item) => item.entity),
        ),
      ])
    }


    /**
     * コマンドが**次に何を待っているか**をプロンプトで確かめる。
     *
     * 描く4verbは点を1つずつ食うので、**クリックが1回でも早すぎる/取りこぼされると
     * 図形は出ないのに理由が出ない**(最後の `waitFor` が「描けない」とだけ言う)。
     * 待ち受けが開いたことを1手ごとに確かめて、どのクリックが届かなかったかを名指しする。
     */
    async function awaitPrompt(text, what) {
      await waitFor(
        (needle) => (document.querySelector('#command-prompt')?.textContent ?? '').includes(needle),
        what,
        [text],
      )
    }

    async function circleExists(cx, cy, r) {
      return page.waitForFunction(
        (x, y, radius) => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return Boolean(
            active &&
              active.entities.some(
                (item) =>
                  item.kind === 'circle' &&
                  Math.abs(item.params.cx - x) < 1e-6 &&
                  Math.abs(item.params.cy - y) < 1e-6 &&
                  Math.abs(item.params.radius - radius) < 1e-6,
              ),
          )
        },
        { timeout, polling: 50 },
        cx,
        cy,
        r,
      )
    }

    // ================================================================ 1. 図形を描く
    const sketchId = await createSketchOnBasePlane(page, 'XY', { timeout })
    await page.click('#sketch-look') // 正面から見る((u,v) → 画素が安定する)
    await sleep(400)

    await page.click('#sketch-line')
    await awaitPrompt('始点', '線分の始点の待ち受けが開かない')
    await clickPlane(AXIS.x, AXIS.y0)
    await awaitPrompt('終点', '線分の始点のクリックが届いていない')
    await clickPlane(AXIS.x, AXIS.y1)
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.some((item) => item.kind === 'line'))
      },
      '軸にする線分を描けない',
    )

    for (const circle of [TARGET, OTHER]) {
      await page.click('#sketch-circle')
      await awaitPrompt('中心', '円の中心の待ち受けが開かない')
      await clickPlane(circle.cx, circle.cy)
      await awaitPrompt('円周', '円の中心のクリックが届いていない')
      await clickPlane(circle.cx + circle.r, circle.cy)
      try {
        await circleExists(circle.cx, circle.cy, circle.r)
      } catch {
        // **何が起きたかを残す** —— 「描けない」だけだと、図形が1つも増えていないのか
        // 座標がずれたのかが後から分からない(クリックの取りこぼしと吸い付きの区別)。
        const state = await page.evaluate(() => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          return JSON.parse(
            JSON.stringify({
              entities: (active?.entities ?? []).map((item) => ({
                kind: item.kind,
                params: item.params,
              })),
              selection: hook.selection?.items ?? [],
              messages: hook.messages ?? [],
            }),
          )
        })
        fail(`円 (${circle.cx}, ${circle.cy}) を描けない: ${JSON.stringify(state)}`)
      }
    }

    await page.click('#sketch-rectangle')
    await awaitPrompt('1つ目の角', '長方形の角の待ち受けが開かない')
    await clickPlane(PLATE.x, PLATE.y)
    await awaitPrompt('対角', '長方形の角のクリックが届いていない')
    await typeInto(page, '#sketch-width', PLATE.width)
    await typeInto(page, '#sketch-height', PLATE.height)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.some((item) => item.kind === 'rectangle'))
      },
      '長方形を描けない',
    )
    await page.keyboard.press('Escape') // 「選ぶ」へ戻す(描くコマンドが点を食わないように)

    const drawn = await sketchNow()
    const axisId = drawn.entities.find((item) => item.kind === 'line').entity_id
    const targetId = drawn.entities.find(
      (item) =>
        item.kind === 'circle' &&
        Math.abs(item.params.cx - TARGET.cx) < 1e-6 &&
        Math.abs(item.params.cy - TARGET.cy) < 1e-6,
    ).entity_id
    const otherId = drawn.entities.find(
      (item) => item.kind === 'circle' && Math.abs(item.params.cy - OTHER.cy) < 1e-6,
    ).entity_id
    ok('鏡映軸にする線分・鏡映する円2つ・板になる長方形をUIから描いた')

    // ================================================================ 2. mirror_sketch_entities
    // 対象の円を選ぶ(中心のハンドルをクリック。図形の参照として選択へ入る)。
    await clickPlane(TARGET.cx, TARGET.cy)
    await waitFor(
      (id) => (window.__duocad.selection?.items ?? []).some((item) => item.entity === id),
      '鏡映する円を選べない',
      [targetId],
    )

    await page.click('#sketch-mirror')
    // **対象が揃っているので軸のピックがその場で開く**(必須のピックは収集の順番で来る)。
    await waitFor(
      () => window.__duocad.wantedPick === 'sketchEntity',
      '鏡映複製を起動しても軸のピックが開かない',
    )
    ok('mirror_sketch_entities: 起動すると軸の線分のピックがその場で開く')

    // ---- 混同の検査(その1): **待っている間も対象を選び直せる**(修飾キー付きは選択へ行く)
    await clickPlane(OTHER.cx, OTHER.cy, 'Shift')
    await waitFor(
      (id) => (window.__duocad.selection?.items ?? []).some((item) => item.entity === id),
      'Shift+クリックが軸のピックに食われて選択へ行かない',
      [otherId],
    )
    const afterShiftSketch = await page.evaluate(() => ({
      wantedPick: window.__duocad.wantedPick,
      picked: (document.querySelector('#sketch-mirror-axis-value')?.textContent ?? '').trim(),
    }))
    if (afterShiftSketch.wantedPick !== 'sketchEntity' || afterShiftSketch.picked !== '未指定') {
      fail(`Shift+クリックが軸のピックとして食われた: ${JSON.stringify(afterShiftSketch)}`)
    }
    ok('mirror_sketch_entities: 軸を待っている間も Shift+クリックで対象を足せる')

    await clickPlane(OTHER.cx, OTHER.cy, 'Shift') // 足したぶんを外す(対象は最初の円だけ)
    await waitFor(
      (id) => !(window.__duocad.selection?.items ?? []).some((item) => item.entity === id),
      'Shift+クリックで足した円を外せない',
      [otherId],
    )

    // ---- 混同の検査(その2): **軸に指した線分は対象の選択へ入らない**
    await clickPlane(AXIS.x, 10)
    await waitFor(
      () =>
        (document.querySelector('#sketch-mirror-axis-value')?.textContent ?? '').trim() === '図形1つ',
      '線分をクリックしても軸として入らない',
    )
    await waitFor(() => window.__duocad.wantedPick === null, '線分を指しても軸のピックが閉じない')
    const selectedNow = await selectedEntityIds()
    if (selectedNow.includes(axisId)) {
      fail(`軸に指した線分が対象の選択へ入った: ${JSON.stringify(selectedNow)}`)
    }
    if (selectedNow.length !== 1 || selectedNow[0] !== targetId) {
      fail(`軸を指したら対象の選択が変わった: ${JSON.stringify(selectedNow)}(期待 [${targetId}])`)
    }
    ok('mirror_sketch_entities: 軸に指した線分は対象の選択へ入らない(別チャンネル)')

    await page.keyboard.press('Enter')
    try {
      await circleExists(-TARGET.cx, TARGET.cy, TARGET.r)
    } catch {
      fail('軸の向こう側に鏡映複製が出ない')
    }
    const afterMirror = await sketchNow()
    const lines = afterMirror.entities.filter((item) => item.kind === 'line')
    if (lines.length !== 1) fail(`軸に使った線分まで複製された(線分が ${lines.length} 本)`)
    const circles = afterMirror.entities.filter((item) => item.kind === 'circle')
    if (circles.length !== 3) {
      fail(`鏡映で増えた図形の数が合わない(円が ${circles.length} 個。期待3個)`)
    }
    ok('mirror_sketch_entities: クリックした線分を軸に、選んだ円だけが鏡映複製された')

    // ================================================================ 3. 板を1枚作る
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Escape')
    await page.click('#sketch-look')

    // **長方形の領域は面積で選ぶ**(円も領域を作るので、添字1が長方形とは限らない)。
    const plateRegion = afterMirror.regions.reduce((best, item) =>
      best === null || item.area > best.area ? item : best,
    null)
    if (!plateRegion) fail('領域が1つも無い')
    await page.click('#extrude')
    {
      const at = await page.evaluate(
        (id, index) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectRegion(index, id)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        sketchId,
        plateRegion.index,
      )
      if (!at) fail(`領域 ${plateRegion.index} を指せる画素が無い`)
      await page.mouse.click(at.x, at.y)
    }
    await waitFor(
      (index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.index === index && item.entity === undefined,
        ),
      '領域を選べない',
      [plateRegion.index],
    )
    await page.click('#extrude-mode-new')
    await typeInto(page, '#distance', PLATE.depth)
    await page.keyboard.press('Enter')
    await waitFor(() => (window.__duocad.parts ?? []).length === 1, '領域を押し出してもボディができない')
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Escape')
    // 通常モードの「押し出す」は面版になったので、領域はスケッチを抜ける前(スケッチ編集中)に押し出す。
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'スケッチを抜けられない')
    await frameAll(page)
    ok('板を1枚作った(ソリッドの鏡映の題材)')

    // ================================================================ 4. mirror_body: 面を鏡映面に指す
    const bodyId = await page.evaluate(() => window.__duocad.parts[0].body_id)
    await waitFor((id) => window.__duocad.getGeometry(id) !== null, 'ボディの形が届かない', [bodyId])

    // 画面から指せる面を2枚選ぶ: **対象に使う面と、鏡映面に使う面は別のもの**にする
    // (同じ面だと「指した面が選択へ入っていない」が判定できない)。
    const faces = await page.evaluate((id) => {
      const geometry = window.__duocad.getGeometry(id)
      return geometry.faces
        .filter((face) => window.__duocad.projectFace(id, face.index) !== null)
        .map((face) => ({ index: face.index, centroid: face.centroid, normal: face.normal }))
    }, bodyId)
    if (faces.length < 2) fail(`画面から指せる面が ${faces.length} 枚しかない(2枚要る)`)
    const targetFace = faces[0]
    const planeFace = faces[1]

    await clickFace(page, bodyId, targetFace.index, { hidePanel: true })
    await waitFor(
      (id, index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'face' && item.index === index,
        ),
      '対象の面を選べない',
      [bodyId, targetFace.index],
    )

    await page.click('#mirror-body')
    // **「面を選ぶ」を押すまで面のピックは開かない**(`when`)。3択で済む人の画面には
    // ピックの口が出ず、選択のクリックが吸われることもない。
    const beforeChoice = await page.evaluate(() => ({
      face: Boolean(document.querySelector('#mirror-body-face')),
      pick: Boolean(document.querySelector('#mirror-body-plane-face')),
      wantedPick: window.__duocad.wantedPick,
    }))
    if (!beforeChoice.face) fail('鏡映面の4つめ「面を選ぶ」がツールバーに無い')
    if (beforeChoice.pick || beforeChoice.wantedPick !== null) {
      fail(`「面を選ぶ」を押す前から面のピックが開いている: ${JSON.stringify(beforeChoice)}`)
    }
    ok('mirror_body: 起動しただけでは面のピックが開かない(XY/XZ/YZ の3択はそのまま使える)')

    await page.click('#mirror-body-face')
    await waitFor(
      () => window.__duocad.wantedPick === 'face',
      '「面を選ぶ」を押しても面のピックが開かない',
    )
    // **面を待っている間は辺を出さない。** 辺は面より先に拾われるので、出したままだと
    // 鏡映面を狙ったクリックが辺に取られ、その辺が対象の選択へ入る。
    const edgesWhileWaiting = await page.evaluate(() => window.__duocad.wantsEdges)
    if (edgesWhileWaiting !== false) fail('面のピック中に辺が出たままになっている')
    ok('mirror_body: 「面を選ぶ」で面のピックが開き、そのあいだ辺は出ない')

    // ---- 混同の検査(その1): **待っている間も対象を選び直せる**(修飾キー付きは選択へ行く)
    await clickFace(page, bodyId, planeFace.index, { modifier: 'Shift', hidePanel: true })
    await waitFor(
      (id, index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'face' && item.index === index,
        ),
      'Shift+クリックが面のピックに食われて選択へ行かない',
      [bodyId, planeFace.index],
    )
    const afterShift = await page.evaluate(() => ({
      wantedPick: window.__duocad.wantedPick,
      picked: (document.querySelector('#mirror-body-plane-face-value')?.textContent ?? '').trim(),
    }))
    if (afterShift.wantedPick !== 'face' || afterShift.picked !== '未指定') {
      fail(`Shift+クリックが鏡映面のピックとして食われた: ${JSON.stringify(afterShift)}`)
    }
    ok('mirror_body: 面を待っている間も Shift+クリックで対象を足せる(ピックには食われない)')

    await clickFace(page, bodyId, planeFace.index, { modifier: 'Shift', hidePanel: true }) // 足したぶんを外して元へ戻す
    await waitFor(
      (id, index) =>
        !(window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'face' && item.index === index,
        ),
      'Shift+クリックで足した面を外せない',
      [bodyId, planeFace.index],
    )

    // ---- 混同の検査(その2): **鏡映面に指した面は対象の選択へ入らない**
    const selectionBefore = await page.evaluate(() =>
      JSON.parse(JSON.stringify(window.__duocad.selection?.items ?? [])),
    )
    await clickFace(page, bodyId, planeFace.index, { hidePanel: true })
    await waitFor(
      (index) =>
        (document.querySelector('#mirror-body-plane-face-value')?.textContent ?? '').trim() ===
        `面 ${index}`,
      '面をクリックしても鏡映面として入らない',
      [planeFace.index],
    )
    await waitFor(() => window.__duocad.wantedPick === null, '面を指してもピックが閉じない')
    const selectionAfter = await page.evaluate(() =>
      JSON.parse(JSON.stringify(window.__duocad.selection?.items ?? [])),
    )
    if (JSON.stringify(selectionAfter) !== JSON.stringify(selectionBefore)) {
      fail(
        `鏡映面に指した面が対象の選択を変えた: ${JSON.stringify(selectionBefore)} → ` +
          `${JSON.stringify(selectionAfter)}。require_bodies がその面のボディまで対象に数える`,
      )
    }
    ok(`mirror_body: 鏡映面に面 ${planeFace.index} を指しても対象の選択は変わらない(別チャンネル)`)

    const centerBefore = await page.evaluate(
      (id) => window.__duocad.getGeometry(id).bbox.center,
      bodyId,
    )
    await page.keyboard.press('Enter')
    await waitFor(() => (window.__duocad.parts ?? []).length === 2, '面を鏡映面にしてもボディが増えない')
    const copyId = await page.evaluate(
      (id) => window.__duocad.parts.map((item) => item.body_id).find((item) => item !== id),
      bodyId,
    )
    await waitFor((id) => window.__duocad.getGeometry(id) !== null, '複製の形が届かない', [copyId])
    const centerCopy = await page.evaluate(
      (id) => window.__duocad.getGeometry(id).bbox.center,
      copyId,
    )
    // 指した面の平面(点=重心 / 法線)で元の中心を映した位置に、複製の中心があること。
    const want = mirrorPoint(centerBefore, planeFace.centroid, planeFace.normal)
    const gap = distance(want, centerCopy)
    if (gap > 0.05) {
      fail(
        `複製が指した面の平面で映っていない(ずれ ${gap.toFixed(3)}mm)。` +
          `期待 ${JSON.stringify(want)} / 実際 ${JSON.stringify(centerCopy)}`,
      )
    }
    ok(`mirror_body: クリックした面の平面でボディが鏡映複製された(中心のずれ ${gap.toFixed(4)}mm)`)

    // **XY/XZ/YZ の3択が壊れていない**(既存経路の回帰): 同じ選択のまま YZ で1つ増やす。
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Escape')
    await clickFace(page, bodyId, targetFace.index, { hidePanel: true })
    await waitFor(
      (id) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'face',
        ),
      '基準平面の検査のために面を選び直せない',
      [bodyId],
    )
    await page.click('#mirror-body')
    await page.click('#mirror-body-yz')
    const pickWhileBasePlane = await page.evaluate(() => ({
      wantedPick: window.__duocad.wantedPick,
      pick: Boolean(document.querySelector('#mirror-body-plane-face')),
    }))
    if (pickWhileBasePlane.wantedPick !== null || pickWhileBasePlane.pick) {
      fail(`YZ平面を選んだのに面のピックが出ている: ${JSON.stringify(pickWhileBasePlane)}`)
    }
    await page.keyboard.press('Enter')
    await waitFor(() => (window.__duocad.parts ?? []).length === 3, 'YZ平面での鏡映が壊れている')
    ok('mirror_body: 基準平面(YZ)の経路は今までどおり動く(回帰無し)')

    await assertNoAppErrors(page, consoleErrors)
  }
})

function distance(a, b) {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])
}

/** 点 `p` を、`origin` を通り `normal` を法線とする平面で映した点。 */
function mirrorPoint(p, origin, normal) {
  const length = Math.hypot(normal[0], normal[1], normal[2])
  const unit = normal.map((value) => value / length)
  const gap =
    (p[0] - origin[0]) * unit[0] + (p[1] - origin[1]) * unit[1] + (p[2] - origin[2]) * unit[2]
  return p.map((value, index) => value - 2 * gap * unit[index])
}
