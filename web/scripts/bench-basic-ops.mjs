/**
 * 実測 — 基本操作のブラウザ側の所要時間(WebSocket 往復・JSON 解析と幾何の作り直し・描画のフレーム間隔)。
 *
 *   node scripts/bench-basic-ops.mjs [--step <STEP>] [--out <json>] [--skip <A,B,C>]
 *
 * 検査ではなく物差し。合否は出さず、数値を表と JSON(`output/bench_basic_ops/client_ops.json`、git 管理外)に残す。
 * サーバ単体の費用は `tools/bench_server_ops.py` が別に測る。
 *
 *   A. 20mm 角の箱(固定費が見える最小の文書)
 *   B. PC ケース STEP(27部品 2835面。回転・ズーム・ホバー・選択・フィレット・undo)
 *   C. 長方形 150 個のスケッチ(スケッチ中のホバーと領域の選択、60領域の押し出しの描画)
 *
 * 測り方: ページに入る前に WebSocket を包んで、上り下りの時刻・大きさ・受信ハンドラの所要(JSON.parse と幾何の作り直しを含む)を
 * 控える。フレーム間隔は requestAnimationFrame の刻みで、PerformanceObserver の longtask(50ms超)も拾う。
 * headless Chrome は実 GPU を掴む(docs/SMOKE_TESTING.md「環境」)ので、描画の数字はこのデスクトップの GPU のもの。
 */
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { pressUndo, sleep, typeInto, waitFrames, watchErrors } from './browser.mjs'
import { ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'
import { clickAt, screenOf, sketchPointAt } from './pick.mjs'
import { PC_CASE_STEP } from './models.mjs'
import { REPO_ROOT } from './server.mjs'

const ROWS = []
function record(scenario, label, ms, extra = {}) {
  const row = { scenario, label, ms: ms === null ? null : Math.round(ms * 10) / 10, ...extra }
  ROWS.push(row)
  const tail = Object.entries(extra)
    .map(([k, v]) => `${k}=${typeof v === 'number' ? Math.round(v * 10) / 10 : JSON.stringify(v)}`)
    .join(' ')
  console.log(`  ${scenario} ${label.padEnd(52)} ${ms === null ? '   -   ' : String(row.ms).padStart(8)} ms  ${tail}`)
  return row
}

/** ページに入る前に仕込む計測の穴(`window.__bench`)。 */
function installBench() {
  const bench = { ws: [], longtasks: [], frames: null }
  window.__bench = bench
  const typeOf = (text, key) => {
    const m = new RegExp(`"${key}":\\s*"([a-z_]+)"`).exec(text.slice(0, 200))
    return m ? m[1] : '?'
  }
  const send = WebSocket.prototype.send
  WebSocket.prototype.send = function (data) {
    const text = String(data)
    bench.ws.push({ t: performance.now(), dir: 'out', size: text.length, type: typeOf(text, 'cmd') })
    return send.call(this, data)
  }
  const wrap = (fn) =>
    function (event) {
      const text = String(event.data)
      const entry = { t: performance.now(), dir: 'in', size: text.length, type: typeOf(text, 'type'), handler_ms: 0 }
      const t0 = performance.now()
      try {
        return fn.call(this, event)
      } finally {
        entry.handler_ms = performance.now() - t0
        bench.ws.push(entry)
      }
    }
  const desc = Object.getOwnPropertyDescriptor(WebSocket.prototype, 'onmessage')
  Object.defineProperty(WebSocket.prototype, 'onmessage', {
    configurable: true,
    get() {
      return desc.get.call(this)
    },
    set(fn) {
      desc.set.call(this, typeof fn === 'function' ? wrap(fn) : fn)
    },
  })
  const wrapped = new WeakMap()
  const add = WebSocket.prototype.addEventListener
  const remove = WebSocket.prototype.removeEventListener
  WebSocket.prototype.addEventListener = function (type, fn, ...rest) {
    if (type !== 'message' || typeof fn !== 'function') return add.call(this, type, fn, ...rest)
    let w = wrapped.get(fn)
    if (!w) {
      w = wrap(fn)
      wrapped.set(fn, w)
    }
    return add.call(this, type, w, ...rest)
  }
  WebSocket.prototype.removeEventListener = function (type, fn, ...rest) {
    return remove.call(this, type, (type === 'message' && wrapped.get(fn)) || fn, ...rest)
  }
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) bench.longtasks.push({ t: e.startTime, d: e.duration })
    }).observe({ entryTypes: ['longtask'] })
  } catch {
    // longtask が無いブラウザでは拾わない
  }
  bench.startFrames = () => {
    const ts = []
    let on = true
    const loop = (t) => {
      ts.push(t)
      if (on) requestAnimationFrame(loop)
    }
    requestAnimationFrame(loop)
    bench.frames = { ts, stop: () => (on = false) }
  }
  bench.stopFrames = () => {
    const f = bench.frames
    if (!f) return null
    f.stop()
    const ts = f.ts
    const gaps = []
    for (let i = 1; i < ts.length; i += 1) gaps.push(ts[i] - ts[i - 1])
    gaps.sort((a, b) => a - b)
    const q = (p) => (gaps.length ? gaps[Math.min(gaps.length - 1, Math.floor(p * gaps.length))] : null)
    const mean = gaps.length ? gaps.reduce((a, b) => a + b, 0) / gaps.length : null
    return { frames: ts.length, mean, p50: q(0.5), p95: q(0.95), max: gaps.length ? gaps[gaps.length - 1] : null, over33: gaps.filter((g) => g > 33).length }
  }
}

