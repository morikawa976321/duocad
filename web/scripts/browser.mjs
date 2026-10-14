/**
 * 検査に使うブラウザの選択と起動(インストール済みのブラウザを使う。puppeteer-core なのでダウンロードしない)。
 *
 * WebGLは実GPU(ANGLE/D3D11)で描く。`--enable-unsafe-swiftshader` はソフトGLへ落ちてよいという
 * 許可で、強制ではない。強制して直るのはCPU消費だけで検査の速さではない(実測: `smoke.mjs` 1本で
 * chromeのCPU 1550秒 -> 215秒、所要時間 163秒 -> 150秒。律速はOCCTとWS往復)。
 * どちらで描いたかは起動時の `GL :` 行に出る。
 */
import { existsSync } from 'node:fs'
import { launch } from 'puppeteer-core'
import { fail, ok } from './args.mjs'

/** Chromeが先: この環境の Edge 151 は headless で起動できず `Code: 0` で即終了する(duocad側の問題ではない)。 */
export const CANDIDATE_BROWSERS = [
  process.env.DUOCAD_BROWSER,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].filter(Boolean)

/** 最初に見つかった実行ファイル。無ければ例外。 */
export function findBrowser() {
  const found = CANDIDATE_BROWSERS.find((candidate) => existsSync(candidate))
  if (!found) {
    throw new Error(
      `ブラウザが見つからない(DUOCAD_BROWSER で指定する): ${CANDIDATE_BROWSERS.join(', ')}`,
    )
  }
  return found
}

export async function launchBrowser(executablePath = findBrowser()) {
  const browser = await launch({
    executablePath,
    headless: true,
    args: [
      '--headless=new',
      '--use-gl=angle',
      '--enable-unsafe-swiftshader',
      '--disable-gpu-sandbox',
      '--window-size=1280,860',
    ],
  })
  console.log(`  GL     : ${await glRenderer(browser)}`)
  return browser
}

/** 実際に描いたGLの実装名。ソフトGLへ落ちても検査は通ってしまうので毎回出す。取得に失敗しても検査は続ける。 */
async function glRenderer(browser) {
  const page = await browser.newPage()
  try {
    return await page.evaluate(() => {
      const gl = document.createElement('canvas').getContext('webgl2')
      if (gl === null) return 'WebGLコンテキストを作れない'
      const info = gl.getExtension('WEBGL_debug_renderer_info')
      return info === null
        ? gl.getParameter(gl.RENDERER)
        : gl.getParameter(info.UNMASKED_RENDERER_WEBGL)
    })
  } catch (error) {
    return `取得できない(${String(error)})`
  } finally {
    await page.close()
  }
}

/**
 * 浮いているブラウザ(`BrowserPanel.svelte`)を畳んだ状態で `run` を走らせ、終わったら元に戻す。
 *
 * パネルは `.viewport` の左上に重なり、その下の画素は押せない。`viewport.ts` の `clickable` が
 * 覆われた画素を候補から落とすので、候補を複数持つ `project*` は別の画素へ逃げるが、
 * `projectSketchPoint` は (u,v) が1画素しかないので逃げ場が無い。畳むのはその1点を押す間だけにする
 * (一覧そのものを触る検査は開いたままの画面を使う)。パネルが無い画面ではそのまま `run` を走らせる。
 */
export async function withoutBrowserPanel(page, run) {
  const changed = await setBrowserPanel(page, false)
  try {
    return await run()
  } finally {
    if (changed) await setBrowserPanel(page, true)
  }
}

/**
 * undo のキー(Ctrl+Z)を送る(画面にボタンは無く、`App.svelte` の `onWindowKeydown` が拾うキー)。
 * 先にフォーカスを外す: INPUT/TEXTAREA にフォーカスが残っていると `onWindowKeydown` は文書の
 * undo を発火しない(意図どおり。`smoke.mjs`「入力欄にフォーカスがある状態のCtrl+Z」)。
 * Enterで確定した直後は入力欄にフォーカスが残っている。
 */
