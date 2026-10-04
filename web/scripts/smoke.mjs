/**
 * 実ブラウザでの起動確認 — 「動くはず」で納品しないための機械検査。
 *
 * サーバは空の文書で立つ。このスクリプトがアプリ内の「STEP読み込み」でPCケース(27部品)を
 * 自分で読み、**既にあるものを本物のクリックとキー入力で触る**側を見る:
 *
 *   - 起動直後は無起動。モードの入口はモードの行にあり、verb の行とは別の次元
 *   - 実クリックで面を選ぶ。Shift / Ctrl はトグル、何も無い所のクリックで全解除
 *   - 入力欄にフォーカスがある間の Ctrl+Z は文書を undo しない
 *   - 距離を打つとプレビューが出て、Esc で無起動へ戻る
 *   - 基準平面は要求している間だけ指せる。スケッチの4種(円・線分・長方形・円弧)を手で描ける
 *
 * 形の契約(体積・undo・選択のリセット・可視性が履歴に載らないこと)は pytest が持つ。
 *
 *   web> node scripts/smoke.mjs
 */
import {
  assertNoAppErrors,
  openApp,
  pressUndo,
  sleep,
  typeInto as typeIntoField,
  waitOrFail,
} from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { PC_CASE_STEP, PC_CASE_SOLIDS } from './models.mjs'
import {
  clickSketchPoint,
  createSketchOnBasePlane,
  faceAt,
  faceAtWhenStill,
} from './pick.mjs'

