/**
 * 実機での確認 — 位置の打ち直し(**3Dの直接選択 + move verb**)が、本物のブラウザで端から端まで動く。
 *
 *   node scripts/smoke-move.mjs
 *
 * pytest では判定できない部分だけを見る(動かした結果の座標・複製・選択のリセットは
 * `tests/test_move_verbs.py`、幽霊の座標と行列は `tests/test_ghost_agreement.py` が持つ):
 *
 *   1. **マウスの割り当て**: 右ドラッグ=カメラ回転 / 中ドラッグ=パン / 左ドラッグ=ウィンドウ選択
 *   2. **ウィンドウ選択**: 矩形に**完全包含**されたものだけが選ばれる(向きは問わない)
 *   3. **move_sketch_entities**: 点を掴んで動かす。回転は種別を選ぶと欄が入れ替わり、中心を
 *      既存の点でピックする(ピックは共有選択を変えない)
 *   4. **スケッチを直接切り替えると選択が残らない**
 *   5. **move_body**: 幽霊がカメラ移動の再配信でも消えない / 辺からも踏める / 回転の軸と中心を
 *      3Dでピックする
 *   6. **右サイドバーの開閉**: `#sidebar-toggle` で `<aside class="sidebar">` が出入りし、
 *      中身は `AiPanel` だけ(= 完全にAI専用)。`.viewport` のサイズ指定は変わらない
 *
 * サーバはこのスクリプトが空の文書で起動し、**形は全部UIから作る**(この検査の主題が
 * 「人が手で操作できるか」なので、MCPで下ごしらえしない)。
 * **`--models-dir` に使い捨てディレクトリを渡す**(リポジトリ直下の実 `models/` を触らない)。
 */
import {
  assertNoAppErrors,
  frameAll,
  openApp,
  sleep,
  typeInto,
  viewportCenter,
  waitOrFail,
  withoutBrowserPanel,
} from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import {
  clickEntityHandle,
  clickFace,
  clickRegion,
  clickSketchPoint,
  createSketchOnBasePlane,
  planarFace,
  screenOf,
  sketchPointAt,
} from './pick.mjs'

/** 板(領域を押し出してボディを作る)。 */
const PLATE = { width: 40, height: 30, depth: 12 }
/** 動かす線分(板から離れた所に置く。ウィンドウ選択で板を巻き込まないため)。 */
const LINE = { x0: 60, y0: 0, x1: 80, y1: 0 }

