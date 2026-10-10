/**
 * 実機での確認 — **鏡映の軸に指した線分が、対象の選択と混ざらない**。
 *
 *   node scripts/smoke-mirror.mjs
 *
 * `mirror_sketch_entities` の軸は、対象の選択とは別チャンネル(`web/src/commands/command.ts` の
 * `PickArgSpec`)で受け取る。混ざると軸の線分まで鏡映され、**自分自身とぴったり重なる複製**が
 * できる(画面では見えない。後で領域の計算が壊れる)。
 *
 *   1. **軸に指した線分が対象の選択へ入らない**
 *   2. **軸を待っている間も対象を選び直せる**(Shift/Ctrl+クリックはピックへ行かない)
 *   3. 複製が軸の向こう側に出て、線分は複製されない
 *
 * サーバはこのスクリプトが空の文書で起動し、**形は全部UIから作る**。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 *
 * **スケッチの図形はスケッチへ入った直後に全部描く。** `#sketch-look` が合わせる視野は
 * そのとき見えているものに依るので、後から離れた場所を押そうとすると画面の外になる
 * (実測: 板だけがある状態で `#sketch-look` した後、原点付近の (0,-20) が画面の外だった)。
 */
import { assertNoAppErrors, frameAll, openApp, sleep, waitOrFail } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { clickSketchPoint, createSketchOnBasePlane, sketchPointAt } from './pick.mjs'

/** 鏡映軸に使う線分(u=0 の縦線)。 */
const AXIS = { x: 0, y0: -20, y1: 20 }
/** 鏡映する円(軸から右に20mm)と、対象の選び直しに使うもう1つの円。 */
const TARGET = { cx: 20, cy: 0, r: 5 }
const OTHER = { cx: 20, cy: 15, r: 3 }

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

    /** いまのスケッチ(図形と選択)。`$state` は素で渡らないので JSON へ落として返す。 */
    async function sketchNow() {
      return page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketch
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
          const active = hook.sketch
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
      () => window.__duocad.sketch?.entities.some((item) => item.kind === 'line'),
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
          const active = hook.sketch
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
    ok('鏡映軸にする線分・鏡映する円2つをUIから描いた')

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

    await assertNoAppErrors(page, consoleErrors)
  }
})