const now = (page) => page.evaluate(() => performance.now())
const wsSince = (page, since) => page.evaluate((s) => window.__bench.ws.filter((e) => e.t >= s), since)
const longtasksSince = (page, since) => page.evaluate((s) => window.__bench.longtasks.filter((e) => e.t >= s), since)

function summarizeWs(entries) {
  const inbound = entries.filter((e) => e.dir === 'in')
  const states = inbound.filter((e) => e.type === 'state')
  const firstOut = entries.find((e) => e.dir === 'out' && e.type !== 'set_view_resolution')
  const firstState = firstOut ? states.find((e) => e.t >= firstOut.t) : null
  return {
    rtt_ms: firstOut && firstState ? Math.round((firstState.t - firstOut.t) * 10) / 10 : null,
    in_msgs: inbound.length,
    in_bytes: inbound.reduce((a, e) => a + e.size, 0),
    state_msgs: states.length,
    handler_ms: Math.round(inbound.reduce((a, e) => a + e.handler_ms, 0) * 10) / 10,
    max_handler_ms: Math.round(Math.max(0, ...inbound.map((e) => e.handler_ms)) * 10) / 10,
  }
}

/** 操作 `action` を打ってから `until`(ページ内の判定)が真になるまで。WS と longtask の内訳も付ける。失敗は行に残して続ける。 */
async function measure(page, scenario, label, action, until, { args = [], timeout = 60000, extra = {} } = {}) {
  const t0 = await now(page)
  try {
    await action()
    await page.waitForFunction(until, { timeout, polling: 'raf' }, ...args)
  } catch (error) {
    const ws = summarizeWs(await wsSince(page, t0))
    const reason = String(error.message ?? error).split(/\r?\n/)[0].slice(0, 60)
    return record(scenario, `${label} [失敗: ${reason}]`, null, ws)
  }
  await waitFrames(page, 1)
  const t1 = await now(page)
  const ws = summarizeWs(await wsSince(page, t0))
  const lt = await longtasksSince(page, t0)
  return record(scenario, label, t1 - t0, { ...ws, longtasks: lt.length, longtask_ms: Math.round(lt.reduce((a, e) => a + e.d, 0)), ...extra })
}

/** node から別の WebSocket で繋ぎ、サーバがほぼ何もしないコマンド(`clear_selection`)の往復時間を測る(ブラウザ抜きの素の往復)。 */
async function wsRoundTrips(url, count = 20, makeMessage = (op) => ({ cmd: 'clear_selection', client_op: op })) {
  const config = await fetch(new URL('/duocad-config.json', url)).then((r) => r.json())
  const socket = new WebSocket(config.ws_url ?? config.wsUrl ?? `ws://127.0.0.1:${config.ws_port ?? config.port}`)
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve)
    socket.addEventListener('error', () => reject(new Error('WS に繋がらない')))
  })
  const samples = []
  let seq = 1000
  const roundTrip = () =>
    new Promise((resolve) => {
      const op = (seq += 1)
      const t0 = performance.now()
      const onMessage = (event) => {
        const text = String(event.data)
        if (text.includes('"type": "state"') || text.includes('"type":"state"')) {
          socket.removeEventListener('message', onMessage)
          resolve(performance.now() - t0)
        }
      }
      socket.addEventListener('message', onMessage)
      socket.send(JSON.stringify(makeMessage(op)))
    })
  // 最初の state(接続時の配信)を読み捨てる
  await new Promise((resolve) => socket.addEventListener('message', resolve, { once: true }))
  for (let i = 0; i < count; i += 1) samples.push(await roundTrip())
  socket.close()
  samples.sort((a, b) => a - b)
  return { p50: samples[Math.floor(samples.length / 2)], min: samples[0], max: samples[samples.length - 1] }
}

