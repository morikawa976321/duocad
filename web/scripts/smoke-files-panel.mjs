/**
 * 実ブラウザでの検査 — **ファイル画面・トップ画面・ブラウザの一覧の操作**(別の出力先に組み立てた画面で走らせる)。
 *
 *   web> node scripts/smoke-files-panel.mjs [--web-dist <組み立てた dist>]
 *
 *   1. 書き出し名の欄の既定は今の文書のファイル名(無題から改名しても追う)
 *   2. 同名のファイルへ書き出すと、知らせに「上書きした」が出る
 *   3. 複製の欄の「複製して開く」で、複製先が開く
 *   4. トップ画面を開くと先頭のカードにフォーカスがあり、Enter 1回で開く
 *   5. ブラウザの「モデル」の見出しで一覧が畳まれ、行の ✕ は ✎ から離れている
 */
import { join } from 'node:path'
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, openApp, typeInto, waitOrFail } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'

runSmoke('smoke-files-panel', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout, workdir }) => {
  await addExtrudedRect(mcp, { corner: [0, 0], width: 20, height: 10, distance: 5 })
  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.parts ?? []).length >= 1,
  })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout })

  // ファイル一覧を開く(開閉は前回の状態を覚えているので、閉じているときだけ押す)。
  if ((await page.$('.files-panel')) === null) await page.click('#data-panel-toggle')
  await page.waitForSelector('#file-name-input', { timeout })

  await waitFor(() => window.__duocad.state?.current_name != null, '最初の変更で無題のファイルに名前が付く')
  await typeInto(page, '#file-name-input', 'bracket')
  await page.click('#file-rename')
  await waitFor(() => window.__duocad.state?.current_name === 'bracket.json', 'bracket.json に改名できる')

  const exportValues = () =>
    page.evaluate(() => ({
      step: document.querySelector('#file-export-step-path').value,
      stl: document.querySelector('#file-export-stl-path').value,
    }))
  await waitFor(
    () =>
      document.querySelector('#file-export-step-path')?.value === 'bracket' &&
      document.querySelector('#file-export-stl-path')?.value === 'bracket',
    '書き出し名の欄が今の文書名(bracket)になる',
  )
  const defaults = await exportValues()
  ok(`書き出し名の既定は今の文書のファイル名(STEP=${defaults.step} STL=${defaults.stl})`)

  // 書き出し先は使い捨ての temp の絶対パスにする(相対だとリポジトリ側の export/ に書く)。
  const stepPath = join(workdir, 'overwrite-check')
  await typeInto(page, '#file-export-step-path', stepPath)
  const toasts = () => page.evaluate(() => [...document.querySelectorAll('#toasts .toast-info-text')].map((el) => el.textContent ?? ''))
  await page.click('#file-export-step')
  await waitFor(() => [...document.querySelectorAll('#toasts .toast-info-text')].some((el) => el.textContent.includes('STEPを書き出した')), '1回目の書き出しの知らせが出る')
  if ((await toasts()).some((text) => text.includes('上書きした'))) fail('初めての書き出しに「上書きした」が出ている')
  await page.click('#file-export-step')
  await waitFor(() => [...document.querySelectorAll('#toasts .toast-info-text')].some((el) => el.textContent.includes('上書きした')), '同名への2回目の書き出しに「上書きした」が出る')
  ok('同名のファイルへ書き出すと「上書きした」が出る(初回には出ない)')

  // 複製して開く
  await page.evaluate(() => {
    const row = [...document.querySelectorAll('.files-row')].find((el) => el.querySelector('.files-name')?.textContent === 'bracket.json')
    row.querySelector('.row-action').click()
  })
  await page.waitForSelector('#file-copy-name', { timeout })
  const copyDefault = await page.$eval('#file-copy-name', (el) => el.value)
  if (copyDefault !== 'bracket-copy') fail(`複製先の先埋めが想定と違う: ${copyDefault}`)
  const fit = await page.evaluate(() => {
    const panel = document.querySelector('.files-panel').getBoundingClientRect()
    const cancel = document.querySelector('.copy-row .files-row-cancel').getBoundingClientRect()
    return { over: cancel.right - panel.right, field: document.querySelector('#file-copy-name').getBoundingClientRect().width }
  })
  if (fit.over > 0 || fit.field < 80) fail(`複製の欄の行が収まらない: ${JSON.stringify(fit)}`)
  await page.click('#file-copy-open')
  await waitFor(() => window.__duocad.state?.current_name === 'bracket-copy.json', '「複製して開く」で複製先が開く')
  if ((await page.$('#file-copy-name')) !== null) fail('複製して開いたあとも複製の欄が残っている')
  ok('「複製して開く」で複製先 bracket-copy.json が開く')

  // ブラウザの一覧: 見出しで畳む・✕ は ✎ から離れている
  await waitFor(() => document.querySelectorAll('.parts-row').length >= 1, 'ブラウザにボディの行が出る')
  const gap = await page.evaluate(() => {
    const row = document.querySelector('.parts-row')
    const edit = row.querySelectorAll('.row-action')[0].getBoundingClientRect()
    const del = row.querySelector('.row-delete').getBoundingClientRect()
    return del.left - edit.right
  })
  if (gap < 12 - 0.5) fail(`✕ と ✎ の間が狭い: ${gap}px`)
  await page.click('#parts-group-toggle')
  await waitFor(() => document.querySelector('.parts-list') === null && document.querySelector('#parts-group-toggle').getAttribute('aria-expanded') === 'false', '「モデル」の見出しで一覧が畳まれる')
  await page.click('#parts-group-toggle')
  await waitFor(() => document.querySelector('.parts-list') !== null, 'もう一度押すと一覧が開く')
  ok(`ブラウザの「モデル」を畳める・開ける。✕ と ✎ の間は ${gap}px`)

  // トップ画面: 先頭のカードにフォーカス → Enter で開く
  await page.click('#file-close')
  await page.waitForSelector('#start-recent .start-card', { timeout })
  await waitFor(() => document.activeElement === document.querySelector('#start-recent .start-card'), '先頭のカードにフォーカスがある')
  const firstName = await page.$eval('#start-recent .start-card .start-name', (el) => el.textContent)
  await page.keyboard.press('Enter')
  await waitFor((name) => window.__duocad.state?.document_open === true && window.__duocad.state?.current_name === name, '先頭のカードを Enter で開ける', [firstName])
  ok(`トップ画面の先頭のカード(${firstName})にフォーカスがあり、Enter 1回で開く`)

  await assertNoAppErrors(page, consoleErrors)
})
