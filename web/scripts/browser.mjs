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
