/**
 * 実ブラウザでのファイルパネルの結線と、Enter の要らないコマンド(即適用)の検査。
 *
 * **空の文書**から始めて、パネルのボタンと欄が本物のクリックとキー入力でサーバへ届き、
 * 一覧とヘッダが追随することだけを見る。保存・改名・複製・削除・書き出しの**契約**(拒否・
 * 1バイトも書き換えない・固定の粗さ・非表示の除外)は `tests/test_files_ui.py` /
 * `tests/test_io_step.py` が持つので、ここでは繰り返さない。
 *
 *   1. 自動保存: 最初の変更で `untitled-<n>.json` が付く。保存ボタンも「未保存」の札も無い
 *   2. 改名(名前欄 + Enter)・再読込しても名前が残る
 *   3. 一覧の行から複製(先埋めの名前) → undo → 複製を開くと undo 前の形が戻る
 *   4. Ctrl+S はブラウザの保存ダイアログを止める
 *   5. 新規・一覧から開き直す・行内2段階の削除
 *   6. STEP/STL の書き出しと、ツールバーからの STEP 読み込み
 *   7. 即適用: 面に貼る `sketch_create` が起動だけでは走らず、起動後の面クリックで走る。
 *      基準平面と重なった面は、カメラを動かしても面が取れる
 *
 *   web> node scripts/smoke-files.mjs [--workdir <書き込めるディレクトリ>]
 */
import { existsSync, statSync } from 'node:fs'
import { join } from 'node:path'
import {
  assertNoAppErrors,
  frameAll,
  openApp,
  pressUndo,
  sleep,
  typeInto as typeIntoField,
  waitFrames,
  waitOrFail,
} from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { clickRegion, clickSketchPoint, createSketchOnBasePlane, faceAt, planarFace } from './pick.mjs'

const PLATE = { width: 60, height: 20, thickness: 5 }

