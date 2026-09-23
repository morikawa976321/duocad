/**
 * 実ブラウザでのトースト検査 — **エラーの見せ方そのものの機械検査**。
 *
 * 他の検査は「操作が通ること」を見ていて、失敗の見せ方は `smoke-features` の
 * フィレット1件（札に理由が出るか）しか触っていない。トーストは**後続のUIが全部この上に載る**
 * ので、骨格の性質だけを独立して固定する:
 *
 *   1. **積層** — 文言の違うエラーが2件、同時に見えている
 *   2. **同じ文言は積まずに数える** — 同じ失敗を繰り返しても枚数は増えず「N回」が付く
 *      （WSが切れている間は同じ2種類が延々と届くので、素直に積むと画面が埋まる）
 *   3. **クリックを吸わない** — 札の真ん中を指すとcanvasが受ける。コピー・閉じるだけが札で受ける
 *      （CADの主対象はcanvas。失敗の直後に出た札が次のクリックを飲むのが最悪の形）
 *   4. **手動close** — ×で1枚だけ消える
 *   5. **自動消滅** — 放っておけば消える
 *
 * ついでに、**AIパネルの開閉カレット**（`#ai-collapse`）も1本だけここで見る
 * ——トーストとは無関係だが、アプリを実際に起動している検査が既にあるのでそこへ同居させた。
 *
 *   web> node scripts/smoke-toast.mjs
 *
 * エラーは**存在しない書き出し先を「STEP書き出し」「STEP読み込み」へ渡して**作る。サーバが
 * 別々の理由で断るので**文言の違うエラーが2件**手に入り、しかもどちらも1回きりで再送されない
 * （WSを切って作ると再接続の試行が延々と札を作り直すので、自動消滅を見られない）。
 * **「開く」は使わない** ——
 * 保存/開くはUIから任意パスを受け付けなくなり、`duocad/models/` の一覧をクリックする
 * だけになった（`FilesPanel.svelte`）ので、「開く」に**存在しないパスを打つ**手段自体が
 * 無くなっている。積層の検査に要るのは「文言の違うエラー2件」だけなので、
 * 引き続きパスを打てる書き出し系(STEP書き出し・STEP読み込み)で作る。
 *
 * **サーバはこのスクリプトが自分で立てる**（`web/scripts/server.mjs`）。
 * 既に立っているものへ繋ぐときだけ `--url` を渡す。
 */
import { findBrowser, launchBrowser } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const attachUrl = options.url ?? null
const wsPort = options.port ?? '8805'
const httpPort = options['http-port'] ?? '8806'
const timeout = Number.parseInt(options.timeout ?? '120000', 10)

/** 出しても消えても構わない、存在しないパス（サーバが断ることだけが要る）。 */
const MISSING_EXPORT_DIR = 'C:/nowhere/duocad-smoke-toast-does-not-exist.step'
const MISSING_STEP = 'C:/nowhere/duocad-smoke-toast-does-not-exist-2.step'

