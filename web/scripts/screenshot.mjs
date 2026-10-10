/**
 * 実機スクリーンショット撮影(before/after比較用)。
 * 「空の文書の初期画面」と「モデルを1つ読み込んだ画面」の2場面を撮る。
 *
 *   web> node scripts/screenshot.mjs --tag before
 *   web> node scripts/screenshot.mjs --tag after
 */
import { mkdirSync } from 'node:fs'
import path from 'node:path'
import { frameAll, openApp, sleep, typeInto } from './browser.mjs'
import { runSmoke } from './harness.mjs'
import { PC_CASE_STEP, PC_CASE_SOLIDS } from './models.mjs'
import { SCREENSHOTS_DIR } from './server.mjs'

runSmoke('screenshot', { timeout: 60000 }, async ({ options, browser, url, timeout }) => {
  const tag = options.tag ?? 'shot'
  const outDir = options.out ?? SCREENSHOTS_DIR
  mkdirSync(outDir, { recursive: true })

  const { page } = await openApp(browser, url, { timeout, watchHttp: false })
  await sleep(300)

  const emptyPath = path.join(outDir, `${tag}-empty.png`)
  await page.screenshot({ path: emptyPath })
  console.log(`  saved: ${emptyPath}`)

  // STEP読み込みはツールバーのコマンド(起動して初めてパス欄が現れる)。
  await page.click('#file-import-step')
  await typeInto(page, '#file-import-path', PC_CASE_STEP, { delay: 20 })
  await page.keyboard.press('Enter')
  await page.waitForFunction(
    (expected) => window.__duocad.parts.length === expected,
    { timeout, polling: 100 },
    PC_CASE_SOLIDS,
  )
  await frameAll(page)
  await sleep(500)

  const modelPath = path.join(outDir, `${tag}-model.png`)
  await page.screenshot({ path: modelPath })
  console.log(`  saved: ${modelPath}`)
})