runSmoke('smoke-move', { timeout: 60000 }, async ({ browser, url, timeout }) => {
  {
    const { page, consoleErrors } = await openApp(browser, url, { timeout })
    await frameAll(page).catch(() => {})

    // ---------------------------------------------------------------- 道具
    const waitFor = (check, what, args = []) =>
      waitOrFail(page, check, what, { args, timeout, polling: 50 })
    /** スケッチ平面の (u,v) の画面座標(押す直前に射影し直す)。無ければ null。 */
    const planeAt = (u, v) => sketchPointAt(page, u, v)
    const clickPlane = (u, v) => clickSketchPoint(page, u, v)

    /** スケッチ平面の点を中心にした矩形を左ドラッグで引く(ウィンドウ選択)。 */
    async function dragBox(from, to, key = null) {
      return withoutBrowserPanel(page, async () => {
        const a = await planeAt(from[0], from[1])
        const b = await planeAt(to[0], to[1])
        if (!a || !b) fail(`矩形の角 (${from}) / (${to}) が画面の中に無い`)
        if (key) await page.keyboard.down(key)
        await page.mouse.move(a.x, a.y)
        await page.mouse.down()
        // **途中の点を経由する**(pointermove が1回も飛ばないと矩形が始まらない)
        await page.mouse.move((a.x + b.x) / 2, (a.y + b.y) / 2, { steps: 4 })
        await page.mouse.move(b.x, b.y, { steps: 4 })
        await page.mouse.up()
        if (key) await page.keyboard.up(key)
      })
    }

    /**
     * 今のスケッチの状態(図形と選択)。
     *
     * **JSONへ落としてから返す**。`window.__duocad` が持つのは Svelte の `$state` プロキシで、
     * puppeteer の構造化コピーに素のまま渡すと**空のオブジェクトとして届く**
     * (実測: `selection.count` が undefined になる)。
     */
    async function sketchNow() {
      return page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch) ?? null
        return JSON.parse(
          JSON.stringify({
            activeSketch: hook.activeSketch,
            entities: active ? active.entities : [],
            regions: active ? active.regions.length : 0,
            selection: hook.selection,
          }),
        )
      })
    }

    /** 線分の図形(1本だけある前提)。 */
    async function lineEntity() {
      const state = await sketchNow()
      const found = state.entities.filter((item) => item.kind === 'line')
      if (found.length === 0) fail('線分が見つからない')
      return found
    }

    // ================================================================ 1. マウスの割り当て
    // **右ドラッグで回る / 中ドラッグでパン / 左ドラッグでは回らない**。
    // 判定は `cameraPose()`(実機検査だけが使う穴)の position で行う。
    const poseBefore = await page.evaluate(() => window.__duocad.cameraPose())
    const box = await viewportCenter(page)

    async function drag(button, dx, dy) {
      await page.mouse.move(box.x, box.y)
      await page.mouse.down({ button })
      await page.mouse.move(box.x + dx, box.y + dy, { steps: 8 })
      await page.mouse.up({ button })
      await sleep(400) // 減衰つきの追随を待つ
      return page.evaluate(() => window.__duocad.cameraPose())
    }

    const afterLeft = await drag('left', 120, 60)
    const movedByLeft = distance(poseBefore.position, afterLeft.position)
    if (movedByLeft > 0.5) fail(`左ドラッグでカメラが動いた(${movedByLeft.toFixed(2)}mm)`)
    ok('左ドラッグではカメラが動かない(ウィンドウ選択に明け渡している)')

    const afterRight = await drag('right', 120, 0)
    const movedByRight = distance(afterLeft.position, afterRight.position)
    if (movedByRight < 1) fail(`右ドラッグでカメラが回らない(${movedByRight.toFixed(2)}mm)`)
    // 回転は**注視点を動かさない**(パンとの見分け)
    const targetShiftOnRotate = distance(afterLeft.target, afterRight.target)
    if (targetShiftOnRotate > 0.5) fail(`右ドラッグが回転ではなくパンになっている(注視点が ${targetShiftOnRotate.toFixed(2)}mm 動いた)`)
    ok(`右ドラッグ = カメラ回転(位置が ${movedByRight.toFixed(1)}mm 動き、注視点は動かない)`)

    const afterMiddle = await drag('middle', 80, 0)
    const targetShiftOnPan = distance(afterRight.target, afterMiddle.target)
    if (targetShiftOnPan < 0.5) fail(`中ドラッグでパンしない(注視点が ${targetShiftOnPan.toFixed(2)}mm しか動かない)`)
    ok(`中ドラッグ = パン(注視点が ${targetShiftOnPan.toFixed(1)}mm 動いた)`)
    await frameAll(page)

    // ================================================================ 2. スケッチを描く
    await createSketchOnBasePlane(page, 'XY', { timeout })

    await page.click('#sketch-look') // 正面から見る(以後の (u,v) → 画素が安定する)
    await sleep(400)

    await page.click('#sketch-rectangle')
    await clickPlane(0, 0)
    await typeInto(page, '#sketch-width', PLATE.width)
    await typeInto(page, '#sketch-height', PLATE.height)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.regions.length === 1)
      },
      '長方形の領域ができない',
    )

    await page.click('#sketch-line')
    await clickPlane(LINE.x0, LINE.y0)
    await clickPlane(LINE.x1, LINE.y1)
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.some((item) => item.kind === 'line'))
      },
      '線分を描けない',
    )
    await page.keyboard.press('Escape') // 「選ぶ」へ戻す(描くコマンドが点を食わないように)
    ok('長方形と線分をUIから描いた')

    // ================================================================ 3. ウィンドウ選択で点を選ぶ
    // **線分の右端だけ**を小さな矩形で囲む(左端も線全体も入らない大きさ)。
    await dragBox([LINE.x1 - 4, LINE.y1 - 4], [LINE.x1 + 4, LINE.y1 + 4])
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).some((item) => item.point === 'x1'),
      'ウィンドウ選択で線分の右端(x1)を選べない',
    )
    const afterBox = await sketchNow()
    if (afterBox.selection.count !== 1) {
      fail(`右端だけを囲んだのに ${afterBox.selection.count} 件選ばれた: ${JSON.stringify(afterBox.selection.items)}`)
    }
    ok('左ドラッグのウィンドウ選択で、矩形に完全包含された点(x1)だけが選ばれた')

    // **向きは問わない**(本人明言)。同じ矩形を右下→左上へ引いても同じ1点が選ばれる。
    await dragBox([LINE.x1 + 4, LINE.y1 + 4], [LINE.x1 - 4, LINE.y1 - 4])
    await waitFor(
      () => {
        const items = window.__duocad.selection?.items ?? []
        return items.length === 1 && items[0].point === 'x1'
      },
      '逆向きに引いた矩形では選べない(ドラッグの向きで判定が変わっている)',
    )
    ok('矩形を逆向き(右下→左上)に引いても同じ点が選ばれた(向きで判定を変えない)')

    // **囲まなかったものは選ばれない**(完全包含の確認): 線分の中ほどだけを囲む
    await dragBox([LINE.x0 + 6, LINE.y0 - 3], [LINE.x1 - 6, LINE.y1 + 3])
    await waitFor(
      () => (window.__duocad.selection?.count ?? -1) === 0,
      '線分の一部だけを囲んだのに何かが選ばれた(交差判定になっている)',
    )
    ok('線分の一部だけを囲んでも何も選ばれない(交差ではなく完全包含)')

    // 動かす点(線分の右端)を選び直す
    await dragBox([LINE.x1 - 4, LINE.y1 - 4], [LINE.x1 + 4, LINE.y1 + 4])
    await waitFor(() => (window.__duocad.selection?.count ?? 0) === 1, '右端を選び直せない')

    // ================================================================ 5. move_sketch_entities(移動)
    const before = (await lineEntity())[0]
    await page.click('#sketch-move')
    await page.waitForSelector('#sketch-move-move', { timeout })
    await page.click('#sketch-move-move')
    await page.waitForFunction(
      () => document.querySelector('#sketch-move-move')?.classList.contains('on'),
      { timeout, polling: 50 },
    )
    // **並進 / 回転も人が選ぶ**(既定を置かない。`ChoiceArgSpec` の流儀)。
    await page.click('#sketch-move-translate')
    await typeInto(page, '#sketch-move-dx', 0)
    await typeInto(page, '#sketch-move-dy', 10)

    // ---------------------------------------------------------------- 確定前の幽霊
    // **`preview: 'local'`** —— 選んだ点を含む図形が、dy を打った時点で
    // 移動先に半透明で出ている(元の図形はその場に残るので、両方が見える)。
    await waitFor(
      () => window.__duocad.ghostInfo().sketch.length === 1,
      '点を動かす前の幽霊が出ない',
    )
    const sketchGhost = await page.evaluate(() => window.__duocad.ghostInfo().sketch[0])
    if (Math.abs(sketchGhost.params.y1 - (before.params.y1 + 10)) > 1e-9) {
      fail(
        `幽霊が dy=10 のぶん動いていない: y1 ${before.params.y1} → ${sketchGhost.params.y1}`,
      )
    }
    ok('move_sketch_entities: 確定前に移動先の幽霊が出る')

    await page.keyboard.press('Enter')
    // **クリックで置いた座標は丸い数字ではない**(画素→(u,v)の逆射影なので 0.0234 のような値)。
    // 判定は絶対値ではなく**移動量**で行う。
    await waitFor(
      (id, y1) => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        const entity = active?.entities.find((item) => item.entity_id === id)
        return Boolean(entity && Math.abs(entity.params.y1 - (y1 + 10)) < 1e-6)
      },
      '点を動かしても y1 が +10 されない',
      [before.entity_id, before.params.y1],
    )
    const moved = (await lineEntity())[0]
    ok(
      `move_sketch_entities で選んだ点が動いた(y1: ${before.params.y1.toFixed(3)} → ` +
        `${moved.params.y1.toFixed(3)})`,
    )

    /**
     * 線分の中ほど(= 点ではなく辺 = 図形まるごと)をクリックする。
     *
     * **中点は `moved` の実座標から求める**(`LINE.x0/x1` と動かす前の `y0` を混ぜない) ——
     * y1 だけを +10 動かした結果、線分は (60,0)-(80,10) のように傾いている。x座標の中点に
     * 動かす前の y0 を組み合わせると実際の中点から約4.5mmずれ、当たり半径の外に出る。
     */
    const clickLineBody = () =>
      withoutBrowserPanel(page, async () => {
        const mid = await planeAt(
          (moved.params.x0 + moved.params.x1) / 2,
          (moved.params.y0 + moved.params.y1) / 2,
        )
        if (!mid) fail('線分の中ほどが画面の中に無い')
        await page.mouse.click(mid.x, mid.y)
      })

    // ================================================================ 6. 回転をUIから起動する(スケッチ)
    //
    // **ツールバーのボタンと3Dのクリックだけ**で回す —— 種別(並進/回転)を選び、
    // 中心を既存の点でピックし、角度を打って Enter する。
    {
      await clickLineBody()
      await waitFor(
        () =>
          (window.__duocad.selection?.items ?? []).some(
            (item) => item.entity !== undefined && item.point === null,
          ),
        '線分そのもの(辺)をクリックで選べない',
      )
      ok('線分そのもの(点ではなく辺)をクリックで選べた')
      const targetId = await page.evaluate(
        () =>
          window.__duocad.selection.items.find(
            (item) => item.entity !== undefined && item.point === null,
          ).entity,
      )
      const before = (await sketchNow()).entities.find((item) => item.entity_id === targetId)
      if (!before) fail('回転させる線分が見つからない')

      await page.click('#sketch-move')
      await page.click('#sketch-move-move')
      await page.click('#sketch-move-rotate')
      // **mode で要る引数が入れ替わる**(`ArgSpec.when`)。並進の欄は消え、角度と中心が出る。
      const fields = await page.evaluate(() => ({
        dx: Boolean(document.querySelector('#sketch-move-dx')),
        dy: Boolean(document.querySelector('#sketch-move-dy')),
        angle: Boolean(document.querySelector('#sketch-move-angle')),
        center: Boolean(document.querySelector('#sketch-move-center')),
      }))
      if (fields.dx || fields.dy) {
        fail(`回転を選んでも X/Y の欄が残っている(mode依存の引数が効いていない): ${JSON.stringify(fields)}`)
      }
      if (!fields.angle || !fields.center) fail(`回転の入力が出ない: ${JSON.stringify(fields)}`)
      ok('move_sketch_entities: 回転を選ぶと X/Y の欄が消え、角度と中心の口が開く')

      await typeInto(page, '#sketch-move-angle', 90)
      // **中心を指す前は幽霊を描かない。** 省略時の中心(対象の代表点の平均)はコアにしか
      // 無い規則で、画面側へ写すと片方だけ変わったときに幽霊と確定結果が別の場所に出る。
      // **確定できる状態になるまで待つ** —— 幽霊は引数が揃ってから組まれるので、
      // 打った直後に読むと「まだ組んでいないだけ」を「描かない」と読み違える。
      await waitFor(
        () => (document.querySelector('#command-prompt')?.textContent ?? '').includes('Enter'),
        '角度を打っても確定できる状態にならない(中心が必須になっている)',
      )
      const noCentreGhosts = await page.evaluate(() => window.__duocad.ghostInfo().sketch.length)
      if (noCentreGhosts !== 0) {
        fail(`中心を指す前に幽霊が出た(既定の中心を画面側へ写している): ${noCentreGhosts}件`)
      }
      ok('move_sketch_entities: 中心を指す前は幽霊を描かない(既定の中心を写していない)')

      await page.click('#sketch-move-center')
      await waitFor(
        () => window.__duocad.wantedPick === 'sketchPoint',
        '「指す」を押しても中心のピックが開かない',
      )
      // **線分の始点そのもの**を中心にする(その点は動かないので判定が読みやすい)。
      await clickEntityHandle(page, targetId)
      await waitFor(
        () => window.__duocad.wantedPick === null,
        '点を指しても中心のピックが閉じない',
      )
      const shown = await page.evaluate(
        () => document.querySelector('#sketch-move-center-value')?.textContent?.trim() ?? '',
      )
      if (shown !== '点 x0') fail(`指した中心が画面に出ていない: ${JSON.stringify(shown)}`)
      // **中心に指した点は共有選択集合へ入らない**(入ると、その点自身が動く対象になる)。
      const selectedEntities = await page.evaluate(
        () => (window.__duocad.selection?.items ?? []).filter((item) => item.entity !== undefined).length,
      )
      if (selectedEntities !== 1) {
        fail(`中心のピックが共有選択を変えた(対象が ${selectedEntities} 件になっている)`)
      }
      ok('move_sketch_entities: 中心の点をピックしても共有選択は変わらない(別チャンネル)')

      await waitFor(
        () => window.__duocad.ghostInfo().sketch.length === 1,
        '中心を指しても幽霊が出ない',
      )
      const cx = before.params.x0
      const cy = before.params.y0
      const want = {
        x0: cx,
        y0: cy,
        x1: cx - (before.params.y1 - cy),
        y1: cy + (before.params.x1 - cx),
      }

      await page.keyboard.press('Enter')
      await waitFor(
        (id, x1, y1) => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          const entity = active?.entities.find((item) => item.entity_id === id)
          return Boolean(
            entity &&
              Math.abs(entity.params.x1 - x1) < 1e-6 &&
              Math.abs(entity.params.y1 - y1) < 1e-6,
          )
        },
        'UIから回した線分が幽霊と同じ位置へ来ない',
        [targetId, want.x1, want.y1],
      )
      const after = (await sketchNow()).entities.find((item) => item.entity_id === targetId)
      if (Math.abs(after.params.x0 - cx) > 1e-6 || Math.abs(after.params.y0 - cy) > 1e-6) {
        fail(`中心に指した始点が動いた: ${JSON.stringify(after.params)}`)
      }
      ok('move_sketch_entities: ツールバーと3Dクリックだけで回転を確定できた(幽霊と一致)')

      // 後片付け —— **回す前へ戻す**(以降の節は元の座標で図形をクリックする)。
      // undo が効くこと自体、UIから送った回転が普通のverbとして操作ログに載っている証拠である。
      await waitFor(
        () => (window.__duocad.selection?.count ?? -1) === 0,
        'UIからの回転の後に選択が空にならない',
      )
      await page.evaluate(() => document.activeElement?.blur?.())
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyZ')
      await page.keyboard.up('Control')
      await waitFor(
        (id, x1, y1) => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
          const entity = active?.entities.find((item) => item.entity_id === id)
          return Boolean(
            entity &&
              Math.abs(entity.params.x1 - x1) < 1e-9 &&
              Math.abs(entity.params.y1 - y1) < 1e-9,
          )
        },
        'UIから回した線分を undo で戻せない',
        [targetId, before.params.x1, before.params.y1],
      )
      ok('move_sketch_entities: UIから回した結果を undo で元の座標へ戻せる')
    }

    // ================================================================ 6b. スケッチを直接切り替えると選択が残らない
    //
    // **`exitSketch` を通らずにAからBへ移る道がある**(ビューポートに浮かぶブラウザの
    // スケッチ名クリック)。外さないと、Bで動かしたつもりでAの図形が動き、反復verbには
    // **Aの図形IDがBの文脈で**渡る(サーバは entity_id を素直に解決するので黙って通る)。
    const sketchA = await page.evaluate(() => window.__duocad.activeSketch)
    // Aの図形を1つ選んでおく
    await clickLineBody()
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).some((item) => item.entity !== undefined),
      'Aの図形を選べない',
    )
    // Bを作る(XZ平面。Aとは別のスケッチ)
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'Aを抜けられない')
    await frameAll(page)
    const sketchB = await createSketchOnBasePlane(page, 'XZ', { timeout })
    if (sketchB === sketchA) fail('XZ平面に2枚目のスケッチを作れない')
    // **Aへ戻り、Aの図形を選んでから、ブラウザのスケッチ名クリックで直接Bへ移る**
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'Bを抜けられない')
    await page.click(`[data-sketch-id="${sketchA}"] .sketch-name`)
    await waitFor((a) => window.__duocad.activeSketch === a, 'Aへ再入場できない', [sketchA])
    await page.click('#sketch-look')
    await sleep(400)
    await clickEntityHandle(page, moved.entity_id)
    await waitFor(
      (a) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.entity !== undefined && item.sketch === a,
        ),
      'Aの図形を選び直せない',
      [sketchA],
    )
    await page.click(`[data-sketch-id="${sketchB}"] .sketch-name`)
    await waitFor((b) => window.__duocad.activeSketch === b, 'Bへ直接移れない', [sketchB])
    await waitFor(
      () =>
        !(window.__duocad.selection?.items ?? []).some((item) => item.entity !== undefined) &&
        (window.__duocad.selectedEntities ?? []).length === 0,
      'Aの図形の選択がBへ持ち越されている(enterSketchが外していない)',
    )
    ok('スケッチAで選んだ図形は、Bへ直接移った時点で選択から外れる')

    // ================================================================ 7. 領域を押し出してボディを作る
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'Bを抜けられない(2回目)')
    await page.click(`[data-sketch-id="${sketchA}"] .sketch-name`)
    await waitFor((a) => window.__duocad.activeSketch === a, 'Aへ戻れない', [sketchA])
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'ツールバーの「抜ける」でスケッチを抜けられない')
    ok('ツールバーの「抜ける」でスケッチモードを抜けた(移設先が機能している)')
    // 通常モードの「押し出す」は面版なので、領域の押し出しはスケッチ編集中に行う。
    await page.click(`[data-sketch-id="${sketchA}"] .sketch-name`)
    await waitFor((a) => window.__duocad.activeSketch === a, '押し出しのためAへ入れない', [sketchA])
    await page.click('#sketch-look')
    await frameAll(page)

    const sketchId = await page.evaluate(() => window.__duocad.sketches[0].sketch_id)
    await page.click('#extrude')
    await clickRegion(page, 1, { sketchId })
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).some((item) => item.index === 1 && item.entity === undefined),
      '領域を選べない',
    )
    await page.click('#extrude-mode-new')
    await typeInto(page, '#distance', PLATE.depth)
    await page.keyboard.press('Enter')
    await waitFor(() => (window.__duocad.parts ?? []).length === 1, '領域を押し出してもボディができない')
    // **Escで無起動へ戻し、スケッチを抜ける**(確定してもコマンドは構えたままなので)。
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Escape')
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, '押し出し後にスケッチを抜けられない')
    await frameAll(page)
    ok('領域を押し出してボディを1つ作った')

    // ================================================================ 8. move_body(移動)
    const bodyId = await page.evaluate(() => window.__duocad.parts[0].body_id)
    const faceIndex = await planarFace(page, bodyId, [0, 0, 1])
    await clickFace(page, bodyId, faceIndex)
    await waitFor(
      (id, index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'face' && item.index === index,
        ),
      '面をクリックで選べない',
      [bodyId, faceIndex],
    )
    const volumeBefore = await page.evaluate(
      (id) => window.__duocad.state.bodies.find((item) => item.body_id === id).volume,
      bodyId,
    )
    const geometryBefore = await page.evaluate((id) => window.__duocad.getGeometry(id).bbox.center, bodyId)

    await page.click('#move-body')
    await page.click('#move-body-move')
    await page.click('#move-body-translate')
    await typeInto(page, '#move-body-dx', 0)
    await typeInto(page, '#move-body-dy', 0)
    await typeInto(page, '#move-body-dz', 25)

    // ---------------------------------------------------------------- 確定前の幽霊
    // **`preview: 'local'`** —— 届いているメッシュを dx/dy/dz ずらして
    // 半透明で重ねる。**本物は隠さない**(移動前と移動後が並んで見えることが中身)ので、
    // 幽霊が出ている間も `getGeometry` の本物は元の位置のままである。
    await waitFor(
      (id) => {
        const ghosts = window.__duocad.ghostInfo().bodies
        return ghosts.length === 1 && ghosts[0].bodyId === id && ghosts[0].offset[2] === 25
      },
      'move_body の幽霊が dz=25 で出ない',
      [bodyId],
    )
    const stillHome = await page.evaluate(
      (id) => window.__duocad.getGeometry(id).bbox.center[2],
      bodyId,
    )
    if (Math.abs(stillHome - geometryBefore[2]) > 1e-6) {
      fail(`幽霊を出しただけで本物が動いた: ${geometryBefore[2]} → ${stillHome}`)
    }

    // ---------------------------------------------------------------- カメラを動かしても消えない
    // **カメラ移動は形を変えないが、状態配信を起こす。** 可変解像度の `focus` が変わると
    // サーバは粗さを版の鍵に入れている(`view.body_revision`)ので、**幽霊が乗っている
    // ボディ自身が測り直されて届く** —— ここで版が上がることを先に確かめてから幽霊を見る
    // (版が上がらなければ、この検査は見たい配信が起きないまま緑になる)。
    // 引数も選択も触っていないので `CommandHost` は幽霊を投げ直さない(`previewedKey`)。
    // 救済は `viewport.update()` 側にあり、届いた形で組み直す。
    {
      const revisionBefore = await page.evaluate(
        (id) => window.__duocad.state.bodies.find((item) => item.body_id === id).revision,
        bodyId,
      )
      const ghostBefore = await page.evaluate(() =>
        JSON.parse(JSON.stringify(window.__duocad.ghostInfo().bodies)),
      )
      if (ghostBefore.length !== 1) {
        fail(`カメラを動かす前に幽霊が消えている: ${JSON.stringify(ghostBefore)}`)
      }
      await drag('right', 140, 60)
      await waitFor(
        (id, before) =>
          window.__duocad.state.bodies.find((item) => item.body_id === id).revision > before,
        'カメラを動かしても対象ボディが配り直されない(この検査が見たい配信が起きていない)',
        [bodyId, revisionBefore],
      )
      const ghostAfter = await page.evaluate(() =>
        JSON.parse(JSON.stringify(window.__duocad.ghostInfo().bodies)),
      )
      if (ghostAfter.length !== 1) {
        fail(`カメラを動かしただけで幽霊が消えた: ${JSON.stringify(ghostAfter)}`)
      }
      // **同じ姿でなければならない** —— 引数も選択も変わっていないのだから、
      // 組み直した幽霊は掛かっている行列まで前と同じである。
      if (JSON.stringify(ghostAfter[0].matrix) !== JSON.stringify(ghostBefore[0].matrix)) {
        fail(
          `カメラを動かしたら幽霊の行列が変わった: ${JSON.stringify(ghostBefore[0].matrix)} → ` +
            JSON.stringify(ghostAfter[0].matrix),
        )
      }
      const revisionAfter = await page.evaluate(
        (id) => window.__duocad.state.bodies.find((item) => item.body_id === id).revision,
        bodyId,
      )
      ok(
        `move_body: カメラを動かして対象ボディが測り直されても(版 ${revisionBefore} → ` +
          `${revisionAfter})幽霊は同じ姿で出続ける`,
      )
      await drag('right', -140, -60) // 以後の節は本物のクリックで面・辺を指すので向きを戻す
    }

    // **Escで消えて、文書は一切変わらない**(サーバへ何も送っていない)
    await page.keyboard.press('Escape')
    await waitFor(
      () => window.__duocad.ghostInfo().bodies.length === 0,
      'Escで move_body の幽霊が消えない',
    )
    const volumeAfterEscape = await page.evaluate(
      (id) => window.__duocad.state.bodies.find((item) => item.body_id === id).volume,
      bodyId,
    )
    if (Math.abs(volumeAfterEscape - volumeBefore) > 1e-9) {
      fail(`Escで中断したのに文書が変わった: ${volumeBefore} → ${volumeAfterEscape}`)
    }
    ok('move_body: 確定前に移動先の幽霊が出て、Escで消えて文書は変わらない')
    await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, 'Escで選択が空にならない')

    // ================================================================ 8b. move_body は辺からも踏める
    //
    // `MOVE_BODY` の選択引数は面/辺/頂点の3種を食う(コアの `require_bodies` が
    // `ShapeRef.body` から逆引きする)。**画面側の配線が本当に届いているか**を見る ——
    // `CommandHost` が主たる種別しか要求しないと、辺のピックが開かないまま
    // 「サーバは受け付けるのに画面から選べない」穴になる(実際そうなっていたことがある)。
    // Esc で選択は空になっている。そのまま move-body を起動すれば辺のピックが開くはずである。
    // **移動直後は1フレーム挟む**(`projectEdge` 等はレンダー済みの射影行列を読むため、
    // 明示的に呼ぶ)。
    await frameAll(page)
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Escape')
    await page.click('#move-body')
    await waitFor(
      () => window.__duocad.wantsEdges === true,
      'move_body を起動しても辺を要求しない(kinds の配線が届いていない)',
    )
    // **`wantsEdges` が真になっても、辺の実データはまだ無い**。
    // あれは `CommandHost` の同期的な `$effect` で即座に立つが、辺そのものは版が違えば
    // `showEdges(null, [])` で空にしてからWS往復で取り直す(`App.svelte`)。その窓では
    // `projectEdge` が `edgeBodyId !== bodyId` の早期returnで**全indexに対しnull**を返し、
    // 下のループが待たずに8本を撃ち切って「1本も選べない」と誤判定していた。**エラーは
    // 1件も出ない**ので原因が遠い。
    //
    // **2周するのは、ループの最中に足元が抜ける経路があるため** —— カメラを動かすと
    // 可変解像度が180ms後に `set_view_resolution` を送り、サーバは粗さを版の鍵に入れて
    // いるので形が同じでも配信し直す。その配信で `viewport.update()` が表示中の辺を消すため、
    // 全indexが途中でnullへ落ちうる。取り直しを待ってもう1周すれば通る。
    let edgePicked = null
    for (let attempt = 0; attempt < 2 && edgePicked === null; attempt++) {
      await waitFor(
        (id) =>
          [1, 2, 3, 4, 5, 6, 7, 8].some((index) => window.__duocad.projectEdge(id, index) !== null),
        '辺の実データが届かない(move_body 起動後、projectEdge がどのindexでもnullのまま)',
        [bodyId],
      )
      for (const edgeIndex of [1, 2, 3, 4, 5, 6, 7, 8]) {
        const at = await screenOf(page, 'projectEdge', bodyId, edgeIndex)
        if (!at) continue
        await page.mouse.click(at.x, at.y)
        try {
          await page.waitForFunction(
            (id, index) =>
              (window.__duocad.selection?.items ?? []).some(
                (item) => item.body === id && item.kind === 'edge' && item.index === index,
              ),
            { timeout: 5000, polling: 50 },
            bodyId,
            edgeIndex,
          )
          edgePicked = edgeIndex
          break
        } catch {
          // その辺は取れなかった(裏に回っている)。次を試す
        }
      }
    }
    if (edgePicked === null) fail('move_body を起動しても辺を1本もクリックで選べない')
    const beforeEdgeMove = await page.evaluate(
      (id) => window.__duocad.getGeometry(id).bbox.center,
      bodyId,
    )
    await page.click('#move-body-move')
    await page.click('#move-body-translate')
    await typeInto(page, '#move-body-dx', 6)
    await typeInto(page, '#move-body-dy', 0)
    await typeInto(page, '#move-body-dz', 0)
    await page.keyboard.press('Enter')
    await waitFor(
      (id, x) => {
        const geometry = window.__duocad.getGeometry(id)
        return Boolean(geometry && Math.abs(geometry.bbox.center[0] - (x + 6)) < 0.5)
      },
      '辺の選択から move_body してもボディが動かない',
      [bodyId, beforeEdgeMove[0]],
    )
    ok(`辺 ${edgePicked} を選んで move_body できた(面だけでなく辺からもボディを指せる)`)

    // ================================================================ 9b. 回転をUIから起動する(ソリッド)
    //
    // **ツールバーのボタンと3Dのクリックだけ**で回す —— 回転を選び、軸にする辺を
    // 3Dでピックし、角度を打って Enter する。**軸の辺は共有選択集合に入らない**ことも見る
    // (入ると `require_bodies` がその辺のボディまで「動かす対象」に数える)。
    {
      await page.evaluate(() => document.activeElement?.blur?.())
      await page.keyboard.press('Escape')
      await frameAll(page)
      await clickFace(page, bodyId, faceIndex)
      await waitFor(
        (id) =>
          (window.__duocad.selection?.items ?? []).some(
            (item) => item.body === id && item.kind === 'face',
          ),
        '回転のために面を選べない',
        [bodyId],
      )

      await page.click('#move-body')
      await page.click('#move-body-move')
      await page.click('#move-body-rotate')
      const fields = await page.evaluate(() => ({
        dx: Boolean(document.querySelector('#move-body-dx')),
        angle: Boolean(document.querySelector('#move-body-angle')),
        axis: Boolean(document.querySelector('#move-body-axis')),
        centre: Boolean(document.querySelector('#move-body-center')),
      }))
      if (fields.dx) fail('回転を選んでも X の欄が残っている(mode依存の引数が効いていない)')
      if (!fields.angle || !fields.axis || !fields.centre) {
        fail(`回転の入力が出ない: ${JSON.stringify(fields)}`)
      }
      // **軸を先に問う**(`args` の並び = 次に何を問うか)。回転を選んだ瞬間に辺が指せる。
      await waitFor(
        () => window.__duocad.wantedPick === 'edge' && window.__duocad.wantsEdges === true,
        '回転を選んでも軸の辺のピックが開かない',
      )
      ok('move_body: 回転を選ぶと X/Y/Z の欄が消え、軸の辺のピックがその場で開く')

      // 指せる辺を1本探して押す(8b と同じ探し方。裏に回っている辺は取れない)。
      const selectionBefore = await page.evaluate(() => window.__duocad.selection?.count ?? 0)
      let axisEdge = null
      for (let attempt = 0; attempt < 2 && axisEdge === null; attempt++) {
        await waitFor(
          (id) =>
            [1, 2, 3, 4, 5, 6, 7, 8].some(
              (index) => window.__duocad.projectEdge(id, index) !== null,
            ),
          '軸に使う辺の実データが届かない',
          [bodyId],
        )
        for (const edgeIndex of [1, 2, 3, 4, 5, 6, 7, 8]) {
          const at = await screenOf(page, 'projectEdge', bodyId, edgeIndex)
          if (!at) continue
          await page.mouse.click(at.x, at.y)
          try {
            await page.waitForFunction(
              (index) =>
                (document.querySelector('#move-body-axis-value')?.textContent ?? '').trim() ===
                `辺 ${index}`,
              { timeout: 3000, polling: 50 },
              edgeIndex,
            )
            axisEdge = edgeIndex
            break
          } catch {
            // その辺は取れなかった(裏に回っている)。次を試す
          }
        }
      }
      if (axisEdge === null) fail('回転軸にする辺を1本もクリックで指せない')
      await waitFor(() => window.__duocad.wantedPick === null, '辺を指しても軸のピックが閉じない')
      // **共有選択は変わっていない**(軸の辺は別チャンネル)。
      const selectionAfterAxis = await page.evaluate(() => ({
        count: window.__duocad.selection?.count ?? 0,
        edges: (window.__duocad.selection?.items ?? []).filter((item) => item.kind === 'edge')
          .length,
      }))
      if (selectionAfterAxis.count !== selectionBefore || selectionAfterAxis.edges !== 0) {
        fail(
          `軸に指した辺が共有選択へ入った(${selectionBefore} → ${JSON.stringify(selectionAfterAxis)})。` +
            'コアの require_bodies がその辺のボディまで動かす対象に数える',
        )
      }
      ok(`move_body: 軸に辺 ${axisEdge} をピックしても共有選択は変わらない(別チャンネル)`)

      await typeInto(page, '#move-body-angle', 90)
      await waitFor(
        () => window.__duocad.ghostInfo().bodies.length === 1,
        'UIから軸を指しても回転の幽霊が出ない',
      )

      // **中心の頂点も指せる**(省略できるので「指す」の1手が要る)。指すと軸の位置だけが動く。
      await page.click('#move-body-center')
      await waitFor(
        () => window.__duocad.wantedPick === 'vertex',
        '「指す」を押しても中心の頂点のピックが開かない',
      )
      // **候補が出ていなければ指しようがない** —— 頂点マーカーは既定では選択に入った分しか
      // 出ないので、待ち受けている間だけ候補を出す配線がここで効く。
      await waitFor(
        (id) => (window.__duocad.vertexMarkers ?? []).some((marker) => marker.bodyId === id),
        '中心を指せるのに、このボディの頂点マーカーが出ない(指せる的が画面に無い)',
        [bodyId],
      )
      let centreVertex = null
      for (const vertexIndex of [1, 2, 3, 4, 5, 6, 7, 8]) {
        const at = await screenOf(page, 'projectVertex', bodyId, vertexIndex)
        if (!at) continue
        await page.mouse.click(at.x, at.y)
        try {
          await page.waitForFunction(
            (index) =>
              (document.querySelector('#move-body-center-value')?.textContent ?? '').trim() ===
              `頂点 ${index}`,
            { timeout: 3000, polling: 50 },
            vertexIndex,
          )
          centreVertex = vertexIndex
          break
        } catch {
          // その頂点は取れなかった(裏に回っている)。次を試す
        }
      }
      if (centreVertex === null) fail('回転中心の頂点を1つもクリックで指せない')
      ok(`move_body: 中心に頂点 ${centreVertex} を3Dのクリックで指せた`)

      await page.click('#move-body-center-clear')
      await waitFor(
        () => (document.querySelector('#move-body-center-value')?.textContent ?? '').trim() === '未指定',
        '「外す」を押しても中心が未指定へ戻らない',
      )
      ok('move_body: 指した中心を「外す」で取り消せる')

      // ---------------------------------------------------------------- 確定
      const volumeBeforeRotate = await page.evaluate(
        (id) => window.__duocad.state.bodies.find((item) => item.body_id === id).volume,
        bodyId,
      )
      const centreBeforeRotate = await page.evaluate(
        (id) => window.__duocad.getGeometry(id).bbox.center,
        bodyId,
      )
      await page.keyboard.press('Enter')
      await waitFor(
        (id, before) => {
          const geometry = window.__duocad.getGeometry(id)
          if (!geometry) return false
          const centre = geometry.bbox.center
          return (
            Math.hypot(centre[0] - before[0], centre[1] - before[1], centre[2] - before[2]) > 1.0
          )
        },
        'UIから回してもボディが動かない',
        [bodyId, centreBeforeRotate],
      )
      const volumeAfterRotate = await page.evaluate(
        (id) => window.__duocad.state.bodies.find((item) => item.body_id === id).volume,
        bodyId,
      )
      if (Math.abs(volumeAfterRotate - volumeBeforeRotate) > 1e-3) {
        fail(`回転で体積が変わった: ${volumeBeforeRotate} → ${volumeAfterRotate}`)
      }
      ok(
        'move_body: ツールバーと3Dクリックだけで回転を確定できた(体積は ' +
          `${volumeAfterRotate.toFixed(1)} のまま)`,
      )
    }

    // ================================================================ 10. 右サイドバーの開閉
    const sidebarBefore = await page.evaluate(() => ({
      sidebar: Boolean(document.querySelector('.sidebar')),
      ai: Boolean(document.querySelector('.sidebar .ai-panel')),
      children: document.querySelector('.sidebar')?.childElementCount ?? -1,
      viewport: document.querySelector('.viewport').getBoundingClientRect().width,
    }))
    if (!sidebarBefore.sidebar) fail('右サイドバーが出ていない')
    if (sidebarBefore.children !== 1) {
      fail(`右サイドバーの中身が AiPanel 1つでない(${sidebarBefore.children}個)`)
    }
    ok('右サイドバーの中身は AiPanel 1つだけ(= 完全にAI専用)')

    await page.click('#sidebar-toggle')
    await waitFor(() => document.querySelector('.sidebar') === null, '開閉ボタンで右サイドバーを畳めない')
    const closed = await page.evaluate(() => ({
      viewport: document.querySelector('.viewport').getBoundingClientRect().width,
      inlineSize: document.querySelector('.viewport').style.width,
    }))
    if (closed.viewport <= sidebarBefore.viewport) {
      fail(`畳んでもビューポートが広がらない: ${sidebarBefore.viewport} → ${closed.viewport}`)
    }
    if (closed.inlineSize !== '') {
      fail(`.viewport にインラインの寸法が付いた(flexの配分だけを変える契約): ${closed.inlineSize}`)
    }
    ok(`右サイドバーを畳んだ(ビューポート幅 ${Math.round(sidebarBefore.viewport)} → ${Math.round(closed.viewport)}px・.viewport の寸法指定は不変)`)

    await page.click('#sidebar-toggle')
    await waitFor(() => document.querySelector('.sidebar') !== null, '開閉ボタンで右サイドバーを戻せない')
    ok('右サイドバーを開き直した')

    // ---- ヘッダーの**右端**にあること(左端のファイル一覧ボタンより右)
    const ends = await page.evaluate(() => {
      const header = document.querySelector('header').getBoundingClientRect()
      const left = document.querySelector('#data-panel-toggle').getBoundingClientRect()
      const right = document.querySelector('#sidebar-toggle').getBoundingClientRect()
      const settings = document.querySelector('#settings-open').getBoundingClientRect()
      return {
        gapLeft: left.left - header.left,
        gapRight: header.right - right.right,
        afterSettings: right.left - settings.right,
      }
    })
    if (ends.gapRight > ends.gapLeft + 4) {
      fail(`開閉ボタンがヘッダーの右端に無い(右の余白 ${ends.gapRight} / 左の余白 ${ends.gapLeft})`)
    }
    if (ends.afterSettings < 0) fail('開閉ボタンが #settings-open より左にある')
    ok('開閉ボタンはヘッダーの右端(#settings-open より後ろ)にある')

    await assertNoAppErrors(page, consoleErrors)
  }
})

function distance(a, b) {
  return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])
}
