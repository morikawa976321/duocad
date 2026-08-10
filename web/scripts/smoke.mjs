/**
 * 実ブラウザでの起動確認 — 「動くはず」で納品しないための機械検査。
 *
 * サーバが立っている状態で走らせると、**本物のマウスクリックで面を選び・数値を打ち・Enterで
 * 押し出し**、体積が変わったことと `measure_log.jsonl` に行が増えたことを確かめる。
 * WebGL・ピッキング・WebSocket・計測の器がすべて実際に動いていなければ落ちる。
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
const distance = Number.parseFloat(options.distance ?? '2')
const timeout = Number.parseInt(options.timeout ?? '45000', 10)

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
        return Boolean(hook && hook.ready && hook.state && hook.state.geometry.faces.length > 0)
      },
      { timeout, polling: 100 },
    )
    const first = await page.evaluate(() => ({
      faceCount: window.__duocad.state.face_count,
      volume: window.__duocad.state.volume,
      triangles: window.__duocad.state.geometry.faces.reduce(
        (total, face) => total + face.indices.length / 3,
        0,
      ),
      untriangulated: window.__duocad.state.geometry.untriangulated,
      canvases: document.querySelectorAll('canvas').length,
      webgl: (() => {
        const probe = document.createElement('canvas')
        return Boolean(probe.getContext('webgl2') || probe.getContext('webgl'))
      })(),
    }))
    if (!first.webgl) fail('WebGLコンテキストが取れない')
    if (first.canvases !== 1) fail(`canvasが1枚でない: ${first.canvases}`)
    if (first.untriangulated.length > 0) {
      fail(`三角形分割が取れない面がある: ${first.untriangulated}`)
    }
    ok(`接続・描画: 面 ${first.faceCount} / 三角形 ${first.triangles} / 体積 ${first.volume}`)

    // 面積が最大の平面を狙う(平らな面の押し出しは体積 = 面積 × 距離 で検算できる)
    const target = await page.evaluate(() => {
      const planes = window.__duocad.state.geometry.faces
        .filter((face) => face.kind === 'plane')
        .sort((a, b) => b.area - a.area)
      const face = planes[0]
      return face ? { index: face.index, area: face.area } : null
    })
    if (!target) fail('平面が1つも無い')

    const point = await page.evaluate((index) => {
      const position = window.__duocad.projectFace(index)
      if (!position) return null
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      return { x: position.x + rect.left, y: position.y + rect.top }
    }, target.index)
    if (!point) fail(`面 ${target.index} の画面座標が取れない(隠れている/視野外)`)

    await page.mouse.click(point.x, point.y)
    await page.waitForFunction(
      (index) => window.__duocad.selected === index,
      { timeout, polling: 50 },
      target.index,
    )
    ok(`実クリックで面を選択: 面 ${target.index} @ (${point.x.toFixed(0)}, ${point.y.toFixed(0)})`)

    // 距離の欄は確定ごとに空になるので、実キー操作で打つ(全選択+削除は空でも無害)
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

    // record は Svelte の $state プロキシなので、**ページの中で** JSON にしてから受け取る
    // (プロキシをそのまま返すと CDP のシリアライズで中身が落ちる。実測)
    async function snapshot() {
      return JSON.parse(
        await page.evaluate(() =>
          JSON.stringify({
            volume: window.__duocad.state.volume,
            faceCount: window.__duocad.state.face_count,
            selected: window.__duocad.selected,
            record: window.__duocad.lastRecord,
            messages: window.__duocad.messages,
            faces: window.__duocad.state.geometry.faces.map((face) => ({
              index: face.index,
              kind: face.kind,
              area: face.area,
            })),
          }),
        ),
      )
    }

    async function waitForRecord(op) {
      await page.waitForFunction(
        (expectedOp) => {
          const record = window.__duocad.lastRecord
          return Boolean(
            record && record.op === expectedOp && typeof record.t_geometry_updated === 'number',
          )
        },
        { timeout, polling: 50 },
        op,
      )
      return snapshot()
    }

    const areaOf = (state, index) => (state.faces.find((face) => face.index === index) || {}).area

    await typeDistance(distance)
    await page.keyboard.press('Enter')

    // **応答待ちの間に次の操作を打ち始める**。計測状態のリセットを描画後(rAF)にやる実装だと、
    // ここで打った時刻が消えて2回目の `ms_to_value` が0になる。その回帰をここで捕まえる。
    await typeDistance(1.5)

    const after = await waitForRecord(1)
    const delta = after.volume - first.volume
    const expected = target.area * distance
    if (Math.abs(delta - expected) > Math.abs(expected) * 1e-6) {
      fail(`体積の増分が面積×距離と合わない: ${delta} vs ${expected}`)
    }
    ok(`押し出し: 体積 ${first.volume} -> ${after.volume} (増分 ${delta.toFixed(4)} = 面積×${distance})`)
    if (typeof after.selected !== 'number') fail('押し出した面の後継が選択に残っていない')
    ok(`押し出した面の後継が選択に残った: 面 ${after.selected}`)

    const total = after.record.t_geometry_updated - after.record.t_command_start
    if (!(total > 0)) fail(`総所要時間が正でない: ${total}`)
    ok(`4段階の時刻が揃った: 総 ${total.toFixed(1)} ms (t_command_start→t_geometry_updated)`)

    // 2回目: 選択を再利用して数値だけ打つ(MEASURE.md の「同じ面を10回押す」経路)。
    // 値は上で「応答待ちの間に」打ってあるので、ここは確定するだけ。
    const chainedArea = areaOf(after, after.selected)
    if (typeof chainedArea !== 'number') fail('後継の面が状態に無い')
    const stillTyped = await page.$eval('#distance', (element) => element.value)
    if (stillTyped !== '1.5') fail(`応答待ち中に打った値が消えた: "${stillTyped}"`)
    await page.keyboard.press('Enter')
    const chained = await waitForRecord(2)
    const chainedDelta = chained.volume - after.volume
    if (Math.abs(chainedDelta - chainedArea * 1.5) > Math.abs(chainedArea * 1.5) * 1e-6) {
      fail(`連続押し出しの増分が合わない: ${chainedDelta} vs ${chainedArea * 1.5}`)
    }
    if (chained.record.reused_selection !== true) {
      fail('2回目が reused_selection として記録されていない')
    }
    ok(
      `連続押し出し(選択の再利用): 増分 ${chainedDelta.toFixed(4)} = 面積×1.5 / ` +
        'reused_selection=true',
    )

    // 曲面(穴の円筒)は押し出せない — 通すと壊れた形状が黙って残る(実測で確認済みの経路)
    const curved = await page.evaluate(() => {
      const face = window.__duocad.state.geometry.faces.find((item) => item.kind !== 'plane')
      return face ? { index: face.index, kind: face.kind } : null
    })
    if (!curved) {
      console.log('  skip 曲面が無いので拒否の検査をしていない')
    } else {
      const spot = await page.evaluate((index) => {
        const position = window.__duocad.projectFace(index)
        if (!position) return null
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        return { x: position.x + rect.left, y: position.y + rect.top }
      }, curved.index)
      if (!spot) {
        console.log(`  skip 曲面 ${curved.index} が画面に出ていないので拒否の検査をしていない`)
      } else {
        await page.mouse.click(spot.x, spot.y)
        await page.waitForFunction(
          (index) => window.__duocad.selected === index,
          { timeout, polling: 50 },
          curved.index,
        )
        await typeDistance(1)
        await page.keyboard.press('Enter')
        await page.waitForFunction(
          () => (window.__duocad.messages || []).some((line) => line.includes('平面の面だけ')),
          { timeout, polling: 50 },
        )
        const rejected = await snapshot()
        if (Math.abs(rejected.volume - chained.volume) > 1e-9) {
          fail(`曲面の押し出しが通ってしまった: ${chained.volume} -> ${rejected.volume}`)
        }
        ok(`曲面(${curved.kind} 面 ${curved.index})の押し出しは拒否され、形状は変わらない`)
      }
    }

    // 狙い外れの記録: 幾何が無い場所をクリックする
    const missed = await page.evaluate(async () => {
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      return [
        { x: rect.left + 4, y: rect.top + 4 },
        { x: rect.right - 4, y: rect.top + 4 },
        { x: rect.left + 4, y: rect.bottom - 4 },
        { x: rect.right - 4, y: rect.bottom - 4 },
      ]
    })
    let misses = 0
    for (const corner of missed) {
      await page.mouse.click(corner.x, corner.y)
      misses = await page.evaluate(() => window.__duocad.misses)
      if (misses > 0) break
    }
    if (misses === 0) fail('狙い外れを1度も記録できなかった(四隅すべてに形状があった?)')
    ok(`狙い外れを記録: ${misses} 回`)

    for (let step = 0; step < 2; step += 1) await page.click('#undo')
    await page.waitForFunction(
      (expectedVolume) => Math.abs(window.__duocad.state.volume - expectedVolume) < 1e-9,
      { timeout, polling: 50 },
      first.volume,
    )
    ok(`undo 2回で元の体積へ戻った: ${first.volume}`)

    // 土台(部品の読み込み・編集用ボディ)まで取り消させない。通すと編集対象が消えて画面が死ぬ。
    // ボタンは無効になり、キーボードから叩いてもサーバが拒否する(二重の守り)。
    await page.waitForFunction(
      () => document.querySelector('#undo').disabled === true,
      { timeout, polling: 50 },
    )
    ok('土台まで戻ったらundoボタンが無効になる')
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyZ')
    await page.keyboard.up('Control')
    await page.waitForFunction(
      () => (window.__duocad.messages || []).some((line) => line.includes('取り消せない')),
      { timeout, polling: 50 },
    )
    const guarded = await snapshot()
    if (Math.abs(guarded.volume - first.volume) > 1e-9) fail('土台のundo防止が効いていない')
    if (guarded.faceCount !== first.faceCount) fail('土台のundoで面数が変わった')
    ok('土台の操作はundoできず、画面も生きたまま')

    if (consoleErrors.length > 0) {
      fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    }
    ok('ブラウザのコンソールにエラーなし')

    if (measureLogPath) {
      // サーバの書き込みを待つ(WebSocketの往復ぶん)
      const deadline = Date.now() + 5000
      let lines = readMeasureLines()
      while (lines.length <= linesBefore && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 100))
        lines = readMeasureLines()
      }
      const added = lines.slice(linesBefore)
      const extrudeLines = added.filter((line) => line.kind === 'extrude')
      const missLines = added.filter((line) => line.kind === 'pick_miss')
      if (extrudeLines.length === 0) fail('measure_log.jsonl に押し出しの行が増えていない')
      if (missLines.length === 0) fail('measure_log.jsonl に狙い外れの行が増えていない')
      const line = extrudeLines[0]
      const required = [
        't_command_start',
        't_face_picked',
        't_value_entered',
        't_committed',
        't_geometry_updated',
        'ms_to_pick',
        'ms_to_value',
        'ms_to_commit',
        'ms_to_geometry',
        'ms_total',
        'face_id',
        'distance',
        'server_extrude_ms',
      ]
      const missing = required.filter((key) => line[key] === undefined)
      if (missing.length > 0) fail(`計測行に欠けている項目: ${missing.join(', ')}`)
      if (!(line.ms_total > 0)) fail(`ms_total が正でない: ${line.ms_total}`)
      if (!(line.ms_to_pick > 0)) fail(`1回目の ms_to_pick が正でない: ${line.ms_to_pick}`)
      if (!(line.ms_to_value > 0)) fail(`1回目の ms_to_value が正でない: ${line.ms_to_value}`)
      if (line.reused_selection !== false) fail('1回目が reused_selection になっている')

      // 2回目(選択を再利用した行)は、打った時間が消えていないことを見る。
      // 応答待ちの間に計測状態をリセットしていると、ここが0になって嘘の速さが記録される。
      const second = extrudeLines.find((item) => item.op === 2)
      if (!second) fail('2回目の押し出しの行が無い')
      if (second.reused_selection !== true) fail('2回目が reused_selection=true になっていない')
      if (!(second.ms_to_value > 0)) {
        fail(`2回目の ms_to_value が正でない(打った時間が消えている): ${second.ms_to_value}`)
      }
      if (!(second.ms_total > 0)) fail(`2回目の ms_total が正でない: ${second.ms_total}`)
      ok(
        `計測ログ: +${added.length} 行 (押し出し ${extrudeLines.length} / 外れ ${missLines.length}) ` +
          `op1 ms_total=${line.ms_total} server=${line.server_extrude_ms} / ` +
          `op2 ms_to_value=${second.ms_to_value} ms_total=${second.ms_total}`,
      )
    }

    console.log('smoke: PASS')
  } finally {
    await browser.close()
  }
}

main().catch((error) => {
  console.error(`smoke: FAIL ${error.message}`)
  process.exit(1)
})