async function main() {
  const executablePath = findBrowser()
  console.log(`smoke-toast: ${executablePath}`)

  const server = attachUrl === null ? await startServer({ wsPort, httpPort }) : null
  const url = server?.url ?? attachUrl
  console.log(`  server : ${url}${server === null ? ' (既に立っているサーバへ接続)' : ''}`)

  const browser = await launchBrowser(executablePath)

  try {
    const page = await browser.newPage()
    await page.setViewport({ width: 1280, height: 860 })
    const consoleErrors = []
    page.on('pageerror', (error) => consoleErrors.push(String(error)))
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })

    await page.goto(url, { waitUntil: 'load', timeout })
    await page.waitForFunction(() => Boolean(window.__duocad && window.__duocad.ready), {
      timeout,
      polling: 100,
    })

    /** 今出ている札（本文と回数）。**文言の正はDOM** —— 人間が見るのはこちらである。 */
    const toasts = async () =>
      JSON.parse(
        await page.evaluate(() =>
          JSON.stringify(
            Array.from(document.querySelectorAll('#toasts .toast')).map((node) => ({
              text: node.querySelector('.toast-text').textContent.trim(),
              count: node.querySelector('.toast-count')?.textContent.trim() ?? null,
            })),
          ),
        ),
      )

    const waitToasts = async (expected, what) => {
      try {
        await page.waitForFunction(
          (want) => document.querySelectorAll('#toasts .toast').length === want,
          { timeout, polling: 50 },
          expected,
        )
      } catch {
        fail(`${what}: ${JSON.stringify(await toasts())}`)
      }
    }

    // **入力欄idはボタンidで決まる**(`#file-path`の時間貸しではなく、
    // STEP読み込み/STEP書き出しがそれぞれ専用の欄を持つ)。
    const INPUT_FOR_BUTTON = {
      'file-export-step': 'file-export-step-path',
      'file-import-step': 'file-import-path',
    }

    /** 対応する欄へパスを打ち、ボタンを押して確定する(入力欄は毎回消してから打つ)。 */
    const run = async (buttonId, path) => {
      const inputId = INPUT_FOR_BUTTON[buttonId]
      // **STEP読み込みだけはツールバーのコマンド**。起動して初めてパス欄がDOMに出るので、
      // 無ければ先にボタンを押して起こす。書き出しは左サイドバーの常設の欄なので、
      // この分岐は素通りする。
      if ((await page.$(`#${inputId}`)) === null) await page.click(`#${buttonId}`)
      await page.click(`#${inputId}`)
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyA')
      await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await page.type(`#${inputId}`, path, { delay: 10 })
      await page.click(`#${buttonId}`)
    }

    // -------------------------------------------------------------- 器はいつでもある
    if ((await page.$('#toasts')) === null) fail('#toasts が無い(実機検査が文言を読めない)')
    if ((await toasts()).length !== 0) fail(`起動直後に札が出ている: ${JSON.stringify(await toasts())}`)
    ok('起動直後: `#toasts` はあるが札は0枚(「今は0件」を素直に書ける)')

    // -------------------------------------------------------------- AIパネルの開閉カレット
    // tier3の常設パネルのうちAIだけが畳めなかったのを、BrowserPanel.svelteと同じ形の
    // カレットで揃えた。トーストの検査とは無関係だが、アプリを起動している実機検査1本で
    // 足りる要件なのでここに同居させる。
    //
    // **`#ai-transcript`と`aria-expanded`だけでなく、`.ai-input`/`.ai-settings`/`.hint`の
    // 残存や220px固定の解除まで見る** —— 部分的な壊れ方(transcriptだけ消えて他が残る、
    // 固定幅が解除されない)を見落とさないため。
    // **`.hint`は`.ai-panel`の中に絞る**(実測で踏んだ穴): `SettingsDialog.svelte`の
    // `<p class="hint dim">`が同名クラスを持ち、native `<dialog>`は非表示でも要素自体は
    // DOMに残るため、絞らずに`page.$('.hint')`だけで見ると常にこちらへ先に当たって
    // AIパネル側の状態を一度も見ないまま緑になっていた。`.ai-input`/`.ai-settings`も
    // 念のため同じ流儀で絞る(今は他所に同名クラスは無いが、増えたときの事故を先回りする)。
    const aiVisible = async () => ({
      transcript: (await page.$('#ai-transcript')) !== null,
      input: (await page.$('.ai-panel .ai-input')) !== null,
      settings: (await page.$('.ai-panel .ai-settings')) !== null,
      hint: (await page.$('.ai-panel .hint')) !== null,
    })
    const aiPanelHeight = async () =>
      page.$eval('.ai-panel', (node) => node.getBoundingClientRect().height)

    if ((await page.$('#ai-collapse')) === null) fail('#ai-collapse が無い(AIパネルが畳めない)')
    const aiExpandedAtStart = await page.$eval('#ai-collapse', (node) => node.getAttribute('aria-expanded'))
    if (aiExpandedAtStart !== 'true') {
      fail(`AIパネルの既定は開いているはず: aria-expanded=${aiExpandedAtStart}`)
    }
    const shownAtStart = await aiVisible()
    // **`.ai-settings`だけは既定で閉じている**(`settingsOpen`の既定はfalse)ので、
    // ここでは対象にしない —— 開いていることを先に作ってから畳む検査は下で別途行う。
    if (!shownAtStart.transcript || !shownAtStart.input || !shownAtStart.hint) {
      fail(`既定で開いているはずのAI本体の要素が欠けている: ${JSON.stringify(shownAtStart)}`)
    }
    // **設定フォームも開いた状態を作ってから畳む** —— `.ai-settings`の非存在(B2)を
    // 実際に踏むには、畳む前に一度出しておく必要がある。
    await page.click('#ai-settings-toggle')
    await page.waitForFunction(() => document.querySelector('.ai-settings') !== null, {
      timeout,
      polling: 50,
    })
    const heightOpen = await aiPanelHeight()

    await page.click('#ai-collapse')
    try {
      await page.waitForFunction(() => document.querySelector('#ai-transcript') === null, {
        timeout,
        polling: 50,
      })
    } catch {
      fail('#ai-collapseを押しても#ai-transcriptがDOMから消えない')
    }
    const aiExpandedAfterCollapse = await page.$eval('#ai-collapse', (node) =>
      node.getAttribute('aria-expanded'),
    )
    if (aiExpandedAfterCollapse !== 'false') {
      fail(`畳んだ後のaria-expandedがfalseでない: ${aiExpandedAfterCollapse}`)
    }
    // **B2**: 畳んだら会話欄だけでなく入力欄・設定フォーム・ヒントも全部DOMから消える。
    const shownWhileCollapsed = await aiVisible()
    if (Object.values(shownWhileCollapsed).some(Boolean)) {
      fail(`畳んだのに残っている要素がある(部分的な壊れ方): ${JSON.stringify(shownWhileCollapsed)}`)
    }
    ok('**畳む(B2)**: `#ai-transcript`/`.ai-input`/`.ai-settings`/`.hint`が全部DOMから消えた')
    // **B3**: 220px固定を効かせず、ヘッダ行だけの高さになる。
    const heightCollapsed = await aiPanelHeight()
    if (!(heightCollapsed < heightOpen * 0.5)) {
      fail(
        `畳んでも220px固定が解除されていない: 開いている時 ${heightOpen}px -> 畳んだ後 ${heightCollapsed}px`,
      )
    }
    ok(`**畳む(B3)**: 220px固定が外れてヘッダ行だけの高さになった(${heightOpen.toFixed(0)}px -> ${heightCollapsed.toFixed(0)}px)`)

    await page.click('#ai-collapse')
    try {
      await page.waitForFunction(() => document.querySelector('#ai-transcript') !== null, {
        timeout,
        polling: 50,
      })
    } catch {
      fail('もう一度#ai-collapseを押しても#ai-transcriptが戻らない')
    }
    const shownAfterReopen = await aiVisible()
    if (!shownAfterReopen.transcript || !shownAfterReopen.input || !shownAfterReopen.settings || !shownAfterReopen.hint) {
      fail(`開き直しても要素が戻らない: ${JSON.stringify(shownAfterReopen)}`)
    }
    ok('**AIパネルの開閉**: `#ai-collapse`を押すと本体一式がDOMから消え、もう一度押すと全部戻る')
    // 後続のトースト検査に影響しないよう、設定フォームは閉じておく。
    await page.click('#ai-settings-toggle')
    await page.waitForFunction(() => document.querySelector('.ai-settings') === null, {
      timeout,
      polling: 50,
    })

    // -------------------------------------------------------------- 検証1: 積層
    await run('file-export-step', MISSING_EXPORT_DIR)
    await waitToasts(1, '「STEP書き出し」の失敗が札にならない')
    await run('file-import-step', MISSING_STEP)
    await waitToasts(2, '2件目が積まれない')
    const stacked = await toasts()
    if (stacked[0].text === stacked[1].text) {
      fail(`文言が同じ札が2枚出た(積層の検査になっていない): ${JSON.stringify(stacked)}`)
    }
    ok(`**積層**: 文言の違うエラー2件が同時に見えている(新しい方が上: ${stacked[0].text.slice(0, 40)}…)`)

    // -------------------------------------------------------------- 検証2: 同じ文言は数える
    await run('file-export-step', MISSING_EXPORT_DIR)
    try {
      await page.waitForFunction(
        () =>
          Array.from(document.querySelectorAll('#toasts .toast-count')).some((node) =>
            node.textContent.includes('2回'),
          ),
        { timeout, polling: 50 },
      )
    } catch {
      fail(`同じ失敗を繰り返しても回数が増えない: ${JSON.stringify(await toasts())}`)
    }
    const repeated = await toasts()
    if (repeated.length !== 2) fail(`同じ文言で札が増えた: ${JSON.stringify(repeated)}`)
    ok('**同じ文言は積まずに数える**: 2枚のまま「2回」が付いた(切断中に画面が埋まらない)')

    // -------------------------------------------------------------- 検証3: クリックを吸わない
    const hit = JSON.parse(
      await page.evaluate(() => {
        const toast = document.querySelector('#toasts .toast')
        const box = toast.getBoundingClientRect()
        const under = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2)
        const copy = toast.querySelector('.toast-copy')
        const copyBox = copy.getBoundingClientRect()
        const onCopy = document.elementFromPoint(
          copyBox.left + copyBox.width / 2,
          copyBox.top + copyBox.height / 2,
        )
        const close = toast.querySelector('.toast-close')
        const closeBox = close.getBoundingClientRect()
        const onClose = document.elementFromPoint(
          closeBox.left + closeBox.width / 2,
          closeBox.top + closeBox.height / 2,
        )
        return JSON.stringify({
          underTag: under?.tagName ?? null,
          underInToast: Boolean(under && under.closest('#toasts')),
          copyIsCopy: onCopy === copy,
          closeIsClose: onClose === close,
        })
      }),
    )
    if (hit.underInToast) fail(`札の真ん中のクリックを札が吸う: ${JSON.stringify(hit)}`)
    if (!hit.copyIsCopy) fail(`コピーがクリックを受け取れない: ${JSON.stringify(hit)}`)
    if (!hit.closeIsClose) fail(`×がクリックを受け取れない: ${JSON.stringify(hit)}`)
    ok(`**クリックを吸わない**: 札の中心は下の ${hit.underTag} が受け、コピーと×だけが札で受ける`)

    // -------------------------------------------------------------- 検証4: 手動close
    await page.click('#toasts .toast .toast-close')
    await waitToasts(1, '×を押しても札が減らない')
    ok('**手動close**: ×で1枚だけ消えた(残りはそのまま)')

    // -------------------------------------------------------------- 検証5: 自動消滅
    await waitToasts(0, '放っておいても札が消えない')
    ok('**自動消滅**: 残りも時間で消えた(閉じ忘れが画面に残らない)')

    // -------------------------------------------------------------- 控えは消えない
    const errors = JSON.parse(
      await page.evaluate(() =>
        JSON.stringify({
          isArray: Array.isArray(window.__duocad.errors),
          items: window.__duocad.errors,
        }),
      ),
    )
    // **配列であることを明示的に見る。** ここが object や undefined に化けると、
    // `smoke-files` の「走行中にエラーなし」が永久に偽になって**赤くならないまま死ぬ**。
    if (!errors.isArray || !Array.isArray(errors.items)) {
      fail(`__duocad.errors が配列で渡らない(検査が黙って死ぬ): ${JSON.stringify(errors)}`)
    }
    if (errors.items.length !== 3) {
      fail(`控えの件数が合わない(出したのは3件): ${JSON.stringify(errors.items)}`)
    }
    ok('**控えは消えない**: 札が全部消えた後も `__duocad.errors` に3件残っている')

    // ステータスバーへ漏れていないこと(=一本化の実体)
    const strayed = JSON.parse(
      await page.evaluate(() => JSON.stringify(window.__duocad.messages ?? [])),
    ).filter((message) => message.startsWith('エラー['))
    if (strayed.length > 0) fail(`エラーがステータスバーにも出ている: ${strayed.join(' | ')}`)
    ok('**一本化**: エラーはステータスバー(通知欄)へ1件も漏れていない')

    if (consoleErrors.length > 0) {
      fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    }
    ok('ブラウザのコンソールにエラーなし')

    console.log('')
    console.log('smoke-toast: PASS')
  } finally {
    await browser.close()
    if (server !== null) await server.stop()
  }
}

main().catch((error) => {
  console.error(`smoke-toast: FAIL ${error.message}`)
  process.exit(1)
})