async function frameStats(page, scenario, label, during) {
  const before = await page.metrics()
  await page.evaluate(() => window.__bench.startFrames())
  await during()
  const stats = await page.evaluate(() => window.__bench.stopFrames())
  const after = await page.metrics()
  // メインスレッドの CPU 時間(JS + レイアウト + 描画の投入。GPU の時間は含まない)を1フレームあたりに割る。
  const frames = Math.max(1, stats?.frames ?? 1)
  const cpuPerFrame = ((after.TaskDuration - before.TaskDuration) * 1000) / frames
  const scriptPerFrame = ((after.ScriptDuration - before.ScriptDuration) * 1000) / frames
  return record(scenario, label, stats?.p50 ?? null, { p95: stats?.p95, max: stats?.max, frames: stats?.frames, over33: stats?.over33, cpu_ms_per_frame: cpuPerFrame, script_ms_per_frame: scriptPerFrame, heap_mb: after.JSHeapUsedSize / 1e6 })
}

/** `openApp` と同じ流れだが、ページを作る前に計測の穴を仕込み、読み込みの所要も測る。 */
async function openBenchPage(browser, url, { timeout, expectParts }) {
  const page = await browser.newPage()
  await page.setViewport({ width: 1280, height: 860 })
  const consoleErrors = watchErrors(page, { watchHttp: true })
  await page.evaluateOnNewDocument(installBench)
  const started = Date.now()
  await page.goto(url, { waitUntil: 'load', timeout })
  await page.waitForFunction(() => Boolean(window.__duocad && window.__duocad.ready), { timeout, polling: 100 })
  await page.waitForFunction(
    (n) => {
      const hook = window.__duocad
      if (!hook?.state?.document_open) return false
      const parts = hook.parts ?? []
      return parts.length >= n && parts.every((p) => hook.getGeometry(p.body_id) !== null)
    },
    { timeout, polling: 100 },
    expectParts,
  )
  await waitFrames(page, 1)
  const loadMs = Date.now() - started
  const ws = summarizeWs(await wsSince(page, 0))
  return { page, consoleErrors, loadMs, ws }
}

const center = async (page) => {
  const rect = await page.evaluate(() => {
    const r = document.querySelector('canvas')?.getBoundingClientRect()
    return r ? { x: r.left, y: r.top, w: r.width, h: r.height } : null
  })
  return { x: rect.x + rect.w / 2, y: rect.y + rect.h / 2, w: rect.w, h: rect.h, left: rect.x, top: rect.y }
}

/** いまの要求(`viewResolution`)の指紋(`waitMatched` の `changedFrom`)。 */
const resolutionPrint = (page) => page.evaluate(() => JSON.stringify(window.__duocad.viewResolution?.() ?? null))

/**
 * 届いた形が今の要求(`viewResolution`)と一致した状態が `stableMs` 続くまで待ち、最初に一致した時刻までの経過を返す。
 * `changedFrom` を渡すと、要求がその指紋から変わるまで(最大 `changeTimeout`)は一致と見なさない: 回転を離した後も
 * 減衰で 600 ms 以上動き続けることがあり、その間は要求が古いまま「揃っている」に見える(1 ms と 2.4 秒に割れていた)。
 */
async function waitMatched(page, startedAt, { timeout = 120000, stableMs = 600, changedFrom = null, changeTimeout = 5000 } = {}) {
  const deadline = Date.now() + timeout
  let matchedSince = null
  for (;;) {
    const waitingForChange =
      changedFrom !== null && Date.now() - startedAt < changeTimeout && (await resolutionPrint(page)) === changedFrom
    const mismatched = waitingForChange ? 1 : await page.evaluate(() => {
      const hook = window.__duocad
      const resolution = hook.viewResolution?.() ?? null
      const focus = resolution?.focus ?? null
      let bad = 0
      for (const part of hook.parts ?? []) {
        const g = hook.getGeometry(part.body_id)
        if (!g) {
          bad += 1
          continue
        }
        const fine = g.fine_face_count ?? g.face_count
        const wanted = focus === null ? null : (focus[part.body_id] ?? null)
        const wantedFine = wanted === null ? g.face_count : wanted.length
        const off = resolution !== null && wantedFine > 0 && Math.abs(g.deviation - resolution.deviation) > resolution.deviation * 1e-6
        if (fine !== wantedFine || off) bad += 1
      }
      return bad
    })
    const t = Date.now()
    if (mismatched === 0) {
      if (matchedSince === null) matchedSince = t
      if (t - matchedSince >= stableMs) return matchedSince - startedAt
    } else {
      matchedSince = null
    }
    if (t > deadline) throw new Error(`${timeout}ms 以内に形が要求へ揃わなかった(mismatched=${mismatched})`)
    await sleep(50)
  }
}