// `--url` を渡したときだけ既に立っているサーバへ繋ぐ(既定は自分で立てる)。
// 既定の timeout を長めにしてある: 一括の可視性切り替え(26部品ぶんのWebSocket往復)は、
// 機械負荷が高いと数十秒かかることを実測した(claude/cursorが60プロセス超
// 並行していた環境。1回の往復自体は軽いが、混雑時はスケジューリング待ちが積み上がる)。
runSmoke('smoke', { attach: true }, async ({ options, browser, url, timeout }) => {
  const stepPath = options.step ?? PC_CASE_STEP
  const expectedParts = Number.parseInt(options['expect-parts'] ?? String(PC_CASE_SOLIDS), 10)
  const distance = Number.parseFloat(options.distance ?? '1')
  {
    const { page, consoleErrors } = await openApp(browser, url, { timeout })
    const typeInto = (selector, value) => typeIntoField(page, selector, value, { delay: 20 })

    // -------------------------------------------------------------- 項番0: 起動直後は無起動
    // **通常モードの既定は「どのコマンドも起動していない」**
    // (`CommandHost.svelte`のdocstring参照)。
    // ページを開いた直後(サーバの使い回しに関わらず、CommandHostはブラウザ側のローカル状態
    // なので毎回まっさらに始まる)、まだ何も操作していない時点でこれを確かめる。
    const idleBefore = await page.evaluate(() => ({
      abort: document.querySelector('#command-abort') !== null,
      prompt: document.querySelector('#command-prompt')?.textContent ?? null,
      onButtons: document.querySelectorAll('.command-host button.on').length,
      inputs: document.querySelectorAll('.command-host input').length,
    }))
    if (idleBefore.abort) fail('起動直後なのに#command-abortがDOMにある(無起動のはず)')
    if (idleBefore.prompt !== '') {
      fail(`起動直後の#command-promptが空文字でない: "${idleBefore.prompt}"`)
    }
    if (idleBefore.onButtons !== 0) {
      fail(`起動直後なのにコマンドボタンが押された状態: ${idleBefore.onButtons}個`)
    }
    if (idleBefore.inputs !== 0) fail(`起動直後なのに引数の入力欄が出ている: ${idleBefore.inputs}個`)
    ok(
      '起動直後: どのコマンドも起動していない' +
        '(#command-abort無し・#command-prompt空・押されたボタン無し・入力欄無し)',
    )

    await page.click('#extrude')
    await page.waitForFunction(
      () => document.querySelector('#command-abort') !== null && document.querySelector('#distance') !== null,
      { timeout, polling: 50 },
    )
    ok('#extrudeを押すと#command-abortと#distanceが両方出た')

    await page.keyboard.press('Escape')
    await page.waitForFunction(
      () => document.querySelector('#command-abort') === null && document.querySelector('#distance') === null,
      { timeout, polling: 50 },
    )
    ok('Escで無起動へ戻った(#command-abort・#distanceともDOMから消えた)')

    // ------------------------------------------------- 項番0b: モードはverbより1つ上の次元
    // **スケッチの入口はverbの行ではなくモードの行にある。** ここが見るのは**次元の分離**
    // そのもの: 入口はモードの行(`.toolbar .mode-row`)にあってverbの行
    // (`.command-host`)には無い / 入った先ではverbのボタンが1つも無い。
    const modeRowIdle = await page.evaluate(() => ({
      mode: window.__duocad.mode,
      sketchInModeRow: document.querySelector('.toolbar .mode-row #sketch-create') !== null,
      optimizeInModeRow: document.querySelector('.toolbar .mode-row #optimize-enter') !== null,
      sketchInCommandHost: document.querySelector('.command-host #sketch-create') !== null,
    }))
    if (modeRowIdle.mode !== 'idle') fail(`起動直後のモードが idle でない: ${modeRowIdle.mode}`)
    if (!modeRowIdle.sketchInModeRow || !modeRowIdle.optimizeInModeRow) {
      fail(`モードの入口がモードの行に無い: ${JSON.stringify(modeRowIdle)}`)
    }
    if (modeRowIdle.sketchInCommandHost) {
      fail('「スケッチ」がverbの行(.command-host)に居る —— 入口がverbの次元へ戻っている')
    }
    ok('通常モード: モードの入口2つはモードの行にあり、verbの行には無い')

    await page.click('#sketch-create')
    await page.waitForFunction(() => window.__duocad.mode === 'sketch-pending', {
      timeout,
      polling: 50,
    })
    const pending = await page.evaluate(() => ({
      activeSketch: window.__duocad.activeSketch,
      // 貼る先待ちは**起動済み**で入る(`CommandHost` の `startCommandId`)。基準平面が
      // 指せていることが、即適用の待ち受けまで構えている唯一の外から見える証拠になる。
      wantsBasePlanes: window.__duocad.wantsBasePlanes,
      armedButtons: document.querySelectorAll('.command-host button.on').length,
      // モードの行が入れ替わっている(入口ではなく札と「抜ける」)。
      exit: document.querySelector('.toolbar .mode-row #sketch-exit') !== null,
      // **verbのボタンは1つも無い**(押し出しはこのモードには居ない)。
      extrude: document.querySelector('#extrude') !== null,
    }))
    if (pending.activeSketch !== null) fail('貼る先待ちなのにスケッチが開いている')
    if (!pending.wantsBasePlanes) fail('貼る先待ちなのに基準平面を指せない(起動していない)')
    if (pending.armedButtons !== 1) {
      fail(`貼る先待ちで起動中のコマンドが1つでない: ${pending.armedButtons}個`)
    }
    if (!pending.exit) fail('貼る先待ちに「抜ける」が無い(モードから出られない)')
    if (pending.extrude) fail('貼る先待ちなのに押し出しのボタンが出ている(モードの分離が壊れている)')
    ok('「スケッチ」を押すと貼る先待ちへ入る(起動済み・verbのボタンは消える・抜けるが出る)')

    await page.click('#sketch-exit')
    await page.waitForFunction(
      () => window.__duocad.mode === 'idle' && document.querySelector('#extrude') !== null,
      { timeout, polling: 50 },
    )
    ok('「抜ける」で通常モードへ戻る(verbの行が返ってくる)')

    // サーバは空で立つ。前回の実行のサーバを使い回している場合は既に読まれている。
    const partCount = await page.evaluate(() => window.__duocad.parts.length)
    if (partCount === 0) {
      // **STEP読み込みはツールバーのコマンド**(`commands/files.ts` の `IMPORT_STEP`・
      // `FilesPanel.svelte` のdocstring参照)。他のverbと同じく `CommandHost` を通るので、
      // **押す(起動)→打つ→Enter(確定)**の3手になる —— 起動して初めて
      // `#file-import-path` がDOMに出るため、先に打つことはできない。
      await page.click('#file-import-step')
      await typeInto('#file-import-path', stepPath)
      await page.keyboard.press('Enter')
      await page.waitForFunction(
        (expected) => window.__duocad.parts.length === expected,
        { timeout, polling: 100 },
        expectedParts,
      )
      ok(`STEP読み込みで${expectedParts}部品が編集可能な状態で入った`)
    } else if (partCount === expectedParts) {
      ok('既に読み込み済み(前回の実行のサーバを使い回している)')
    } else {
      fail(
        `部品数が ${partCount} 個(空でも ${expectedParts} でもない)。サーバを起動し直す`,
      )
    }

    // サーバは長寿命(このスクリプトを何度も同じプロセスへ再接続する運用がありうる)。
    // 前回の実行が可視性を変えたまま中断していた場合に備え、**必ず全表示から始める**。
    await page.click('#parts-show-all')
    await page.waitForFunction(
      () => window.__duocad.parts.every((b) => b.visible),
      { timeout, polling: 50 },
    )

    const first = await page.evaluate(() => ({
      bodyCount: window.__duocad.parts.length,
      visibleCount: window.__duocad.parts.filter((b) => b.visible).length,
      totalFaces: window.__duocad.parts.reduce((sum, b) => sum + b.face_count, 0),
      canvases: document.querySelectorAll('canvas').length,
      webgl: (() => {
        const probe = document.createElement('canvas')
        return Boolean(probe.getContext('webgl2') || probe.getContext('webgl'))
      })(),
    }))
    if (!first.webgl) fail('WebGLコンテキストが取れない')
    if (first.canvases !== 1) fail(`canvasが1枚でない: ${first.canvases}`)
    ok(
      `接続・描画: 部品 ${first.bodyCount}(可視 ${first.visibleCount}) / 面(全部品合計) ${first.totalFaces}`,
    )

    // -------------------------------------------------------------- 項番1: パーツ一覧が27件
    const rowCount = await page.evaluate(() => document.querySelectorAll('.parts-row').length)
    if (rowCount !== first.bodyCount) {
      fail(`パーツ一覧の行数(${rowCount})が部品数(${first.bodyCount})と一致しない`)
    }
    ok(`パーツ一覧: ${rowCount} 行(部品数と一致)`)

    // -------------------------------------------------------------- 実クリックで面を選ぶ
    // **27部品全部が見えている状態は密集していて、多くの面が他パーツに隠れている**
    // (実測で 0/94 面しか指せない部品もあった)。特定の1部品に決め打ちすると
    // 「隠れていて選べない」だけで失敗しうるので、**pickできる平面が見つかるまで部品を
    // 面数の小さい順に探す**。
    const extrudeChoice = await page.evaluate(() => {
      const parts = [...window.__duocad.parts].sort((a, b) => a.face_count - b.face_count)
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      for (const part of parts) {
        const geometry = window.__duocad.getGeometry(part.body_id)
        if (!geometry) continue
        const planes = [...geometry.faces]
          .filter((face) => face.kind === 'plane')
          .sort((a, b) => b.area - a.area)
        for (const face of planes) {
          const position = window.__duocad.projectFace(part.body_id, face.index)
          if (!position) continue
          return {
            bodyId: part.body_id,
            name: part.name,
            faceIndex: face.index,
            area: face.area,
            x: position.x + rect.left,
            y: position.y + rect.top,
          }
        }
      }
      return null
    })
    if (!extrudeChoice) fail('押し出し対象にできる(pickできる)平面が27部品のどこにも見つからない')
    const extrudeTarget = { body_id: extrudeChoice.bodyId, name: extrudeChoice.name }
    const target = { index: extrudeChoice.faceIndex, area: extrudeChoice.area }
    const point = { x: extrudeChoice.x, y: extrudeChoice.y }

    await page.mouse.click(point.x, point.y)
    await page.waitForFunction(
      (bodyId, index) =>
        window.__duocad.selected &&
        window.__duocad.selected.bodyId === bodyId &&
        window.__duocad.selected.faceIndex === index,
      { timeout, polling: 50 },
      extrudeTarget.body_id,
      target.index,
    )
    ok(
      `実クリックで面を選択: パーツ「${extrudeTarget.name}」面 ${target.index} @ ` +
        `(${point.x.toFixed(0)}, ${point.y.toFixed(0)})`,
    )

    // -------------------------------------------------------------- B: 複数選択(Shift / Ctrl)
    // フィレット・面取りには必須で、M1のUIは単一選択しか持っていなかった。
    // **選択の正はコア**なので、ここで見ているのは `state.selection`(コアが返した集合)である。
    // **クリックの直前に射影し直す**(使い回さない)。画面のどこかが伸縮すると
    // ビューポートのアスペクトが変わり、前に測った画素は別の面を指すようになる。
    const locateFace = (bodyId, faceIndex) => faceAt(page, bodyId, faceIndex)
    const locateFaceWhenStill = (bodyId, faceIndex) => faceAtWhenStill(page, bodyId, faceIndex)

    const secondIndex = await page.evaluate(
      (bodyId, taken) => {
        const part = window.__duocad.parts.find((entry) => entry.body_id === bodyId)
        for (let index = 1; index <= part.face_count; index += 1) {
          if (index === taken) continue
          if (window.__duocad.projectFace(bodyId, index)) return index
        }
        return null
      },
      extrudeTarget.body_id,
      target.index,
    )
    if (!secondIndex) fail(`パーツ「${extrudeTarget.name}」に2つ目の選べる面が無い(複数選択を検査できない)`)

    let second = await locateFace(extrudeTarget.body_id, secondIndex)
    if (!second) fail(`面 ${secondIndex} の位置が取れない`)
    await page.keyboard.down('Shift')
    await page.mouse.click(second.x, second.y)
    await page.keyboard.up('Shift')
    await page.waitForFunction(
      (bodyId, first, added) => {
        const items = window.__duocad.selection?.items ?? []
        const has = (index) => items.some((item) => item.body === bodyId && item.index === index)
        return items.length === 2 && has(first) && has(added)
      },
      { timeout, polling: 50 },
      extrudeTarget.body_id,
      target.index,
      secondIndex,
    )
    ok(`Shift+クリックで複数選択: 2件(面 ${target.index} と面 ${secondIndex})`)

    second = await locateFace(extrudeTarget.body_id, secondIndex)
    if (!second) fail(`面 ${secondIndex} の位置が取れない(2回目)`)
    await page.keyboard.down('Control')
    await page.mouse.click(second.x, second.y)
    await page.keyboard.up('Control')
    await page.waitForFunction(
      (bodyId, index) => {
        const selection = window.__duocad.selection
        return (
          selection?.count === 1 &&
          selection.items[0].body === bodyId &&
          selection.items[0].index === index
        )
      },
      { timeout, polling: 50 },
      extrudeTarget.body_id,
      target.index,
    )
    ok(`Ctrl+クリックで1本外した(残ったのは最初に選んだ面 ${target.index})`)

    // **何も無い所を素でクリックすると全部外れる**(「全解除」ボタンの代わりはこれ1つ)。
    // canvasの隅は部品にも
    // 浮いているパネルにも当たらない画素を選ぶ。
    const empty = await page.evaluate(() => {
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      return { x: rect.left + rect.width - 6, y: rect.top + rect.height - 6 }
    })
    await page.mouse.click(empty.x, empty.y)
    await page.waitForFunction(() => window.__duocad.selection?.count === 0, {
      timeout,
      polling: 50,
    })
    ok('何も無い所のクリックで全部外れた(旧「全解除」ボタンの代わり)')

    // 押し出しのために選び直す(以後の検査は面1つが選ばれている前提)。
    //
    // **`locateFaceWhenStill` を使う** —— 直前の「全解除」の直後は画面がまだ落ち着いて
    // おらず、素の `locateFace` だと**射影した瞬間と押した瞬間で画素の意味がずれ、
    // 隣の面に当たる**。視点を変えて指せる面が別の面になると、
    // **6回に2回ここで落ちた**(それまでの面はたまたま境界から
    // 遠く、数画素ずれても同じ面だった)。射影の前に往復を1つ挟むだけで4回連続で通った
    // ことから、原因は「面が取れない」ではなく**射影と実クリックの時間差**である。
    const again = await locateFaceWhenStill(extrudeTarget.body_id, target.index)
    if (!again) fail(`面 ${target.index} の位置が取れない(選び直し)`)
    await page.mouse.click(again.x, again.y)
    await page.waitForFunction(
      (bodyId, index) => {
        const selection = window.__duocad.selection
        return selection?.count === 1 && selection.items[0].body === bodyId && selection.items[0].index === index
      },
      { timeout, polling: 50 },
      extrudeTarget.body_id,
      target.index,
    )
    ok(`押し出す面 ${target.index} を選び直した`)

    // **通常モードの既定は無起動なので、ここで明示的に「押し出し」を起動する**。
    // 面は選択済みのまま(起動は選択を触らない)なので、#distanceだけがここで出る。
    await page.click('#extrude')
    await page.waitForSelector('#distance')
    ok('「押し出す」を起動した(#extrudeを押す1手)')

    // -------------------------------------------------------------- 入力欄のCtrl+Z
    // フォーカスが入力欄(INPUT/TEXTAREA)にあるとき、
    // Ctrl+Zはブラウザ標準のテキスト取り消しに任せ、文書のundoを発火しない。
    // 入力欄の外では従来どおり文書がundoされる(App.svelte `onWindowKeydown`)。
    async function opSnapshot() {
      return page.evaluate(() => ({
        log: window.__duocad.state.log.length,
        redo: window.__duocad.state.redo_count,
      }))
    }

    await page.click('#distance')
    await page.type('#distance', '9', { delay: 20 })
    const beforeFieldCtrlZ = await opSnapshot()
    if (beforeFieldCtrlZ.log === 0) {
      fail('入力欄Ctrl+Zの検査が意味を持たない(この時点で文書の操作履歴が0件)')
    }
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyZ')
    await page.keyboard.up('Control')
    // 発火していれば非同期(WS往復)で反映されるので、待たずに比べると「まだ届いていない
    // だけ」を「発火していない」と誤判定しかねない。3秒は伊達ではない —— 実測で
    // undo一往復に2秒近くかかることがあった(この節を書いた最初のバグ入り版で
    // 400ms待ちにしたところ「変わっていない」と誤判定し、退行検知に失敗した)。
    await sleep(3000)
    const afterFieldCtrlZ = await opSnapshot()
    if (afterFieldCtrlZ.log !== beforeFieldCtrlZ.log || afterFieldCtrlZ.redo !== beforeFieldCtrlZ.redo) {
      fail(
        `入力欄にフォーカスがあるのにCtrl+Zで文書のundoが発火した: ` +
          `log ${beforeFieldCtrlZ.log}->${afterFieldCtrlZ.log} / redo ${beforeFieldCtrlZ.redo}->${afterFieldCtrlZ.redo}`,
      )
    }
    ok('入力欄(#distance)にフォーカスがある状態のCtrl+Zは文書の操作数を変えない')

    // 入力欄の外でCtrl+Z: 従来どおり文書がundoされることを固定する(退行検知)。
    await page.evaluate(() => document.activeElement && document.activeElement.blur())
    const blurredTag = await page.evaluate(() => document.activeElement?.tagName ?? null)
    if (blurredTag === 'INPUT' || blurredTag === 'TEXTAREA') {
      fail(`blur()してもまだ入力欄にフォーカスが残っている: ${blurredTag}`)
    }
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyZ')
    await page.keyboard.up('Control')
    await page.waitForFunction(
      (beforeLog) => window.__duocad.state.log.length === beforeLog - 1,
      { timeout, polling: 50 },
      beforeFieldCtrlZ.log,
    )
    ok('入力欄の外でのCtrl+Zは従来どおり文書をundoした')

    // 元の状態へ戻す(以後の押し出し検査へ影響を残さない)。
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyY')
    await page.keyboard.up('Control')
    await page.waitForFunction(
      (beforeLog) => window.__duocad.state.log.length === beforeLog,
      { timeout, polling: 50 },
      beforeFieldCtrlZ.log,
    )

    // #distanceへ残った打ち間違いの文字を消す(以後のtypeDistanceが上書きするので実害は
    // 無いが、念のため空へ戻す)。undo/redoで選択が外れていた場合は選び直す
    // (`set_visible` は `resets_selection` を持たないので通常は外れないが、念のため)。
    await page.click('#distance')
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyA')
    await page.keyboard.up('Control')
    await page.keyboard.press('Backspace')
    const stillSelected = await page.evaluate(
      (bodyId, index) => {
        const selection = window.__duocad.selection
        return selection?.count === 1 && selection.items[0].body === bodyId && selection.items[0].index === index
      },
      extrudeTarget.body_id,
      target.index,
    )
    if (!stillSelected) {
      const reselect = await locateFace(extrudeTarget.body_id, target.index)
      if (!reselect) fail(`面 ${target.index} の位置が取れない(Ctrl+Z検査後の選び直し)`)
      await page.mouse.click(reselect.x, reselect.y)
      await page.waitForFunction(
        (bodyId, index) => {
          const selection = window.__duocad.selection
          return selection?.count === 1 && selection.items[0].body === bodyId && selection.items[0].index === index
        },
        { timeout, polling: 50 },
        extrudeTarget.body_id,
        target.index,
      )
    }

    /** 距離を打ち、**打った値を読み返して**確かめる(取りこぼしを黙って通さない)。 */
    async function typeDistance(value) {
      await typeInto('#distance', value)
      const typed = await page.$eval('#distance', (element) => element.value)
      if (typed !== String(value)) fail(`距離の入力が一致しない: "${typed}" != "${value}"`)
    }

    // -------------------------------------------------------------- B: プレビューとEsc
    // **確定前に形が見える**こと(プレビューが文書に何も残さないことは `tests/test_feature_ui.py`)。
    // 種別は人が1回選ぶまで引数が揃わない(既定値を持たない)ので、プレビューの前に結合を選ぶ。
    await page.click('#extrude-mode-join')
    await typeDistance(distance)
    await page.waitForFunction(() => window.__duocad.previewing === true, {
      timeout,
      polling: 50,
    })
    ok('距離を打つとプレビューが出た(確定前に形が見えている)')

    // **Escは既定コマンドへではなく無起動へ戻る**ので、
    // **#distance自体がDOMから消える**(#command-abortも同様)。
    await page.keyboard.press('Escape')
    await page.waitForFunction(() => window.__duocad.previewing === false, {
      timeout,
      polling: 50,
    })
    const afterEscape = await page.evaluate(() => ({
      distanceGone: document.querySelector('#distance') === null,
      abortGone: document.querySelector('#command-abort') === null,
    }))
    if (!afterEscape.distanceGone || !afterEscape.abortGone) {
      fail(`Escの後も押し出しが構えたまま(無起動に戻っていない): ${JSON.stringify(afterEscape)}`)
    }
    ok('Escで中断: プレビューが消え、無起動へ戻った')

    // ================================================================ スケッチ
    const clickPlane = (u, v) => clickSketchPoint(page, u, v)
    const createSketchOnBase = (name = 'XY') => createSketchOnBasePlane(page, name, { timeout })

    // -------------------------------------------------------------- 基準平面に描く
    //
    // **先に部品を全部隠す。** 基準平面のグリッドは原点の周りの正方形で、**内容の大きさに
    // 追随して広がる**ので、この文書のPCケースは常にその中に丸ごと覆われる ——
    // 面と平面はレイ距離で競うので(通常のオクルージョン)、部品が見えている限りグリッドは
    // 1画素も指せない。`projectBasePlane` が null を返すのはその正しい振る舞いであり、
    // 人がやることも同じ(隠してから指す)。
    await page.click('#parts-hide-all')
    await page.waitForFunction(() => window.__duocad.parts.every((item) => !item.visible), {
      timeout,
      polling: 50,
    })

    // ---------------------------------------------------------- 無起動では基準平面が指せない
    // 「起動中は指せる」側(この下の `createSketchOnBase`)だけでなく、無起動の側も
    // 見る。部品を全部隠した直後・まだ何も起動していない状態(ここまでの
    // 「平面でない面」ループはEscapeで無起動へ戻っている)で確かめる —— 隠すのを先にした
    // 状態で比べないと、「部品に隠れているから」なのか「要求していないから」なのか
    // 区別が付かない。
    const idlePlanes = await page.evaluate(() => ({
      wants: window.__duocad.wantsBasePlanes,
      XY: window.__duocad.projectBasePlane('XY'),
      XZ: window.__duocad.projectBasePlane('XZ'),
      YZ: window.__duocad.projectBasePlane('YZ'),
    }))
    if (idlePlanes.wants) {
      fail('起動していないのに基準平面を要求している(既定のコマンドは「押し出す」=面)')
    }
    if (idlePlanes.XY !== null || idlePlanes.XZ !== null || idlePlanes.YZ !== null) {
      fail(`要求していないのに基準平面へ当たる画素がある: ${JSON.stringify(idlePlanes)}`)
    }
    ok('**要求していない間は指せない**: 通常モードで基準平面へ当たる画素が1つも無い(XY/XZ/YZとも)')

    await createSketchOnBase('XY')
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.plane.kind === 'base' && active.frame)
      },
      { timeout, polling: 50 },
    )
    ok('基準平面(XY)にスケッチを作れた(部品を全非表示にしてグリッドを指した)')

    // モード操作(札・正面から見る・抜ける)は、スケッチモード中はツールバーの中に出る。
    const modeControls = await page.evaluate(() =>
      ['#sketch-mode', '#sketch-look', '#sketch-exit'].filter(
        (id) => document.querySelector(`.toolbar ${id}`) === null,
      ),
    )
    if (modeControls.length > 0) fail(`スケッチモード中なのにツールバーに無い: ${modeControls.join(' / ')}`)
    ok('スケッチモード中: モード操作がコマンドツールバーに出た')

    // 円: 中心をクリック → 半径を打つ → Enter(**Bの数値入力をそのまま使っている**)
    await page.click('#sketch-circle')
    await clickPlane(0, 0)
    await typeInto('#sketch-radius', 20)
    await page.keyboard.press('Enter')
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === 1 && active.regions.length === 1)
      },
      { timeout, polling: 50 },
    )
    ok('円を手で描けた(中心をクリック → 半径20を打つ → Enter。閉領域が1つできた)')

    // 線分: 2点をクリックで置く。**円を横切る線**なので領域が2つに割れる(検証3)
    //
    // **Enterは押さない**。線分は長さ・角度の欄を持つが `fromPoints` なので how に
    // 数えず、`needsEnter` が false になり **2点目のクリックで即適用**される。ここで
    // Enterを押さないままこの下の検査が通ることが、実機でのその証拠になる
    // (点で揃うコマンドの側。選択で揃う側は `smoke-files.mjs` の(a)(b)(c))。
    await page.click('#sketch-line')
    await clickPlane(-30, 5)
    await clickPlane(30, 5)
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === 2 && active.regions.length === 2)
      },
      { timeout, polling: 50 },
    )
    ok('線分を手で描けた(**2点目のクリックで即適用**。Enterを押していない)。**円が線で2つの領域に割れた**')

    // 打鍵だけでも置ける: 始点をクリック → 長さ・角度を打つ → Enter。**終点はクリックしない**
    // (見えているゴムひもの線がそのまま確定される)。円と交差しない位置へ置き、確かめたら
    // undo で戻すので、この下の検査は図形2つ・領域2つのまま続く。
    await page.click('#sketch-line')
    await clickPlane(-40, -30)
    await typeInto('#sketch-line-length', 20)
    await typeInto('#sketch-line-angle', 30)
    await page.keyboard.press('Enter')
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === 3 && active.regions.length === 2)
      },
      { timeout, polling: 50 },
    )
    const keyed = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      const line = active.entities.filter((entity) => entity.kind === 'line').at(-1)
      const p = line.params
      return {
        length: Math.hypot(p.x1 - p.x0, p.y1 - p.y0),
        angle: (Math.atan2(p.y1 - p.y0, p.x1 - p.x0) * 180) / Math.PI,
      }
    })
    if (Math.abs(keyed.length - 20) > 1e-6 || Math.abs(keyed.angle - 30) > 1e-6) {
      fail(`打った長さ・角度で線が引けていない: 長さ ${keyed.length} / 角度 ${keyed.angle}`)
    }
    ok(`線分を打鍵だけで引けた(始点をクリック → 長さ20・角度30を打つ → Enter。実測 ${keyed.length.toFixed(3)}mm / ${keyed.angle.toFixed(3)}度)`)
    await pressUndo(page)
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === 2)
      },
      { timeout, polling: 50 },
    )

    // 長方形と円弧も描ける(4種そろっていること)
    await page.click('#sketch-rectangle')
    await clickPlane(30, -30)
    await typeInto('#sketch-width', 10)
    await typeInto('#sketch-height', 6)
    await page.keyboard.press('Enter')
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === 3)
      },
      { timeout, polling: 50 },
    )
    await page.click('#sketch-arc')
    // 始点 → 終点 → 円弧上の点(中心(-40,-30)・半径4の**ちょうど半円**)。**Enterは
    // 押さない** —— 半径は `fromPoints` なので how に数えず、3点目で即適用される。
    await clickPlane(-44, -30)
    await clickPlane(-36, -30)
    await clickPlane(-40, -26)
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === 4)
      },
      { timeout, polling: 50 },
    )
    const kinds = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      return active.entities.map((entity) => entity.kind)
    })
    if (kinds.join(',') !== 'circle,line,rectangle,arc') fail(`4種そろっていない: ${kinds.join(',')}`)
    ok('長方形と円弧も描けた(線分・円・長方形・円弧の4種すべて)')

    // `PC_CASE_IMPORT_WARNINGS`(三角形分割の警告)はこの本では出ない —— 出るのは問題の部品へ寄って
    // 細かく測り直したときで、この本は全体表示のまま面を触り、スケッチは部品を全部隠してから描く。
    await assertNoAppErrors(page, consoleErrors)
  }
})
