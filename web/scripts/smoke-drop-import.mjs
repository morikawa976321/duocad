/** OS 由来のドロップを合成し、本文の送信から既存の取り込み・通知・取り消しまで通す。 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { assertNoAppErrors, openApp, pressUndo, waitFrames, waitOrFail } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'

runSmoke('smoke-drop-import', { timeout: 60000, mcp: true }, async ({ browser, url, mcp, workdir, timeout }) => {
  const { page, consoleErrors } = await openApp(browser, url, { timeout, newDocument: false })
  const wait = (predicate, what, args = []) => waitOrFail(page, predicate, what, { timeout, args })
  let uploads = 0
  let held = null
  let holdUpload = false
  await page.setRequestInterception(true)
  page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/upload') {
      uploads += 1
      if (holdUpload) { held = request; return }
    }
    void request.continue()
  })
  const svg = '<svg xmlns="http://www.w3.org/2000/svg"><rect x="1000" y="2000" width="10" height="20"/><text>skip</text></svg>'
  const send = async (files, events = ['dragenter', 'dragover', 'drop'], text = false, target = 'header') => page.evaluate(({ files, events, text, target }) => {
    const dataTransfer = new DataTransfer()
    if (text) dataTransfer.setData('text/plain', 'selected text')
    else for (const file of files) dataTransfer.items.add(new File([
      file.step ? window.__dropStep : (file.content ?? ''), ' '.repeat(file.padding ?? 0),
    ], file.name))
    return events.map((name) => {
      const event = new DragEvent(name, { dataTransfer, bubbles: true, cancelable: true })
      document.querySelector(target).dispatchEvent(event)
      return event.defaultPrevented
    })
  }, { files, events, text, target })
  const overlay = async (visible) => wait((want) => Boolean(document.querySelector('#file-drop-overlay')) === want,
    `ドロップの覆いが ${visible} にならない`, [visible])
  const toast = async (message) => waitOrFail(page, (text) => document.querySelector('#toasts')?.textContent.includes(text),
    `知らせが出ない: ${message}`, { args: [message], timeout: 10000, dump: () => page.evaluate(() => ({
      toasts: document.querySelector('#toasts')?.textContent,
      open: window.__duocad.state.document_open, errors: [...window.__duocad.errors],
    })) })
  const closeToasts = () => page.evaluate(() => document.querySelectorAll('.toast-close').forEach((button) => button.click()))
  const rejected = async (files, message) => {
    await closeToasts()
    const before = uploads
    const state = await page.evaluate(() => JSON.stringify([window.__duocad.state.log, window.__duocad.parts.length,
      window.__duocad.sketch?.entities.length ?? null]))
    assert.equal((await send(files)).at(-1), true, '拒否したファイルでもページ遷移を止める')
    await toast(message)
    await overlay(false)
    assert.equal(uploads, before, '拒否したファイルを送信しない')
    assert.equal(await page.evaluate(() => JSON.stringify([window.__duocad.state.log, window.__duocad.parts.length,
      window.__duocad.sketch?.entities.length ?? null])), state, '拒否で文書を変えない')
  }

  if (await page.evaluate(() => window.__duocad.state.document_open)) await page.click('#file-close')
  await wait(() => window.__duocad.state.document_open === false, 'トップ画面に戻らない')
  await rejected([{ name: 'top.step' }], '文書を開いていない')
  await page.click('#start-new')
  await wait(() => window.__duocad.state.document_open, '文書が開かない')
  await addExtrudedRect(mcp, { corner: [0, 0], width: 10, height: 20, distance: 5 })
  const fixture = join(workdir, 'fixture.step')
  await mcp.callTool('export_step', { path: fixture })
  await page.evaluate((step) => { window.__dropStep = step }, readFileSync(fixture, 'utf8'))
  await wait(() => window.__duocad.parts.length === 1 && window.__duocad.mode === 'idle', '題材が届かない')
  await waitFrames(page, 4)

  assert.deepEqual(await send([], ['dragenter', 'dragover', 'drop'], true), [false, false, false])
  await overlay(false)
  await send([{ name: 'part.step' }], ['dragenter'])
  await overlay(true)
  await send([{ name: 'part.step' }], ['dragenter', 'dragleave'], false, 'header button')
  await overlay(true)
  await send([{ name: 'part.step' }], ['dragleave'])
  await overlay(false)
  await send([{ name: 'part.step' }], ['dragenter'])
  await overlay(true)
  await page.keyboard.press('Escape')
  await overlay(false)

  await rejected([{ name: 'drawing.svg', content: svg }], 'SVG はスケッチの中でしか')
  await rejected([{ name: 'unknown.txt' }], '1ファイル')
  await rejected([{ name: 'one.step' }, { name: 'two.stp' }], '1ファイル')
  await page.click('#file-import-step')
  await page.type('#file-import-path', 'unfinished')
  await rejected([{ name: 'part.step', step: true }], 'コマンドの途中')
  assert.equal(await page.$eval('#file-import-path', (input) => input.value), 'unfinished')
  await page.keyboard.press('Escape')
  await wait(() => !document.querySelector('#command-abort'), 'コマンドが中断しない')
  await waitFrames(page, 4)

  holdUpload = true
  const beforeUploads = uploads
  await send([{ name: '落とした部品.STP', step: true }])
  await overlay(false)
  await waitOrFail(page, () => Boolean(document.querySelector('.wait-bar')), 'アップロード中の待ち表示が出ない', { timeout })
  assert.ok(held, 'アップロード要求を保持できていない')
  await rejected([{ name: 'duplicate.step', step: true }], '処理中')
  holdUpload = false
  await held.continue()
  held = null
  await wait(() => window.__duocad.parts.length === 2, 'STEP のドロップでボディが増えない')
  await wait(() => document.querySelector('.browser-panel')?.textContent.includes('落とした部品'), '元の名前がブラウザに出ない')
  assert.equal(await page.evaluate(() => window.__duocad.state.log.at(-1).args.source_name), '落とした部品.STP')
  assert.equal(uploads, beforeUploads + 1, '二重に送信された')
  await waitFrames(page, 4)
  await pressUndo(page)
  await wait(() => window.__duocad.parts.length === 1, '1回の Ctrl+Z で取り込みが戻らない')
  await waitFrames(page, 4)

  await send([{ name: 'large.step', step: true, padding: 9 * 1024 * 1024 }])
  await wait(() => window.__duocad.parts.length === 2, '8 MiB を超える STEP が取り込めない')
  await waitFrames(page, 4)
  await pressUndo(page)
  await wait(() => window.__duocad.parts.length === 1, '大きい STEP の取り込みが戻らない')
  await waitFrames(page, 4)

  holdUpload = true
  await send([{ name: 'changed.step', step: true }])
  await wait(() => Boolean(document.querySelector('.wait-bar')), '状態変更の検査で送信が始まらない')
  assert.ok(held)
  await page.click('#sketch-create')
  await wait(() => window.__duocad.mode === 'sketch-pending', '送信中に作業状態が変わらない')
  await page.click('#sketch-exit')
  await wait(() => window.__duocad.mode === 'idle', '送信中の作業状態が戻らない')
  await closeToasts()
  holdUpload = false
  await held.continue()
  held = null
  await toast('送信中に接続・文書・作業状態が変わった')
  assert.equal(await page.evaluate(() => window.__duocad.parts.length), 1)

  const sketch = await mcp.callTool('sketch_create', { base_plane: 'XY' })
  await wait((id) => window.__duocad.activeSketch === id, 'スケッチが開かない', [sketch.sketch_id])
  await waitFrames(page, 4)
  await rejected([{ name: 'part.step', step: true }], 'STEP はスケッチの中では')
  const pose = await page.evaluate(() => window.__duocad.cameraPose().target)
  await send([{ name: 'drawing.svg', content: svg }])
  await wait(() => window.__duocad.sketch?.entities.length === 4, 'SVG の図形が入らない')
  await toast('図形 4 個')
  await toast('取り込めなかった:')
  await wait((before) => window.__duocad.cameraPose().target.some((value, index) => Math.abs(value - before[index]) > 100),
    '取り込んだ図形へ視点が寄らない', [pose])
  await assertNoAppErrors(page, consoleErrors)
})