/** 画面に見えている面を1つ(`projectFace` が座標を返すもの)。 */
async function visibleFace(page, bodyId, { from = 1, limit = 300 } = {}) {
  for (let index = from; index <= limit; index += 1) {
    const at = await screenOf(page, 'projectFace', bodyId, index)
    if (at) return { index, at }
  }
  return null
}

/** 選択・フィレット・undo の一連(箱でも PC ケースでも同じ手)。 */
async function selectFilletUndo(page, mcp, scenario, bodyId, { radius }) {
  // 全体表示に戻してから(ズーム後は対象ボディが画面に無いことがある)
  await page.evaluate(() => window.__duocad.frameAll())
  await waitFrames(page, 3)
  if ((await visibleFace(page, bodyId)) === null) {
    const other = await page.evaluate((id) => (window.__duocad.parts ?? []).map((p) => p.body_id).filter((b) => b !== id), bodyId)
    for (const candidate of other) {
      if ((await visibleFace(page, candidate)) !== null) {
        record(scenario, `対象ボディ ${bodyId} の面が画面に無いので ${candidate} で測る`, null)
        bodyId = candidate
        break
      }
    }
  }
  // 選択クリック x5(面を替える)
  let from = 1
  for (let k = 0; k < 5; k += 1) {
    const face = await visibleFace(page, bodyId, { from })
    if (!face) break
    from = face.index + 1
    const t0 = await now(page)
    await measure(
      page,
      scenario,
      `面を選ぶ(クリック→選択が返る→1フレーム) #${k + 1}`,
      () => clickAt(page, face.at),
      (id, index) => (window.__duocad.selection?.items ?? []).some((i) => i.body === id && i.kind === 'face' && i.index === index),
      { args: [bodyId, face.index] },
    )
    if (k === 0) {
      const seq = await wsSince(page, t0)
      console.log('      WS の並び: ' + seq.map((e) => `${e.dir === 'out' ? '→' : '←'}${e.type}@${(e.t - t0).toFixed(1)}ms${e.dir === 'in' ? `(${e.size}B, handler ${e.handler_ms.toFixed(1)})` : ''}`).join('  '))
    }
  }
  // 選択を解いて、面を1つ選び直してからフィレットを起動(辺はそのボディのものが届く)
  await mcp.callTool('clear_selection')
  await page.waitForFunction(() => (window.__duocad.selection?.count ?? 0) === 0, { timeout: 30000, polling: 'raf' })
  const face = await visibleFace(page, bodyId)
  if (face === null) {
    record(scenario, 'フィレット: 画面に見える面が無い', null)
    return
  }
  await clickAt(page, face.at)
  await page.waitForFunction((id) => (window.__duocad.selection?.items ?? []).some((i) => i.body === id), { timeout: 30000, polling: 'raf' }, bodyId)
  await measure(
    page,
    scenario,
    'フィレット起動→辺が届いて描ける(get_edges 往復)',
    () => page.click('#fillet'),
    (id) => window.__duocad.edges !== null && window.__duocad.edges?.body_id === id && (window.__duocad.edges?.edges ?? []).length > 0,
    { args: [bodyId], timeout: 60000 },
  )
  // 同じボディでもう一度起動(辺はサーバのキャッシュから)
  await page.keyboard.press('Escape')
  await page.waitForFunction(() => window.__duocad.command === null && window.__duocad.edges === null, { timeout: 30000, polling: 'raf' })
  await clickAt(page, face.at)
  await page.waitForFunction((id) => (window.__duocad.selection?.items ?? []).some((i) => i.body === id), { timeout: 30000, polling: 'raf' }, bodyId)
  await measure(
    page,
    scenario,
    'フィレット起動→辺が届いて描ける(再取得: 同じボディをもう一度)',
    () => page.click('#fillet'),
    (id) => window.__duocad.edges !== null && window.__duocad.edges?.body_id === id && (window.__duocad.edges?.edges ?? []).length > 0,
    { args: [bodyId], timeout: 60000 },
  )
  const candidates = await page.evaluate(() => {
    const edges = window.__duocad.edges?.edges ?? []
    return edges
      .filter((e) => e.kind === 'line' && e.length > 3)
      .sort((a, b) => b.length - a.length)
      .slice(0, 40)
      .map((e) => {
        const n = e.points.length / 3
        return { index: e.index, mid: [(e.points[0] + e.points[(n - 1) * 3]) / 2, (e.points[1] + e.points[(n - 1) * 3 + 1]) / 2, (e.points[2] + e.points[(n - 1) * 3 + 2]) / 2] }
      })
  })
  let edge = null
  for (const c of candidates) {
    const at = await screenOf(page, 'projectPoint', c.mid)
    if (at) {
      edge = { index: c.index, at }
      break
    }
  }
  if (!edge) {
    record(scenario, 'フィレット: 画面に見える直線の辺が無い', null)
    await page.keyboard.press('Escape')
    return
  }
  await measure(
    page,
    scenario,
    '辺に乗せる→連なりが届く(get_edge_chains 往復)',
    () => page.mouse.move(edge.at.x, edge.at.y),
    (id, index) => window.__duocad.edgeChain(id, index) !== null,
    { args: [bodyId, edge.index], timeout: 60000 },
  )
  await measure(
    page,
    scenario,
    '辺を選ぶ(クリック→選択が返る→1フレーム)',
    () => clickAt(page, edge.at),
    (id) => (window.__duocad.selection?.items ?? []).some((i) => i.body === id && i.kind === 'edge'),
    { args: [bodyId] },
  )
  await typeInto(page, '#fillet-radius', String(radius))
  await measure(
    page,
    scenario,
    `半径を打ち終える→ゴーストが出る(debounce 250ms 込み)`,
    async () => {},
    () => window.__duocad.previewing === true,
    { timeout: 60000 },
  )
  {
    // カメラ由来の再配信(粗さだけ変わる)で、起動中のプレビューを投げ直すか(issue #169。0 が目標)
    const t0 = await now(page)
    const before = await resolutionPrint(page)
    await orbit(page)
    await waitMatched(page, Date.now(), { changedFrom: before }).catch(() => null)
    await sleep(600) // 投げ直しの待ち(`PREVIEW_REFIRE_MS` 250)より長く
    const resent = (await wsSince(page, t0)).filter((e) => e.dir === 'out' && e.type === 'preview').length
    record(scenario, 'ゴースト表示中にカメラを回す→プレビューを投げ直した回数', resent)
    await page.waitForFunction(() => window.__duocad.previewing === true, { timeout: 60000, polling: 'raf' })
  }
  const revisionBefore = await page.evaluate((id) => window.__duocad.parts.find((p) => p.body_id === id)?.revision ?? null, bodyId)
  await measure(
    page,
    scenario,
    'Enter→新しい形が描ける(verb+autosave+再テセレーション+配信+作り直し)',
    () => page.keyboard.press('Enter'),
    (id, rev) => {
      const hook = window.__duocad
      const part = hook.parts.find((p) => p.body_id === id)
      return part && part.revision !== rev && hook.command === null && hook.getGeometry(id) !== null
    },
    { args: [bodyId, revisionBefore], timeout: 120000 },
  )
  const revisionAfter = await page.evaluate((id) => window.__duocad.parts.find((p) => p.body_id === id)?.revision ?? null, bodyId)
  await measure(
    page,
    scenario,
    'undo→元の形が描ける',
    () => pressUndo(page),
    (id, rev) => {
      const part = window.__duocad.parts.find((p) => p.body_id === id)
      return part && part.revision !== rev && window.__duocad.getGeometry(id) !== null
    },
    { args: [bodyId, revisionAfter], timeout: 120000 },
  )
  await page.keyboard.press('Escape')
}

