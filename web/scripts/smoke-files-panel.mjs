/**
 * 実ブラウザでの検査 — **ファイル画面・トップ画面・ブラウザの一覧の操作**(別の出力先に組み立てた画面で走らせる)。
 *
 *   web> node scripts/smoke-files-panel.mjs [--web-dist <組み立てた dist>]
 *
 *   1. 書き出し名の欄の既定は今の文書のファイル名(無題から改名しても追う)
 *   2. 同名のファイルへ書き出すと、知らせに「上書きした」が出る
 *   3. 複製の欄の「複製して開く」で、複製先が開く
 *   4. トップ画面を開くと先頭のカードにフォーカスがあり、Enter 1回で開く
 *   5. ブラウザの「ボディ」の見出しで一覧が畳まれ、行の ✕ は ✎ から離れている
 */
import { join } from 'node:path'
import { fail, ok } from './args.mjs'
import { assertNoAppErrors, frameAll, openApp, typeInto, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { faceAt, planarFace } from './pick.mjs'

runSmoke('smoke-files-panel', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout, workdir, wsPort }) => {
  await addExtrudedRect(mcp, { corner: [0, 0], width: 20, height: 10, distance: 5 })
  const { page, consoleErrors } = await openApp(browser, url, {
    timeout,
    until: () => (window.__duocad?.parts ?? []).length >= 1,
  })
  const waitFor = (check, what, args = []) => waitOrFail(page, check, what, { args, timeout })
  const regressionFailures = []
  // open_document は MCP に公開されていないため、別接続の操作は WebSocket で送る。
  const openFromOtherClient = async (name) => {
    const socket = new WebSocket(`ws://127.0.0.1:${wsPort}`)
    try {
      await new Promise((resolve, reject) => {
        socket.addEventListener('open', resolve, { once: true })
        socket.addEventListener('error', reject, { once: true })
      })
      socket.send(JSON.stringify({ cmd: 'open_document', name, client_op: 1 }))
      await waitFor((name) => window.__duocad.state.current_name === name, '別接続から文書を開けない', [name])
    } finally {
      socket.close()
    }
  }

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
  await waitFor(() => document.querySelector('.parts-list') === null && document.querySelector('#parts-group-toggle').getAttribute('aria-expanded') === 'false', '「ボディ」の見出しで一覧が畳まれる')
  await page.click('#parts-group-toggle')
  await waitFor(() => document.querySelector('.parts-list') !== null, 'もう一度押すと一覧が開く')
  ok(`ブラウザの「ボディ」を畳める・開ける。✕ と ✎ の間は ${gap}px`)

  const selectionBefore = await page.evaluate(() => JSON.stringify(window.__duocad.selection))
  const hoveredBody = await page.evaluate(() => window.__duocad.parts.find((body) => body.visible !== false).body_id)
  await frameAll(page)
  const top = await planarFace(page, hoveredBody, [0, 0, 1])
  const hoverColor = await withoutBrowserPanel(page, async () => {
    const at = await faceAt(page, hoveredBody, top)
    if (!at) fail('ボディの上面を指せない')
    await page.mouse.move(at.x, at.y)
    await waitFor((id) => window.__duocad.hoveredRef()?.face?.bodyId === id, '画面上のボディがホバーされない', [hoveredBody])
    return page.evaluate(() => window.__duocad.hoveredRef().color)
  })
  await page.hover('.parts-row .parts-name')
  await waitFor((id, color) => window.__duocad.hoveredRef()?.body === id && window.__duocad.hoveredRef()?.color === color, 'ボディの行に乗せても画面のボディがホバー色で強調されない', [hoveredBody, hoverColor])
  if (await page.evaluate(() => JSON.stringify(window.__duocad.selection)) !== selectionBefore) fail('行のホバーで選択が変わった')
  await page.hover('.browser-header')
  await waitFor(() => window.__duocad.hoveredRef()?.body == null, 'ボディの行から離れても強調が残る')
  await page.hover('.parts-row .parts-name')
  await waitFor((id) => window.__duocad.hoveredRef()?.body === id, '再び行に乗せても強調されない', [hoveredBody])
  await page.evaluate(() => document.querySelector('#parts-group-toggle').click())
  await waitFor(() => window.__duocad.hoveredRef()?.body == null, '一覧を畳んでも強調が残る')
  await page.click('#parts-group-toggle')
  await mcp.callTool('begin_command', { verb: 'fillet' })
  await waitFor(() => window.__duocad.command?.verb === 'fillet', 'フィレットが起動しない')
  await page.hover('.parts-row .parts-name')
  if (await page.evaluate(() => window.__duocad.hoveredRef()?.body != null)) fail('コマンド起動中に行の強調が付いた')
  await mcp.callTool('end_command')
  await waitFor(() => window.__duocad.command === null, 'フィレットが終了しない')
  await page.hover('.browser-header')
  await page.click('.parts-row input[type="checkbox"]')
  await waitFor(() => document.querySelector('.parts-row')?.classList.contains('hidden-row'), 'ボディが非表示にならない')
  await page.hover('.parts-row .parts-name')
  if (await page.evaluate(() => window.__duocad.hoveredRef()?.body != null)) fail('非表示の行に強調が付いた')
  await page.click('.parts-row input[type="checkbox"]')
  await waitFor(() => !document.querySelector('.parts-row')?.classList.contains('hidden-row'), 'ボディが再表示されない')
  await page.hover('.parts-row .parts-name')
  await page.evaluate(() => document.querySelector('#browser-collapse').click())
  await waitFor(() => window.__duocad.hoveredRef()?.body == null, 'パネルを畳んでも強調が残る')
  await page.click('#browser-collapse')
  await page.hover('.parts-row .parts-name')
  await page.evaluate(() => document.querySelector('.parts-row .row-delete').click())
  await waitFor(() => document.querySelector('.parts-row') === null && window.__duocad.hoveredRef()?.body == null, '行を削除しても強調が残る')
  await mcp.callTool('undo')
  await waitFor(() => document.querySelector('.parts-row') !== null, '削除したボディを戻せない')
  ok('ボディの行のホバーで強調し、離脱・折りたたみで消える。選択は変わらず、非表示・起動中は抑止する')

  // トップ画面: 先頭のカードにフォーカス → Enter で開く
  await page.hover('.parts-row .parts-name')
  await waitFor((id) => window.__duocad.hoveredRef()?.body === id, '文書を閉じる前の強調が付かない', [hoveredBody])
  await page.evaluate(() => document.querySelector('#file-close').click())
  await waitFor(() => window.__duocad.hoveredRef()?.body == null, '文書を閉じても強調が残る')
  await page.waitForSelector('#start-recent .start-card', { timeout })
  await waitFor(() => document.activeElement === document.querySelector('#start-recent .start-card'), '先頭のカードにフォーカスがある')
  const firstName = await page.$eval('#start-recent .start-card .start-name', (el) => el.textContent)
  await page.keyboard.press('Enter')
  await waitFor((name) => window.__duocad.state?.document_open === true && window.__duocad.state?.current_name === name, '先頭のカードを Enter で開ける', [firstName])
  ok(`トップ画面の先頭のカード(${firstName})にフォーカスがあり、Enter 1回で開く`)

  const bodyBeforeRename = await page.evaluate(() => ({ ...window.__duocad.state.bodies[0] }))
  const otherName = firstName === 'bracket.json' ? 'bracket-copy.json' : 'bracket.json'
  await page.click('.parts-row .row-action')
  await typeInto(page, '.parts-row .rename-input', 'uncommitted-body-name')
  await openFromOtherClient(otherName)
  await waitFrames(page, 2)
  const switchedDocument = await mcp.callTool('describe_document')
  const switchedBody = switchedDocument.bodies.find((body) => body.body_id === bodyBeforeRename.body_id)
  if (!switchedBody) fail('複製先に同じ ID のボディがない')
  if (switchedBody.name !== bodyBeforeRename.name) {
    regressionFailures.push(`文書の切り替えで改名の下書きが送られた: ${switchedBody.name}`)
  } else {
    ok('改名の下書きがあっても、別接続から開いた同じボディ ID の文書の名前は変わらない')
  }
  if (await page.$('.parts-row .rename-input')) await page.keyboard.press('Escape')
  await page.hover('.parts-row .parts-name')
  await openFromOtherClient(firstName)
  await waitFor((id, color) => window.__duocad.hoveredRef()?.body === id && window.__duocad.hoveredRef()?.color === color, '同じ ID の行の上で文書を切り替えたら強調が消えた', [hoveredBody, hoverColor])
  await page.hover('.browser-header')
  await openFromOtherClient(otherName)
  await waitFrames(page, 2)
  if (await page.evaluate(() => window.__duocad.hoveredRef()?.body != null)) fail('行から離れて文書を切り替えても強調が残る')
  ok('文書を切り替えても、同じ ID の行の上だけ強調し、行から離れていれば強調しない')

  const checkNewFocus = async (button) => {
    const generation = await page.evaluate(() => window.__duocad.state.generation)
    await page.click(button)
    await waitFor((before) => window.__duocad.state.generation > before && window.__duocad.state.document_open, '新規の文書にならない', [generation])
    await waitFor(() => document.activeElement?.id === 'file-name-input', '新規のあと名前の欄へフォーカスが移らない')
    const field = await page.$eval('#file-name-input', (el) => ({ value: el.value, start: el.selectionStart, end: el.selectionEnd }))
    if (field.start !== 0 || field.end !== field.value.length) fail(`新規の名前欄が全選択になっていない: ${JSON.stringify(field)}`)
  }
  if ((await page.$('#ai-message')) === null) await page.click('#sidebar-toggle')
  await page.waitForFunction(() => !document.querySelector('#ai-message')?.disabled, { timeout })
  const generationBeforeTyping = await page.evaluate(() => {
    const generation = window.__duocad.state.generation
    document.querySelector('#file-new').click()
    document.querySelector('#ai-message').focus()
    return generation
  })
  await waitFor((before) => window.__duocad.state.generation > before && window.__duocad.state.document_open, '入力中の新規の文書にならない', [generationBeforeTyping])
  await waitFrames(page, 2)
  if (await page.evaluate(() => document.activeElement?.id !== 'ai-message')) {
    regressionFailures.push('新規の応答が別の入力欄からフォーカスを奪った')
  } else {
    ok('新規の応答を待つ間に移った AI の入力欄からフォーカスを奪わない')
  }
  await checkNewFocus('#file-new')
  await page.keyboard.type('new-part')
  await page.keyboard.press('Escape')
  if (await page.$eval('#file-name-input', (el) => el.value) !== '') fail('Esc で新規の名前の下書きを取り消せない')
  await page.keyboard.type('new-part')
  await page.keyboard.press('Enter')
  await waitFor(() => window.__duocad.state.current_name === 'new-part.json', 'Enter で新規の名前を確定できない')
  await page.click('#file-close')
  await page.waitForSelector('#start-new')
  await checkNewFocus('#start-new')
  await page.click('#file-close')
  await page.click('#data-panel-toggle')
  await page.click('#start-new')
  await waitFor(() => window.__duocad.state.document_open, 'パネルを閉じたトップ画面から新規にできない')
  if (await page.$('#file-name-input')) fail('トップ画面からの新規で閉じていたファイルパネルが開いた')
  ok('パネルとトップ画面の新規後は見えている名前欄へフォーカスし、Enter・Esc が働く。閉じたパネルは開かない')

  await assertNoAppErrors(page, consoleErrors)
  if (regressionFailures.length) fail(regressionFailures.join('\n'))
})
