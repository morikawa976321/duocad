/**
 * 実ブラウザでの領域ピック検査 — **領域を押し出せるのはスケッチ編集中だけ**であることの機械検査。
 *
 * 「押し出す」は1つで、スケッチ編集中は閉領域を、通常モードはソリッドの面を選ぶ(通常モードで
 * 領域を指す道は無い)。領域一覧UIは無いので、領域は編集中のスケッチを3Dでクリックして選ぶ。
 *
 *   web> node scripts/smoke-regions.mjs
 *
 * 見るのは4つ(領域が消える変更で選択が空になることは `tests/test_server_selection_reset.py`):
 *
 *   1. **通常モードで領域は指せない** — 領域に当たる画素が1つも無い
 *   2. **通常モードの「押し出す」も領域を要求しない** — 起動しても領域に当たる画素はできない
 *   3. **スケッチ編集中は一巡して形が変わる** — 領域クリック → 距離 → 結合 → Enter で板が生まれ、
 *      確定した操作が「距離10の結合」である
 *   4. **修飾キーの規則が面と同じ** — 置換 / Shift追加 / Ctrl除去(編集中のスケッチの領域)
 *
 * **順序に意味がある**: スケッチ2枚を**ソリッドが1つも無いうちに**作る。基準平面の画素は
 * 「部品に隠れていない所」を探して返すが、**部品の輪郭のすぐ外側**が返ることがあり、
 * クリックが1画素ずれるだけで平面ではなく面に当たる(この検査を書く過程で実際に踏み、
 * 「XY平面に貼ったつもりが板の側面に貼れていた」まま緑になっていた)。
 * `createSketchOnBase` は貼れた平面を毎回検算するので、同じ取り違えは以後**赤で出る**。
 *
 * **サーバはこのスクリプトが自分で立てる**(`web/scripts/server.mjs`)。
 * 既に立っているものへ繋ぐときだけ `--url` を渡す。
 */
import {
  assertNoAppErrors,
  openApp,
  typeInto as typeIntoField,
  waitFrames,
  waitOrFail,
} from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import {
  clickAt as clickAtOf,
  clickRegion as clickRegionOf,
  clickSketchPoint,
  createSketchOnBasePlane,
  regionAt,
  sketchPointAt,
} from './pick.mjs'

/** 板の寸法(mm)。体積の検算に使うので丸い数字にしてある。 */
const PLATE = { width: 40, height: 30, thickness: 10 }
/**
 * 2枚目のスケッチに置く円。**板の footprint (u,v) = (0..40, 0..30) の外**に置く ——
 * 同じXY平面の領域どうしが重なると、レイ距離が同じでどちらが取れるか決まらない
 * (検査を不安定にするだけで、見たい性質はそこではない)。
 */
const DISC = { u: -15, v: 15, radius: 5 }
/**
 * 同じスケッチに置く2つめの円。**1つめと重ねる**(中心の距離15 < 5+13)ので領域は3つに割れ、
 * 半径を1.5へ縮めると重なりが解けて2つに戻る ——「選んでいた領域(添字3)が消える」を
 * パラメータ編集だけで作れる最小の形。検証6が使う。
 *
 * **中心のクリック点は1つめの円の輪郭から10mm以上離す**(実測)。`63a30a7` の
 * ホバー/スナップ導入後、既存図形の輪郭から数mm以内をクリックすると新しい点が
 * その輪郭へ吸い付く(実測: このカメラ距離で輪郭から4mmはスナップし5mmはしない)。
 * 以前の値(輪郭から2mm)は無関係な円へ吸い付いて中心がずれ、検証6が壊れていた。
 */
const DISC_B = { u: -30, v: 15, radius: 13, shrunk: 1.5 }

