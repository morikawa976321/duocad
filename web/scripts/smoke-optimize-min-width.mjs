/**
 * 実ブラウザでの検査 — 最小幅の段(m=2,3,…)が画面に出て、段のあいだの入力の切り上げ先が実行前に見える。
 *
 *   web> node scripts/smoke-optimize-min-width.mjs
 *
 * 最適化は走らせない。サーバは自分で立てる(`--models-dir` は使い捨て)。
 */
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, typeInto, waitOrFail } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { clickFace } from './pick.mjs'

runSmoke('smoke-optimize-min-width', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  {
    const addBox = async (corner, size, distance) =>
      (await addExtrudedRect(mcp, { corner, width: size, height: size, distance })).bodyId
    const bodyA = await addBox([0, 0], 12, 12)
    const bodyB = await addBox([48, 0], 12, 12)

    const { page, consoleErrors } = await openApp(browser, url, {
      timeout,
      until: () => (window.__duocad?.parts ?? []).length >= 2,
    })
    await frameAll(page)

    const waitFor = (check, what, args = []) =>
      waitOrFail(page, check, what, { args, timeout, polling: 100 })
    const pickAnyFace = async (bodyId, exclude = []) => {
      const found = await page.evaluate(
        (id, skip) => {
          const geometry = window.__duocad.getGeometry(id)
          for (const face of geometry?.faces ?? []) {
            if (skip.includes(face.index)) continue
            if (window.__duocad.projectFace(id, face.index)) return face.index
          }
          return null
        },
        bodyId,
        exclude,
      )
      if (found === null) fail(`ボディ ${bodyId} に画面から指せる面が無い`)
      return found
    }
    const text = (selector) =>
      page.evaluate((sel) => document.querySelector(sel)?.textContent?.replace(/\s+/g, ' ').trim() ?? null, selector)
    const inputValue = () => page.evaluate(() => Number(document.querySelector('#optimize-min-width').value))
    const runDisabled = () => page.evaluate(() => document.querySelector('#optimize-run').disabled === true)

    await page.click('#optimize-enter')
    await waitFor(() => window.__duocad.optimizeMode === true, 'モードに入らない')
    await page.click('#set-preserve-bodies')
    await clickFace(page, bodyA, await pickAnyFace(bodyA))
    await clickFace(page, bodyB, await pickAnyFace(bodyB))
    await page.keyboard.press('Enter')
    await waitFor(() => (window.__duocad.state.optimize.preserve_bodies ?? []).length === 2, '保持が2つにならない')
    await page.click('#set-fixtures')
    await clickFace(page, bodyA, await pickAnyFace(bodyA))
    await page.keyboard.press('Enter')
    await waitFor(() => (window.__duocad.state.optimize.fixtures ?? []).length === 1, '拘束面が載らない')
    await page.click('#set-loads')
    await clickFace(page, bodyB, await pickAnyFace(bodyB))
    await typeInto(page, '#load-magnitude', 200)
    await page.click('#load-direction-down')
    await page.keyboard.press('Enter')
    await waitFor(() => window.__duocad.optimizeConditions?.ready === true, '条件がそろわない')
    await waitFor(() => Boolean(window.__duocad.optimizeResolution?.steps?.length), '解像度の目盛りが来ない')

    const step = await page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.optimizeResolution.steps[0])))
    if (!Array.isArray(step.min_width_steps_mm)) fail(`段に min_width_steps_mm が無い: ${JSON.stringify(step)}`)
    const [m2, m3] = step.min_width_steps_mm
    if (m2 !== step.min_width_floor_mm) fail(`段の先頭が下限と一致しない: ${m2} vs ${step.min_width_floor_mm}`)

    console.log('\n[1] 段のボタンが出て、押すと丸めない値が入る')
    const buttons = await page.$$eval('#optimize-min-width-steps button.step', (els) => els.map((e) => e.textContent.trim()))
    if (buttons.length < 2 || !buttons[0].includes('m=2') || !buttons[1].includes('m=3')) {
      fail(`段のボタンが出ていない: ${JSON.stringify(buttons)}`)
    }
    const listed = await page.$$eval('#optimize-min-width-list option', (els) => els.map((e) => [Number(e.value), e.label]))
    if (listed.length !== step.min_width_steps_mm.length || listed[0][0] !== m2 || listed[listed.length - 1][1] !== `m=${listed.length + 1}`) {
      fail(`入力欄の候補(datalist)が全段になっていない: ${listed.length}件 / 段 ${step.min_width_steps_mm.length}`)
    }
    await page.click('#optimize-min-width-steps button.step:nth-of-type(2)')
    await waitFor((v) => Number(document.querySelector('#optimize-min-width').value) === v, '段のボタンで値が入らない', [m3])
    const exact = await text('#optimize-min-width-note')
    if (!exact?.includes('実際に保証される幅') || exact.includes('切り上がって')) fail(`段ちょうどなのに切り上げの警告: ${exact}`)
    ok(`[1] ${buttons.join(' / ')} …。m=3 のボタン → 入力 ${m3}(段ちょうど。警告なし)`)

    console.log('\n[2] 段のあいだの値は、m=3 へ切り上がることが実行前に出る')
    const between = Number(((m2 + m3) / 2 - 0.001).toFixed(3))
    await typeInto(page, '#optimize-min-width', between)
    await waitFor(() => document.querySelector('#optimize-min-width-note')?.textContent.includes('切り上がって'), '切り上げの文が出ない')
    const rounded = await text('#optimize-min-width-note')
    if (!rounded.includes('m=3') || !rounded.includes('太い部品が出る')) fail(`切り上げの文が足りない: ${rounded}`)
    if (await runDisabled()) fail('段のあいだの値は有効な入力なのに実行が押せない')
    ok(`[2] 入力 ${between}mm → 「${rounded}」`)

    console.log('\n[3] 「そろえる」で切り上げ先(m=3)の値、「下げる」でひとつ下(m=2)の値が入り、警告が消える')
    await page.click('#optimize-min-width-note button.step:nth-of-type(2)')
    await waitFor((v) => Number(document.querySelector('#optimize-min-width').value) === v, 'そろえるボタンで値が入らない', [m3])
    if ((await text('#optimize-min-width-note')).includes('切り上がって')) fail('そろえたのに警告が残っている')
    await typeInto(page, '#optimize-min-width', between)
    await waitFor(() => document.querySelector('#optimize-min-width-note')?.textContent.includes('切り上がって'), '切り上げの文が再び出ない')
    await page.click('#optimize-min-width-note button.step:nth-of-type(1)')
    await waitFor((v) => Number(document.querySelector('#optimize-min-width').value) === v, '下げるボタンで値が入らない', [m2])
    const lowered = await text('#optimize-min-width-note')
    if (lowered.includes('切り上がって')) fail(`下げたのに警告が残っている: ${lowered}`)
    if ((await inputValue()) !== m2) fail('入力が m=2 の段の値でない')
    ok(`[3] 入力 ${m2}(m=2)。「${lowered}」`)

    console.log('\n[4] 下限を割る値は下限の文が出て、実行が押せない')
    await typeInto(page, '#optimize-min-width', 1)
    await waitFor(() => document.querySelector('#optimize-min-width-note')?.textContent.includes('下限'), '下限の文が出ない')
    if (!(await runDisabled())) fail('下限割れなのに実行が押せる')
    ok('[4] 下限割れ: 文が出て、実行は押せない')

    await assertNoAppErrors(page, consoleErrors)
  }
})