/** 右ドラッグで約1秒まわす(60 手・16ms 刻み)。 */
async function orbit(page) {
  const c = await center(page)
  await page.mouse.move(c.x, c.y)
  await page.mouse.down({ button: 'right' })
  for (let i = 1; i <= 60; i += 1) {
    await page.mouse.move(c.x + i * 4, c.y + i * 1.5)
    await sleep(16)
  }
  await page.mouse.up({ button: 'right' })
}

/** モデルの上をポインタが横切る(30 手)。 */
async function sweep(page) {
  const c = await center(page)
  for (let i = 0; i <= 30; i += 1) {
    await page.mouse.move(c.left + c.w * (0.2 + 0.6 * (i / 30)), c.top + c.h * (0.3 + 0.4 * (i / 30)))
    await sleep(33)
  }
}

runSmoke('bench-basic-ops', { timeout: 600000, mcp: true }, async (ctx) => {
  const { options, timeout } = ctx
  const skip = new Set((options.skip ?? '').split(',').filter(Boolean))
  const stepPath = options.step ?? PC_CASE_STEP
  const out = options.out ?? resolve(REPO_ROOT, 'output', 'bench_basic_ops', 'client_ops.json')

  // ---------------------------------------------------------------- A. 箱
  if (!skip.has('A')) {
    console.log('\n## A. 20mm 角の箱')
    const { bodyId } = await addExtrudedRect(ctx.mcp, { corner: [0, 0], width: 20, height: 20, distance: 20 })
    const { page, loadMs, ws } = await openBenchPage(ctx.browser, ctx.url, { timeout, expectParts: 1 })
    record('A', '読み込み(goto→全ボディが描ける)', loadMs, ws)
    try {
      const rtt = await wsRoundTrips(ctx.url)
      record('A', 'WS 素の往復(node→clear_selection→state) p50', rtt.p50, { min: rtt.min, max: rtt.max })
      const rttSel = await wsRoundTrips(ctx.url, 20, (op) => ({ cmd: 'modify_selection', add: [{ body: bodyId, kind: 'face', index: 1 + (op % 6) }], remove: [], client_op: op, ack_id: op }))
      record('A', 'WS 素の往復(node→modify_selection 面1件+ack_id→state) p50', rttSel.p50, { min: rttSel.min, max: rttSel.max })
      await ctx.mcp.callTool('clear_selection')
      const inPage = await page.evaluate(async () => {
        const config = await fetch('duocad-config.json', { cache: 'no-store' }).then((r) => r.json())
        const socket = new WebSocket(config.ws_url)
        await new Promise((resolve, reject) => {
          socket.addEventListener('open', resolve)
          socket.addEventListener('error', () => reject(new Error('WS に繋がらない')))
        })
        await new Promise((resolve) => socket.addEventListener('message', resolve, { once: true }))
        const samples = []
        let seq = 5000
        for (let i = 0; i < 20; i += 1) {
          samples.push(
            await new Promise((resolve) => {
              const t0 = performance.now()
              const onMessage = (event) => {
                if (String(event.data).includes('"type": "state"')) {
                  socket.removeEventListener('message', onMessage)
                  resolve(performance.now() - t0)
                }
              }
              socket.addEventListener('message', onMessage)
              socket.send(JSON.stringify({ cmd: 'clear_selection', client_op: (seq += 1) }))
            }),
          )
        }
        socket.close()
        samples.sort((a, b) => a - b)
        return { p50: samples[10], min: samples[0], max: samples[19] }
      })
      record('A', 'WS 素の往復(ページ内の別ソケット→clear_selection→state) p50', inPage.p50, { min: inPage.min, max: inPage.max })
      // 入力イベントの中から送ると往復が延びるか(ブラウザの入力→描画の優先スケジューリングの切り分け)
      await page.evaluate(async () => {
        const config = await fetch('duocad-config.json', { cache: 'no-store' }).then((r) => r.json())
        const socket = new WebSocket(config.ws_url)
        await new Promise((resolve, reject) => {
          socket.addEventListener('open', resolve)
          socket.addEventListener('error', () => reject(new Error('WS に繋がらない')))
        })
        await new Promise((resolve) => socket.addEventListener('message', resolve, { once: true }))
        window.__benchInputSocket = socket
        window.__benchInputSamples = []
        let seq = 7000
        document.addEventListener(
          'pointerup',
          () => {
            const t0 = performance.now()
            const onMessage = (event) => {
              if (String(event.data).includes('"type": "state"')) {
                socket.removeEventListener('message', onMessage)
                window.__benchInputSamples.push(performance.now() - t0)
              }
            }
            socket.addEventListener('message', onMessage)
            socket.send(JSON.stringify({ cmd: 'clear_selection', client_op: (seq += 1) }))
          },
          true,
        )
      })
      for (let i = 0; i < 10; i += 1) {
        const c = await center(page)
        await page.mouse.move(c.x + 30 + i, c.y + 30)
        await page.mouse.down({ button: 'right' })
        await page.mouse.up({ button: 'right' })
        await sleep(100)
      }
      const viaInput = await page.evaluate(() => {
        const s = window.__benchInputSamples.slice().sort((a, b) => a - b)
        window.__benchInputSocket.close()
        return { p50: s[Math.floor(s.length / 2)] ?? null, min: s[0] ?? null, max: s[s.length - 1] ?? null, n: s.length }
      })
      record('A', 'WS 素の往復(別ソケット・pointerup の中から送る→state) p50', viaInput.p50, { min: viaInput.min, max: viaInput.max, n: viaInput.n })
    } catch (error) {
      record('A', `WS 素の往復 [失敗: ${error.message}]`, null)
    }
    await page.evaluate(() => window.__duocad.frameAll())
    await waitFrames(page, 3)
    await frameStats(page, 'A', '静止時のフレーム間隔 2秒', () => sleep(2000))
    await frameStats(page, 'A', '右ドラッグ回転中のフレーム間隔', () => orbit(page))
    await selectFilletUndo(page, ctx.mcp, 'A', bodyId, { radius: 2 })
    await page.close()
  }

  // ---------------------------------------------------------------- B. PC ケース
  if (!skip.has('B') && existsSync(stepPath)) {
    console.log(`\n## B. PC ケース ${stepPath}`)
    await ctx.restart()
    const imported = await ctx.mcp.callTool('import_step', { path: stepPath })
    const bodyIds = imported.body_ids
    const big = await ctx.mcp.callTool('describe_document', {}).then((d) => {
      const bodies = d.bodies ?? []
      return bodies.reduce((best, b) => ((b.face_count ?? 0) > (best?.face_count ?? -1) ? b : best), null)
    })
    const bigId = big?.body_id ?? bodyIds[0]
    ok(`STEP を読んだ: ${bodyIds.length} 部品。最大ボディ ${bigId}(${big?.face_count ?? '?'} 面)`)
    const { page, loadMs, ws } = await openBenchPage(ctx.browser, ctx.url, { timeout, expectParts: bodyIds.length })
    record('B', '読み込み(goto→全27部品が描ける)', loadMs, ws)
    await page.evaluate(() => window.__duocad.frameAll())
    await waitFrames(page, 3)
    try {
      await waitMatched(page, Date.now(), { timeout: 120000 })
    } catch (error) {
      console.log(`  (全体表示の収束待ちをあきらめた: ${error.message})`)
    }
    await frameStats(page, 'B', '静止時のフレーム間隔 2秒', () => sleep(2000))
    {
      const t0 = await now(page)
      const before = await resolutionPrint(page)
      await frameStats(page, 'B', '右ドラッグ回転中のフレーム間隔', () => orbit(page))
      const released = Date.now()
      const settled = await waitMatched(page, released, { changedFrom: before })
      const ws2 = summarizeWs(await wsSince(page, t0))
      record('B', '回転を離してから形が要求に揃うまで(再テセレーション往復)', settled, ws2)
    }
    await frameStats(page, 'B', 'ホバー(面の上を横切る)中のフレーム間隔', () => sweep(page))
    {
      const c = await center(page)
      await page.mouse.move(c.x, c.y)
      const t0 = await now(page)
      const started = Date.now()
      for (let i = 0; i < 8; i += 1) {
        await page.mouse.wheel({ deltaY: -100 })
        await sleep(50)
      }
      const settled = await waitMatched(page, started)
      record('B', 'ズームイン8段→形が要求に揃うまで', settled, summarizeWs(await wsSince(page, t0)))
      const t1 = await now(page)
      const started2 = Date.now()
      for (let i = 0; i < 8; i += 1) {
        await page.mouse.wheel({ deltaY: 100 })
        await sleep(50)
      }
      const settled2 = await waitMatched(page, started2)
      record('B', 'ズームアウト8段(元へ)→形が要求に揃うまで', settled2, summarizeWs(await wsSince(page, t1)))
    }
    await frameStats(page, 'B', 'ズーム後・静止時のフレーム間隔 2秒', () => sleep(2000))
    await selectFilletUndo(page, ctx.mcp, 'B', bigId, { radius: 0.5 })
    await page.close()
  }

  // ---------------------------------------------------------------- C. スケッチ 150 長方形
  if (!skip.has('C')) {
    console.log('\n## C. 長方形 150 個のスケッチ')
    await ctx.restart()
    const sketch = await ctx.mcp.callTool('sketch_create', { base_plane: 'XY' })
    const sid = sketch.sketch_id
    const t0 = Date.now()
    for (let i = 0; i < 150; i += 1) {
      await ctx.mcp.callTool('add_rectangle', { sketch_id: sid, corner: [(i % 15) * 12, Math.floor(i / 15) * 12], width: 10, height: 10 })
    }
    record('C', 'MCP add_rectangle x150(往復込み合計)', Date.now() - t0)
    const { page, loadMs, ws } = await openBenchPage(ctx.browser, ctx.url, { timeout, expectParts: 0 })
    record('C', '読み込み(goto→スケッチが描ける)', loadMs, ws)
    await page.evaluate(() => window.__duocad.frameAll())
    await waitFrames(page, 3)
    await frameStats(page, 'C', 'スケッチ中・静止時のフレーム間隔 2秒', () => sleep(2000))
    await frameStats(page, 'C', 'スケッチ中・ホバー(横切る)中のフレーム間隔', () => sweep(page))
    for (let k = 0; k < 3; k += 1) {
      const at = await sketchPointAt(page, (k % 15) * 12 + 5, Math.floor(k / 15) * 12 + 5)
      if (!at) break
      const epoch = await page.evaluate(() => window.__duocad.stateEpoch)
      const rebuildsBefore = await page.evaluate(() => window.__duocad.sketchRebuilds?.() ?? null)
      const row = await measure(
        page,
        'C',
        `領域を選ぶ(クリック→選択が返る→1フレーム) #${k + 1}`,
        () => clickAt(page, at),
        (n) => window.__duocad.stateEpoch > n && (window.__duocad.selection?.count ?? 0) > 0,
        { args: [epoch] },
      )
      const rebuildsAfter = await page.evaluate(() => window.__duocad.sketchRebuilds?.() ?? null)
      if (rebuildsBefore !== null && rebuildsAfter !== null) {
        row.sketch_rebuilds = rebuildsAfter - rebuildsBefore
        console.log(`      スケッチの作り直し: ${row.sketch_rebuilds} 回`)
      }
    }
    await ctx.mcp.callTool('clear_selection')
    await page.waitForFunction(() => (window.__duocad.selection?.count ?? 0) === 0, { timeout: 30000, polling: 'raf' })
    {
      const t1 = await now(page)
      await ctx.mcp.callTool('set_selection', { refs: Array.from({ length: 60 }, (_, i) => ({ sketch: sid, index: i + 1 })) })
      await page.waitForFunction(() => (window.__duocad.selection?.count ?? 0) === 60, { timeout: 60000, polling: 'raf' })
      record('C', 'MCP で60領域を選ぶ→画面に選択が映る', (await now(page)) - t1, summarizeWs(await wsSince(page, t1)))
      const t2 = await now(page)
      const partsBefore = await page.evaluate(() => (window.__duocad.parts ?? []).length)
      const started = Date.now()
      let extruded = null
      try {
        extruded = await ctx.mcp.callTool('extrude', { distance: 5, mode: 'new' })
      } catch (error) {
        record('C', `MCP extrude 60領域 [失敗: ${String(error.message ?? error).slice(0, 80)}]`, null)
      }
      const mcpMs = Date.now() - started
      if (extruded !== null) {
        await page.waitForFunction((n) => (window.__duocad.parts ?? []).length > n && window.__duocad.parts.every((p) => window.__duocad.getGeometry(p.body_id) !== null), { timeout: 60000, polling: 'raf' }, partsBefore)
        await waitFrames(page, 1)
        const parts = await page.evaluate(() => (window.__duocad.parts ?? []).length)
        record('C', `MCP extrude 60領域→${parts}ボディが描ける`, (await now(page)) - t2, { mcp_ms: mcpMs, bodies: extruded.bodies?.length ?? null, ...summarizeWs(await wsSince(page, t2)) })
      }
    }
    await frameStats(page, 'C', '押し出し後・静止時のフレーム間隔 2秒', () => sleep(2000))
    await page.close()
  }

  mkdirSync(resolve(out, '..'), { recursive: true })
  writeFileSync(out, JSON.stringify({ rows: ROWS, step: stepPath, when: new Date().toISOString() }, null, 1))
  console.log(`\nwrote ${out}`)
})
