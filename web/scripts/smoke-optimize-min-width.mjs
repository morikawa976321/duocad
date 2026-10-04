/**
 * 実ブラウザでの検査 — 最小幅の段(m=2,3,…)がボタンと候補として画面に出て、押すと入力欄へ入る。
 *
 *   web> node scripts/smoke-optimize-min-width.mjs
 *
 * 切り上げ・下限割れの**判定と文言**は `tests/eval_min_width.mjs` が持つ。ここが見るのは、段が
 * サーバから届いて DOM に並び、ボタンが入力欄を書き換えることだけ。最適化は走らせない。
 */
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, typeInto, waitOrFail } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addBoxPair, placeConditions } from './optimize-fixture.mjs'

runSmoke('smoke-optimize-min-width', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  const bodies = await addBoxPair(mcp)
  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.parts ?? []).length >= 2,
  })
  await frameAll(page)

  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout, polling: 100 })
  const note = () =>
    page.evaluate(() => document.querySelector('#optimize-min-width-note')?.textContent?.replace(/\s+/g, ' ').trim() ?? '')
  const valueIs = (expected, what) =>
    waitFor((v) => Number(document.querySelector('#optimize-min-width').value) === v, what, [expected])

  await page.click('#optimize-enter')
  await waitFor(() => window.__duocad.optimizeMode === true, 'モードに入らない')
  await placeConditions(mcp, page, bodies)
  await waitFor(() => window.__duocad.optimizeConditions?.ready === true, '条件がそろわない')
  await waitFor(() => Boolean(window.__duocad.optimizeResolution?.steps?.length), '解像度の目盛りが来ない')

  const step = await page.evaluate(() => JSON.parse(JSON.stringify(window.__duocad.optimizeResolution.steps[0])))
  if (!Array.isArray(step.min_width_steps_mm)) fail(`段に min_width_steps_mm が無い: ${JSON.stringify(step)}`)
  const [m2, m3] = step.min_width_steps_mm

  console.log('\n[1] 段のボタンと候補が出て、押すと丸めない値が入る')
  const buttons = await page.$$eval('#optimize-min-width-steps button.step', (els) => els.map((e) => e.textContent.trim()))
  if (buttons.length < 2 || !buttons[0].includes('m=2') || !buttons[1].includes('m=3')) {
    fail(`段のボタンが出ていない: ${JSON.stringify(buttons)}`)
  }
  const listed = await page.$$eval('#optimize-min-width-list option', (els) => els.map((e) => [Number(e.value), e.label]))
  if (listed.length !== step.min_width_steps_mm.length || listed[0][0] !== m2 || listed[listed.length - 1][1] !== `m=${listed.length + 1}`) {
    fail(`入力欄の候補(datalist)が全段になっていない: ${listed.length}件 / 段 ${step.min_width_steps_mm.length}`)
  }
  await page.click('#optimize-min-width-steps button.step:nth-of-type(2)')
  await valueIs(m3, '段のボタンで値が入らない')
  ok(`[1] ${buttons.join(' / ')} …。m=3 のボタン → 入力 ${m3}`)

  console.log('\n[2] 段のあいだの値では「そろえる」「下げる」が出て、押すとその段の値が入る')
  const between = Number(((m2 + m3) / 2 - 0.001).toFixed(3))
  const typeBetween = async () => {
    await typeInto(page, '#optimize-min-width', between)
    await waitFor(
      () => document.querySelector('#optimize-min-width-note')?.textContent.includes('切り上がって'),
      '切り上げの文が出ない',
    )
  }
  await typeBetween()
  await page.click('#optimize-min-width-note button.step:nth-of-type(2)')
  await valueIs(m3, 'そろえるボタンで値が入らない')
  if ((await note()).includes('切り上がって')) fail('そろえたのに警告が残っている')
  await typeBetween()
  await page.click('#optimize-min-width-note button.step:nth-of-type(1)')
  await valueIs(m2, '下げるボタンで値が入らない')
  if ((await note()).includes('切り上がって')) fail('下げたのに警告が残っている')
  ok(`[2] 入力 ${between} → そろえる ${m3} / 下げる ${m2}`)

  // `typeInto` は1文字ずつ打つので、途中の値(「29.999」の「2」)が下限割れになり通知が控えに積まれる。
  // 打った値は固定せず「最小幅 …: …下限…」の形で許可する(下限に触れない別のエラーは赤のまま)。
  const belowFloor = await page.evaluate(() =>
    [...window.__duocad.errors].filter((item) => String(item).startsWith('最小幅 ') && String(item).includes('下限')),
  )
  await assertNoAppErrors(page, consoleErrors, { allow: belowFloor })
})
