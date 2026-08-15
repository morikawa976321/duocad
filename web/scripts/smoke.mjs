/**
 * 実ブラウザでの起動確認(M3.5・2026-08-15改訂) — 「動くはず」で納品しないための機械検査。
 *
 * サーバは空の文書で立つ(`--step` / `--no-step` は廃止済み)。このスクリプトがアプリ内の
 * 「STEP読み込み」でPCケースを自分で読み、そのあと**本物のマウスクリックで面を選び・数値を打ち・
 * Enterで押し出し**、体積が変わったことを確かめる(M1由来)。
 * これに加えてM3.5で: **パーツ一覧が27件出る**・**手前のパーツを隠すと奥の面が選べる**
 * (`viewport.project` は他Bodyに隠れている面に対して null を返す設計を利用して検査する)・
 * **可視性の切り替えがundoで戻る**、を検査する。
 *
 *   web> node scripts/smoke.mjs
 *
 * **サーバはこのスクリプトが自分で立てる**(`web/scripts/server.mjs`)。
 * 既に立っているものへ繋ぐときだけ `--url http://127.0.0.1:8766/` を渡す。
 *
 * ブラウザは**インストール済みのEdge/Chromeを使う**(puppeteer-core なのでブラウザを
 * ダウンロードしない)。`DUOCAD_BROWSER` で明示もできる。headless で WebGL を出すために
 * SwiftShader(ソフトウェアGL)を明示的に許可している。
 */
import { existsSync } from 'node:fs'
import { findBrowser, launchBrowser } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { startServer } from './server.mjs'
import { PC_CASE_STEP, PC_CASE_SOLIDS } from './models.mjs'

const options = parseArgs(process.argv.slice(2))
// `--url` を渡したときだけ既に立っているサーバへ繋ぐ(既定は自分で立てる)。
const attachUrl = options.url ?? null
const wsPort = options.port ?? '8775'
const httpPort = options['http-port'] ?? '8776'
const stepPath = options.step ?? PC_CASE_STEP
const expectedParts = Number.parseInt(options['expect-parts'] ?? String(PC_CASE_SOLIDS), 10)
const distance = Number.parseFloat(options.distance ?? '1')
// 既定を長めにしてある: 一括の可視性切り替え(26部品ぶんのWebSocket往復)は、
// 機械負荷が高いと数十秒かかることを実測した(2026-08-11・claude/cursorが60プロセス超
// 並行していた環境。1回の往復自体は軽いが、混雑時はスケジューリング待ちが積み上がる)。
const timeout = Number.parseInt(options.timeout ?? '120000', 10)




