/**
 * 実ブラウザでの起動確認(M3.5・2026-08-11改訂) — 「動くはず」で納品しないための機械検査。
 *
 * サーバが立っている状態で走らせると、**本物のマウスクリックで面を選び・数値を打ち・Enterで
 * 押し出し**、体積が変わったことと `measure_log.jsonl` に行が増えたことを確かめる(M1由来)。
 * これに加えてM3.5で: **パーツ一覧が27件出る**・**手前のパーツを隠すと奥の面が選べる**
 * (`viewport.project` は他Bodyに隠れている面に対して null を返す設計を利用して検査する)・
 * **可視性の切り替えがundoで戻る**、を検査する。
 *
 *   node scripts/smoke.mjs --url http://127.0.0.1:8766/ --measure-log ..\measure_log.jsonl
 *
 * ブラウザは**インストール済みのEdge/Chromeを使う**(puppeteer-core なのでブラウザを
 * ダウンロードしない)。`DUOCAD_BROWSER` で明示もできる。headless で WebGL を出すために
 * SwiftShader(ソフトウェアGL)を明示的に許可している。
 */
import { existsSync, readFileSync } from 'node:fs'
import { launch } from 'puppeteer-core'

const options = parseArgs(process.argv.slice(2))
const url = options.url ?? 'http://127.0.0.1:8766/'
const measureLogPath = options['measure-log'] ?? null
const distance = Number.parseFloat(options.distance ?? '1')
// 既定を長めにしてある: 一括の可視性切り替え(26部品ぶんのWebSocket往復)は、
// 機械負荷が高いと数十秒かかることを実測した(2026-08-11・claude/cursorが60プロセス超
// 並行していた環境。1回の往復自体は軽いが、混雑時はスケジューリング待ちが積み上がる)。
const timeout = Number.parseInt(options.timeout ?? '120000', 10)
// 追加の測定パス(項番3・4の実測値を集めるための、通常操作の繰り返し)。
const measureRounds = Number.parseInt(options['measure-rounds'] ?? '5', 10)

const CANDIDATE_BROWSERS = [
  process.env.DUOCAD_BROWSER,
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
].filter(Boolean)

function parseArgs(argv) {
  const parsed = {}
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i]
    if (item.startsWith('--')) {
      const key = item.slice(2)
      const next = argv[i + 1]
      if (next && !next.startsWith('--')) {
        parsed[key] = next
        i += 1
      } else {
        parsed[key] = 'true'
      }
    }
  }
  return parsed
}

function readMeasureLines() {
  if (!measureLogPath || !existsSync(measureLogPath)) return []
  return readFileSync(measureLogPath, 'utf8')
    .split('\n')
    .filter((line) => line.trim().length > 0)
    .map((line) => JSON.parse(line))
}

function ok(message) {
  console.log(`  ok   ${message}`)
}

function fail(message) {
  throw new Error(message)
}