runSmoke('smoke-regions', { attach: true }, async ({ browser, url, timeout }) => {
  {
    const { page, consoleErrors } = await openApp(browser, url, { timeout, height: 900 })
    if ((await page.evaluate(() => window.__duocad.parts.length)) !== 0) {
      fail('空の文書で起動していない(サーバを起動し直す)')
    }
    ok('空の文書で起動した')

    // ------------------------------------------------------------------ 道具
    const look = async () =>
      JSON.parse(
        await page.evaluate(() => {
          const hook = window.__duocad
          return JSON.stringify({
            bodies: hook.parts.map((body) => ({
              id: body.body_id,
              volume: body.volume,
              faces: body.face_count,
            })),
            activeSketch: hook.activeSketch,
            sketches: hook.sketches.map((item) => ({
              id: item.sketch_id,
              regions: item.regions.map((region) => region.index),
            })),
            wantsRegions: hook.wantsRegions,
            selection: hook.selection?.items ?? [],
            errors: hook.errors,
          })
        }),
      )

    const waitFor = (predicate, what, ...args) =>
      waitOrFail(page, predicate, what, { args, timeout, dump: look })

    /**
     * **描画が落ち着くまで待つ**。コマンドを起動したり引数が1つ埋まったりすると
     * ツールバーの行が変わり、ビューポートの高さ＝射影が動く。射影して即クリックすると
     * 狙いと違う場所を押すので、射影の前に必ず挟む(`pick.mjs` の `settle: true` が同じ2フレーム)。
     */
    const settleLayout = () => waitFrames(page, 2)

    // 指す手は `pick.mjs`(射影は押す直前・レイアウトの2フレーム待ち・パネルは畳む・貼れた
    // 平面の検算)。この本の呼び方(`sketchId` を先に渡す等)に合わせた薄い皮。
    const typeInto = (selector, value) => typeIntoField(page, selector, value)
    const clickAt = (at, modifier = null) => clickAtOf(page, at, { modifier })
    const createSketchOnBase = (name = 'XY') =>
      createSketchOnBasePlane(page, name, { timeout, settle: true, verifyFrame: true })
    const clickPlane = (u, v) => clickSketchPoint(page, u, v, { settle: true })
    /** 領域の画素。指せない状態なら null —— それ自体がゲートの検査になる。 */
    const regionPixel = (index, sketchId = null) => regionAt(page, index, { sketchId })
    const clickRegion = (sketchId, index, modifier = null) =>
      clickRegionOf(page, index, { sketchId, modifier, hidePanel: true, settle: true })
    /** スケッチ平面上の点の画素(スケッチを指定すれば通常モードでも取れる)。 */
    const sketchPixel = (sketchId, u, v) => sketchPointAt(page, u, v, { sketchId, settle: true })

    /** 選択にある領域だけを `sketch:index` の形で並べる。 */
    const pickedRegions = async () =>
      JSON.parse(
        await page.evaluate(() =>
          JSON.stringify(
            (window.__duocad.selection?.items ?? [])
              // **領域だけ**(`sketch_entity` も `sketch` を持つ)
              .filter((item) => 'sketch' in item && !('entity' in item))
              .map((item) => `${item.sketch}:${item.index}`),
          ),
        ),
      )

    // ================================================================ 下ごしらえ: スケッチ2枚
    // **ソリッドが1つも無いうちに作る**(冒頭の注記。基準平面のクリックが部品に取られない)。
    const plateSketch = await createSketchOnBase('XY')
    await page.click('#sketch-rectangle')
    await clickPlane(0, 0)
    await typeInto('#sketch-width', PLATE.width)
    await typeInto('#sketch-height', PLATE.height)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.regions.length === 1)
      },
      '板の領域ができない',
    )
    await page.keyboard.press('Escape')
    await page.click('#sketch-exit')

    const discSketch = await createSketchOnBase('XY')
    await page.click('#sketch-circle')
    await clickPlane(DISC.u, DISC.v)
    await typeInto('#sketch-radius', DISC.radius)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.regions.length === 1)
      },
      '円の領域ができない',
    )
    await page.keyboard.press('Escape')

    // 2つめの円(1つめと重ねる)。**パラメータの名前は打った値から引く** ——
    // `d?` の採番はそれまでの図形の数で決まるので、決め打ちにすると図形を1つ足すたびに壊れる。
    await page.click('#sketch-circle')
    await clickPlane(DISC_B.u, DISC_B.v)
    await typeInto('#sketch-radius', DISC_B.radius)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.regions.length === 3)
      },
      '2つの円が重なって領域が3つにならない',
    )
    await page.keyboard.press('Escape')
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'スケッチモードを抜けられない')
    if (plateSketch === discSketch) fail('スケッチが2枚に分かれていない')
    ok('XY平面にスケッチを2枚(長方形・重なる円2つ=領域3つ)置き、通常モードへ戻った')

    // ============================================== 検証1: 要求していない間は領域を指せない
    if ((await look()).wantsRegions !== false) {
      fail('起動していないのに領域を要求している')
    }
    if ((await regionPixel(1, plateSketch)) !== null || (await regionPixel(1, discSketch)) !== null) {
      fail('領域を要求していないのに、領域へ当たる画素がある(常時ピック可能になっている)')
    }
    ok('**要求していない間は指せない**: 通常モードで領域へ当たる画素が1つも無い')

    // ============================================== 検証2: 通常モードの「押し出す」は領域を要求しない
    await page.click('#extrude')
    await settleLayout()
    if ((await look()).wantsRegions !== false) {
      fail('通常モードの「押し出す」が領域を要求している(面版のはず)')
    }
    if ((await regionPixel(1, plateSketch)) !== null || (await regionPixel(1, discSketch)) !== null) {
      fail('通常モードの「押し出す」を起動したら、領域へ当たる画素ができた')
    }
    await page.keyboard.press('Escape')
    ok('**通常モードの「押し出す」は面を選ぶ**: 起動しても領域は要求されず、指せる画素も無い')

    // ============================================== 検証3: スケッチ編集中に一巡して形が変わる
    await page.click(`[data-sketch-id="${plateSketch}"] .sketch-name`)
    await waitFor((id) => window.__duocad.activeSketch === id, 'plateSketchへ入れない', plateSketch)
    await page.keyboard.press('Escape')
    await page.click('#sketch-look')
    await clickRegion(plateSketch, 1)
    await waitFor(
      (sketch) => {
        const items = window.__duocad.selection?.items ?? []
        return items.length === 1 && items[0].sketch === sketch && items[0].index === 1
      },
      'スケッチ編集中に領域を選べない',
      plateSketch,
    )
    await page.click('#extrude')
    await typeInto('#distance', PLATE.thickness)
    await page.click('#extrude-mode-join')
    await page.keyboard.press('Enter')
    const plateVolume = PLATE.width * PLATE.height * PLATE.thickness
    await waitFor(
      (expected) => {
        const parts = window.__duocad.parts
        return parts.length === 1 && Math.abs(parts[0].volume - expected) < expected * 1e-3
      },
      'スケッチ編集中の「押し出す」で板ができない',
      plateVolume,
    )
    // **送られた操作そのものを見る**。体積だけでは「向きが結合として渡ったか」を分けられない
    // (この文書にはまだソリッドが無いので、cutでも未指定でも同じ体積になりうる)。
    const applied = JSON.parse(
      await page.evaluate(() => {
        const log = window.__duocad.state.log
        return JSON.stringify(log[log.length - 1] ?? null)
      }),
    )
    if (
      !applied ||
      applied.verb !== 'extrude' ||
      applied.args.mode !== 'join' ||
      applied.args.distance !== PLATE.thickness
    ) {
      fail(`確定した操作が「距離10の結合の領域押し出し」ではない: ${JSON.stringify(applied)}`)
    }
    ok(`**一巡して形が変わった**: スケッチ編集中に 領域クリック → 距離 → 結合 → Enter (体積 ${plateVolume})`)
    await page.evaluate(() => document.activeElement?.blur?.())
    await page.keyboard.press('Escape')
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'plateSketchを抜けられない')

    // ============================================== 検証4: 修飾キーの規則が面と同じ
    await page.click(`[data-sketch-id="${discSketch}"] .sketch-name`)
    await waitFor((id) => window.__duocad.activeSketch === id, 'discSketchへ入れない', discSketch)
    await page.keyboard.press('Escape')
    await page.click('#sketch-look')
    await clickRegion(discSketch, 1)
    await waitFor(
      (sketch) => {
        const items = window.__duocad.selection?.items ?? []
        return items.length === 1 && items[0].sketch === sketch && items[0].index === 1
      },
      '素のクリックで領域が選べない',
      discSketch,
    )
    await clickRegion(discSketch, 2, 'Shift')
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).length === 2,
      'Shift+クリックで領域が追加されない',
    )
    const both = await pickedRegions()
    if (!both.includes(`${discSketch}:1`) || !both.includes(`${discSketch}:2`)) {
      fail(`2つの領域が同時に選べない: ${JSON.stringify(both)}`)
    }
    await clickRegion(discSketch, 1, 'Control')
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).length === 1,
      'Ctrl+クリックで領域が外れない',
    )
    const left = await pickedRegions()
    if (left.length !== 1 || left[0] !== `${discSketch}:2`) {
      fail(`Ctrl除去の後に残る領域が違う: ${JSON.stringify(left)}`)
    }
    ok('**修飾キーは面と同じ**: 置換 / Shift追加 / Ctrl除去')

    await page.keyboard.press('Escape')
    await page.click('#sketch-exit')

    // ============================================== 走行中にエラーが出ていないこと
    // **札(DOM)ではなく控えを見る** —— トーストは8秒で消えるので、最後に画面を見ると
    // 途中で出て消えたエラーを取りこぼす(`Toast.svelte` の `errorLog`)。
    await assertNoAppErrors(page, consoleErrors)
  }
})
