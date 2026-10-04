/**
 * 検査に使うブラウザの選択と起動。
 *
 * **インストール済みのブラウザを使う**(puppeteer-core なのでダウンロードしない)。
 * **WebGLは実GPU(ANGLE/D3D11)で描く。** `--enable-unsafe-swiftshader` は「掴めなかったときに
 * ソフトGLへ落ちてよい」という許可であって強制ではない。**強制すると直るのはCPU消費であって
 * 検査の速さではない** —— `smoke.mjs` 1本の実測で、chromeのCPUは 1550秒(平均9.5コア) ->
 * 215秒(1.4コア)、所要時間は 163秒 -> 150秒でほぼ変わらない(検査の律速はOCCT側とWS往復で、
 * 描画のfpsではない)。どちらで描いたかは起動時の `GL :` 行に出る。
 */
import { existsSync } from 'node:fs'
import { launch } from 'puppeteer-core'
import { fail, ok } from './args.mjs'

/**
 * 探す順。**Chromeが先** —— この環境の Edge 151 は headless で起動できず、
 * `Failed to launch the browser process: Code: 0` を出して即座に終了する(専用プロファイルを
 * 与えても同じで、duocad側の問題ではない)。Edgeは候補として残す(Chromeが無い環境のため)。
 */
export const CANDIDATE_BROWSERS = [
  process.env.DUOCAD_BROWSER,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].filter(Boolean)

/** 最初に見つかった実行ファイル。無ければ例外(**黙って別の経路へ落ちない**)。 */
export function findBrowser() {
  const found = CANDIDATE_BROWSERS.find((candidate) => existsSync(candidate))
  if (!found) {
    throw new Error(
      `ブラウザが見つからない(DUOCAD_BROWSER で指定する): ${CANDIDATE_BROWSERS.join(', ')}`,
    )
  }
  return found
}

/** headless で起動する。戻り値は puppeteer の Browser。 */
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

/**
 * 実際に描いたGLの実装名。**黙ってソフトGLへ落ちたことに気づくため**に毎回出す
 * (落ちても検査は通ってしまい、遅いだけの状態が見えないため)。取得に失敗しても検査は続ける。
 */
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
 * ビューポートに浮かぶ**ブラウザ(`BrowserPanel.svelte`)を畳んだ状態で `run` を走らせ、
 * 終わったら元に戻す。
 *
 * このパネルは `.viewport` の左上に重なっており、**その下の画素は押せない**(押すと
 * クリックがパネルに吸われる)。`viewport.ts` の `clickable` が覆われた画素を候補から
 * 落とすので、`project*` のうち**候補を複数持つもの**(面・辺・領域・基準平面)は勝手に
 * 別の画素へ逃げる。逃げ場が無いのは `projectSketchPoint` だけ —— **スケッチ平面の
 * 特定の (u,v) は1画素しかない**ので、そこがパネルの下に来ると押せない。
 *
 * だから畳むのは**その1点を押す間だけ**にする。パネルを開けたままにしておくのは、
 * 一覧(モデル・スケッチ)そのものを触る検査が同じ画面を使うからである。
 *
 * パネルが無い画面でも落ちない(何もせず `run` を走らせる)。
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
 * undo / redo。画面にボタン(`#undo`/`#redo`)は無いので、
 * `App.svelte` の `onWindowKeydown` が拾うのと同じキー(Ctrl+Z / Ctrl+Y)を送る。
 *
 * **先にフォーカスを外す**。数値入力(INPUT/TEXTAREA)にフォーカスが残っていると、
 * `onWindowKeydown` はブラウザ標準のテキスト取り消しに譲って文書のundo/redoを
 * 発火しない(意図どおりの挙動。`smoke.mjs`「入力欄にフォーカスがある状態のCtrl+Z」参照)。
 * コマンドは確定後も構えたままなので、Enterで確定した直後は入力欄にフォーカスが残っている。
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

/**
 * 視点をシーン全体へ戻す。画面にボタン(`#view-fit`)は無いので、
 * 実機検査だけが使う穴(`window.__duocad.frameAll`)を直接呼ぶ。
 */
export async function frameAll(page) {
  await page.evaluate(() => window.__duocad.frameAll())
}