// 保存/開くの置き場(`modelsDir`)は harness が渡す使い捨ての `workdir` で、STEP/STL の書き出しも
// 同じディレクトリに置く(実在確認が楽になる)。
runSmoke('smoke-files', { attach: true }, async ({ workdir, browser, url, timeout }) => {
  const { page, consoleErrors } = await openApp(browser, url, { timeout, height: 900 })
  if ((await page.evaluate(() => window.__duocad.parts.length)) !== 0) {
    fail('空の文書で起動していない(サーバを起動し直す)')
  }
  ok('空の文書で起動した')

  // ------------------------------------------------------------------ 道具
  const look = async () =>
    JSON.parse(
      await page.evaluate(() => {
        const hook = window.__duocad
        return JSON.stringify({
          bodies: hook.parts.map((body) => ({
            id: body.body_id,
            volume: body.volume,
            faces: body.face_count,
            visible: body.visible,
          })),
          activeSketch: hook.activeSketch,
          // 文書にあるスケッチの件数(即適用の検査が「増えていないこと」を数える)
          sketches: hook.sketches.length,
          lastFile: hook.lastFile,
          documentList: hook.documentList,
          currentDocumentName: hook.currentDocumentName,
          log: hook.state.log.length,
          messages: hook.messages,
          errors: hook.errors,
        })
      }),
    )

  const waitFor = (predicate, what, ...args) =>
    waitOrFail(page, predicate, what, { args, timeout, dump: look })

  /**
   * **何も無い所を素でクリックして全部外す**(`App.svelte` の `onPick`)。
   *
   * どの画素が空いているかは視点と部品の形で変わるので、四隅を順に試して
   * **実際に0件になった時点で成功**とする(取れた画素を決め打ちしない)。
   */
  async function clearSelectionByClickingNothing() {
    const rect = await page.evaluate(() => {
      const box = document.querySelector('.viewport').getBoundingClientRect()
      return { left: box.left, top: box.top, width: box.width, height: box.height }
    })
    // **ビューキューブの矩形を候補から外す**。キューブは右上のクリックを吸うので
    // `[0.97, 0.03]` がそのまま矩形の内側に入る。矩形は右上アンカーで**ビューポートの寸法が
    // 変わると動く**ので、座標を書き換えるのではなく毎回 `viewCubeRect()` に聞いて弾く。
    const cube = await page.evaluate(() => window.__duocad.viewCubeRect?.() ?? null)
    const insideCube = (x, y) =>
      cube !== null && x >= cube.x && x < cube.x + cube.w && y >= cube.y && y < cube.y + cube.h
    const candidates = [[0.97, 0.97], [0.03, 0.97], [0.97, 0.03], [0.5, 0.97]].filter(
      ([fx, fy]) => !insideCube(rect.width * fx, rect.height * fy),
    )
    if (candidates.length === 0) fail('空白クリックの候補が全部ビューキューブの矩形の下だった')
    for (const [fx, fy] of candidates) {
      await page.mouse.click(rect.left + rect.width * fx, rect.top + rect.height * fy)
      try {
        await page.waitForFunction(
          () => (window.__duocad.selection?.items ?? []).length === 0,
          { timeout: 2000, polling: 50 },
        )
        return
      } catch {
        // そこには部品が居た。次の隅を試す
      }
    }
    fail(`何も無い所のクリックで全解除できない: ${JSON.stringify(await look())}`)
  }

  /**
   * **起きないことを見る前の猶予**。適用が走るなら「クリック → WS往復 → 状態配信」で
   * 済む時間(実測で数十〜数百ms)を過ぎてから数える —— 待たずに数えると、
   * 走っていても「まだ返ってきていないだけ」を緑と読んでしまう。
   */
  const graceForNothingToHappen = () => sleep(1000)

  const typeInto = (selector, value) => typeIntoField(page, selector, value)

  /**
   * 一覧の操作数が今の文書に追いつくのを待つ(自動保存が本当に書いたことの証拠。一覧は文書が
   * 変わるたびに取り直される —— `documentFiles.svelte.ts` の `onState`)。書けた後なので、
   * 「未保存」の札(`#save-state`。自動保存が書けなかったときだけ出る)はその場で見てよい。
   */
  async function expectAutosaved(where, name) {
    await waitFor(
      (expected) => {
        const file = (window.__duocad.documentList ?? []).find((item) => item.name === expected)
        return Boolean(file && file.operations === window.__duocad.state.log.length)
      },
      `${where}: 一覧の操作数が今の文書に追いつかない(自動保存が書いていない)`,
      name,
    )
    if ((await page.$('#save-state')) !== null) {
      fail(`${where}: 自動保存されているのにヘッダに「未保存」の札が出ている`)
    }
  }

  const listed = (name) =>
    waitFor(
      (expected) => (window.__duocad.documentList ?? []).some((file) => file.name === expected),
      `${name} が一覧に出ない`,
      name,
    )

  const header = () => page.$eval('#current-file', (el) => el.textContent.trim())

  // ================================================================ 板を作る
  await createSketchOnBasePlane(page, 'XY', { timeout })
  await waitFor(() => window.__duocad.activeSketch !== null, 'XY平面のスケッチに入れない')
  await page.click('#sketch-rectangle')
  await clickSketchPoint(page, 0, 0)
  await typeInto('#sketch-width', PLATE.width)
  await typeInto('#sketch-height', PLATE.height)
  await page.keyboard.press('Enter')
  await waitFor(
    () => window.__duocad.sketch?.regions.length === 1,
    '板の領域ができない',
  )
  await page.keyboard.press('Escape')
  await page.click('#sketch-look')
  await clickRegion(page, 1)
  await page.click('#extrude')
  await typeInto('#distance', PLATE.thickness)
  await page.click('#extrude-mode-join')
  await page.keyboard.press('Enter')
  await waitFor(() => window.__duocad.parts.length === 1, '板ができない')
  await page.click('#sketch-exit')
  await frameAll(page)
  const plate = (await look()).bodies[0]
  ok(`板を作った(体積 ${plate.volume})`)

  // ================================================================ 1. 自動保存
  const untitledName = 'untitled-1.json'
  const firstName = 'model.json'
  await waitFor(
    (expected) => window.__duocad.currentDocumentName === expected,
    '最初の変更で無題の文書に名前が自動で付かない',
    untitledName,
  )
  if (!existsSync(join(workdir, untitledName))) fail(`自動で付いた名前のファイルが無い: ${untitledName}`)
  if ((await page.$('#file-save')) !== null) fail('明示の保存ボタン(#file-save)が残っている')
  await expectAutosaved('図形を作った直後', untitledName)
  ok(`最初の変更で ${untitledName} として自動保存された(保存ボタンも「未保存」の札も無い)`)

  // ================================================================ 2. 改名と再読込
  await typeInto('#file-name-input', firstName)
  await page.keyboard.press('Enter')
  await waitFor(
    (expected) => window.__duocad.currentDocumentName === expected,
    '改名後に現在のファイル名が更新されない',
    firstName,
  )
  if (!existsSync(join(workdir, firstName))) fail(`改名したはずのファイルが無い: ${firstName}`)
  if ((await header()) !== firstName) fail(`ヘッダのファイル名が違う: "${await header()}"`)
  ok(`名前欄で ${untitledName} → ${firstName} へ改名できた`)

  // 編集中のファイル名を持っているのはサーバなので、再読込しても戻る。
  await page.reload({ waitUntil: 'load', timeout })
  await page.waitForFunction(() => Boolean(window.__duocad && window.__duocad.ready), {
    timeout,
    polling: 100,
  })
  await waitFor(
    (expected) => window.__duocad.currentDocumentName === expected,
    '再読込するとヘッダから編集中のファイル名が消える',
    firstName,
  )
  ok('再読込しても編集中のファイル名が残った')

  // ================================================================ 3. 複製 → undo → 複製を開く
  // 自動保存の下では編集中のファイルが常に今の文書の鏡なので、開き直しても違いが出ない。
  // 先に複製してから undo し、複製の側を開いて undo 前の形が戻ることを見る。
  await listed(firstName) // 再接続で一覧が届くのを待つ(`App.svelte` が `onStatus('open')` で取り直す)
  const secondName = firstName.replace(/\.json$/, '-copy.json')
  await page.click(`.files-row[data-name="${firstName}"] .files-copy`)
  await waitFor(() => document.activeElement?.id === 'file-copy-name', '複製の名前欄が出ない(フォーカスが来ない)')
  const suggested = await page.$eval('#file-copy-name', (el) => el.value)
  if (suggested !== secondName) fail(`複製先の先埋めが違う: "${suggested}"`)
  await page.keyboard.press('Enter')
  await listed(secondName)
  ok(`一覧の行から複製できた(先埋め "${suggested}" を Enter)`)

  await pressUndo(page)
  await waitFor(
    (before) => Math.abs((window.__duocad.parts[0]?.volume ?? 0) - before) > 1e-6,
    'undoしても形が変わらない(開く検査の前提が作れない)',
    plate.volume,
  )
  await expectAutosaved('undoの直後', firstName)

  await page.click(`.files-row[data-name="${secondName}"] .files-open`)
  await waitFor(
    (expectedVolume) => {
      const body = window.__duocad.parts[0]
      return Boolean(body && Math.abs(body.volume - expectedVolume) < 1e-6)
    },
    '複製を開いても undo 前の形にならない',
    plate.volume,
  )
  await waitFor(
    (expected) => window.__duocad.currentDocumentName === expected,
    '開いた後に現在のファイル名が更新されない',
    secondName,
  )
  ok('複製 → undo → 複製を開く で undo 前の形が戻った')

  // 以降は複製の側を編集中、model.json を編集中でないファイルとして使う。
  const documentName = secondName

  // ================================================================ 4. Ctrl+S
  // **`preventDefault()` が効いていることを直に見る。** puppeteerが合成したキーで
  // ネイティブの「ページを保存」ダイアログが必ず開くとは限らないので、
  // 「先へ進めたから止まっている」では検査にならない —— `window` のバブル段階へ
  // **後から**足した聞き手は `App.svelte` の `onWindowKeydown` の後に走るので、
  // そこで `defaultPrevented` を読めば止めたかどうかが確実に分かる。
  await page.evaluate(() => {
    window.__ctrlS = null
    window.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        window.__ctrlS = { prevented: event.defaultPrevented }
      }
    })
  })
  await page.keyboard.down('Control')
  await page.keyboard.press('KeyS')
  await page.keyboard.up('Control')
  await waitFor(() => window.__ctrlS !== null, 'Ctrl+S のキーイベントが window まで届いていない(検査が空振りしている)')
  if (!(await page.evaluate(() => window.__ctrlS.prevented))) {
    fail('Ctrl+S を preventDefault() で止めていない(ブラウザの保存ダイアログが開く)')
  }
  ok('Ctrl+S はブラウザの保存ダイアログを止める')

  // ================================================================ 5. 新規・開き直す・削除
  await page.click('#file-new')
  await waitFor(
    () => window.__duocad.parts.length === 0 && window.__duocad.currentDocumentName === null,
    '「新規」で空の文書にならない',
  )
  if ((await header()) === documentName) fail('新規にしてもヘッダに前のファイル名が残る')
  await page.click(`.files-row[data-name="${documentName}"] .files-open`)
  await waitFor(
    (expectedVolume) => {
      const body = window.__duocad.parts[0]
      return Boolean(body && Math.abs(body.volume - expectedVolume) < 1e-6)
    },
    '新規にした後、一覧から開き直しても同じ形にならない',
    plate.volume,
  )
  ok('「新規」で空の文書に戻り、一覧から開き直せた')

  // 削除は行内の2段階(「削除」→「本当に削除」)。
  await page.click(`.files-row[data-name="${firstName}"] .files-delete`)
  await page.waitForSelector(`.files-row[data-name="${firstName}"] .files-delete-confirm`, { timeout })
  if (!existsSync(join(workdir, firstName))) fail('1段目の「削除」でもう消えている(確認を挟んでいない)')
  await page.click(`.files-row[data-name="${firstName}"] .files-delete-confirm`)
  await waitFor(
    (name) => !(window.__duocad.documentList ?? []).some((file) => file.name === name),
    '削除したファイルが一覧から消えない',
    firstName,
  )
  if (existsSync(join(workdir, firstName))) fail('一覧から消えたのにディスクに残っている')
  if ((await look()).currentDocumentName !== documentName) fail('削除で編集中の文書が動いた')
  ok('一覧の行から削除できた(行内の2段階。ディスクからも消える)')

  // ================================================================ 6. 書き出しと読み込み
  const stepPath = join(workdir, 'out.step')
  await typeInto('#file-export-step-path', stepPath)
  await page.click('#file-export-step')
  await waitFor(() => window.__duocad.lastFile?.action === 'export_step', 'STEP書き出しの応答が返らない')
  if (!existsSync(stepPath) || statSync(stepPath).size === 0) fail('STEPが書けていない')

  const stlPath = join(workdir, 'out.stl')
  await typeInto('#file-export-stl-path', stlPath)
  await page.click('#file-export-stl')
  await waitFor(() => window.__duocad.lastFile?.action === 'export_stl', 'STL書き出しの応答が返らない')
  if (!existsSync(stlPath) || statSync(stlPath).size === 0) fail('STLが書けていない')
  ok('パネルから STEP と STL を書き出した')

  // **STEP読み込みはツールバーのコマンド**。起動して初めてパス欄が現れるので
  // **押す→打つ→Enter**の順になる(`commands/files.ts` の `IMPORT_STEP`)。
  const idsBefore = (await look()).bodies.map((body) => body.id)
  await page.click('#file-import-step')
  await typeInto('#file-import-path', stepPath)
  await page.keyboard.press('Enter')
  await waitFor(
    (previous) => window.__duocad.parts.length === previous + 1,
    'STEPを読み込めない',
    idsBefore.length,
  )
  const importedId = (await look()).bodies.find((body) => !idsBefore.includes(body.id))?.id ?? null
  if (!importedId) fail('読み込んでできたボディが特定できない')
  ok('ツールバーから STEP を読み込んだ')

  // 読み込んだのは自分を書き出した STEP なので、元の板と同じ場所に重なっている。
  // 先に全部隠してから対象だけ表示に戻し、読み込んだボディの面を指せるようにする。
  await page.click('#parts-hide-all')
  await waitFor(() => window.__duocad.parts.every((body) => !body.visible), 'ボディを隠せない')
  await page.click(`.parts-row[data-body-id="${importedId}"] input.visible-toggle`)
  // **XZ基準平面と重なる手前の壁(法線 −Y)を名指す。** ホームビューは −Y 側から見るので、板の
  // ほかの面へのレイは先に XZ 基準平面をくぐり、「スケッチ」の起動中は基準平面が勝つ。
  const faceIndex = await planarFace(page, importedId, [0, -1, 0])
  await waitFor(
    (id, index) => window.__duocad.projectFace(id, index) !== null,
    '読み込んだボディの手前の壁が画面から指せない',
    importedId,
    faceIndex,
  )
  /** 読み込んだボディの面を押す。**射影は押す直前**(起動・Esc でツールバーの行が変わる)。 */
  async function clickImportedFace(where) {
    await waitFrames(page, 2)
    const at = await faceAt(page, importedId, faceIndex)
    if (at === null) fail(`${where}: 面${faceIndex}が画面から指せない`)
    await page.mouse.click(at.x, at.y)
  }
  await clickImportedFace('読み込み直後')
  await waitFor(
    (id) =>
      (window.__duocad.selection?.items ?? []).some((item) => item.body === id && item.kind === 'face'),
    '読み込んだボディの面を選べない',
    importedId,
  )

  // 非表示のボディを除いて書き出したとき、札が個数と除外を言う(`documentFiles.svelte.ts` の `onFile`)。
  const visibleOnlyPath = join(workdir, 'visible-only.step')
  await typeInto('#file-export-step-path', visibleOnlyPath)
  await page.click('#file-export-step')
  await waitFor(
    (expected) => window.__duocad.lastFile?.path === expected,
    '表示中のみのSTEP書き出しの応答が返らない',
    visibleOnlyPath,
  )
  const exportMessage = (await look()).messages[0] ?? ''
  if (!exportMessage.includes('1個') || !exportMessage.includes('除外')) {
    fail(`札に書き出した個数と除外が出ていない: ${JSON.stringify((await look()).messages)}`)
  }
  ok(`非表示のボディを除いて書き出すと札がそれを言う("${exportMessage}")`)

  // ================================================================ 7. 即適用
  // Enterの要らないコマンド(`command.ts` の `needsEnter` が false)の骨格を固定する。
  // 材料は**面に貼る `sketch_create`**(対象は貼る先1つ・howの入力なし)。
  //   (a) 面が選ばれた状態で**起動しても発火しない**
  //   (b) **起動した後の面クリック**で発火する(Enterを押さない)
  //   (c) 起動 → Esc のあとは発火しない
  // (a)(c) は「起きないこと」の検査なので、往復ぶんの猶予を与えてから見る。
  const sketchesBefore = (await look()).sketches
  // ここが `undefined` に化けると下の比較が `undefined !== undefined` で全部素通りする。
  if (typeof sketchesBefore !== 'number') {
    fail(`スケッチの件数が数値で来ない(検査が黙って死ぬ): ${JSON.stringify(sketchesBefore)}`)
  }
  await page.click('#sketch-create')
  await graceForNothingToHappen()
  if ((await look()).sketches !== sketchesBefore) {
    fail('(a) 起動しただけでスケッチが走った(即適用が起動時に発火している)')
  }
  ok('(a) 面が選ばれた状態で起動しても発火しない')

  await page.keyboard.press('Escape')
  await clearSelectionByClickingNothing()
  await page.click('#sketch-create')
  await page.keyboard.press('Escape')
  await clickImportedFace('(c)')
  await graceForNothingToHappen()
  if ((await look()).sketches !== sketchesBefore) {
    fail('(c) Escで降りた後の面クリックでスケッチが走った')
  }
  ok('(c) 起動 → Esc のあとの面クリックでは発火しない')

  /** 今入っているスケッチの貼り先(`plane.kind`)。 */
  const activePlaneKind = async () =>
    JSON.parse(
      await page.evaluate(() => {
        const hook = window.__duocad
        const active = hook.sketch
        return JSON.stringify(active?.plane ?? null)
      }),
    )?.kind

  await clearSelectionByClickingNothing()
  await page.click('#sketch-create')
  await clickImportedFace('(b)')
  await waitFor(
    (expected) => window.__duocad.sketches.length === expected,
    '(b) 起動後の面クリックで即適用されない',
    sketchesBefore + 1,
  )
  // **面を起点に貼れたことまで見る** —— 基準平面のグリッドに取られていると
  // `setBasePlane` の同期経路になり、ここで見たい「選択が届いてから送る」待ち受け
  // (`CommandHost` の `selfSelections` を数える `$effect`)を踏んでいない。
  await waitFor(() => window.__duocad.activeSketch !== null, '(b) 貼ったスケッチへ入らない')
  const immediateKind = await activePlaneKind()
  if (immediateKind !== 'face') fail(`(b) 面ではなく ${immediateKind} に貼られた(即適用の経路が違う)`)
  ok('(b) 起動 → 面をクリック だけで面にスケッチを貼れた(Enterを押していない)')
  await page.click('#sketch-exit')
  await waitFor(() => window.__duocad.activeSketch === null, 'スケッチから抜けられない')

  // ---------------------------------------------------------- (d) 同一平面の同着
  // **基準平面に描いて押し出した面は、その基準平面とぴったり重なる。** `resolveHit` は
  // 面と基準平面を「レイ距離が近い方」で決めるので、この重なりでは2つの距離が同着になり、
  // 素の `<` では**勝敗が浮動小数の最後の桁で決まる**。実際、`fitDistance` を数学的に
  // 同値な式へ書き換えただけ(カメラ位置の差 7e-14)で、上の(b)が面ではなくXZ基準平面を
  // 取って赤くなった —— 見た目も `projectFace` が返す画素も同じままで、である。
  //
  // **(b)を1回通すだけでは検出力が無い**(同着はどちらに転んでもおかしくないので、
  // たまたま面が勝っていれば緑になる)。そこで**画角を振ってカメラを視線上で動かし**、
  // 丸め方の違う位置を何通りも作って、そのすべてで面が勝つことを見る。画角を変えても
  // 見かけの大きさは保たれる(`viewport.ts` の `setFov`)ので、同じ面を指し続けられる。
  //
  // **実測**: 同着に決着を付けない実装で、この6通りのうち**51度と55度**が
  // XZ基準平面を取った(45/47/49/53度は面。同じ実装で上の(b)も平面を取っている ——
  // (b)も画角45なのに(d)の45度は面で、**同じ画角でもクリックごとに転ぶ**)。
  // **ここは丸めの当たり外れなので、通ったからといって角度の本数を減らさないこと。**
  const tieAngles = [45, 47, 49, 51, 53, 55]
  const tieResults = []
  for (const angle of tieAngles) {
    await page.evaluate((deg) => window.__duocad.setFov(deg), angle)
    await waitFor((deg) => window.__duocad.fov === deg, '画角が変わらない', angle)
    // **件数はこの回の開始時から数える。** (b)が貼ったスケッチが残っているので、
    // `sketchesBefore + 1` を待つと**クリックの前に条件が満たされている**(実測で踏んだ)。
    const before = await page.evaluate(() => window.__duocad.sketches.length)
    await clearSelectionByClickingNothing()
    await page.click('#sketch-create')
    await clickImportedFace(`(d) 画角${angle}`)
    // 即適用は貼ると同時にそのスケッチへ入るので、`activeSketch` が立つことが手が届いた証拠になる。
    await waitFor(() => window.__duocad.activeSketch !== null, `(d) 画角${angle}で面クリックが届かない`)
    tieResults.push(`${angle}:${await activePlaneKind()}`)
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, `(d) 画角${angle}で抜けられない`)
    // **1本ずつ戻す**。残すと以降の件数の比較が全部ずれる。
    await pressUndo(page)
    await waitFor(
      (expected) => window.__duocad.sketches.length === expected,
      `(d) 画角${angle}のスケッチをundoで戻せない`,
      before,
    )
  }
  await page.evaluate(() => window.__duocad.setFov(45))
  await waitFor(() => window.__duocad.fov === 45, '画角を45へ戻せない')
  const tieBad = tieResults.filter((entry) => !entry.endsWith(':face'))
  if (tieBad.length > 0) {
    fail(
      `(d) 基準平面と同一平面の面が、画角によっては平面に取られる: ${tieBad.join(' / ')}` +
        `(全結果 ${tieResults.join(' / ')})`,
    )
  }
  ok(`(d) 基準平面と重なった面は、カメラを動かしても面が取れる(画角 ${tieAngles.join('/')} の6通り)`)

  // サーバのエラーは札へ出るだけでコンソールには出ないので、控え(`errors`)を最後に見る。
  await assertNoAppErrors(page, consoleErrors)
})
