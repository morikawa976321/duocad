/**
 * 実ブラウザでの確認 — **コマンド起動中は素のクリックが選択のトグル、終わったら選択は全解除**。
 *
 *   web> node scripts/smoke-command-selection.mjs
 *
 * 見るもの(題材は MCP で作る箱。「押し出す」を使い、面を3枚指す):
 *
 *   1. 無起動の素のクリックは置き換えのまま(A → B で B だけ)
 *   2. 起動しても選択は残る(先に選んでから起動する手)。起動中は素のクリックで
 *      足す・外す。何もない所を素でクリックしても選択は変わらない。Ctrl+クリックも同じトグル
 *   3. Esc で選択が空になり、コマンドは無起動へ戻る
 *   4. 先に選んでから起動 → 中断ボタン(`#command-abort`)でも空になる
 *   5. AIの提案が載っている間は素のクリックを置き換えのまま残す(本人判断待ち)
 */
import { frameAll, openApp, sleep, waitOrFail } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { clickAt, clickFace } from './pick.mjs'

runSmoke('smoke-command-selection', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  {
    const { bodyId } = await addExtrudedRect(mcp, {
      corner: [0, 0],
      width: 40,
      height: 30,
      distance: 20,
      mode: 'join',
    })
    const { page, consoleErrors } = await openApp(browser, url, { timeout })
    await frameAll(page)

    const look = () =>
      page.evaluate(() => ({
        selection: JSON.parse(JSON.stringify(window.__duocad.selection)),
        phase: document.querySelector('.command-host')?.dataset.phase ?? null,
      }))
    const waitFor = (predicate, what, ...args) => waitOrFail(page, predicate, what, { args, timeout, dump: look })
    /** 選択の面の添字が、順不同でこの集合に一致するまで待つ。 */
    const waitForFaces = (indexes, what) =>
      waitFor(
        (expected) => {
          const items = window.__duocad.selection?.items ?? []
          return (
            items.length === expected.length &&
            items.every((item) => item.kind === 'face') &&
            expected.every((index) => items.some((item) => item.index === index))
          )
        },
        what,
        indexes,
      )
    const waitForEmpty = (what) => waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, what)

    await waitFor((id) => window.__duocad.getGeometry(id) !== null, 'ボディの形が届かない', bodyId)
    const reachable = await page.evaluate(
      (id) =>
        window.__duocad
          .getGeometry(id)
          .faces.filter((face) => face.kind === 'plane' && window.__duocad.projectFace(id, face.index) !== null)
          .map((face) => face.index),
      bodyId,
    )
    if (reachable.length < 3) fail(`画面から指せる面が3枚無い: ${JSON.stringify(reachable)}`)
    const [a, b, c] = reachable

    // ================================================================ 1. 無起動の素のクリックは置き換え
    await clickFace(page, bodyId, a, { hidePanel: true })
    await waitForFaces([a], '無起動で面Aを選べない')
    await clickFace(page, bodyId, b, { hidePanel: true })
    await waitForFaces([b], '無起動の素のクリックが置き換えでなくなった(Aが残った)')
    ok('無起動の素のクリックは置き換えのまま')

    // ================================================================ 2. 起動中は素のクリックがトグル
    await page.click('#extrude')
    await waitFor(() => document.querySelector('#command-abort') !== null, '押し出しが起動しない')
    await waitForFaces([b], '起動したら選択が消えた(先に選んでから起動する手が壊れた)')
    ok('起動しても先に選んだ面は残った')

    await clickFace(page, bodyId, a, { hidePanel: true })
    await waitForFaces([a, b], '起動中の素のクリックで面Aが足されない')
    ok('起動中の素のクリックで足せた(Ctrl無し)')

    await clickFace(page, bodyId, a, { hidePanel: true })
    await waitForFaces([b], '起動中に選択済みの面Aを素でクリックしても外れない')
    ok('起動中に選択済みの面を素でクリックしたら外れた')

    // 何もない所(ビューポートの左下隅。箱は中央付近・パネルは左上)。
    const empty = await page.evaluate(() => {
      const box = document.querySelector('.viewport').getBoundingClientRect()
      return { x: box.left + 6, y: box.bottom - 6 }
    })
    const hovered = await page.evaluate(
      (at) => document.elementFromPoint(at.x, at.y)?.tagName ?? null,
      empty,
    )
    if (hovered !== 'CANVAS') fail(`空の画素がキャンバスでない(別の要素が覆っている): ${hovered}`)
    await clickAt(page, empty)
    await sleep(400) // 空振りで選択が飛ぶなら配信が届く時間
    await waitForFaces([b], '起動中に何もない所を素でクリックしたら選択が変わった')
    ok('起動中に何もない所を素でクリックしても選択は変わらない')

    await clickFace(page, bodyId, c, { modifier: 'Control', hidePanel: true })
    await waitForFaces([b, c], '起動中のCtrl+クリックが足さない')
    ok('起動中のCtrl+クリックも同じトグル')

    // ================================================================ 3. Esc で全解除
    await page.keyboard.press('Escape')
    await waitForEmpty('Escで選択が空にならない')
    await waitFor(() => document.querySelector('#command-abort') === null, 'Escで無起動へ戻らない')
    ok('Escで選択が空になり、無起動へ戻った')

    // ================================================================ 4. 先に選んでから起動 → 中断ボタンで全解除
    await clickFace(page, bodyId, a, { hidePanel: true })
    await waitForFaces([a], '無起動で面Aを選べない(2回目)')
    await page.click('#extrude')
    await waitFor(() => document.querySelector('#command-abort') !== null, '押し出しが起動しない(2回目)')
    await waitForFaces([a], '先に選んだ面が起動で消えた(2回目)')
    await page.click('#command-abort')
    await waitForEmpty('中断ボタンで選択が空にならない')
    await waitFor(() => document.querySelector('#command-abort') === null, '中断ボタンで無起動へ戻らない')
    ok('先に選んで起動 → 中断ボタンで選択が空になった')

    // ================================================================ 5. AIの提案が載っている間は置き換えのまま
    await mcp.callTool('propose_selection', {
      refs: [
        { body: bodyId, kind: 'face', index: a },
        { body: bodyId, kind: 'face', index: b },
      ],
    })
    await waitFor(() => window.__duocad.selection?.origin === 'proposed', 'AIの提案が届かない')
    await page.click('#extrude')
    await waitFor(() => document.querySelector('#command-abort') !== null, '押し出しが起動しない(3回目)')
    await clickFace(page, bodyId, c, { hidePanel: true })
    await waitForFaces([c], '提案の上の素のクリックが置き換えでなくなった')
    const origin = await page.evaluate(() => window.__duocad.selection?.origin)
    if (origin !== 'user') fail(`クリックしたのに提案のまま: ${origin}`)
    ok('AIの提案が載っている間の素のクリックは従来どおり置き換えて確定する')
    await page.keyboard.press('Escape')
    await waitForEmpty('Escで選択が空にならない(3回目)')

    if (consoleErrors.length > 0) fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    ok('ブラウザのコンソールにエラーは出なかった')
  }
})
