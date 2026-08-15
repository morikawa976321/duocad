/**
 * 検査に使うブラウザの選択と起動 — 5本が同じ候補リストと同じ起動引数を持っていた(2026-08-15)。
 *
 * **インストール済みのブラウザを使う**(puppeteer-core なのでダウンロードしない)。
 * headless で WebGL を出すために SwiftShader(ソフトウェアGL)を明示的に許可している。
 */
import { existsSync } from 'node:fs'
import { launch } from 'puppeteer-core'

/**
 * 探す順。**Chromeが先**(2026-08-15) —— この環境の Edge 151 は headless で起動できず、
 * `Failed to launch the browser process: Code: 0` を出して即座に終了する(専用プロファイルを
 * 与えても同じで、duocad側の問題ではない)。Edgeは候補として残す(Chromeが無い環境のため)。
 * `DUOCAD_BROWSER` を置けばそれが最優先。
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
  return launch({
    executablePath,
    headless: true,
    args: [
      '--headless=new',
      '--use-gl=angle',
      '--use-angle=swiftshader',
      '--enable-unsafe-swiftshader',
      '--disable-gpu-sandbox',
      '--window-size=1280,860',
    ],
  })
}
