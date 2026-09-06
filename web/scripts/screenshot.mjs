/**
 * 区画・境界の視認性タスク用の実機スクリーンショット撮影。
 * 「空の文書の初期画面」と「モデルを1つ読み込んだ画面」の2場面を撮る
 * (before/after比較の材料。本人が目で見て良し悪しを決める)。
 *
 *   web> node scripts/screenshot.mjs --tag before
 *   web> node scripts/screenshot.mjs --tag after
 *
 * サーバは `--models-dir` に使い捨てのtempディレクトリを渡す
 * (渡し忘れるとリポジトリ直下の実 models/ を触ってしまう)。
 */
import { mkdtempSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { findBrowser, launchBrowser, frameAll } from './browser.mjs'
import { parseArgs } from './args.mjs'
import { startServer } from './server.mjs'
import { PC_CASE_STEP, PC_CASE_SOLIDS } from './models.mjs'

const options = parseArgs(process.argv.slice(2))
const tag = options.tag ?? 'shot'
const outDir = options.out ?? 'C:\\Users\\tm976\\mf\\duocad\\screenshots'
const wsPort = options.port ?? '8785'
const httpPort = options['http-port'] ?? '8786'

mkdirSync(outDir, { recursive: true })
const modelsDir = mkdtempSync(path.join(tmpdir(), 'duocad-screenshot-'))

async function main() {
  const executablePath = findBrowser()
  const server = await startServer({ wsPort, httpPort, modelsDir })
  const browser = await launchBrowser(executablePath)
  try {
    const page = await browser.newPage()
    await page.setViewport({ width: 1280, height: 860 })
    await page.goto(server.url, { waitUntil: 'load', timeout: 30000 })
    await page.waitForFunction(() => Boolean(window.__duocad && window.__duocad.ready), {
      timeout: 30000,
      polling: 100,
    })
    await new Promise((resolve) => setTimeout(resolve, 300))

    const emptyPath = path.join(outDir, `${tag}-empty.png`)
    await page.screenshot({ path: emptyPath })
    console.log(`  saved: ${emptyPath}`)

    async function typeInto(selector, value) {
      await page.click(selector)
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyA')
      await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await page.type(selector, String(value), { delay: 20 })
    }

    // **STEP読み込みはツールバーのコマンド**(起動して初めてパス欄が現れる。
    // 詳細は `smoke-files.mjs` 参照)。
    await page.click('#file-import-step')
    await typeInto('#file-import-path', PC_CASE_STEP)
    await page.keyboard.press('Enter')
    await page.waitForFunction(
      (expected) => window.__duocad.parts.length === expected,
      { timeout: 60000, polling: 100 },
      PC_CASE_SOLIDS,
    )
    await frameAll(page)
    await new Promise((resolve) => setTimeout(resolve, 500))

    const modelPath = path.join(outDir, `${tag}-model.png`)
    await page.screenshot({ path: modelPath })
    console.log(`  saved: ${modelPath}`)
  } finally {
    await browser.close()
    await server.stop()
  }
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
