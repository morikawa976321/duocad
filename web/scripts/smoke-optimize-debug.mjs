/**
 * 実ブラウザでの**開発用デバッグ表示**の検査(20260905-1700) ——
 * `?debug=space` / `?debug=anchors` / `?voxel=1` が本体で出ることを見る。
 *
 *   web> node scripts/smoke-optimize-debug.mjs
 *
 * **守るべき最重要の性質は「デモ題材に固定されていないこと」**である —— この検査は
 * **その場で押し出して作った箱2つ**(組み込みの合成題材のどれでもない)を題材にする。
 *
 * 見るもの:
 *
 *   1. **フラグ無しの画面では何も描かれていない**(製品の見た目を1画素も変えない)。
 *   2. `?debug=space` —— 塊(設計空間)のボクセルが出る。**実行を押してすぐ**
 *      (`stage:'derive'`)に出る。塊は探索を通して1つなので、走り切った後も同じ塊が残る。
 *   3. `?debug=anchors` —— 拘束/荷重に割り当たった節点が**両方とも**出る。
 *      **球の深度検査が切れている**(切れていないと形状の裏側の節点が見えない ——
 *      「見えているつもりで見えていない」)。`anchorsDepthTest` は**固定と荷重のOR**なので、
 *      片方だけ戻した退化もここで落ちる。
 *   4. `?voxel=1` —— 平滑化する前のボクセルが出て、**結果のメッシュボディは隠れている**
 *      (差し替えずボクセルのまま止める)。**文書は変わっていない**(部品一覧には居る)。
 *   5. ブラウザのコンソールにエラーが出ていない。
 *
 * **サーバはこのスクリプトが自分で立てる**(`--models-dir` に使い捨てを渡すので、
 * リポジトリ直下の実 `models/` は触らない)。**本物のSIMPを1回通す**(実測 60〜120秒) ——
 * 塊も節点も密度場も、走らせなければ1つも生まれない。
 */
import { mkdirSync } from 'node:fs'
import path from 'node:path'

import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, waitOrFail } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addBoxPair, placeConditions } from './optimize-fixture.mjs'

