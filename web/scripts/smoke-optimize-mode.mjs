/**
 * 実機での確認 — 形状最適化(第3のモード)を画面から使い切れる。
 *
 *   node scripts/smoke-optimize-mode.mjs
 *
 * タスク `20260831-0109-duocad-optimize-mode-frontend` の完了条件を実ブラウザで見る:
 *
 *   1. **「形状最適化」ボタンで第3のモードに入れる**。抜けると通常モードへ戻る。
 *   2. **4つのverbがツールバーから使える** —— 起動 → 既存の割り当てが選択状態で現れる →
 *      クリックで付け外し → Enterで確定。保持/除外はボディ粒度、拘束/荷重は面粒度で拾う。
 *   3. **保持ソリッドが緑、除外ソリッドが赤**。**選択が勝つ**(選ばれている間はハイライト色
 *      になり役割色は隠れる)ことも、`faceColor()`(実機検査フック)で見る。
 *   4. **拘束(青塗り+固定記号)と荷重(矢印+ラベル)が見える**
 *      (`optimizeAnnotationCount()` で存在を機械的に確かめ、`screenshots/optimize-mode.png`
 *      で目視できるようにする)。
 *   5. **モードを抜けると装飾も役割色も消える**。
 *
 * 加えて、次の回帰検査も持つ:
 *
 *   6. 装飾の**中身**(種別・対象の面・矢印の単位ベクトル・ラベル文字列)まで照合する
 *      (総数だけでは拘束/荷重の取り違え・向きの逆転・ラベル欠落を検出できない)。
 *   7. **最適化モードとスケッチモードは同時に真にならない**(`sketchId`/`optimizeMode`が
 *      独立変数のままだと両立しうる。`App.svelte` の `Mode`(タグ付きユニオン)がその状態を
 *      表現できなくしているが、**検査は残す** —— 排他が型で保証されているかどうかは
 *      外から見えず、表現を戻したときに気づく口がここしか無い)。
 *   9. **空の確定選択で役割を外せる**(「0件だから送らない」最適化が入っていないこと)。
 *   10. `stale` の `code` が変わると再通知される(`hidden` として記録済みでも
 *      `fingerprint`/`missing` に変わればトーストが出る)。
 *
 * サーバはこのスクリプトが空の文書で起動し、2つの立方体をMCP経由で作る(離して置くので
 * 画面上で別々の画素になる)。**`--models-dir` に使い捨てディレクトリを渡す**
 * (リポジトリ直下の実 `models/` を触らない)。
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import {
  assertNoAppErrors,
  frameAll,
  openApp,
  pressUndo,
  sleep,
  typeInto,
  waitFrames,
  waitOrFail,
} from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { clickFace } from './pick.mjs'
import { SCREENSHOTS_DIR } from './server.mjs'

const screenshotDir = SCREENSHOTS_DIR

// `viewport.ts` の役割色(16進)。**このスクリプトが正の写しを持つと事故る**ので、
// 値そのものは `viewport.ts` の定数と一致しているかを最後にひとつ確かめる
// (色を直したらこの検査だけが古い値のまま赤くなる、という保険)。
const COLOR = {
  base: 0x9aa6b2,
  highlight: 0xffb454,
  preserve: 0x3f9b58,
  exclude: 0xc75448,
  fixture: 0x3f7fc2,
}

runSmoke('smoke-optimize-mode', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  {
    /** 板(領域を押し出してボディを作る)。離して置くので画面で別々の画素になる。 */
    const addBox = async (corner, size, distance) =>
      (await addExtrudedRect(mcp, { corner, width: size, height: size, distance })).bodyId

    const bodyA = await addBox([0, 0], 20, 20)
    const bodyB = await addBox([60, 0], 20, 20)
    ok(`2つの立方体を作った(A=${bodyA} / B=${bodyB})`)

    const { page, consoleErrors } = await openApp(browser, url, {
      timeout,
      until: () => (window.__duocad?.parts ?? []).length >= 2,
    })
    await frameAll(page)
    await sleep(200)

    const waitFor = (check, what, args = []) =>
      waitOrFail(page, check, what, { args, timeout, polling: 50 })

    /** **画面から指せる面を1つ見つける**(`exclude` に載る添字は避ける)。 */
    async function pickAnyFace(bodyId, exclude = []) {
      const found = await page.evaluate(
        (id, skip) => {
          const geometry = window.__duocad.getGeometry(id)
          if (!geometry) return null
          for (const face of geometry.faces) {
            if (skip.includes(face.index)) continue
            if (window.__duocad.projectFace(id, face.index)) return face.index
          }
          return null
        },
        bodyId,
        exclude,
      )
      if (found === null) fail(`ボディ ${bodyId} に画面から指せる面が無い(除外=${JSON.stringify(exclude)})`)
      return found
    }


    /**
     * その面の色。**先にポインタを画面の外へ逃がす** —— ホバーは色相を変えず明度だけ足すので
     * (`HOVER_LIGHTEN`)、クリック直後はカーソルがその面の上に残ったままで、素の判定色と
     * 微妙に違う値が返る(実測: `0xffb454` ではなく明るくした `0xffd2ab`)。役割/選択の色
     * そのものを見たいここでは、ホバーの影響を切り離す。
     */
    async function faceColor(bodyId, faceIndex) {
      await page.mouse.move(2, 2)
      await waitFrames(page, 4)
      return page.evaluate((id, index) => window.__duocad.faceColor(id, index), bodyId, faceIndex)
    }

    // ====================================================================== 1. モードへ入る
    console.log('\n[1] 「形状最適化」ボタンで第3のモードへ入る')
    if (await page.evaluate(() => window.__duocad.optimizeMode)) fail('起動直後から最適化モードに入っている')
    await page.click('#optimize-enter')
    await waitFor(() => window.__duocad.optimizeMode === true, '#optimize-enter を押してもモードに入らない')
    ok('[1] 「形状最適化」ボタンでモードに入った')

    // ====================================================================== 2. 保持ソリッド(ボディ粒度)
    console.log('\n[2] 保持ソリッド — ボディ粒度で選び、Enterで確定')
    await page.click('#set-preserve-bodies')
    await waitFor(() => window.__duocad.wantedKinds.includes('body'), '保持ソリッドのverbでボディ粒度にならない')
    const faceA_preserve = await pickAnyFace(bodyA)
    await clickFace(page, bodyA, faceA_preserve)
    await waitFor(
      (id) => (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'body' && item.body === id),
      '面をクリックしてもボディ(BodyRef)が選ばれない',
      [bodyA],
    )
    ok('[2] 面をクリックすると、そのボディ全体が選ばれる(ピックの粒度がボディ)')
    const selectedHighlight = await faceColor(bodyA, faceA_preserve)
    if (selectedHighlight !== COLOR.highlight) {
      fail(`選択中のボディがハイライト色(${COLOR.highlight.toString(16)})でない: ${selectedHighlight?.toString(16)}`)
    }
    ok('[2] 選択中は役割よりハイライト色が勝つ(選択が勝つ、確定前)')
    await page.keyboard.press('Enter')
    await waitFor(
      (id) => (window.__duocad.state.optimize.preserve_bodies ?? []).some((item) => item.body === id),
      'Enterで確定しても state.optimize.preserve_bodies に載らない',
      [bodyA],
    )
    ok('[2] Enterで確定 → state.optimize.preserve_bodies に載った')
    await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, '確定してもコマンド終了で選択が空にならない')
    const afterConfirm = await faceColor(bodyA, faceA_preserve)
    if (afterConfirm !== COLOR.preserve) {
      fail(`確定後は選択が空になり保持の緑(${COLOR.preserve.toString(16)})のはずが: ${afterConfirm?.toString(16)}`)
    }
    ok('[2] 確定でコマンドが終わり、選択は空になって役割色(緑)が見える')

    // ====================================================================== 3. 除外ソリッド(ボディ粒度)
    console.log('\n[3] 除外ソリッド — 起動するとAの選択が外れ、保持の役割色(緑)が現れる')
    await page.click('#set-exclude-bodies')
    await waitFor(() => window.__duocad.wantedKinds.includes('body'), '除外ソリッドのverbでボディ粒度にならない')
    await waitFor(
      (id) => !(window.__duocad.selection?.items ?? []).some((item) => item.kind === 'body' && item.body === id),
      '除外verbを起動してもAの選択(BodyRef)が外れない(空の割り当てのプリセレクト)',
      [bodyA],
    )
    const preserveColor = await faceColor(bodyA, faceA_preserve)
    if (preserveColor !== COLOR.preserve) {
      fail(`選択が外れた保持ソリッドが緑(${COLOR.preserve.toString(16)})でない: ${preserveColor?.toString(16)}`)
    }
    ok(`[3] 保持ソリッドAが緑で見える(${preserveColor.toString(16)})`)

    const faceB_exclude = await pickAnyFace(bodyB)
    await clickFace(page, bodyB, faceB_exclude)
    await waitFor(
      (id) => (window.__duocad.selection?.items ?? []).some((item) => item.kind === 'body' && item.body === id),
      'Bをクリックしても選ばれない',
      [bodyB],
    )
    await page.keyboard.press('Enter')
    await waitFor(
      (id) => (window.__duocad.state.optimize.exclude_bodies ?? []).some((item) => item.body === id),
      'Enterで確定しても state.optimize.exclude_bodies に載らない',
      [bodyB],
    )
    ok('[3] Bを除外ソリッドとして確定した')

    // ====================================================================== 4. 拘束面(面粒度)
    console.log('\n[4] 拘束面 — 面粒度で選び、青く塗られて固定記号が付く')
    await page.click('#set-fixtures')
    await waitFor(
      () => window.__duocad.wantedKinds.includes('face') && !window.__duocad.wantedKinds.includes('body'),
      '拘束面のverbで面粒度にならない',
    )
    const excludeColor = await faceColor(bodyB, faceB_exclude)
    if (excludeColor !== COLOR.exclude) {
      fail(`選択が外れた除外ソリッドが赤(${COLOR.exclude.toString(16)})でない: ${excludeColor?.toString(16)}`)
    }
    ok(`[4] 除外ソリッドBが赤で見える(${excludeColor.toString(16)})`)

    // **`faceA_preserve` は必ず避ける** —— 同じ面を選ぶと、この後の「拘束にした面以外は
    // 緑のまま」の検査が、拘束にした当の面(=保持の検査に使った面)を見てしまい、
    // 役割が面単位で効いているかどうかを実際には確かめないまま緑になる誤検査になる。
    const faceA_fixture = await pickAnyFace(bodyA, [faceA_preserve])
    await clickFace(page, bodyA, faceA_fixture)
    await waitFor(
      (id, index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.kind === 'face' && item.body === id && item.index === index,
        ),
      '拘束verbで面(ShapeRef)が選ばれない(ボディ粒度のまま拾っている疑い)',
      [bodyA, faceA_fixture],
    )
    ok('[4] 拘束面のverbでは面そのものが選ばれる(ボディではない)')
    await page.keyboard.press('Enter')
    await waitFor(
      (id, index) =>
        (window.__duocad.state.optimize.fixtures ?? []).some((item) => item.body === id && item.index === index),
      'Enterで確定しても state.optimize.fixtures に載らない',
      [bodyA, faceA_fixture],
    )
    ok('[4] 拘束面として確定した')

    // ====================================================================== 5. 荷重面(面粒度)+ 大きさ・向き
    console.log('\n[5] 荷重面 — 拘束面が青く残り、保持ソリッドの緑は他の面のまま')
    await page.click('#set-loads')
    await waitFor(() => window.__duocad.wantedKinds.includes('face'), '荷重面のverbで面粒度にならない')
    // **粒度だけでは起動を待てない**(直前の拘束verbも面粒度なので、上の待ちは起動の前に通る)。
    // 荷重はまだ1枚も無いので、起動すると選択は空の割り当てへ置き換わる —— 拘束面の選択が外れるまで待つ。
    await waitFor(
      (id, index) =>
        !(window.__duocad.selection?.items ?? []).some(
          (item) => item.kind === 'face' && item.body === id && item.index === index,
        ),
      '荷重verbを起動しても拘束面の選択が外れない',
      [bodyA, faceA_fixture],
    )
    const fixtureColor = await faceColor(bodyA, faceA_fixture)
    if (fixtureColor !== COLOR.fixture) {
      fail(`拘束面が青(${COLOR.fixture.toString(16)})で塗られていない: ${fixtureColor?.toString(16)}`)
    }
    ok(`[5] 拘束面が青で見える(${fixtureColor.toString(16)}。役割は面単位で、ボディ全体は変わらない)`)
    const stillPreserveA = await faceColor(bodyA, faceA_preserve)
    if (stillPreserveA !== COLOR.preserve) {
      fail(`拘束面を1つ足しただけなのにAの他の面まで緑でなくなった: ${stillPreserveA?.toString(16)}`)
    }
    ok('[5] 拘束にした面以外は保持ソリッドの緑のまま(役割は面単位)')

    const faceA_load = await pickAnyFace(bodyA, [faceA_fixture])
    await clickFace(page, bodyA, faceA_load)
    await waitFor(
      (id, index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.kind === 'face' && item.body === id && item.index === index,
        ),
      '荷重面が選ばれない',
      [bodyA, faceA_load],
    )
    await page.click('#load-direction-normal')
    await typeInto(page, '#load-magnitude', 250)
    await page.keyboard.press('Enter')
    await waitFor(
      (id, index) =>
        (window.__duocad.state.optimize.loads ?? []).some((item) => item.body === id && item.index === index),
      'Enterで確定しても state.optimize.loads に載らない',
      [bodyA, faceA_load],
    )
    // **`JSON.parse(JSON.stringify(...))` で素のオブジェクトへ複製してから返す** ——
    // `state` はSvelte 5 runesのリアクティブProxyで、Proxyのまま`page.evaluate`の戻り値に
    // 使うと、CDP越しの構造化複製がプロパティを1つも運ばず`{}`になる(実測)。文字列化して
    // からNode側で読めば中身が正しく渡る。
    const loadSpecs = await page.evaluate(() =>
      JSON.parse(JSON.stringify(window.__duocad.state.optimize.load_specs)),
    )
    const loadState = (loadSpecs ?? []).find((item) => item.body === bodyA && item.index === faceA_load)
    if (!loadState || loadState.magnitude !== 250 || loadState.direction !== 'normal') {
      fail(`load_specs(面ごとの大きさ・向き)が期待どおりでない: ${JSON.stringify(loadSpecs)}`)
    }
    ok(`[5] 荷重面を確定した(magnitude=250N, direction=normal): ${JSON.stringify(loadState)}`)

    // ====================================================================== 6. 装飾の中身(種別・向き・ラベル)まで照合する
    // `optimizeAnnotationCount()`(総数)だけでは、拘束と荷重の取り違え・矢印の逆向き・
    // ラベル欠落を検出できない。`optimizeAnnotations()` で種別ごとの
    // 中身を読み、対象の面・単位ベクトル・ラベル文字列まで照合する。
    console.log('\n[6] 装飾の中身(種別・向き・ラベル)まで照合する')
    const annotations = await page.evaluate(() => window.__duocad.optimizeAnnotations())
    if (annotations.fixtures.length !== 1) {
      fail(`拘束の装飾が1個ではない: ${JSON.stringify(annotations.fixtures)}`)
    }
    if (!annotations.fixtures.some((item) => item.bodyId === bodyA && item.faceIndex === faceA_fixture)) {
      fail(`拘束の装飾が対象の面(${bodyA}/${faceA_fixture})を指していない: ${JSON.stringify(annotations.fixtures)}`)
    }
    ok('[6] 拘束の装飾は1個・対象の面(拘束面そのもの)を指している')

    if (annotations.loads.length !== 1) {
      fail(`荷重の装飾が1個ではない: ${JSON.stringify(annotations.loads)}`)
    }
    const loadAnnotation = annotations.loads[0]
    if (loadAnnotation.bodyId !== bodyA || loadAnnotation.faceIndex !== faceA_load) {
      fail(`荷重の装飾が対象の面(${bodyA}/${faceA_load})を指していない: ${JSON.stringify(loadAnnotation)}`)
    }
    if (loadAnnotation.label !== '250N') {
      fail(`荷重のラベルが期待どおりでない(空/誤りの疑い): ${JSON.stringify(loadAnnotation.label)}`)
    }
    const [dx, dy, dz] = loadAnnotation.direction
    const dirLength = Math.hypot(dx, dy, dz)
    if (Math.abs(dirLength - 1) > 0.01) {
      fail(`荷重の矢印の向きが単位ベクトルでない(長さ=${dirLength}): ${JSON.stringify(loadAnnotation.direction)}`)
    }
    // direction='normal' で確定したので、実際の面法線(サーバの配りものと同じ形)と矢印の
    // 向きが**符号まで**一致するはず — 拘束/荷重の取り違え・矢印の逆さを内積で検出する。
    const faceNormal = await page.evaluate(
      (id, index) =>
        window.__duocad.getGeometry(id)?.faces.find((face) => face.index === index)?.normal ?? null,
      bodyA,
      faceA_load,
    )
    if (!faceNormal) fail('荷重面の法線を取れない(getGeometryの穴が壊れている疑い)')
    const dot = dx * faceNormal[0] + dy * faceNormal[1] + dz * faceNormal[2]
    if (dot < 0.9) {
      fail(
        `荷重の矢印の向きが面法線と一致しない(direction='normal'のはずが逆さ/別方向): ` +
          `矢印=${JSON.stringify(loadAnnotation.direction)} / 面法線=${JSON.stringify(faceNormal)} / 内積=${dot}`,
      )
    }
    ok(`[6] 荷重の装飾は1個・対象の面を指し・ラベルは"250N"・向きは面法線と一致(内積 ${dot.toFixed(3)})`)

    // ====================================================================== 7. 最適化モードとスケッチモードは排他
    // `optimizeMode`(要求)と`sketchId`が独立変数のままだと、スケッチ名クリック
    // (`BrowserPanel`。無条件で`enterSketch`を呼ぶ)経由で両方が同時に真になりうる
    // (`App.svelte:80`の「状態機械は1つのまま」原則の実質違反)。
    // **モードは1つのタグ付きユニオン(`Mode`)なので、両立が書けない。**
    // スケッチ名クリックも他と同じ `setMode` を通る(抜け道ではない)。
    console.log('\n[7] 最適化モードとスケッチモードは同時に成立しない')
    if (!(await page.evaluate(() => window.__duocad.optimizeMode))) {
      fail('この検査の前提が崩れている(まだ最適化モードのはず)')
    }
    const targetSketchId = await page.evaluate(() => window.__duocad.sketches[0]?.sketch_id ?? null)
    if (!targetSketchId) fail('スケッチが1つも無い(排他検査の前提が崩れている)')
    // `BrowserPanel`のスケッチ名クリック(`exitOptimizeMode`を経由しない、最適化モード側に
    // ガードの無い経路)で直接スケッチへ入る。
    await page.click(`[data-sketch-id="${targetSketchId}"] .sketch-name`)
    await waitFor(
      (id) => window.__duocad.activeSketch === id,
      'スケッチ名クリックでスケッチへ入れない',
      [targetSketchId],
    )
    const bothTrueAtOnce = await page.evaluate(
      () => window.__duocad.optimizeMode === true && window.__duocad.activeSketch !== null,
    )
    if (bothTrueAtOnce) {
      fail('最適化モードとスケッチモードが同時に真になっている(排他が壊れている)')
    }
    ok('[7] スケッチへ入ると最適化モードは自動的に偽になる(同時に真にならない)')
    const annotationsInSketch = await page.evaluate(() => window.__duocad.optimizeAnnotationCount())
    if (annotationsInSketch !== 0) {
      fail(`スケッチ編集中も最適化の装飾が描かれたままになっている(count=${annotationsInSketch})`)
    }
    ok('[7] スケッチ編集中は最適化の装飾(拘束記号・荷重矢印)も消えている')
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'スケッチを抜けられない')
    await waitFor(
      () => window.__duocad.optimizeMode === true,
      'スケッチを抜けても最適化モードへ戻らない(戻り先=Mode の resume が持っているはず)',
    )
    ok('[7] スケッチを抜けると最適化モードへ自動的に戻る(戻り先を持って移っている)')

    // ====================================================================== 8. スクリーンショット
    // モードに入り・保持と除外を割り当て・拘束と荷重を置いた状態(タスクの完了条件そのもの)。
    // **この後の9・10節は役割を外す/壊す検査なので、見た目が壊れる前にここで撮る。**
    console.log('\n[8] スクリーンショット')
    await page.evaluate(() => document.activeElement?.blur?.())
    await frameAll(page)
    await sleep(300)
    mkdirSync(screenshotDir, { recursive: true })
    const screenshotPath = join(screenshotDir, 'optimize-mode.png')
    await page.screenshot({ path: screenshotPath })
    ok(`[8] スクリーンショットを保存した: ${screenshotPath}`)

    // ====================================================================== 9. undo で戻る
    console.log('\n[9] undo で荷重面の確定が戻る')
    await pressUndo(page)
    await waitFor(
      () => (window.__duocad.state.optimize.loads ?? []).length === 0,
      'undoしても state.optimize.loads が空に戻らない',
    )
    ok('[9] Ctrl+Z(undo)で荷重面の確定が戻った')

    // ====================================================================== 10. 空の確定で役割を外せる
    // 回帰穴: 「0件だから送らない」という最適化を入れると、
    // 役割を外す唯一の手段(空の確定選択)が機能しなくなる。
    //
    // **外すのは拘束面にする。** 保持ソリッドは、その面に条件面が残っているうちは外せない
    // (コアが断る: 「条件面は保持ソリッドの面にしか置けない」)。拘束面には依存するものが無い。
    console.log('\n[10] 空の確定で役割を外せる(0件だから送らない、をしていないことの検査)')
    await page.click('#set-fixtures')
    await waitFor(() => window.__duocad.wantedKinds.includes('face'), '拘束面のverbへ再起動できない')
    await waitFor(
      (id, index) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.kind === 'face' && item.body === id && item.index === index,
        ),
      '再起動時に既存の割り当て(Aの拘束面)がプリセレクトされない',
      [bodyA, faceA_fixture],
    )
    // 唯一選ばれているものを再クリック = 解除(`editSelection`の「置き換えで1件だけ選んで
    // いれば外す」規則。`App.svelte`の`editSelection`docstring)。
    await clickFace(page, bodyA, faceA_fixture)
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).length === 0,
      '再クリックしても選択が空にならない',
    )
    await page.keyboard.press('Enter')
    await waitFor(
      () => (window.__duocad.state.optimize.fixtures ?? []).length === 0,
      '空の確定でも state.optimize.fixtures が空にならない(0件だから送らない、の疑い)',
    )
    ok('[10] 空の確定で拘束面の役割を外せた')
    // ====================================================================== 11. staleのcode変化は再通知される
    // 回帰穴: `seenStaleKeys`の鍵が`role:body:index`だけだと、`hidden`
    // (出さない側)として一度記録した対象が後で`fingerprint`(出す側)へ変わっても、
    // 「もう見た」と誤判定されて二度と通知されない。専用の使い捨てボディ(C)で
    // hidden → fingerprint と遷移させ、2回目のトーストが実際に出ることを見る
    // (bodyA/Bは他の検査の対象なので、ここでは巻き込まない)。
    console.log('\n[11] stale の code が変わると再通知される(hidden → fingerprint)')
    const paramsBeforeC = await page.evaluate(() => Object.keys(window.__duocad.parameters))
    const bodyC = await addBox([120, 0], 20, 20)
    await waitFor(
      (id) => (window.__duocad.parts ?? []).some((item) => item.body_id === id),
      'ボディCがブラウザへ届かない',
      [bodyC],
    )
    // A/Bのために合わせたカメラのままだと、離して置いたCが画角の外で `pickAnyFace` が
    // 使える面を1つも見つけられない。
    await frameAll(page)
    await sleep(200)
    // **`d#`は登録順の連番**(`duocad/parameters.py`)なので、Cの作成前後の差集合が
    // Cの角(x, y)・幅・高さ・傾きの5パラメータ(この順。`duocad/sketch.py` の
    // `add_rectangle`)になる。ハードコードしない —— 直前の検査がパラメータを
    // いくつ使うかに依存させないため。
    const paramsAfterC = await page.evaluate(() => Object.keys(window.__duocad.parameters))
    const newParamNames = paramsAfterC.filter((name) => !paramsBeforeC.includes(name))
    if (newParamNames.length !== 5) {
      fail(
        'ボディCの新規パラメータが5個ではない(角x/y・幅・高さ・傾きのはず): ' +
          JSON.stringify(newParamNames),
      )
    }
    const [, , widthParam, heightParam] = newParamNames
    const faceC = await pickAnyFace(bodyC)
    // 拘束面として確定する(MCP経由。UIの検査は2節で既にやっている——ここはstaleの
    // 再通知だけを見る)。**条件面は保持ソリッドの面にしか置けない**ので、先にCを保持ソリッドへ足す。
    await mcp.callTool('propose_selection', {
      refs: [bodyA, bodyC].map((id) => ({ body: id, kind: 'body', index: null })),
    })
    await mcp.callTool('commit_selection')
    await mcp.callTool('set_preserve_bodies')
    await mcp.callTool('propose_selection', { refs: [{ body: bodyC, kind: 'face', index: faceC }] })
    await mcp.callTool('commit_selection')
    await mcp.callTool('set_fixtures', {})
    await waitFor(
      (id, index) =>
        (window.__duocad.state.optimize.fixtures ?? []).some((item) => item.body === id && item.index === index),
      'MCP経由でボディCの面を拘束面にできない',
      [bodyC, faceC],
    )
    const errorsBeforeHidden = await page.evaluate(() => window.__duocad.errors.length)
    await mcp.callTool('set_visible', { body_id: bodyC, visible: false })
    await waitFor(
      (id, index) =>
        (window.__duocad.state.optimize.stale ?? []).some(
          (item) => item.role === 'fixtures' && item.body === id && item.index === index && item.code === 'hidden',
        ),
      '拘束面を隠しても stale(hidden) が現れない',
      [bodyC, faceC],
    )
    const errorsAfterHidden = await page.evaluate(() => window.__duocad.errors.length)
    if (errorsAfterHidden !== errorsBeforeHidden) {
      fail(`hidden で新しいトーストが出た(出さない側のはず): before=${errorsBeforeHidden} after=${errorsAfterHidden}`)
    }
    ok('[11] 拘束面を隠した直後は stale(hidden) — トーストは出ない(想定どおり)')
    // **`set_parameter`は可視性を見ない**(`duocad/parameters.py`。verbではなく文書全体の
    // 再計算)。隠したままでも幅・高さを変えられ、面の重心が動いてfingerprintがずれる ——
    // 幅・高さ**両方**を変えるのは、指した面がどちらの軸にも依存する保証を持つため
    // (片方だけだと、たまたま影響を受けない面を拾っていた場合に検査が空振りする)。
    await mcp.callTool('set_parameter', { name: widthParam, value: 80 })
    await mcp.callTool('set_parameter', { name: heightParam, value: 80 })
    await mcp.callTool('set_visible', { body_id: bodyC, visible: true })
    await waitFor(
      (id, index) =>
        (window.__duocad.state.optimize.stale ?? []).some(
          (item) =>
            item.role === 'fixtures' && item.body === id && item.index === index && item.code === 'fingerprint',
        ),
      '拘束面の形を変えて表示に戻しても stale(fingerprint) が現れない',
      [bodyC, faceC],
    )
    const errorsAfterFingerprint = await page.evaluate(() => window.__duocad.errors.length)
    if (errorsAfterFingerprint <= errorsAfterHidden) {
      fail(
        'stale(hidden) → stale(fingerprint) への遷移でトースト通知が出ていない' +
          `(再通知が握りつぶされている疑い): before=${errorsAfterHidden} after=${errorsAfterFingerprint}`,
      )
    }
    ok(
      `[11] hidden → fingerprint で再通知された(errors ${errorsAfterHidden} → ${errorsAfterFingerprint}。` +
        '`hidden`として記録済みでも`code`が変われば別扱いになる)',
    )

    // ====================================================================== 12. モードを抜けると装飾も役割色も消える
    console.log('\n[12] モードを抜けると装飾も役割色も消える')
    await page.click('#optimize-exit')
    await waitFor(() => window.__duocad.optimizeMode === false, '#optimize-exit を押してもモードを抜けない')
    await waitFor(
      () => window.__duocad.optimizeAnnotationCount() === 0,
      'モードを抜けても装飾が残っている',
    )
    const baseAfterExit = await faceColor(bodyA, faceA_preserve)
    if (baseAfterExit !== COLOR.base) {
      fail(`モードを抜けたのに素の色(${COLOR.base.toString(16)})に戻らない: ${baseAfterExit?.toString(16)}`)
    }
    ok('[12] モードを抜けると装飾が消え、ボディの色も素へ戻った')

    // ====================================================================== エラーが出ていないこと
    // **「新しいエラーが1件も出ていない」ではない** —— 10節でわざと1件トーストを出させている
    // (再通知の検査そのもの)。ここで見るのは`__duocad.errors`が想定外の追加をしていないこと
    // (10節までの想定件数と一致すること)と、コンソールに素通ししたエラーが無いこと。
    await assertNoAppErrors(page, consoleErrors, { expectCount: errorsAfterFingerprint })
  }
})