async function main() {
  const executablePath = findBrowser()
  console.log(`smoke: browser ${executablePath}`)

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
    // どのURLで落ちたかを残す(「404が1件」だけ言われても原因が分からない)
    page.on('response', (response) => {
      if (response.status() >= 400) {
        consoleErrors.push(`HTTP ${response.status()} ${response.url()}`)
      }
    })

    await page.goto(url, { waitUntil: 'load', timeout })

    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        return Boolean(hook && hook.ready)
      },
      { timeout, polling: 100 },
    )

    async function typeInto(selector, value) {
      await page.click(selector)
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyA')
      await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await page.type(selector, String(value), { delay: 20 })
    }

    // サーバは空で立つ。前回の実行のサーバを使い回している場合は既に読まれている。
    const partCount = await page.evaluate(() => window.__duocad.parts.length)
    if (partCount === 0) {
      await page.click('#file-import-step')
      await typeInto('#file-path', stepPath)
      await page.keyboard.press('Enter')
      await page.waitForFunction(
        (expected) => window.__duocad.parts.length === expected,
        { timeout, polling: 100 },
        expectedParts,
      )
      const references = await page.evaluate(() => {
        const refs = window.__duocad.state.bodies.filter((body) => body.kind === 'reference')
        return {
          count: refs.length,
          hidden: refs.filter((body) => body.visible === false).length,
        }
      })
      if (references.count !== expectedParts || references.hidden !== expectedParts) {
        fail(
          `参照ボディが隠れていない: ${references.count} 個中 非表示 ${references.hidden} ` +
            `(期待 ${expectedParts} 個すべて visible === false)`,
        )
      }
      ok(
        `STEP読み込みで${expectedParts}部品が編集可能な状態で入った` +
          `(参照ボディ ${references.count} 個はすべて非表示)`,
      )
      // **Escで既定のコマンド(押し出す)へ戻す**。ツールバーからコマンドを起動すると
      // 確定後もそのコマンドが構えたままで(`CommandHost.commit` は `commandId` を戻さない)、
      // 面を選んでも距離の入力欄 `#distance` が出ない —— 以後の押し出しの段が
      // 「セレクタが無い」で落ちる(2026-08-15に実際に踏んだ)。
      await page.keyboard.press('Escape')
      await page.waitForFunction(() => document.querySelector('#file-path') === null, {
        timeout,
        polling: 50,
      })
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

    // -------------------------------------------------------------- 項番2: 手前を隠すと奥が選べる
    // 「見えないものは指せない」の逆を確かめる: 全部見えている状態でどの面がpickできないかを
    // 数え、他の全パーツを隠して対象パーツだけにしたときにpickできる面が増えることを見る。
    // (viewport.project は他Bodyに隠れている面に対して null を返す設計。viewport.ts 参照)
    const occlusionTarget = await page.evaluate(() => {
      // 一番面数が多い部品(=占有範囲が広く、他パーツに隠されにくい)以外から選ぶ:
      // 中央値くらいの部品を対象にする(他の26個に囲まれて隠れている可能性が高い)。
      const bodies = [...window.__duocad.parts].sort((a, b) => a.face_count - b.face_count)
      const middle = bodies[Math.floor(bodies.length / 2)]
      return { bodyId: middle.body_id, name: middle.name, faceCount: middle.face_count }
    })
    const beforeOcclusion = await page.evaluate((bodyId) => {
      const body = window.__duocad.parts.find((b) => b.body_id === bodyId)
      let pickable = 0
      for (let index = 1; index <= body.face_count; index += 1) {
        if (window.__duocad.projectFace(bodyId, index)) pickable += 1
      }
      return { pickable, total: body.face_count }
    }, occlusionTarget.bodyId)

    // 対象パーツだけ残して他を全部隠す(パーツ一覧の一括操作「選択したものだけ表示」)。
    await page.evaluate((bodyId) => {
      const row = document.querySelector(`.parts-row[data-body-id="${bodyId}"] input.pick`)
      row.click()
    }, occlusionTarget.bodyId)
    await page.click('#parts-show-only-picked')
    await page.waitForFunction(
      (bodyId) => {
        const bodies = window.__duocad.parts
        const target = bodies.find((b) => b.body_id === bodyId)
        return target.visible && bodies.every((b) => b.body_id === bodyId || !b.visible)
      },
      { timeout, polling: 50 },
      occlusionTarget.bodyId,
    )
    const afterOcclusion = await page.evaluate((bodyId) => {
      const body = window.__duocad.parts.find((b) => b.body_id === bodyId)
      let pickable = 0
      for (let index = 1; index <= body.face_count; index += 1) {
        if (window.__duocad.projectFace(bodyId, index)) pickable += 1
      }
      return { pickable, total: body.face_count }
    }, occlusionTarget.bodyId)

    if (afterOcclusion.pickable < beforeOcclusion.pickable) {
      fail(
        `他パーツを隠したのにpickできる面が減った: ${beforeOcclusion.pickable} -> ${afterOcclusion.pickable}`,
      )
    }
    ok(
      `パーツ「${occlusionTarget.name}」(${occlusionTarget.faceCount}面): 他26部品が見える状態で ` +
        `pickできる面 ${beforeOcclusion.pickable}/${beforeOcclusion.total} -> 他を隠すと ` +
        `${afterOcclusion.pickable}/${afterOcclusion.total}` +
        (afterOcclusion.pickable > beforeOcclusion.pickable
          ? '(手前のパーツを隠すと奥の面が選べるようになることを確認)'
          : '(この部品は元々全周が他パーツに隠れていなかった)'),
    )

    // 全部表示に戻す(以後の検査のため)
    await page.click('#parts-show-all')
    await page.waitForFunction(
      () => window.__duocad.parts.every((b) => b.visible),
      { timeout, polling: 50 },
    )
    ok('全表示に戻した')

    // -------------------------------------------------------------- 項番5: undoで戻る(可視性)
    const undoTarget = occlusionTarget.bodyId
    const beforeHideRevision = await page.evaluate(
      (bodyId) => window.__duocad.parts.find((b) => b.body_id === bodyId).revision,
      undoTarget,
    )
    await page.click(`.parts-row[data-body-id="${undoTarget}"] input.visible-toggle`)
    await page.waitForFunction(
      (bodyId) => window.__duocad.parts.find((b) => b.body_id === bodyId).visible === false,
      { timeout, polling: 50 },
      undoTarget,
    )
    ok(`パーツ「${occlusionTarget.name}」を個別チェックボックスで非表示にした`)
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyZ')
    await page.keyboard.up('Control')
    await page.waitForFunction(
      (bodyId) => window.__duocad.parts.find((b) => b.body_id === bodyId).visible === true,
      { timeout, polling: 50 },
      undoTarget,
    )
    const afterUndoRevision = await page.evaluate(
      (bodyId) => window.__duocad.parts.find((b) => b.body_id === bodyId).revision,
      undoTarget,
    )
    if (afterUndoRevision === beforeHideRevision) {
      fail('undo後の版が非表示にする前と同じ(版が動いていない=差分が届いていない疑い)')
    }
    const checkboxRestored = await page.$eval(
      `.parts-row[data-body-id="${undoTarget}"] input.visible-toggle`,
      (element) => element.checked,
    )
    if (!checkboxRestored) fail('undo後もチェックボックスが非表示のまま(画面に反映されていない)')
    ok('Ctrl+Z(undo)で可視性が元に戻り、パネルのチェックボックスにも反映された')

    // -------------------------------------------------------------- 押し出し(体積検算)
    // 面積が大きい平面を狙う(平らな面の押し出しは体積 = 面積 × 距離 で検算できる)。
    // **27部品全部が見えている状態は密集していて、多くの面が他パーツに隠れている**
    // (上の occlusion 検査で実際に0/94という部品もあった)。特定の1部品に決め打ちすると
    // 「隠れていて選べない」だけで失敗しうるので、**pickできる面が見つかるまで部品を
    // 面数の小さい順に探す**(小さい部品を優先するのは、体積検算の面積計算が単純な平面に
    // なりやすいという以外に他意はない)。
    const extrudeChoice = await page.evaluate(() => {
      const parts = [...window.__duocad.parts].sort((a, b) => a.face_count - b.face_count)
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      for (const part of parts) {
        const geometry = window.__duocad.bodyGeometry[part.body_id]
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
    async function locateFace(bodyId, faceIndex) {
      return page.evaluate(
        (id, index) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectFace(id, index)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        bodyId,
        faceIndex,
      )
    }

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
    const summaryText = await page.$eval('#selection-summary', (element) => element.textContent.trim())
    if (!summaryText.includes('2')) fail(`選択パネルの件数表示が2件になっていない: "${summaryText}"`)
    ok(`Shift+クリックで複数選択: ${summaryText}(面 ${target.index} と面 ${secondIndex})`)

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

    // 選択パネルの「外す」ボタンでも1件ずつ落とせること。**3Dで指せない辺・頂点を
    // 人が直せる唯一の場所**なので、面で経路が生きていることをここで確かめる。
    await page.click('.selection-row .selection-remove')
    await page.waitForFunction(() => window.__duocad.selection?.count === 0, {
      timeout,
      polling: 50,
    })
    ok('選択パネルの「外す」で1件落ちた(辺・頂点を人が直す経路と同じもの)')

    // 押し出しのために選び直す(以後の検査は面1つが選ばれている前提)
    const again = await locateFace(extrudeTarget.body_id, target.index)
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

    async function typeDistance(value) {
      await page.click('#distance')
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyA')
      await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await page.type('#distance', String(value), { delay: 20 })
      const typed = await page.$eval('#distance', (element) => element.value)
      if (typed !== String(value)) fail(`距離の入力が一致しない: "${typed}" != "${value}"`)
    }

    async function snapshot() {
      return JSON.parse(
        await page.evaluate(() =>
          JSON.stringify({
            bodies: window.__duocad.parts,
            selected: window.__duocad.selected,
            applied: window.__duocad.lastApplied,
            appliedCount: window.__duocad.state?.applied_count ?? 0,
            messages: window.__duocad.messages,
          }),
        ),
      )
    }

    // 押し出し完了は `applied_count` の増加と対象 Body の体積変化で待つ
    // (可視性切り替えも client_op を消費するので、op番号の予測は使わない)。
    let lastSeenAppliedCount = await page.evaluate(
      () => window.__duocad.state?.applied_count ?? 0,
    )
    async function waitForExtrude(bodyId, previousVolume, waitTimeout = timeout) {
      await page.waitForFunction(
        (id, sinceCount, beforeVolume) => {
          const state = window.__duocad.state
          if (!state || state.applied_count <= sinceCount) return false
          const applied = window.__duocad.lastApplied
          if (!applied || applied.verb !== 'extrude_face' || applied.body_id !== id) return false
          const body = window.__duocad.parts.find((entry) => entry.body_id === id)
          return Boolean(body && Math.abs(body.volume - beforeVolume) > 1e-9)
        },
        { timeout: waitTimeout, polling: 50 },
        bodyId,
        lastSeenAppliedCount,
        previousVolume,
      )
      const snap = await snapshot()
      lastSeenAppliedCount = snap.appliedCount
      return snap
    }

    const volumeBefore = (
      await page.evaluate(
        (bodyId) => window.__duocad.parts.find((b) => b.body_id === bodyId).volume,
        extrudeTarget.body_id,
      )
    )

    // -------------------------------------------------------------- B: プレビューとEsc
    // **確定前に形が見える**こと。プレビューはサーバがverbを投機的に実行して形だけ返し、
    // 文書は元へ戻す(`Session.preview`)ので、ここで操作ログが伸びてはいけない。
    const logBefore = await page.evaluate(() => ({
      applied: window.__duocad.state.applied_count,
      log: window.__duocad.state.log.length,
    }))
    await typeDistance(distance)
    await page.waitForFunction(() => window.__duocad.previewing === true, {
      timeout,
      polling: 50,
    })
    ok('距離を打つとプレビューが出た(確定前に形が見えている)')

    // **Escで中断**。文書が一切変わっていないこと(操作ログの長さも増えない)。
    await page.keyboard.press('Escape')
    await page.waitForFunction(() => window.__duocad.previewing === false, {
      timeout,
      polling: 50,
    })
    const afterEscape = await page.evaluate(() => ({
      applied: window.__duocad.state.applied_count,
      log: window.__duocad.state.log.length,
      distance: document.querySelector('#distance').value,
    }))
    if (afterEscape.applied !== logBefore.applied || afterEscape.log !== logBefore.log) {
      fail(
        `Escで中断したのに操作が残った: applied ${logBefore.applied}->${afterEscape.applied} / ` +
          `log ${logBefore.log}->${afterEscape.log}`,
      )
    }
    if (afterEscape.distance !== '') fail(`Escの後も距離が残っている: "${afterEscape.distance}"`)
    const volumeAfterEscape = await page.evaluate(
      (bodyId) => window.__duocad.parts.find((b) => b.body_id === bodyId).volume,
      extrudeTarget.body_id,
    )
    if (Math.abs(volumeAfterEscape - volumeBefore) > 1e-9) {
      fail(`Escで中断したのに体積が変わった: ${volumeBefore} -> ${volumeAfterEscape}`)
    }
    ok('Escで中断: 操作ログ・体積とも変化なし(プレビューは1行も残さない)')

    // -------------------------------------------------------------- 確定(Enter)
    await typeDistance(distance)
    await page.keyboard.press('Enter')
    const after = await waitForExtrude(extrudeTarget.body_id, volumeBefore)
    const volumeAfter = after.bodies.find((b) => b.body_id === extrudeTarget.body_id).volume
    const delta = volumeAfter - volumeBefore
    const expected = target.area * distance
    if (Math.abs(delta - expected) > Math.abs(expected) * 1e-6) {
      fail(`体積の増分が面積×距離と合わない: ${delta} vs ${expected}`)
    }
    ok(
      `押し出し: パーツ「${extrudeTarget.name}」体積 ${volumeBefore} -> ${volumeAfter} ` +
        `(増分 ${delta.toFixed(4)} = 面積×${distance})`,
    )
    if (!after.selected || typeof after.selected.faceIndex !== 'number') {
      fail('押し出した面の後継が選択に残っていない')
    }
    ok(`押し出した面の後継が選択に残った: 面 ${after.selected.faceIndex}`)

    // -------------------------------------------------------------- undo(押し出し)
    await page.click('#undo')
    await page.waitForFunction(
      (bodyId, expectedVolume) =>
        Math.abs(
          window.__duocad.parts.find((b) => b.body_id === bodyId).volume - expectedVolume,
        ) < 1e-9,
      { timeout, polling: 50 },
      extrudeTarget.body_id,
      volumeBefore,
    )
    ok(`undo 1回で押し出し前の体積へ戻った: ${volumeBefore}`)

    // ================================================================ C: スケッチ
    // **手動操作が一級の操作モードになる**ところ(実装計画書§8b C)。スケッチはM2から
    // コアとMCPにあったが、人からは描けなかった。ここで見るのはタスクCの検証1〜5で、
    // 検証6(MCPから同じ文書に見える)は smoke-mcp.mjs 側にある。
    async function clickPlane(u, v) {
      // **クリックの直前に射影し直す**(使い回さない。面のときと同じ理由)
      const at = await page.evaluate(
        (uu, vv) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectSketchPoint(uu, vv)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        u,
        v,
      )
      if (!at) fail(`スケッチ平面の点 (${u}, ${v}) が画面の中に無い`)
      await page.mouse.click(at.x, at.y)
      return at
    }

    /** 待って落ちたら**画面の中身を添えて**失敗する(「120秒待った」だけでは切り分けられない)。 */
    async function waitForSketch(predicate, what, ...args) {
      try {
        await page.waitForFunction(predicate, { timeout, polling: 50 }, ...args)
      } catch (error) {
        const dump = await page.evaluate(() =>
          JSON.stringify({
            activeSketch: window.__duocad.activeSketch,
            sketches: window.__duocad.sketches.map((item) => ({
              id: item.sketch_id,
              plane: item.plane,
              entities: item.entities.length,
              kinds: item.entities.map((entity) => entity.kind),
              regions: item.regions.length,
            })),
            selection: window.__duocad.selection,
            messages: window.__duocad.messages,
          }),
        )
        fail(`${what}: ${dump}`)
      }
    }

    // **ページの中で JSON.stringify する**(Svelteの状態はプロキシで、そのまま返すと
    // CDPのシリアライズで空オブジェクトになる。既存の `snapshot()` と同じ理由)。
    const sketchState = async () =>
      JSON.parse(
        await page.evaluate(() => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch) ?? null
          return JSON.stringify({
            activeSketch: hook.activeSketch,
            plane: active?.plane ?? null,
            entities: active?.entities?.length ?? 0,
            kinds: (active?.entities ?? []).map((entity) => entity.kind),
            regions: active?.regions?.length ?? 0,
            log: hook.state.log.length,
            appliedCount: hook.state.applied_count,
          })
        }),
      )

    // -------------------------------------------------------------- 検証1(その1): 面に貼る
    // 直前のundoで押し出した面が選び直されている(M1からの挙動)ので、それに貼る。
    //
    // **選択が落ち着くまで待つ**。押し出し・undoの後継の選び直しは「サーバの状態が届く →
    // アプリ層が `modify_selection` を送る → もう一度状態が届く」の**もう1往復**で決まるので、
    // 体積が戻った瞬間の選択はまだ1つ前の面(押し出し後の後継)を指している。そこへ
    // スケッチを貼ろうとすると、その添字が今の形状では曲面を指していて失敗する
    // (2026-08-13に実際に踏んだ: face 34 is cylinder_partial)。**平面が選ばれるまで待つ**。
    await waitForSketch(
      (bodyId) => {
        const hook = window.__duocad
        const items = hook.selection?.items ?? []
        if (items.length !== 1 || items[0].body !== bodyId) return false
        const geometry = hook.bodyGeometry[bodyId]
        const face = geometry?.faces.find((entry) => entry.index === items[0].index)
        return Boolean(face && face.kind === 'plane')
      },
      '押し出しのundo後に平面が1つ選ばれた状態にならない',
      extrudeTarget.body_id,
    )
    await page.click('#sketch-new-face')
    await waitForSketch(() => window.__duocad.activeSketch !== null, '面に貼ったスケッチが作られない')
    const onFace = await sketchState()
    if (onFace.plane?.kind !== 'face') fail(`面に貼ったスケッチになっていない: ${JSON.stringify(onFace.plane)}`)
    ok('既存ソリッドの面にスケッチを作れた(スケッチモードに入り、平面は face 参照)')
    await page.click('#sketch-exit')
    await page.waitForFunction(() => window.__duocad.activeSketch === null, { timeout, polling: 50 })

    // -------------------------------------------------------------- 検証1(その2)・2: 基準平面に描く
    await page.click('#sketch-new-XY')
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.plane.kind === 'base' && active.frame)
      },
      { timeout, polling: 50 },
    )
    ok('基準平面(XY)にもスケッチを作れた')

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
    await page.click('#sketch-line')
    await clickPlane(-30, 5)
    await clickPlane(30, 5)
    await page.keyboard.press('Enter')
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === 2 && active.regions.length === 2)
      },
      { timeout, polling: 50 },
    )
    ok('線分を手で描けた(2点をクリック)。**円が線で2つの領域に割れた**')

    // -------------------------------------------------------------- 検証4: Escで中断
    // 長方形の引数を途中まで集めてEscを押す。文書が一切変わらないこと。
    const beforeAbort = await sketchState()
    await page.click('#sketch-rectangle')
    await clickPlane(30, -30)
    await typeInto('#sketch-width', 10)
    await typeInto('#sketch-height', 6)
    await page.keyboard.press('Escape')
    const afterAbort = await sketchState()
    if (afterAbort.entities !== beforeAbort.entities || afterAbort.log !== beforeAbort.log) {
      fail(
        `Escで中断したのに図形が残った: 図形 ${beforeAbort.entities}->${afterAbort.entities} / ` +
          `log ${beforeAbort.log}->${afterAbort.log}`,
      )
    }
    ok('Escで中断: 図形も操作ログも増えていない(スケッチの幽霊はサーバへ投げていない)')

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
    await clickPlane(-40, -30)
    await typeInto('#sketch-arc-radius', 8)
    await typeInto('#sketch-arc-start', 0)
    await typeInto('#sketch-arc-end', 120)
    await page.keyboard.press('Enter')
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === 4)
      },
      { timeout, polling: 50 },
    )
    const kinds = (await sketchState()).kinds
    if (kinds.join(',') !== 'circle,line,rectangle,arc') fail(`4種そろっていない: ${kinds.join(',')}`)
    ok('長方形と円弧も描けた(線分・円・長方形・円弧の4種すべて)')

    // -------------------------------------------------------------- 検証5: undoで1つずつ消える
    const beforeUndo = await sketchState()
    await page.click('#undo')
    await page.waitForFunction(
      (expected) => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === expected)
      },
      { timeout, polling: 50 },
      beforeUndo.entities - 1,
    )
    ok(`undoで図形が1つずつ消えた(${beforeUndo.entities} -> ${beforeUndo.entities - 1}。円弧だけが消えた)`)

    // -------------------------------------------------------------- 検証2(後半): 値の打ち直し
    // **クリックだけで置いた後も寸法を打ち直せる**(§3.4 パラメトリック・プリミティブ)。
    // 円の半径を20->12へ。領域の面積が変われば、コアが作り直したものが届いている。
    const areaBefore = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      return active.regions[0].area
    })
    // 属性セレクタで引く(図形IDは数字始まりもありうるので `#id` は使えない)
    const radiusInput = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      const circle = active.entities.find((entity) => entity.kind === 'circle')
      return `[id="param-${circle.entity_id}-radius"]`
    })
    await typeInto(radiusInput, 12)
    await page.keyboard.press('Enter')
    await page.waitForFunction(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        const circle = active.entities.find((entity) => entity.kind === 'circle')
        return Math.abs(circle.params.radius - 12) < 1e-9
      },
      { timeout, polling: 50 },
    )
    const areaAfter = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      return active.regions[0].area
    })
    if (!(areaAfter < areaBefore)) fail(`半径を小さくしたのに領域の面積が減らない: ${areaBefore} -> ${areaAfter}`)
    ok(`置いた後に寸法を打ち直せた(半径 20 -> 12。領域の面積 ${areaBefore.toFixed(1)} -> ${areaAfter.toFixed(1)} mm²)`)

    // -------------------------------------------------------------- 検証3(後半): 領域をクリックで選ぶ
    // 描くコマンドはEscで抜けて「選ぶ」に戻っている。**3Dの領域を本物のクリックで選ぶ**。
    await page.keyboard.press('Escape')
    // 描いたもの全体を画面へ収める(領域を狙うクリックの精度のため)。
    // **描き終わってから呼ぶ** —— 途中で呼ぶと、まだ描いていない位置が画面の外に出る。
    await page.click('#sketch-look')
    const regionAt = await page.evaluate(() => {
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      const position = window.__duocad.projectRegion(1)
      return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
    })
    if (!regionAt) fail('領域1の画面上の位置が取れない')
    await page.mouse.click(regionAt.x, regionAt.y)
    await page.waitForFunction(
      () => {
        const items = window.__duocad.selection?.items ?? []
        return items.length === 1 && 'sketch' in items[0] && items[0].index === 1
      },
      { timeout, polling: 50 },
    )
    ok('領域を本物のクリックで選べた(共有選択集合に領域が入った=Dの押し出しの引数になる)')

    if (consoleErrors.length > 0) {
      fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    }
    ok('ブラウザのコンソールにエラーなし')

    console.log('')
    console.log('smoke: PASS')
  } finally {
    await browser.close()
    if (server !== null) await server.stop()
  }
}

main().catch((error) => {
  console.error(`smoke: FAIL ${error.message}`)
  process.exit(1)
})
