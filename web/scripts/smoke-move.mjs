/**
 * 実機での確認 — **スケッチパラメータパネルの後継**が、本物のブラウザで端から端まで動く。
 *
 *   node scripts/smoke-move.mjs
 *
 * 右サイドバーの `SketchPanel.svelte`(数値入力欄・モード表示・「正面から見る」・
 * 「抜ける」)は廃止済みで、位置の打ち直しは**3Dの直接選択 + move verb**に置き換わっている。
 * この置き換えが本当に成立していることを、pytestでは判定できない部分について見る:
 *
 *   1. **マウスの割り当て**: 右ドラッグ=カメラ回転 / 中ドラッグ=パン / 左ドラッグ=ウィンドウ選択
 *   2. **ウィンドウ選択**: 矩形に**完全包含**されたものだけが選ばれる(向きは問わない)
 *   3. **Shift と Ctrl がどちらも同じトグル**(選択済みなら外す・未選択なら足す)
 *   4. **move_sketch_entities**: 点を掴んで動かす / 図形ごと複製する
 *   5. **move_body**: 面をクリックしてボディを動かす / 複製する
 *   6. **モード操作の移設**: `#sketch-mode` / `#sketch-look` / `#sketch-exit` が
 *      **コマンドツールバーの中**にあり、スケッチモード中に機能する
 *   7. **右サイドバーの開閉**: `#sidebar-toggle` で `<aside class="sidebar">` が出入りし、
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

    // ---- 6の一部: モード操作が**ツールバーの中**にある(右サイドバーではない)
    const modeRow = await page.evaluate(() => ({
      inToolbar: Boolean(document.querySelector('.toolbar #sketch-mode')),
      look: Boolean(document.querySelector('.toolbar #sketch-look')),
      exit: Boolean(document.querySelector('.toolbar #sketch-exit')),
      inSidebar: Boolean(document.querySelector('.sidebar #sketch-mode')),
      sketchPanel: Boolean(document.querySelector('.sketch-panel')),
    }))
    if (!modeRow.inToolbar || !modeRow.look || !modeRow.exit) {
      fail(`モード操作がツールバーに無い: ${JSON.stringify(modeRow)}`)
    }
    if (modeRow.inSidebar || modeRow.sketchPanel) {
      fail(`スケッチパネルが右サイドバーに残っている: ${JSON.stringify(modeRow)}`)
    }
    ok('モード表示・正面から見る・抜けるがコマンドツールバー側にある(右サイドバーには無い)')

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

    // ================================================================ 4. Shift / Ctrl のトグル
    await dragBox([LINE.x1 - 4, LINE.y1 - 4], [LINE.x1 + 4, LINE.y1 + 4])
    await waitFor(() => (window.__duocad.selection?.count ?? 0) === 1, '右端を選び直せない')
    const rightEnd = await planeAt(LINE.x1, LINE.y1)
    if (!rightEnd) fail('線分の右端が画面の中に無い')

    await withoutBrowserPanel(page, async () => {
      await page.keyboard.down('Shift')
      await page.mouse.click(rightEnd.x, rightEnd.y)
      await page.keyboard.up('Shift')
    })
    await waitFor(
      () => (window.__duocad.selection?.count ?? -1) === 0,
      'Shift+クリックで選択済みの点を外せない(トグルになっていない)',
    )
    ok('Shift+クリックが選択済みの点を外した(= トグル)')

    await withoutBrowserPanel(page, async () => {
      await page.keyboard.down('Control')
      await page.mouse.click(rightEnd.x, rightEnd.y)
      await page.keyboard.up('Control')
    })
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).some((item) => item.point === 'x1'),
      'Ctrl+クリックで未選択の点を足せない(除外専用のままになっている)',
    )
    ok('Ctrl+クリックが未選択の点を足した(Shiftと同一機能)')

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
    // **選んだのは点1つ(`point:'x1'`)なので、反対の端点は動かない。** 図形ごと平行移動
    // する幽霊を描くと、ここで落ちる(コアは選んだ点だけを動かすので、幽霊と結果が食い違う)。
    if (Math.abs(sketchGhost.params.y0 - before.params.y0) > 1e-9) {
      fail(
        `点1つを選んだのに図形ごと動く幽霊になっている: y0 ${before.params.y0} → ` +
          `${sketchGhost.params.y0}`,
      )
    }
    ok('move_sketch_entities: 確定前に移動先の幽霊が出て、選んだ点だけが dy ぶん動く')

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
    if (Math.abs(moved.params.y0 - before.params.y0) > 1e-6) {
      fail(`点だけを動かしたのに反対の端点(y0)まで動いた: ${moved.params.y0}`)
    }
    ok(
      `move_sketch_entities で選んだ点だけが動いた(y1: ${before.params.y1.toFixed(3)} → ` +
        `${moved.params.y1.toFixed(3)}・y0 は不変)`,
    )

    // **選択は空になる**(他の形状変更verbと同じ既定の契約)
    const afterMoveSelection = await page.evaluate(() =>
      JSON.parse(JSON.stringify(window.__duocad.selection)),
    )
    if (afterMoveSelection.count !== 0) {
      fail(`移動の後も選択が残っている(リセットされていない): ${JSON.stringify(afterMoveSelection)}`)
    }
    ok('移動の後は選択が空になる(次は選び直しになる)')

    // ================================================================ 6. move_sketch_entities(複製)
    // 図形全体を選び直す(線の中ほどをクリック = 点ではなく辺)。
    //
    // **中点は `moved` の実座標から求める**(`LINE.x0/x1` と動かす前の `y0` を混ぜない) ——
    // 検証5でy1だけを+10動かした結果、線分は (60,0)-(80,10) のように傾いている。
    // 傾いた線分の中点は ((x0+x1)/2, (y0+y1)/2) であって、x座標の中点に動かす前のy0を
    // 組み合わせた点ではない(後者は実際の中点から辺の垂直距離で約4.5mmずれ、
    // クリックの当たり半径の外に外れて「辺そのものをクリックで選べない」がタイムアウトする)。
    await withoutBrowserPanel(page, async () => {
      const midU = (moved.params.x0 + moved.params.x1) / 2
      const midV = (moved.params.y0 + moved.params.y1) / 2
      const mid = await planeAt(midU, midV)
      if (!mid) fail('線分の中ほどが画面の中に無い')
      await page.mouse.click(mid.x, mid.y)
    })
    await waitFor(
      () =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.entity !== undefined && item.point === null,
        ),
      '線分そのもの(辺)をクリックで選べない',
    )
    ok('線分そのもの(点ではなく辺)をクリックで選べた')

    const countBeforeCopy = (await sketchNow()).entities.length
    await page.click('#sketch-move')
    await page.click('#sketch-move-copy')
    await page.click('#sketch-move-translate')
    await typeInto(page, '#sketch-move-dx', 0)
    await typeInto(page, '#sketch-move-dy', -20)
    await page.keyboard.press('Enter')
    await waitFor(
      (n) => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === n + 1)
      },
      '複製で図形が増えない',
      [countBeforeCopy],
    )
    const lines = await lineEntity()
    if (lines.length !== 2) fail(`線分が2本になっていない: ${lines.length}`)
    ok(`copy=true で図形が複製された(図形 ${countBeforeCopy} → ${countBeforeCopy + 1}・元も残っている)`)

    // ================================================================ 6c. 回転の幽霊(スケッチ)
    //
    // **`mode="rotate"` の引数はまだUIが組めない**(軸・中心の指し方が未決。
    // `duocad/catalog.py` の note)ので、`CommandDef` には角度の入力欄が無い。
    // ここは `__duocad.showLocalGhost`(実機検査用の書き込み穴)から直接叩いて、
    // **幽霊の側だけ**を確かめる —— 引数の集め方が決まったとき、通る道は同じである。
    {
      /** (x,y) を (cx,cy) まわりに90度(u->v が正)回した [x, y]。 */
      const turn90 = (x, y, cx, cy) => [cx - (y - cy), cy + (x - cx)]

      // ---------------------------------------------------------------- 点だけを回す
      // **`move_sketch_entities` は選んだ点だけを回す**(図形全体ではない。コアの
      // `_rotated_params(point="x1")`)。図形ごと回す幽霊を描くと、ここで落ちる。
      await dragBox([LINE.x1 - 4, moved.params.y1 - 4], [LINE.x1 + 4, moved.params.y1 + 4])
      await waitFor(() => {
        const items = (window.__duocad.selection?.items ?? []).filter(
          (item) => item.entity !== undefined,
        )
        return items.length === 1 && items[0].point === 'x1'
      }, '線分の右端(点x1)だけを選べない(回転の幽霊)')
      {
        const line = (await lineEntity()).find(
          (item) => Math.abs(item.params.y1 - moved.params.y1) < 1e-6,
        )
        if (!line) fail('動かした線分が見つからない(点だけの回転)')
        const pointGhosts = await page.evaluate(() => {
          window.__duocad.showLocalGhost('move_sketch_entities', {
            mode: 'rotate',
            angle: 90,
            center: [0, 0],
            copy: false,
          })
          return JSON.parse(JSON.stringify(window.__duocad.ghostInfo().sketch))
        })
        if (pointGhosts.length !== 1) fail(`点だけの回転の幽霊が1つにならない: ${JSON.stringify(pointGhosts)}`)
        const [wx1, wy1] = turn90(line.params.x1, line.params.y1, 0, 0)
        const wantPoint = { x0: line.params.x0, y0: line.params.y0, x1: wx1, y1: wy1 }
        for (const [key, value] of Object.entries(wantPoint)) {
          if (Math.abs(pointGhosts[0].params[key] - value) > 1e-6) {
            fail(
              `点だけを選んだのに ${key} が合わない: ${pointGhosts[0].params[key]}(期待 ${value})。` +
                `幽霊 ${JSON.stringify(pointGhosts[0].params)}`,
            )
          }
        }
        ok('move_sketch_entities(mode=rotate): 選んだ点だけが回る(図形ごと回さない)')

        // **360の倍数はコアが ValueError**(0度と同じ)。幽霊も描かない。
        const full = await page.evaluate(() => {
          window.__duocad.showLocalGhost('move_sketch_entities', {
            mode: 'rotate',
            angle: 360,
            center: [0, 0],
          })
          return window.__duocad.ghostInfo().sketch.length
        })
        if (full !== 0) fail(`360度の回転で幽霊が出た: ${full}件`)
        ok('move_sketch_entities(mode=rotate): 360度(コアが断る角度)では幽霊を描かない')
      }

      // ---------------------------------------------------------------- 図形まるごと回す
      // 線分そのもの(点ではなく辺)を選ぶ。**辺 = 図形まるごと**なので、コアは
      // `_rotated_params(point=None)` の側を通る。
      //
      // **中点は `moved` の実座標から求める**(検証6と同じ注意 —— 線分は (60,0)-(80,10) と
      // 傾いたままなので、x座標の中点に動かす前のy0を組み合わせると実際の中点から外れる)。
      await withoutBrowserPanel(page, async () => {
        const midU = (moved.params.x0 + moved.params.x1) / 2
        const midV = (moved.params.y0 + moved.params.y1) / 2
        const mid = await planeAt(midU, midV)
        if (!mid) fail('線分の中ほどが画面の中に無い(回転の幽霊)')
        await page.mouse.click(mid.x, mid.y)
      })
      await waitFor(
        () =>
          (window.__duocad.selection?.items ?? []).some(
            (item) => item.entity !== undefined && item.point === null,
          ),
        '回転の幽霊のために線分そのもの(辺)を選べない',
      )
      const picked = await page.evaluate(
        () =>
          (window.__duocad.selection?.items ?? []).find(
            (item) => item.entity !== undefined && item.point === null,
          ).entity,
      )
      const source = (await sketchNow()).entities.find((item) => item.entity_id === picked)
      if (!source || source.kind !== 'line') fail(`選ばれたのが線分ではない: ${JSON.stringify(source)}`)

      // **中心は選んだ線分の始点**にする(その点は動かないので、判定が読みやすい)。
      const center = [source.params.x0, source.params.y0]
      const ghosts = await page.evaluate(
        (uv) => {
          window.__duocad.showLocalGhost('move_sketch_entities', {
            mode: 'rotate',
            angle: 90,
            center: uv,
            copy: false,
          })
          return JSON.parse(JSON.stringify(window.__duocad.ghostInfo().sketch))
        },
        center,
      )
      if (ghosts.length !== 1 || ghosts[0].kind !== 'line') {
        fail(`回転の幽霊が線分1本にならない: ${JSON.stringify(ghosts)}`)
      }
      // 90度(u->v の反時計回りが正)。始点は中心なので動かず、終点は (dx,dy) -> (-dy,dx)。
      const dx = source.params.x1 - center[0]
      const dy = source.params.y1 - center[1]
      const want = { x0: center[0], y0: center[1], x1: center[0] - dy, y1: center[1] + dx }
      for (const [key, value] of Object.entries(want)) {
        if (Math.abs(ghosts[0].params[key] - value) > 1e-6) {
          fail(
            `回転の幽霊の ${key} が合わない: ${ghosts[0].params[key]}(期待 ${value})。` +
              `幽霊 ${JSON.stringify(ghosts[0].params)}`,
          )
        }
      }
      ok('move_sketch_entities(mode=rotate): 線分の幽霊が中心まわりに90度回る')

      // **中心を渡さなければ描かない**(コアの既定「対象の代表点の平均」は写していない ——
      // 規則が2箇所に増えると、片方だけ変わったときに幽霊と確定結果が別の場所へ出る)。
      const noCenter = await page.evaluate(() => {
        window.__duocad.showLocalGhost('move_sketch_entities', { mode: 'rotate', angle: 90 })
        return window.__duocad.ghostInfo().sketch.length
      })
      if (noCenter !== 0) fail(`中心を渡していないのに回転の幽霊が出た: ${noCenter}件`)
      ok('move_sketch_entities(mode=rotate): 中心を渡さなければ幽霊を描かない(重心を近似しない)')

      // ---------------------------------------------------------------- 長方形をまるごと回す
      // 長方形は `params.angle` で向きを持つので、まるごと回しても**長方形1つのまま**である。
      // 幽霊の4隅を、元の4隅を三角関数で回した点と直接突き合わせる(TS側の
      // `rectangleCorners` がコアの `kernel_sketch.rectangle_corners` と同じ規則かを見る)。
      // 長方形をまるごと囲む(**完全包含された図形は `point: null` = 図形まるごと**で
      // 選択に入る。辺の1本をクリックで指す道もあるが、内側の領域と重なって不安定)。
      await dragBox([-5, -5], [PLATE.width + 5, PLATE.height + 5])
      // **選択の正はコア**なので、ドラッグの直後は往復待ちが要る(`waitFor`)。
      await waitFor(() => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        const ids = new Set(
          active.entities.filter((item) => item.kind === 'rectangle').map((item) => item.entity_id),
        )
        // **領域も一緒に囲まれる**(`onBoxSelect` は領域も拾う)が、幽霊の対象になるのは
        // スケッチ図形の参照だけ(`ghostSources`)。見るのはその内訳である。
        const entities = (hook.selection?.items ?? []).filter((item) => item.entity !== undefined)
        return (
          entities.length === 1 && ids.has(entities[0].entity) && entities[0].point === null
        )
      }, '長方形そのもの(図形まるごと)だけを選べない')
      // **元の姿はコアが持っている値を読む**(`PLATE` の名目値ではない ——
      // 板はマウスで置いたので、角の座標は画素の丸めぶんだけ 0 からずれている)。
      const rectSource = await page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return { ...active.entities.find((item) => item.kind === 'rectangle').params }
      })
      const rectGhosts = await page.evaluate(() => {
        window.__duocad.showLocalGhost('move_sketch_entities', {
          mode: 'rotate',
          angle: 30,
          center: [0, 0],
          copy: false,
        })
        return window.__duocad.ghostInfo().sketch.map((item) => ({
          kind: item.kind,
          params: { ...item.params },
        }))
      })
      if (rectGhosts.length !== 1 || rectGhosts[0].kind !== 'rectangle') {
        fail(
          '長方形をまるごと回した幽霊が「長方形1つ」にならない: ' + JSON.stringify(rectGhosts),
        )
      }
      // **4隅はこの検査が自前の三角関数で出す**(製品の `rectangleCorners` を借りると、
      // 同じ間違いを両側で共有して素通りする)。`angle` の値そのものは期待値にしない ——
      // 同じ4隅を張る `angle` と基準の角の組は複数ある。
      const radians = (30 * Math.PI) / 180
      const sourceRad = ((rectSource.angle ?? 0) * Math.PI) / 180
      const wantCorners = [
        [0, 0],
        [rectSource.width, 0],
        [rectSource.width, rectSource.height],
        [0, rectSource.height],
      ]
        .map(([ou, ov]) => [
          rectSource.x + ou * Math.cos(sourceRad) - ov * Math.sin(sourceRad),
          rectSource.y + ou * Math.sin(sourceRad) + ov * Math.cos(sourceRad),
        ])
        .map(([u, v]) => [
          u * Math.cos(radians) - v * Math.sin(radians),
          u * Math.sin(radians) + v * Math.cos(radians),
        ])
      const ghostRad = ((rectGhosts[0].params.angle ?? 0) * Math.PI) / 180
      const got = [
        [0, 0],
        [rectGhosts[0].params.width, 0],
        [rectGhosts[0].params.width, rectGhosts[0].params.height],
        [0, rectGhosts[0].params.height],
      ].map(([ou, ov]) => [
        rectGhosts[0].params.x + ou * Math.cos(ghostRad) - ov * Math.sin(ghostRad),
        rectGhosts[0].params.y + ou * Math.sin(ghostRad) + ov * Math.cos(ghostRad),
      ])
      const offCorner = wantCorners.find(
        (corner) =>
          !got.some(
            (point) =>
              Math.abs(point[0] - corner[0]) < 1e-6 && Math.abs(point[1] - corner[1]) < 1e-6,
          ),
      )
      if (offCorner) {
        fail(
          `回した長方形の幽霊の隅が合わない: 期待 ${JSON.stringify(offCorner)} / ` +
            `幽霊 ${JSON.stringify(got)}`,
        )
      }
      ok('move_sketch_entities(mode=rotate): 長方形をまるごと回した幽霊が長方形のまま4隅も合う')

      // 後片付け —— 幽霊を消し、選択を空へ戻す(以降の節が前提にしている状態)。
      await page.evaluate(() => window.__duocad.showLocalGhost('move_sketch_entities', {}))
      await dragBox([LINE.x0 + 6, LINE.y0 - 3], [LINE.x1 - 6, LINE.y1 + 3])
      await waitFor(
        () => (window.__duocad.selection?.count ?? -1) === 0,
        '回転の幽霊の後で選択を空へ戻せない',
      )
    }

    // ================================================================ 6d. 回転をUIから起動する(スケッチ)
    //
    // **6c との違いは入口である。** あちらは `showLocalGhost` の穴から幽霊の層だけを叩く。
    // ここは**ツールバーのボタンと3Dのクリックだけ**で回す —— 種別(並進/回転)を選び、
    // 中心を既存の点でピックし、角度を打って Enter する。
    {
      await withoutBrowserPanel(page, async () => {
        const midU = (moved.params.x0 + moved.params.x1) / 2
        const midV = (moved.params.y0 + moved.params.y1) / 2
        const mid = await planeAt(midU, midV)
        if (!mid) fail('線分の中ほどが画面の中に無い(UIからの回転)')
        await page.mouse.click(mid.x, mid.y)
      })
      await waitFor(
        () =>
          (window.__duocad.selection?.items ?? []).some(
            (item) => item.entity !== undefined && item.point === null,
          ),
        'UIからの回転のために線分そのものを選べない',
      )
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

      // **幽霊は指した中心に追随する**(参照ではなく解決した数値で描かれている)。
      await waitFor(
        () => window.__duocad.ghostInfo().sketch.length === 1,
        '中心を指しても幽霊が出ない',
      )
      const ghost = await page.evaluate(() =>
        JSON.parse(JSON.stringify(window.__duocad.ghostInfo().sketch[0])),
      )
      const cx = before.params.x0
      const cy = before.params.y0
      const want = {
        x0: cx,
        y0: cy,
        x1: cx - (before.params.y1 - cy),
        y1: cy + (before.params.x1 - cx),
      }
      for (const [key, value] of Object.entries(want)) {
        if (Math.abs(ghost.params[key] - value) > 1e-6) {
          fail(
            `UIから指した中心の幽霊の ${key} が合わない: ${ghost.params[key]}(期待 ${value})。` +
              `幽霊 ${JSON.stringify(ghost.params)}`,
          )
        }
      }
      ok('move_sketch_entities: ピックした点を中心に幽霊が90度回る')

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
    await withoutBrowserPanel(page, async () => {
      const midU = (moved.params.x0 + moved.params.x1) / 2
      const midV = (moved.params.y0 + moved.params.y1) / 2
      const mid = await planeAt(midU, midV)
      if (!mid) fail('線分の中ほどが画面の中に無い')
      await page.mouse.click(mid.x, mid.y)
    })
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
    await clickEntityHandle(page, lines[0].entity_id)
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
    const faceIndex = await pickAnyFace(page, bodyId)
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
    // Escで選択も空になったので、次の節のために面を選び直す。
    await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, 'Escで選択が空にならない')
    await clickFace(page, bodyId, faceIndex)
    await waitFor(
      (id, index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'face' && item.index === index,
        ),
      'Esc後に面を選び直せない',
      [bodyId, faceIndex],
    )

    // ---------------------------------------------------------------- 回転の幽霊(ボディ)
    // **`mode="rotate"` の引数はまだUIが組めない**(軸の指し方が未決。`catalog.py` の note)。
    // 6c と同じく `__duocad.showLocalGhost` の穴から直接叩いて、幽霊の側だけを確かめる。
    // 直前に選び直した面があるので、ここでは同じボディが対象になる。
    {
      // **軸は原点を通さない**(`[10, 0, 0]`)。原点を通す軸だと `T(p)·R·T(-p)` の
      // 平行移動2項が恒等になり、**軸の位置を落とした実装でも通ってしまう**。
      const rotated = await page.evaluate(() => {
        window.__duocad.showLocalGhost('move_body', {
          mode: 'rotate',
          angle: 90,
          axis_point: [10, 0, 0],
          axis_dir: [0, 0, 1],
        })
        return JSON.parse(JSON.stringify(window.__duocad.ghostInfo().bodies))
      })
      if (rotated.length !== 1 || rotated[0].bodyId !== bodyId) {
        fail(`回転の幽霊が出ない: ${JSON.stringify(rotated)}`)
      }
      if (!rotated[0].rotate || rotated[0].rotate.angleDeg !== 90) {
        fail(`幽霊に回転が乗っていない: ${JSON.stringify(rotated[0])}`)
      }
      // **グループに実際に掛かっている行列**を検算する(three.jsは列優先の16要素)。
      // **指示だけを見ても「行列を組む式が正しいか」は分からない**ので、行列そのものを当てる。
      const m = rotated[0].matrix
      if (!Array.isArray(m) || m.length !== 16) fail(`幽霊の行列が読めない: ${JSON.stringify(m)}`)
      const apply = (x, y, z) => [
        m[0] * x + m[4] * y + m[8] * z + m[12],
        m[1] * x + m[5] * y + m[9] * z + m[13],
        m[2] * x + m[6] * y + m[10] * z + m[14],
      ]
      const cases = [
        // 軸上の点は動かない(軸の位置を落とした実装ならここで落ちる)
        [[10, 0, 0], [10, 0, 0]],
        // 軸から +X に10離れた点は、+Y に10離れた位置へ回る
        [[20, 0, 0], [10, 10, 0]],
        // 軸の向き(Z)には動かない
        [[10, 0, 7], [10, 0, 7]],
      ]
      for (const [from, want] of cases) {
        const got = apply(from[0], from[1], from[2])
        if (got.some((value, index) => Math.abs(value - want[index]) > 1e-6)) {
          fail(
            `Z軸(x=10を通る)+90度の行列が違う: ${JSON.stringify(from)} → ${JSON.stringify(got)}` +
              `(期待 ${JSON.stringify(want)})`,
          )
        }
      }
      ok('move_body(mode=rotate): 幽霊のグループに「軸上の点まわりの+90度」の行列が掛かる')

      // **軸をY・角度を負・向きを非単位ベクトルにした場合**も当てる。Z軸+正の角度だけだと
      // 「軸の取り違え」「符号の反転」「正規化の忘れ」が3つとも素通りする。
      // Y軸まわり-90度(右ねじ): 相対 (5,0,0) -> (0,0,5)、相対 (0,0,5) -> (-5,0,0)。
      const other = await page.evaluate(() => {
        window.__duocad.showLocalGhost('move_body', {
          mode: 'rotate',
          angle: -90,
          axis_point: [10, 0, 0],
          axis_dir: [0, 2, 0], // **わざと長さ2**(正規化していなければ回転量が狂う)
        })
        return window.__duocad.ghostInfo().bodies[0]?.matrix ?? null
      })
      if (!Array.isArray(other) || other.length !== 16) fail('Y軸の回転で幽霊の行列が読めない')
      const applyOther = (x, y, z) => [
        other[0] * x + other[4] * y + other[8] * z + other[12],
        other[1] * x + other[5] * y + other[9] * z + other[13],
        other[2] * x + other[6] * y + other[10] * z + other[14],
      ]
      for (const [from, want] of [
        [[15, 0, 0], [10, 0, 5]],
        [[10, 0, 5], [5, 0, 0]],
        [[10, 3, 0], [10, 3, 0]], // 軸上(Y方向)の点は動かない
      ]) {
        const got = applyOther(from[0], from[1], from[2])
        if (got.some((value, index) => Math.abs(value - want[index]) > 1e-6)) {
          fail(
            `Y軸(x=10を通る)-90度の行列が違う: ${JSON.stringify(from)} → ${JSON.stringify(got)}` +
              `(期待 ${JSON.stringify(want)})`,
          )
        }
      }
      ok('move_body(mode=rotate): Y軸・負の角度・非単位の向きでも行列が合う(正規化と符号)')

      // **軸を数値で渡さなければ描かない**(辺への参照も「省略時=重心」もブラウザでは
      // 解けない。近似で置くと幽霊と確定結果が別の場所に出る —— 出さないほうが正しい)。
      const noAxis = await page.evaluate(() => {
        window.__duocad.showLocalGhost('move_body', { mode: 'rotate', angle: 90 })
        return window.__duocad.ghostInfo().bodies.length
      })
      if (noAxis !== 0) fail(`軸を渡していないのに回転の幽霊が出た: ${noAxis}件`)
      ok('move_body(mode=rotate): 軸を数値で渡さなければ幽霊を描かない(重心を近似しない)')

      // **長さ0の軸・360の倍数の角度でも描かない**(向きの無い軸は回転を決められず、
      // 360度はコアが断る)。**平行移動だけ掛けた幽霊を元の位置に出さない**のが要点。
      const degenerate = await page.evaluate(() => {
        const hook = window.__duocad
        hook.showLocalGhost('move_body', {
          mode: 'rotate',
          angle: 90,
          axis_point: [10, 0, 0],
          axis_dir: [0, 0, 0],
        })
        const zeroAxis = hook.ghostInfo().bodies.length
        hook.showLocalGhost('move_body', {
          mode: 'rotate',
          angle: 360,
          axis_point: [10, 0, 0],
          axis_dir: [0, 0, 1],
        })
        return { zeroAxis, fullTurn: hook.ghostInfo().bodies.length }
      })
      if (degenerate.zeroAxis !== 0) fail(`長さ0の軸で幽霊が出た: ${degenerate.zeroAxis}件`)
      if (degenerate.fullTurn !== 0) fail(`360度の回転で幽霊が出た: ${degenerate.fullTurn}件`)
      ok('move_body(mode=rotate): 長さ0の軸・360度では幽霊を描かない')

      await page.evaluate(() => window.__duocad.showLocalGhost('move_body', {}))
      await waitFor(
        () => window.__duocad.ghostInfo().bodies.length === 0,
        '回転の幽霊を消せない',
      )
    }

    // 打ち直して今度は確定する(面は上で選び直してある)
    await page.click('#move-body')
    await page.click('#move-body-move')
    await page.click('#move-body-translate')
    await typeInto(page, '#move-body-dx', 0)
    await typeInto(page, '#move-body-dy', 0)
    await typeInto(page, '#move-body-dz', 25)
    await waitFor(
      () => window.__duocad.ghostInfo().bodies.length === 1,
      '打ち直した move_body の幽霊が出ない',
    )
    await page.keyboard.press('Enter')
    await waitFor(
      (id, z) => {
        const geometry = window.__duocad.getGeometry(id)
        return Boolean(geometry && Math.abs(geometry.bbox.center[2] - (z + 25)) < 0.5)
      },
      'move_body でボディがZ方向へ動かない',
      [bodyId, geometryBefore[2]],
    )
    const volumeAfter = await page.evaluate(
      (id) => window.__duocad.state.bodies.find((item) => item.body_id === id).volume,
      bodyId,
    )
    if (Math.abs(volumeAfter - volumeBefore) > 1e-3) {
      fail(`平行移動で体積が変わった: ${volumeBefore} → ${volumeAfter}`)
    }
    ok(`move_body でボディがZ+25動いた(体積は ${volumeAfter.toFixed(1)} のまま)`)

    const selectionAfterBodyMove = await page.evaluate(() =>
      JSON.parse(JSON.stringify(window.__duocad.selection)),
    )
    if (selectionAfterBodyMove.count !== 0) {
      fail(`ボディの移動の後も選択が残っている(リセットされていない): ${JSON.stringify(selectionAfterBodyMove)}`)
    }
    ok('ボディの移動の後は選択が空になる(次は選び直しになる)')

    // ================================================================ 8b. move_body は辺からも踏める
    //
    // `MOVE_BODY` の選択引数は面/辺/頂点の3種を食う(コアの `require_bodies` が
    // `ShapeRef.body` から逆引きする)。**画面側の配線が本当に届いているか**を見る ——
    // `CommandHost` が主たる種別しか要求しないと、辺のピックが開かないまま
    // 「サーバは受け付けるのに画面から選べない」穴になる(実際そうなっていたことがある)。
    // move_body は確定のたびに選択を空にするので、選択は既に空。
    // そのまま move-body を起動すれば辺のピックが開くはずである。
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

    // 面の選択へ戻す(次の複製は面から踏む)
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Escape')
    await clickFace(page, bodyId, faceIndex)
    await waitFor(
      (id, index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'face' && item.index === index,
        ),
      '面を選び直せない',
      [bodyId, faceIndex],
    )

    // ================================================================ 9. move_body(複製)
    await page.click('#move-body')
    await page.click('#move-body-copy')
    await page.click('#move-body-translate')
    await typeInto(page, '#move-body-dx', 80)
    await typeInto(page, '#move-body-dy', 0)
    await typeInto(page, '#move-body-dz', 0)
    await page.keyboard.press('Enter')
    await waitFor(() => (window.__duocad.parts ?? []).length === 2, 'copy=true でボディが増えない')
    const bodies = await page.evaluate(() => window.__duocad.parts.map((item) => item.body_id))
    const copyId = bodies.find((id) => id !== bodyId)
    await waitFor(
      (id) => Boolean(window.__duocad.getGeometry(id)),
      '複製したボディの形が画面へ届かない',
      [copyId],
    )
    const copyCenter = await page.evaluate((id) => window.__duocad.getGeometry(id).bbox.center, copyId)
    const originalCenter = await page.evaluate((id) => window.__duocad.getGeometry(id).bbox.center, bodyId)
    if (Math.abs(copyCenter[0] - (originalCenter[0] + 80)) > 0.5) {
      fail(`複製の位置がX+80でない: ${JSON.stringify(copyCenter)} / 元 ${JSON.stringify(originalCenter)}`)
    }
    ok(`copy=true でボディが複製された(2個・複製はX+80の位置・元はその場)`)

    // ================================================================ 9b. 回転をUIから起動する(ソリッド)
    //
    // **8の回転の節との違いは入口である。** あちらは `showLocalGhost` の穴から幽霊の層だけを
    // 叩く。ここは**ツールバーのボタンと3Dのクリックだけ**で回す —— 回転を選び、軸にする辺を
    // 3Dでピックし、角度を打って Enter する。**軸の辺は共有選択集合に入らない**ことも見る
    // (入ると `require_bodies` がその辺のボディまで「動かす対象」に数える)。
    {
      await page.evaluate(() => document.activeElement?.blur?.())
      await page.keyboard.press('Escape')
      await frameAll(page)
      const faceForRotate = await pickAnyFace(page, bodyId)
      await clickFace(page, bodyId, faceForRotate)
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

      // **指した辺の実際の座標**(配られた折れ線)。幽霊の軸がこれと一致すること。
      const axisLine = await page.evaluate((index) => {
        const payload = window.__duocad.edges
        const edge = payload?.edges.find((item) => item.index === index)
        if (!edge) return null
        const points = edge.points
        return {
          kind: edge.kind,
          start: points.slice(0, 3),
          end: points.slice(points.length - 3),
        }
      }, axisEdge)
      if (!axisLine || axisLine.kind !== 'line') {
        fail(`軸の辺の折れ線が読めない: ${JSON.stringify(axisLine)}`)
      }

      await typeInto(page, '#move-body-angle', 90)
      await waitFor(
        () => window.__duocad.ghostInfo().bodies.length === 1,
        'UIから軸を指しても回転の幽霊が出ない',
      )
      const ghost = await page.evaluate(() =>
        JSON.parse(JSON.stringify(window.__duocad.ghostInfo().bodies[0])),
      )
      if (!ghost.rotate || ghost.rotate.angleDeg !== 90) {
        fail(`幽霊に回転が乗っていない: ${JSON.stringify(ghost)}`)
      }
      // **参照ではなく数値へ解決してから幽霊へ渡している**ことの検算。
      for (let axis = 0; axis < 3; axis++) {
        if (Math.abs(ghost.rotate.point[axis] - axisLine.start[axis]) > 1e-6) {
          fail(
            `幽霊の軸の位置が指した辺と違う: ${JSON.stringify(ghost.rotate.point)} / ` +
              `辺 ${JSON.stringify(axisLine.start)}`,
          )
        }
        const want = axisLine.end[axis] - axisLine.start[axis]
        if (Math.abs(ghost.rotate.dir[axis] - want) > 1e-6) {
          fail(`幽霊の軸の向きが指した辺と違う: ${JSON.stringify(ghost.rotate.dir)}`)
        }
      }
      ok('move_body: 幽霊の軸がピックした辺の実座標と一致する(参照を数値へ解決している)')

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
      // **座標は、指す頂点ごとに押す直前に控える。** 指した瞬間に待ち受けが降り、候補のマーカーは
      // 消える(出しているのは「待っている間だけ」という規律そのもの)。先にまとめて控えると、
      // 候補が届ききる前の一覧を掴むことがある(実測: 並列で流したとき、指せた頂点が控えに無かった)。
      let centreVertex = null
      let vertexPoint = null
      for (const vertexIndex of [1, 2, 3, 4, 5, 6, 7, 8]) {
        const at = await screenOf(page, 'projectVertex', bodyId, vertexIndex)
        if (!at) continue
        const point = await page.evaluate(
          (id, index) => {
            const marker = (window.__duocad.vertexMarkers ?? []).find(
              (item) => item.bodyId === id && item.vertexIndex === index,
            )
            return marker ? [...marker.point] : null
          },
          bodyId,
          vertexIndex,
        )
        if (point === null) continue // 候補として出ていない頂点は指せない
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
          vertexPoint = point
          break
        } catch {
          // その頂点は取れなかった(裏に回っている)。次を試す
        }
      }
      if (centreVertex === null) fail('回転中心の頂点を1つもクリックで指せない')
      const withCentre = await page.evaluate(() =>
        JSON.parse(JSON.stringify(window.__duocad.ghostInfo().bodies[0])),
      )
      for (let axis = 0; axis < 3; axis++) {
        if (Math.abs(withCentre.rotate.point[axis] - vertexPoint[axis]) > 1e-6) {
          fail(
            '中心を指したのに軸の位置が頂点へ寄っていない: ' +
              `${JSON.stringify(withCentre.rotate.point)} / 頂点 ${JSON.stringify(vertexPoint)}`,
          )
        }
        if (Math.abs(withCentre.rotate.dir[axis] - ghost.rotate.dir[axis]) > 1e-6) {
          fail(`中心を指したら軸の向きまで変わった: ${JSON.stringify(withCentre.rotate.dir)}`)
        }
      }
      ok(`move_body: 中心に頂点 ${centreVertex} を指すと、軸の向きはそのままに位置だけが寄る`)

      // **外すと既定へ戻る**(指したものを取り消す手がある)。
      await page.click('#move-body-center-clear')
      await waitFor(
        (start) => {
          const rotate = window.__duocad.ghostInfo().bodies[0]?.rotate
          return Boolean(rotate) && Math.abs(rotate.point[0] - start[0]) < 1e-6
        },
        '中心を外しても軸の位置が辺へ戻らない',
        [axisLine.start],
      )
      ok('move_body: 指した中心を「外す」と、軸の位置が辺そのものへ戻る')

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

/** 画面から指せる面を1つ見つける(見つからなければ失敗)。 */
async function pickAnyFace(page, bodyId) {
  const found = await page.evaluate((id) => {
    const geometry = window.__duocad.getGeometry(id)
    if (!geometry) return null
    for (const face of geometry.faces) {
      if (window.__duocad.projectFace(id, face.index)) return face.index
    }
    return null
  }, bodyId)
  if (found === null) fail('画面から指せる面が1つも無い')
  return found
}