runSmoke('smoke-optimize-debug', { timeout: 60000, mcp: true }, async ({ options, workdir, mcp, browser, url, timeout }) => {
  /** 最適化1回ぶんの待ち(`smoke-optimize.mjs` と同じ理由で既定の60秒では足りない)。 */
  const runTimeout = Number.parseInt(options['run-timeout'] ?? '420000', 10)
  /** 目で見るための控え(`/screenshots/` は `.gitignore` 対象)。**合否には使わない**し、消さない。 */
  const shotDir = options.shots ?? path.join(path.dirname(workdir), 'duocad-debug-shots')
  {
    // ================================================================== 題材(デモではない)
    const bodies = await addBoxPair(mcp)

    /** ページを1枚開いて、共通の穴が立つまで待つ(`query` はデバッグ表示のフラグ)。 */
    async function openPage(query) {
      const opened = await openApp(browser, `${url}${query}`, {
        timeout,
        until: () => (window.__duocad?.parts ?? []).length >= 2,
      })
      await frameAll(opened.page)
      return opened
    }

    // ================================================================== 1. フラグ無し
    console.log('\n[1] フラグ無しの画面には、デバッグ表示が1つも無い')
    {
      const { page, consoleErrors } = await openPage('')
      const flags = await page.evaluate(() => window.__duocad.debugFlags)
      if (flags.space || flags.anchors || flags.voxel) {
        fail(`フラグ無しなのに立っている: ${JSON.stringify(flags)}`)
      }
      const counts = await page.evaluate(() => window.__duocad.optimizeDebugCounts())
      if (counts.space !== 0 || counts.rho !== 0 || counts.fixed !== 0 || counts.load !== 0) {
        fail(`フラグ無しなのに描かれている: ${JSON.stringify(counts)}`)
      }
      if (counts.hiddenBodyId !== null) fail('フラグ無しなのにボディを隠している')
      await assertNoAppErrors(page, consoleErrors)
      ok('[1] `debugFlags` は全部 false・描かれているものは0・隠しているボディも無い')

      // 面を名指すのに画面が持っている形が要るので、この頁が開いているうちに4条件を置く。
      await placeConditions(mcp, page, bodies)
      await page.close()
    }

    // ================================================================== 2〜4. 3つとも立てる
    console.log('\n[2-4] `?debug=space,anchors&voxel=1` で走らせる(本物のSIMP。60〜120秒)')
    const { page, consoleErrors } = await openPage('?debug=space,anchors&voxel=1')
    const flags = await page.evaluate(() => window.__duocad.debugFlags)
    if (!flags.space || !flags.anchors || !flags.voxel) {
      fail(`フラグが読めていない: ${JSON.stringify(flags)}`)
    }

    const waitFor = (check, what, args = [], limit = timeout) =>
      waitOrFail(page, check, what, { args, timeout: limit, polling: 100 })

    await page.click('#optimize-enter')
    await waitFor(() => window.__duocad.optimizeMode === true, 'モードに入らない')
    await waitFor(() => window.__duocad.optimizeConditions?.ready === true, '条件がそろわない')
    await waitFor(
      () => Boolean(window.__duocad.optimizeResolution?.steps?.length),
      '解像度の目盛りが用意されない',
    )
    // **既定の段(いちばん粗い 12mm)では結果が出ない** —— 12mm の立方体が1要素にしかならず、
    // 全材料でも剛体運動を拘束できないと判定される(`smoke-optimize.mjs` の6節と同じ)。1段上げる。
    await page.focus('#optimize-resolution')
    await page.keyboard.press('ArrowRight')
    await waitFor(() => window.__duocad.optimizeStepIndex === 1, 'スライダーを右へ動かしても段が変わらない')

    const partsBefore = await page.evaluate(() => window.__duocad.parts.length)
    await page.click('#optimize-run')
    await waitFor(() => window.__duocad.optimizeRunning === true, '走り出さない')

    // -------------------------------------------------- 2. 塊は**走り切る前に**出る
    await waitFor(
      () => window.__duocad.optimizeDebugCounts().space > 0,
      '塊(?debug=space)が出ない',
      [],
      runTimeout,
    )
    const early = await page.evaluate(() => ({
      counts: window.__duocad.optimizeDebugCounts(),
      running: window.__duocad.optimizeRunning,
    }))
    ok(
      `[2] 塊が ${early.counts.space} 要素で出た(実行中=${early.running}・` +
        `間引き 1/${early.counts.spaceStride})`,
    )

    await waitFor(() => window.__duocad.optimizeRunning === false, '最適化が終わらない', [], runTimeout)
    await waitFor(
      () => (window.__duocad.parts ?? []).some((body) => body.kind === 'mesh'),
      '結果のメッシュボディが部品一覧に出ない',
      [],
      runTimeout,
    )

    const counts = await page.evaluate(() => window.__duocad.optimizeDebugCounts())
    const parts = await page.evaluate(() => window.__duocad.parts.length)

    // -------------------------------------------------- 2(続き). 本番の塊へ置き換わった
    if (!(counts.space > 0)) fail(`走り切ったのに塊が消えている: ${JSON.stringify(counts)}`)
    ok(`[2] 走り切った後も塊が出ている(${counts.space} 要素・間引き 1/${counts.spaceStride})`)

    // -------------------------------------------------- 3. 面アンカー
    if (!(counts.fixed > 0)) fail('拘束面に割り当たった節点が1つも出ていない')
    if (!(counts.load > 0)) fail('荷重面に割り当たった節点が1つも出ていない')
    if (counts.anchorsDepthTest !== false) {
      fail(
        '面アンカーの球の深度検査が切れていない(形状の裏側の節点が見えない=' +
          `「見えているつもりで見えていない」): depthTest=${counts.anchorsDepthTest}`,
      )
    }
    ok(`[3] 節点が出た(固定 ${counts.fixed} / 荷重 ${counts.load})・深度検査は切れている`)

    // -------------------------------------------------- 4. 平滑化前のボクセル
    if (!(counts.rho > 0)) fail('平滑化前のボクセル(?voxel=1)が出ていない')
    if (counts.hiddenBodyId === null) fail('?voxel=1 なのに結果のメッシュボディが隠れていない')
    if (parts !== partsBefore + 1) {
      fail(`結果のボディが文書に入っていない(隠すのは表示だけのはず): ${partsBefore} -> ${parts}`)
    }
    ok(
      `[4] ボクセル ${counts.rho} 個(間引き 1/${counts.rhoStride})・` +
        `結果ボディ ${counts.hiddenBodyId} は表示だけ隠れている(部品一覧には居る: ${parts}件)`,
    )

    // **probe が画面の文字で出していた情報**(面の添字・担当面積・間引く前の総数)が
    // 本体でも読めることを、突き合わせの記録として出す。
    const payloads = await page.evaluate(() => window.__duocad.optimizeDebugPayloads())
    console.log(`  info  ${JSON.stringify(payloads)}`)

    // 目で見るための控えを1枚だけ残す(**合否はここまでの数えで決まっている**。
    // 画像は「色と重なりが読める形になっているか」を人が後から見るためのもの)。
    mkdirSync(shotDir, { recursive: true })
    const shot = path.join(shotDir, 'optimize-debug.png')
    await page.screenshot({ path: shot })
    ok(`控えを撮った: ${shot}`)

    // ================================================================== 5. エラー
    await assertNoAppErrors(page, consoleErrors)
  }
})