async function blurTextField(page) {
  await page.evaluate(() => {
    const el = document.activeElement
    if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) el.blur()
  })
}

export async function pressUndo(page) {
  await blurTextField(page)
  await page.keyboard.down('Control')
  await page.keyboard.press('KeyZ')
  await page.keyboard.up('Control')
}

/** 視点をシーン全体へ戻す(画面にボタンは無く、実機検査用の穴 `window.__duocad.frameAll` を呼ぶ)。 */
export async function frameAll(page) {
  await page.evaluate(() => window.__duocad.frameAll())
}

/** パネルの開閉を `open` に合わせる。動かしたときだけ true。 */
async function setBrowserPanel(page, open) {
  const changed = await page.evaluate((want) => {
    const panel = document.querySelector('.browser-panel')
    if (panel === null) return false
    if (!panel.classList.contains('collapsed') === want) return false
    document.querySelector('#browser-collapse')?.click()
    return true
  }, open)
  // Svelteの更新は次のタスクなので、押した直後のDOMは古い
  if (changed) {
    await page.waitForFunction(
      (want) => {
        const panel = document.querySelector('.browser-panel')
        return panel !== null && !panel.classList.contains('collapsed') === want
      },
      { polling: 20, timeout: 5000 },
      open,
    )
  }
  return changed
}

/**
 * ページのエラーを控える(`pageerror`・`console.error`・4xx/5xx 応答)。最後に `assertNoAppErrors` へ渡す。
 * サーバのエラーはトーストへ出るだけでコンソールには出ないので、控え `window.__duocad.errors` と合わせて見る。
 */
export function watchErrors(page, { watchHttp = true } = {}) {
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  if (watchHttp) {
    page.on('response', (response) => {
      if (response.status() >= 400) errors.push(`HTTP ${response.status()} ${response.url()}`)
    })
  }
  return errors
}

/**
 * 画面を開いて `__duocad.ready` まで待つ。戻り値は `{ page, consoleErrors }`。
 *
 * `height` の既定は 860。高さが変わると掴む画素が変わるので本ごとの値は揃えない。
 * `until` は ready の後に待つ述語。`newDocument`(既定 true)はトップ画面が出ていれば「新規」を押す
 * (トップ画面そのものを見る本だけ false)。
 * `frameAll` はここでは呼ばない: 初回フレーミングを見る本と、near/far の作り直しで再現条件が壊れる本
 * (`smoke-clip-planes.mjs`)がある。
 */
export async function openApp(
  browser,
  url,
  { timeout, height = 860, watchHttp = true, until = null, newDocument = true } = {},
) {
  const page = await browser.newPage()
  await page.setViewport({ width: 1280, height })
  const consoleErrors = watchErrors(page, { watchHttp })
  await page.goto(url, { waitUntil: 'load', timeout })
  await page.waitForFunction(() => Boolean(window.__duocad && window.__duocad.ready), {
    timeout,
    polling: 100,
  })
  // サーバはトップ画面で起動し、その間は文書を変える操作が断られるので「新規」を押す。
  // 文書が開いていればモードは問わない(MCP でスケッチを作った後は画面がそのスケッチに入っていて、通常モードの入口
  // `#sketch-create` が無い。issue #153)。
  if (newDocument) {
    await page.waitForFunction(
      () => document.querySelector('#start-new') !== null || window.__duocad?.state?.document_open === true,
      { timeout, polling: 100 },
    )
    if ((await page.$('#start-new')) !== null) {
      await page.click('#start-new')
      await page.waitForSelector('#sketch-create', { timeout })
    }
  }
  if (until !== null) await page.waitForFunction(until, { timeout, polling: 100 })
  return { page, consoleErrors }
}

/**
 * 走行の終わりに「エラーが1件も出ていない」を見る。
 *
 * 読む先は `window.__duocad.errors`(札は8秒で消えるので DOM は見ない)と、`watchErrors` の控え。
 * 控えが配列で来ることを先に確かめる: `undefined` や object に化けると `length > 0` が偽のまま
 * 検査が赤くならない。
 *
 * `allow` はわざと出させたエラー文言(完全一致)。積んだ文言が控えに実在することも見る
 * (出ていないのに除外だけ積むと、空振りしていても緑になる)。`expectCount` は件数で見る本用(`allow` と併用しない)。
 */
