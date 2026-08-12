/**
 * 実ブラウザでの起動確認(M3.5・2026-08-11改訂) — 「動くはず」で納品しないための機械検査。
 *
 * サーバが立っている状態で走らせると、**本物のマウスクリックで面を選び・数値を打ち・Enterで
 * 押し出し**、体積が変わったことを確かめる(M1由来)。
 * これに加えてM3.5で: **パーツ一覧が27件出る**・**手前のパーツを隠すと奥の面が選べる**
 * (`viewport.project` は他Bodyに隠れている面に対して null を返す設計を利用して検査する)・
 * **可視性の切り替えがundoで戻る**、を検査する。
 *
 *   node scripts/smoke.mjs --url http://127.0.0.1:8766/
 *
 * ブラウザは**インストール済みのEdge/Chromeを使う**(puppeteer-core なのでブラウザを
 * ダウンロードしない)。`DUOCAD_BROWSER` で明示もできる。headless で WebGL を出すために
 * SwiftShader(ソフトウェアGL)を明示的に許可している。
 */
import { existsSync } from 'node:fs'
import { launch } from 'puppeteer-core'

const options = parseArgs(process.argv.slice(2))
const url = options.url ?? 'http://127.0.0.1:8766/'
const distance = Number.parseFloat(options.distance ?? '1')
// 既定を長めにしてある: 一括の可視性切り替え(26部品ぶんのWebSocket往復)は、
// 機械負荷が高いと数十秒かかることを実測した(2026-08-11・claude/cursorが60プロセス超
// 並行していた環境。1回の往復自体は軽いが、混雑時はスケジューリング待ちが積み上がる)。
const timeout = Number.parseInt(options.timeout ?? '120000', 10)

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

    if (consoleErrors.length > 0) {
      fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    }
    ok('ブラウザのコンソールにエラーなし')

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
