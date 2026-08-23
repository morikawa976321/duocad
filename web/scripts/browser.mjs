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
 * undo / redo。**2026-08-23に画面のボタン(`#undo`/`#redo`)を削除した**ので、
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

export async function pressRedo(page) {
  await blurTextField(page)
  await page.keyboard.down('Control')
  await page.keyboard.press('KeyY')
  await page.keyboard.up('Control')
}

/**
 * 視点をシーン全体へ戻す。**2026-08-23に画面のボタン(`#view-fit`)を削除した**ので、
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