export async function assertNoAppErrors(page, consoleErrors, { allow = [], expectCount = null } = {}) {
  // `$state` のプロキシは構造化複製で `{}` になるので、ページの中で素の配列にして返す。
  const errors = await page.evaluate(() => {
    const list = window.__duocad.errors
    return Array.isArray(list) ? JSON.parse(JSON.stringify(list)) : { notArray: String(list) }
  })
  if (!Array.isArray(errors)) {
    fail(`__duocad.errors が配列で来ない(検査が黙って死ぬ): ${JSON.stringify(errors)}`)
  }
  if (expectCount !== null) {
    if (errors.length !== expectCount) {
      fail(`控えの件数が合わない(想定 ${expectCount} / 実際 ${errors.length}): ${JSON.stringify(errors)}`)
    }
  } else {
    const missing = allow.filter((message) => !errors.includes(message))
    if (missing.length > 0) fail(`出るはずのエラーが控えに無い: ${missing.join(' | ')}`)
    const unexpected = errors.filter((message) => !allow.includes(message))
    if (unexpected.length > 0) fail(`走行中にエラーが出た: ${unexpected.join(' | ')}`)
  }
  if (consoleErrors.length > 0) {
    fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
  }
  if (expectCount !== null) {
    ok(`走行中のエラーは想定どおり ${expectCount} 件だけ。コンソールにエラーなし`)
  } else if (allow.length > 0) {
    ok(`走行中に予期しないエラーは出ていない(わざと出させた ${allow.length} 件を除く)。コンソールにエラーなし`)
  } else {
    ok('走行中にエラーは1件も出ていない。コンソールにエラーなし')
  }
  return errors
}

/** 入力欄へ打つ: クリック → 全選択 → 消す → 打つ。`delay` は打鍵の間隔(ms)。 */
export async function typeInto(page, selector, value, { delay = 15 } = {}) {
  await page.click(selector)
  await page.keyboard.down('Control')
  await page.keyboard.press('KeyA')
  await page.keyboard.up('Control')
  await page.keyboard.press('Backspace')
  await page.type(selector, String(value), { delay })
}

/**
 * 述語が真になるまで待ち、待ちきれなければ `what` を添えて失敗する。
 * `args` は述語へ渡す引数の配列(可変長にすると2つの呼び方が混在して引数が黙ってずれる)。
 * `dump` を渡すと落ちたときにその戻り(`look()` 等)を添える。
 */
export async function waitOrFail(
  page,
  predicate,
  what,
  { args = [], timeout = 60000, polling = 50, dump = null } = {},
) {
  try {
    await page.waitForFunction(predicate, { timeout, polling }, ...args)
  } catch {
    const detail = dump === null ? '(タイムアウト)' : `: ${JSON.stringify(await dump())}`
    fail(`${what}${detail}`)
  }
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

/**
 * 描画ループの `n` フレームを待つ。ホバー・不透明度・レイアウトは次のフレームで入れ直されるので、
 * 動かした直後に読む前・射影する前に挟む(2 でレイアウト、4 でホバーの解決が相場)。
 */
export async function waitFrames(page, n) {
  await page.evaluate(
    (count) =>
      new Promise((resolve) => {
        let left = count
        const step = () => {
          if (left <= 0) {
            resolve()
            return
          }
          left -= 1
          requestAnimationFrame(step)
        }
        step()
      }),
    n,
  )
}

/** `.viewport` の中央(ページ座標)。ホイールはカーソル位置で発火する。 */
export async function viewportCenter(page) {
  return page.evaluate(() => {
    const box = document.querySelector('.viewport').getBoundingClientRect()
    return { x: box.left + box.width / 2, y: box.top + box.height / 2 }
  })
}