/** ブラウザの開閉を `open` に合わせる。**動かしたときだけ true**(戻す要否の判定に使う)。 */
async function setBrowserPanel(page, open) {
  const changed = await page.evaluate((want) => {
    const panel = document.querySelector('.browser-panel')
    if (panel === null) return false
    if (!panel.classList.contains('collapsed') === want) return false
    document.querySelector('#browser-collapse')?.click()
    return true
  }, open)
  // **DOMに反映されるまで待つ**(Svelteの更新は次のタスクなので、押した直後は古い)
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

// ---------------------------------------------------------------------------------- 画面を開く・終わりのエラー確認

/**
 * ページのエラーを控える(`pageerror`・`console.error`・4xx/5xx 応答)。走行の最後に
 * `assertNoAppErrors` へ渡す。**サーバのエラーはトーストへ出るだけでコンソールには出ない**ので、
 * これだけでは足りない(控え `window.__duocad.errors` と合わせて見る)。
 */
export function watchErrors(page, { watchHttp = true } = {}) {
  const errors = []
  page.on('pageerror', (error) => errors.push(String(error)))
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  if (watchHttp) {
    // どのURLで落ちたかを残す(「404が1件」だけ言われても原因が分からない)
    page.on('response', (response) => {
      if (response.status() >= 400) errors.push(`HTTP ${response.status()} ${response.url()}`)
    })
  }
  return errors
}

/**
 * 画面を開いて `__duocad.ready` まで待つ。戻り値は `{ page, consoleErrors }`。
 *
 * `height` は 860 が既定(900 の本もある)。**高さが変わると掴む画素が変わる**ので本ごとの値は
 * 揃えない。`until` は ready の後に待つ述語(`page.waitForFunction` に渡す。部品が届いたこと等)。
 * **`frameAll` はここでは呼ばない** —— 初回フレーミングそのものを見る本と、near/far を作り直すと
 * 再現条件が壊れる本(`smoke-clip-planes.mjs`)がある。
 */
export async function openApp(browser, url, { timeout, height = 860, watchHttp = true, until = null } = {}) {
  const page = await browser.newPage()
  await page.setViewport({ width: 1280, height })
  const consoleErrors = watchErrors(page, { watchHttp })
  await page.goto(url, { waitUntil: 'load', timeout })
  await page.waitForFunction(() => Boolean(window.__duocad && window.__duocad.ready), {
    timeout,
    polling: 100,
  })
  if (until !== null) await page.waitForFunction(until, { timeout, polling: 100 })
  return { page, consoleErrors }
}

/**
 * 走行の終わりに「エラーが1件も出ていない」を見る。
 *
 * 読む先は2つ。走行全体の有無は控え(`window.__duocad.errors`。**札は8秒で消える**ので DOM を
 * 最後に1回見ても取りこぼす)、ブラウザ側の例外・console.error・4xx は `watchErrors` の控え。
 * **控えが配列で来ることを先に確かめる** —— `undefined` や object に化けると `length > 0` が
 * 永久に偽になり、赤くならないまま検査が死ぬ(穴を1つ足すたびに起きうる形なので握らずに落とす)。
 *
 * `allow` はわざと出させたエラー文言(受け取った文字列との完全一致。種別や前方一致で緩めない)。
 * **積んだ文言が控えに実在することも見る** —— 出ていないのに除外だけ積むと、「拒否されたはず」の
 * 検査が空振りしていても緑になる。`expectCount` は文言ではなく件数で見る本のため(`allow` と併用しない)。
 */
export async function assertNoAppErrors(page, consoleErrors, { allow = [], expectCount = null } = {}) {
  // **`$state` のプロキシは構造化複製で `{}` になる**ので、配列かどうかはページの中で見て素の配列にして返す
  // (`?? []` で読んでいた本は、プロキシが `{}` で届いて `length > 0` が偽のまま緑だった)。
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

// ---------------------------------------------------------------------------------- 入力と待ち

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
 * 述語が真になるまで待ち、待ちきれなければ **`what` を添えて失敗する**。
 *
 * `args` は述語へ渡す引数の**配列**(可変長ではない —— 2つの呼び方が混在すると引数が黙ってずれる)。
 * `dump` を渡すと落ちたときにその戻り(`look()` 等の画面の中身)を添える —— 「120秒待った」だけでは
 * 切り分けられない。
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
 * 描画ループの `n` フレームを待つ(`requestAnimationFrame` を n 回)。ホバー・段の不透明度・
 * レイアウトは `tick()` や次のフレームで入れ直されるので、動かした直後に読む前・射影する前に挟む
 * (2 でレイアウト、4 でホバーの解決を待つのが各本の相場)。
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

/** `.viewport` の中央(ページ座標)。ホイールはカーソル位置で発火するので、その前に置く先。 */
export async function viewportCenter(page) {
  return page.evaluate(() => {
    const box = document.querySelector('.viewport').getBoundingClientRect()
    return { x: box.left + box.width / 2, y: box.top + box.height / 2 }
  })
}