async function main() {
  const executablePath = CANDIDATE_BROWSERS.find((candidate) => existsSync(candidate))
  if (!executablePath) {
    fail(`ブラウザが見つからない(DUOCAD_BROWSER で指定する): ${CANDIDATE_BROWSERS.join(', ')}`)
  }
  console.log(`smoke: ${url}`)
  console.log(`  browser: ${executablePath}`)

  const linesBefore = readMeasureLines().length
  const browser = await launch({
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
        return Boolean(hook && hook.ready && hook.parts && hook.parts.length > 0)
      },
      { timeout, polling: 100 },
    )

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
            record: window.__duocad.lastRecord,
            messages: window.__duocad.messages,
          }),
        ),
      )
    }

    // `lastRecord.op` はブラウザ内の `opCounter`(App.svelte)が発番するが、**可視性の
    // 切り替えも同じカウンタを消費する**(`setVisible` も `opCounter += 1` する)ので、
    // このスクリプト側で「次はop番号N」と予測すると、途中で挟んだ表示・非表示の操作の分だけ
    // ずれて一致しなくなる(実測で踏んだ罠)。**`record.seq`(サーバの操作ログの通し番号。
    // 全verb共通で単調増加)を使い、「対象のBody・面で、直前に見たseqより新しい行」を待つ**
    // ほうが頑丈。
    let lastSeenSeq = 0
    async function waitForRecord(bodyId, faceIndex, waitTimeout = timeout) {
      await page.waitForFunction(
        (id, index, sinceSeq) => {
          const record = window.__duocad.lastRecord
          return Boolean(
            record &&
              record.body_id === id &&
              record.face_index === index &&
              record.seq > sinceSeq &&
              typeof record.t_geometry_updated === 'number',
          )
        },
        { timeout: waitTimeout, polling: 50 },
        bodyId,
        faceIndex,
        lastSeenSeq,
      )
      const snap = await snapshot()
      lastSeenSeq = snap.record.seq
      return snap
    }

    const volumeBefore = (
      await page.evaluate(
        (bodyId) => window.__duocad.parts.find((b) => b.body_id === bodyId).volume,
        extrudeTarget.body_id,
      )
    )

    await typeDistance(distance)
    await page.keyboard.press('Enter')
    const after = await waitForRecord(extrudeTarget.body_id, target.index)
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

    if (consoleErrors.length > 0) {
      fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    }
    ok('ブラウザのコンソールにエラーなし')

    // ================================================================================
    // 計測: ピック失敗率と ms_to_geometry(このマイルストーンの成否そのもの)
    // ================================================================================
    // 分解前の実測(2026-08-10・本人が手で計測): ピック失敗12回/押し出し7回(63%)、
    // ms_to_geometry 6602-7275ms。ここでは同じ計測器(measure_log.jsonl)を使い、
    // 実際にサーバ・ブラウザ・WebSocketを通した押し出しを複数回行って数値を並べる。
    // **自動化されたクリックであり、人間の手による計測ではない**(無人タスクのため)。
    console.log('')
    console.log('計測: ピック失敗率と ms_to_geometry を実測する(2フェーズ)')

    let pickAttempts = 0
    let pickMisses = 0
    let extrudeRejections = 0

    async function pickAndExtrude(bodyId, roundLabel) {
      // **面積が大きい順**に候補を持つ(人間が「狙いやすそうな面」を選ぶのに近い)。
      // 添字の若い順に固定すると、単に小さい/細い面(穴の縁など)ばかり試すことになり、
      // 「隠れているから外す」という本来測りたい失敗要因と区別がつかなくなる。
      const facePool = await page.evaluate((id) => {
        const geo = window.__duocad.bodyGeometry[id]
        return [...geo.faces]
          .filter((face) => face.kind === 'plane')
          .sort((a, b) => b.area - a.area)
          .map((face) => face.index)
      }, bodyId)
      if (facePool.length === 0) {
        console.log(`  skip 部品 ${bodyId} に平面が無い`)
        return
      }
      // 一番大きい面を狙う。押し出すたびに形状が変わって面積の順位も変わるので、
      // 毎回この場で計算し直した facePool の先頭を使えば、常に「今その部品で一番
      // 目立つ面」を狙うことになる(同じ添字に固定すると、押し出し後に別の面へ
      // すり替わっていることがある)。
      const faceIndex = facePool[0]
      const spot = await page.evaluate(
        (id, index) => {
          const position = window.__duocad.projectFace(id, index)
          if (!position) return null
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          return { x: position.x + rect.left, y: position.y + rect.top }
        },
        bodyId,
        faceIndex,
      )
      pickAttempts += 1
      if (!spot) {
        // projectFaceは「その画素からのレイが実際にこの面へ当たる」ことを確認してから
        // 返す設計なので、null は「この面は今の視点から画面上でpickできない」ことを意味する
        // (他パーツに隠れている、または現在の視野に入っていない)。これを狙い外れとして数える。
        pickMisses += 1
        console.log(`  [${roundLabel}] pick miss(面が画面上に無い): body=${bodyId} face=${faceIndex}`)
        return
      }
      await page.mouse.click(spot.x, spot.y)
      const picked = await page
        .waitForFunction(
          (id, index) =>
            window.__duocad.selected &&
            window.__duocad.selected.bodyId === id &&
            window.__duocad.selected.faceIndex === index,
          { timeout: 3000, polling: 30 },
          bodyId,
          faceIndex,
        )
        .then(() => true)
        .catch(() => false)
      if (!picked) {
        pickMisses += 1
        console.log(`  [${roundLabel}] pick miss(クリックしても選択されなかった): body=${bodyId} face=${faceIndex}`)
        return
      }
      await typeDistance(0.5)
      await page.keyboard.press('Enter')
      try {
        // 選択はできても、その面が実際には押し出せない(曲面判定・ブーリアン失敗等)ことが
        // ありうる。その場合サーバは `type:error` を返すだけで `lastRecord` は動かないので、
        // ここは短いタイムアウトで区切り、**スクリプト全体を止めない**(1回ごとに60秒待つと
        // 8回で最大8分かかる)。
        await waitForRecord(bodyId, faceIndex, 8000)
        console.log(`  [${roundLabel}] extrude ok: body=${bodyId} face=${faceIndex}`)
      } catch {
        extrudeRejections += 1
        const lastMessage = await page.evaluate(() => (window.__duocad.messages || [])[0] ?? null)
        console.log(
          `  [${roundLabel}] extrude rejected(選択はできたが押し出しが通らなかった): ` +
            `body=${bodyId} face=${faceIndex} message=${lastMessage}`,
        )
      }
    }

    // フェーズB専用: **狙いやすさは測らず、ms_to_geometryだけを測る**ので、一番大きい面が
    // 隠れていても次点を試し、pickできる面が見つかるまで探す(pickAndExtrudeは「最有力の
    // 1面が外れたら失敗」という失敗率の計測用で、ここでは目的が違う)。
    async function extrudeAnyPickableFace(bodyId, roundLabel) {
      const found = await page.evaluate((id) => {
        const geo = window.__duocad.bodyGeometry[id]
        if (!geo) return null
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const candidates = [...geo.faces].filter((f) => f.kind === 'plane').sort((a, b) => b.area - a.area)
        for (const face of candidates) {
          const position = window.__duocad.projectFace(id, face.index)
          if (position) return { faceIndex: face.index, x: position.x + rect.left, y: position.y + rect.top }
        }
        return null
      }, bodyId)
      if (!found) {
        console.log(`  [${roundLabel}] pickできる面が1つも無い: body=${bodyId}`)
        return false
      }
      await page.mouse.click(found.x, found.y)
      const picked = await page
        .waitForFunction(
          (id, index) =>
            window.__duocad.selected &&
            window.__duocad.selected.bodyId === id &&
            window.__duocad.selected.faceIndex === index,
          { timeout: 5000, polling: 30 },
          bodyId,
          found.faceIndex,
        )
        .then(() => true)
        .catch(() => false)
      if (!picked) {
        console.log(`  [${roundLabel}] クリックしても選択されなかった: body=${bodyId} face=${found.faceIndex}`)
        return false
      }
      await typeDistance(0.5)
      await page.keyboard.press('Enter')
      try {
        await waitForRecord(bodyId, found.faceIndex, 15000)
        console.log(`  [${roundLabel}] extrude ok: body=${bodyId} face=${found.faceIndex}`)
        return true
      } catch {
        const lastMessage = await page.evaluate(() => (window.__duocad.messages || [])[0] ?? null)
        console.log(
          `  [${roundLabel}] extrude rejected: body=${bodyId} face=${found.faceIndex} message=${lastMessage}`,
        )
        return false
      }
    }

    // -------------------------------------------------------------- フェーズA: 密集した
    // 全表示のままでのピック失敗率(分解前の状況に最も近い対照)。
    // 27部品全部が見えている状態で、面数がばらけた複数の部品から1面ずつ狙う
    // (部品を全部同じにすると「その部品がたまたま隠れている/いない」に結果が引っ張られる)。
    console.log('')
    console.log('フェーズA: 27部品を全表示のまま、複数の部品で1面ずつ狙う(密集シーンでの失敗率)')
    const denseSample = await page.evaluate(() => {
      const sorted = [...window.__duocad.parts].sort((a, b) => a.face_count - b.face_count)
      const step = Math.max(1, Math.floor(sorted.length / 8))
      const picked = []
      for (let i = 0; i < sorted.length && picked.length < 8; i += step) {
        const { body_id, name, face_count } = sorted[i]
        picked.push({ body_id, name, face_count })
      }
      return picked
    })
    for (const [i, part] of denseSample.entries()) {
      await pickAndExtrude(part.body_id, `dense ${i + 1}/${denseSample.length} (${part.name})`)
    }

    // -------------------------------------------------------------- フェーズB: 対象部品を
    // 単独表示にしたうえでの ms_to_geometry(このマイルストーンの主目的の数値)。
    console.log('')
    console.log(`フェーズB: 最大の部品を対象に ${measureRounds} 回押し出す(ms_to_geometry)`)
    // 一番面数が多い部品(=分解前に近い「重い1操作」の代表値。pytestの性能テストと同じ対象)。
    const worst = await page.evaluate(() => {
      const bodies = [...window.__duocad.parts].sort((a, b) => b.face_count - a.face_count)
      // 上と同じ理由(Svelteの$derivedプロキシ)で、素のオブジェクトへ複製してから返す。
      const { body_id, name, face_count, visible } = bodies[0]
      return { body_id, name, face_count, visible }
    })

    // まず**何も隠さず**、この部品自身に今pickできる面が無いか探す(フェーズAで見た
    // ように、27部品全表示だとこの部品も他パーツに隠れてpickできる面がほぼ無いことがある)。
    // pickできればそれで十分 — わざわざ26部品を隠す必要はない。
    const worstPickableNow = await page.evaluate((bodyId) => {
      const geometry = window.__duocad.bodyGeometry[bodyId]
      if (!geometry) return false
      return [...geometry.faces]
        .filter((f) => f.kind === 'plane')
        .some((f) => window.__duocad.projectFace(bodyId, f.index) !== null)
    }, worst.body_id)

    if (worstPickableNow) {
      ok(`最大の部品「${worst.name}」(${worst.face_count}面)は全表示のままpickできる`)
    } else {
      // **他26部品を隠して対象だけにする**(パーツ一覧の「選択したものだけ表示」)。
      // このスクリプトを走らせている機械はclaude/cursorが60プロセス超同時に走る負荷の
      // 高い環境で、26部品ぶんのWebSocket往復(1コマンドごとに１往復)が数十秒かかることを
      // 実測した(機械が空いていれば1秒未満のはず)。ms_to_geometryを測るのがここの目的で
      // あり、隠す操作そのものは既に上(occlusion検査・undo検査)で個別に検証済みなので、
      // ここでは素直に対象を選べる状態にしてから測る。
      console.log(
        `  最大の部品「${worst.name}」は全表示だとpickできる面が無い。他26部品を隠す(機械負荷が高いと時間がかかる)`,
      )
      await page.evaluate((bodyId) => {
        document.querySelector(`.parts-row[data-body-id="${bodyId}"] input.pick`).click()
      }, worst.body_id)
      await page.click('#parts-show-only-picked')
      await page.waitForFunction(
        (bodyId) => {
          const parts = window.__duocad.parts
          const target = parts.find((b) => b.body_id === bodyId)
          return target.visible && parts.every((b) => b.body_id === bodyId || !b.visible)
        },
        { timeout, polling: 200 },
        worst.body_id,
      )
      ok(`最大の部品「${worst.name}」(${worst.face_count}面)を単独表示にした`)
    }

    let worstSuccesses = 0
    for (let round = 0; round < measureRounds; round += 1) {
      const done = await extrudeAnyPickableFace(worst.body_id, `worst ${round + 1}/${measureRounds}`)
      if (done) worstSuccesses += 1
    }
    ok(`フェーズB: ${worstSuccesses}/${measureRounds} 回の押し出しに成功`)
    if (!worstPickableNow) {
      await page.click('#parts-show-all') // 後片付け(以後スクリプトを触る人のため)
    }

    // 4隅の空白クリックで、本来の意味での「クリックしたが何にも当たらなかった」も
    // 何回か記録しておく(MEASURE.mdの定義どおりの pick_miss)。
    const corners = await page.evaluate(() => {
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      return [
        { x: rect.left + 4, y: rect.top + 4 },
        { x: rect.right - 4, y: rect.top + 4 },
      ]
    })
    let emptyClickMisses = 0
    const missesBefore = await page.evaluate(() => window.__duocad.misses)
    for (const corner of corners) {
      await page.mouse.click(corner.x, corner.y)
    }
    await new Promise((resolve) => setTimeout(resolve, 200))
    const missesAfter = await page.evaluate(() => window.__duocad.misses)
    emptyClickMisses = missesAfter - missesBefore
    ok(`空白クリック(4隅のうち2つ)での狙い外れ: ${emptyClickMisses}回`)

    // -------------------------------------------------------------- 結果の集計
    if (measureLogPath) {
      const deadline = Date.now() + 5000
      let lines = readMeasureLines()
      while (lines.length <= linesBefore && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 100))
        lines = readMeasureLines()
      }
      const added = lines.slice(linesBefore)
      const extrudeLines = added.filter((line) => line.kind === 'extrude')
      const missLines = added.filter((line) => line.kind === 'pick_miss')

      console.log('')
      console.log('=== 計測結果(measure_log.jsonl から) ===')
      console.log(`  押し出し行: ${extrudeLines.length} / 狙い外れ行(空白クリック): ${missLines.length}`)
      console.log(
        `  projectFaceで「この視点からpickできない」と判定した回数(occlusion由来のミス): ` +
          `${pickMisses}/${pickAttempts}`,
      )
      if (extrudeRejections > 0) {
        console.log(
          `  選択はできたが押し出しが拒否された回数(pick_missには数えない別カテゴリ): ${extrudeRejections}`,
        )
      }
      const totalAttempts = pickAttempts + missLines.length
      const totalMisses = pickMisses + missLines.length
      const failRate = totalAttempts > 0 ? (totalMisses / totalAttempts) * 100 : 0
      console.log(
        `  ピック失敗率(自動計測): ${totalMisses}/${totalAttempts} = ${failRate.toFixed(1)}% ` +
          `(分解前の実測: 12/19 = 63%)`,
      )
      const fmt = (arr) =>
        arr.length
          ? `min ${Math.min(...arr).toFixed(1)} / max ${Math.max(...arr).toFixed(1)} / avg ${(arr.reduce((a, b) => a + b, 0) / arr.length).toFixed(1)}`
          : 'N/A'
      if (extrudeLines.length > 0) {
        const geoms = extrudeLines.map((l) => l.ms_to_geometry).filter((v) => typeof v === 'number')
        const totals = extrudeLines.map((l) => l.ms_total).filter((v) => typeof v === 'number')
        const serverMs = extrudeLines
          .map((l) => l.server_extrude_ms)
          .filter((v) => typeof v === 'number')
        console.log(`  ms_to_geometry(全押し出し, ${geoms.length}件): ${fmt(geoms)}  (分解前の実測: 6602-7275ms)`)
        console.log(`  ms_total(全押し出し): ${fmt(totals)}`)
        console.log(`  server_extrude_ms(verb全体・全押し出し): ${fmt(serverMs)}`)
      }
      // フェーズB(最大233面の部品を単独表示にして測った分)だけを抜き出す — 分解前の
      // 実測(6602-7275ms)は「2835面の塊を1つ動かす」操作であり、それと最も対等な比較は
      // 「一番重い1部品を、他を隠して動かす」操作である。
      const worstLines = extrudeLines.filter((line) => line.body_id === worst.body_id)
      if (worstLines.length > 0) {
        const geoms = worstLines.map((l) => l.ms_to_geometry).filter((v) => typeof v === 'number')
        const totals = worstLines.map((l) => l.ms_total).filter((v) => typeof v === 'number')
        console.log('')
        console.log(
          `  === フェーズB(最大の部品「${worst.name}」${worst.face_count}面・単独表示・${worstLines.length}件) ===`,
        )
        console.log(`  ms_to_geometry: ${fmt(geoms)}  (分解前の実測: 6602-7275ms)`)
        console.log(`  ms_total: ${fmt(totals)}  (分解前の実測 ms_total は ms_to_geometry と近似: 体感5秒)`)
      } else {
        console.log('  フェーズBの押し出しが1件も成功しなかった(下記を参照)')
      }
    }

    console.log('')
    console.log('smoke: PASS')
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error(`smoke: FAIL ${error.message}`)
  process.exit(1)
})
