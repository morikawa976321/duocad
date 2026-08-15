/**
 * 実ブラウザでのフィーチャ検査(タスクD・2026-08-14) — **手だけで形が作れることの機械検査**。
 *
 * `smoke.mjs` は27部品のSTEPを読んだ文書で「既にあるものを触る」側を見る。こちらは
 * **空の文書**から始めて、**人が手でやる順序そのもの**を本物のクリックと
 * キー入力で通す:
 *
 *   板を描く → 押し出す → 穴を開ける → 辺を選んで丸める → 面取り → 反復
 *
 * 分けてあるのは、`join` の押し出しが「対象が無ければ新しいソリッドを作る」verbだから
 * (27部品が居る文書では既存の部品へ融合してしまい、**最初の板が生まれる**経路が通らない)。
 *
 *   web> node scripts/smoke-features.mjs
 *
 * **サーバはこのスクリプトが自分で立てる**(`web/scripts/server.mjs`)。
 * 毎回まっさらな文書で始まるので、下記の「最初の板が生まれる」経路がそのまま通る。
 * 既に立っているものへ繋ぐときだけ `--url` を渡す。
 *
 * 見るのはタスクDの検証1〜5(検証6のMCP突合は `tests/test_feature_ui.py` 側にある):
 *
 *   1. 手だけで板に穴を開けられる(スケッチ → 領域選択 → cut で押し出し)
 *   2. 辺を複数選んでフィレット。**失敗する半径では理由が画面に出て、選択が保持されたまま戻る**
 *   3. 面取りで円錐ができる(`PC_case_feet` と同じ種別の面が手だけで作れる)
 *   4. 矩形反復・円形反復(**全周8個で隣り合う角度が45度**)
 *   5. すべてのコマンドで Esc中断 と undo が効く
 */
import { existsSync } from 'node:fs'
import { launch } from 'puppeteer-core'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
// `--url` を渡したときだけ既に立っているサーバへ繋ぐ(既定は自分で立てる)。
const attachUrl = options.url ?? null
const wsPort = options.port ?? '8785'
const httpPort = options['http-port'] ?? '8786'
const timeout = Number.parseInt(options.timeout ?? '120000', 10)

// **Chromeを先に見る**(2026-08-15。理由は `smoke.mjs` の同じ箇所)。
const CANDIDATE_BROWSERS = [
  process.env.DUOCAD_BROWSER,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].filter(Boolean)

/** 板の寸法(mm)。体積の検算に使うので、丸い数字にしてある。 */
const PLATE = { width: 40, height: 30, thickness: 10 }
const HOLE = { u: 30, v: 20, radius: 4 }

function parseArgs(argv) {
  const parsed = {}
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i]
    if (!item.startsWith('--')) continue
    const key = item.slice(2)
    const next = argv[i + 1]
    if (next && !next.startsWith('--')) {
      parsed[key] = next
      i += 1
    } else {
      parsed[key] = 'true'
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
  console.log(`smoke-features: ${executablePath}`)

  const server = attachUrl === null ? await startServer({ wsPort, httpPort }) : null
  const url = server?.url ?? attachUrl
  console.log(`  server : ${url}${server === null ? ' (既に立っているサーバへ接続)' : ''}`)

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
    page.on('response', (response) => {
      if (response.status() >= 400) consoleErrors.push(`HTTP ${response.status()} ${response.url()}`)
    })

    await page.goto(url, { waitUntil: 'load', timeout })
    await page.waitForFunction(() => Boolean(window.__duocad && window.__duocad.ready), {
      timeout,
      polling: 100,
    })
    const parts = await page.evaluate(() => window.__duocad.parts.length)
    // **サーバは長寿命**(文書は接続より長生きする)。前回の実行が作った板が残っていると
    // 「最初のソリッドが生まれる」経路を通れないので、**毎回サーバを起動し直して**走らせる。
    if (parts !== 0) {
      fail(`空の文書で起動していない(部品が ${parts} 個ある。サーバを起動し直す)`)
    }
    ok('空の文書で起動した(ここから手だけで作る)')

    // ------------------------------------------------------------------ 道具
    /** ページの中で JSON.stringify する(Svelteの状態はプロキシで、そのままでは返らない)。 */
    const look = async () =>
      JSON.parse(
        await page.evaluate(() => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch) ?? null
          return JSON.stringify({
            bodies: hook.parts.map((body) => ({
              id: body.body_id,
              volume: body.volume,
              faces: body.face_count,
              revision: body.revision,
            })),
            activeSketch: hook.activeSketch,
            entities: active?.entities?.length ?? 0,
            drawn: active?.drawn?.length ?? 0,
            regions: active?.regions?.length ?? 0,
            selection: hook.selection,
            selectedEntities: hook.selectedEntities,
            wantsEdges: hook.wantsEdges,
            edges: hook.edges ? hook.edges.edges.map((edge) => edge.index) : [],
            edgeKinds: hook.edges ? hook.edges.edges.map((edge) => edge.kind) : [],
            log: hook.state.log.length,
            appliedCount: hook.state.applied_count,
            messages: hook.messages,
          })
        }),
      )

    async function waitFor(predicate, what, ...args) {
      try {
        await page.waitForFunction(predicate, { timeout, polling: 50 }, ...args)
      } catch {
        fail(`${what}: ${JSON.stringify(await look())}`)
      }
    }

    /**
     * **今の形の辺**が画面に出るまで待つ。
     *
     * 版(revision)まで見るのが要点 —— 形が変わると古い辺は捨てられ、取り直しになる。
     * 届いている辺の本数だけで待つと、**1つ前の形の辺**を見て「出た」と判断してしまい、
     * そこから拾った添字は今の形の別の辺を指す(§7の添字の壊れ方そのもの)。
     */
    async function waitForEdges(what) {
      await waitFor(() => {
        const hook = window.__duocad
        if (!hook.wantsEdges || !hook.edges) return false
        const body = hook.parts.find((item) => item.body_id === hook.edgeTargetBody)
        return Boolean(body && hook.edges.revision === body.revision && hook.edges.edges.length > 0)
      }, what)
    }

    async function typeInto(selector, value) {
      await page.click(selector)
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyA')
      await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await page.type(selector, String(value), { delay: 15 })
    }

    /** スケッチ平面の (u,v) を**クリックの直前に射影して**押す(画面が動いても狂わない)。 */
    async function clickPlane(u, v) {
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
    }

    async function clickRegion(index) {
      const at = await page.evaluate((idx) => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const position = window.__duocad.projectRegion(idx)
        return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
      }, index)
      if (!at) fail(`領域 ${index} が画面の中に無い`)
      await page.mouse.click(at.x, at.y)
    }

    /**
     * 辺を本物のクリックで選ぶ(`shift` で追加)。**押す直前に射影し直す**。
     *
     * **選択が届くまで待つ**(押した直後に見ない)。選択の正はコアなので、クリックは
     * `modify_selection` を送るだけで、結果は次の状態配信で返ってくる —— 押してすぐ
     * `window.__duocad.selection` を読むと、まだ前の状態を見ることになる。
     */
    async function clickEdge(bodyId, edgeIndex, shift = false) {
      const at = await page.evaluate(
        (id, index) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectEdge(id, index)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        bodyId,
        edgeIndex,
      )
      if (!at) return false
      if (shift) await page.keyboard.down('Shift')
      await page.mouse.click(at.x, at.y)
      if (shift) await page.keyboard.up('Shift')
      try {
        await page.waitForFunction(
          (id, index) =>
            (window.__duocad.selection?.items ?? []).some(
              (item) => item.body === id && item.kind === 'edge' && item.index === index,
            ),
          { timeout: 5000, polling: 50 },
          bodyId,
          edgeIndex,
        )
        return true
      } catch {
        return false // 狙いが外れた(隣の辺に取られた等)。呼び出し側が次の辺を試す
      }
    }

    // ================================================================ 検証1: 板と穴
    await page.click('#sketch-new-XY')
    await waitFor(() => window.__duocad.activeSketch !== null, 'XY平面のスケッチに入れない')

    await page.click('#sketch-rectangle')
    await clickPlane(0, 0)
    await typeInto('#sketch-width', PLATE.width)
    await typeInto('#sketch-height', PLATE.height)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.regions.length === 1)
      },
      '長方形を描いても領域が1つにならない',
    )
    ok(`長方形を手で描いた(${PLATE.width}×${PLATE.height})`)

    // 「選ぶ」へ戻して領域をクリック(描くコマンドが起動している間は点が優先される)
    await page.keyboard.press('Escape')
    await page.click('#sketch-look')
    await clickRegion(1)
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).some((item) => 'sketch' in item),
      '領域を選べない',
    )
    ok('領域を本物のクリックで選んだ')

    // 領域押し出し(結合)。**空の文書なので、ここで最初のソリッドが生まれる**
    await page.click('#extrude-regions')
    await typeInto('#regions-distance', PLATE.thickness)
    await page.click('#extrude-mode-join')
    await page.keyboard.press('Enter')
    await waitFor(() => window.__duocad.parts.length === 1, '領域押し出しでソリッドができない')
    const plate = (await look()).bodies[0]
    const expectedVolume = PLATE.width * PLATE.height * PLATE.thickness
    if (Math.abs(plate.volume - expectedVolume) > expectedVolume * 1e-9) {
      fail(`板の体積が合わない: ${plate.volume} != ${expectedVolume}`)
    }
    ok(`領域を押し出して板ができた(体積 ${plate.volume} = ${PLATE.width}×${PLATE.height}×${PLATE.thickness})`)

    // 穴: 新しいXY平面のスケッチに円を描いて cut で押し出す。
    // **一度モードを抜ける** —— 平面を選ぶボタンは「スケッチに入っていないとき」だけ出る
    // (編集中は「抜ける」「正面から見る」に入れ替わる)。
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, '1枚目のスケッチを抜けられない')
    await page.click('#sketch-new-XY')
    await waitFor(
      () => window.__duocad.activeSketch !== null && window.__duocad.sketches.length === 2,
      '2枚目のスケッチに入れない',
    )
    await page.click('#sketch-circle')
    await clickPlane(HOLE.u, HOLE.v)
    await typeInto('#sketch-radius', HOLE.radius)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.regions.length === 1)
      },
      '円の領域ができない',
    )
    await page.keyboard.press('Escape')
    await page.click('#sketch-look')
    await clickRegion(1)
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).some((item) => 'sketch' in item),
      '穴の領域を選べない',
    )
    await page.click('#extrude-regions')
    await typeInto('#regions-distance', PLATE.thickness)
    await page.click('#extrude-mode-cut')
    await page.keyboard.press('Enter')
    const holeVolume = Math.PI * HOLE.radius * HOLE.radius * PLATE.thickness
    await waitFor(
      (before, expected) => {
        const body = window.__duocad.parts[0]
        return Boolean(body && Math.abs(before - body.volume - expected) < expected * 1e-3)
      },
      '穴が開かない(体積が円柱ぶん減っていない)',
      plate.volume,
      holeVolume,
    )
    const afterHole = (await look()).bodies[0]
    ok(
      `**手だけで板に穴を開けた**(体積 ${plate.volume} -> ${afterHole.volume.toFixed(2)}` +
        ` / 減り ${(plate.volume - afterHole.volume).toFixed(2)} ≒ π×${HOLE.radius}²×${PLATE.thickness})`,
    )

    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'スケッチモードを抜けられない')
    // **視点を全体へ戻す**。直前の `#sketch-look` は描いていた図形(半径4mm)に寄っているので、
    // そのままでは40mmの板が画面の外にあり、辺をクリックで指せない。
    await page.click('#view-fit')

    // ================================================================ 検証2: フィレット
    // 面を1つ選んでおく(辺を取りに行く対象のBodyが決まる)。
    const bodyId = afterHole.id
    await page.click('#fillet')
    await waitForEdges('フィレットを起動しても辺が届かない')
    const edgeState = await look()
    ok(`フィレットを起動: 辺 ${edgeState.edges.length} 本が届いた(種別 ${[...new Set(edgeState.edgeKinds)].join('/')})`)

    // 指せる辺を2本、本物のクリックで選ぶ(1本目は素、2本目はShift+クリック)
    const picked = []
    for (const index of edgeState.edges) {
      if (picked.length >= 2) break
      if (await clickEdge(bodyId, index, picked.length > 0)) picked.push(index)
    }
    if (picked.length < 2) {
      // **どこで落ちたかを添えて失敗する**(「選べない」だけでは、辺が届いていないのか・
      // 画面に出ていないのか・当たり判定に入らないのかが切り分けられない)。
      const probe = await page.evaluate(
        (id, indexes) =>
          indexes.map((index) => [index, Boolean(window.__duocad.projectEdge(id, index))]),
        bodyId,
        edgeState.edges,
      )
      fail(
        `辺を2本選べない(選べたのは ${JSON.stringify(picked)})。` +
          `画面上の位置が取れた辺: ${JSON.stringify(probe)} / ${JSON.stringify(await look())}`,
      )
    }
    const selectedCount = (await look()).selection.count
    if (selectedCount !== 2) fail(`選択が2件になっていない: ${selectedCount}`)
    ok(`**辺を2本、本物のクリックで選んだ**(辺 ${picked.join(' と ')}。2本目はShift+クリック)`)

    // -------------------------------------------------------------- 検証5: Escで中断
    const beforeAbort = await look()
    await typeInto('#fillet-radius', 2)
    await page.keyboard.press('Escape')
    const afterAbort = await look()
    if (afterAbort.log !== beforeAbort.log || afterAbort.appliedCount !== beforeAbort.appliedCount) {
      fail(`Escで中断したのに操作が残った: log ${beforeAbort.log}->${afterAbort.log}`)
    }
    if (afterAbort.selection.count !== 2) fail('Escで選択まで消えた(選び直しになる)')
    ok('Escで中断: 操作ログは伸びず、**選択は残った**(選び直しにならない)')

    // -------------------------------------------------------------- 失敗する半径
    // §7: OCCTのフィレットは例外を投げずに `IsDone()=False` で失敗し、半径に対して単調でない。
    // **握りつぶさず理由を出し、選択を保持したまま戻す**ことがタスクDの要件。
    await page.click('#fillet')
    await typeInto('#fillet-radius', 1000)
    await page.keyboard.press('Enter')
    await waitFor(
      () => (window.__duocad.messages ?? []).some((message) => message.includes('fillet')),
      '失敗する半径なのに理由が画面に出ない',
    )
    const afterFailure = await look()
    if (afterFailure.selection.count !== 2) {
      fail(`失敗で選択が消えた: ${afterFailure.selection.count}(24本選び直しになる)`)
    }
    if (afterFailure.log !== beforeAbort.log) fail('失敗したのに操作ログが伸びた')
    const keptRadius = await page.$eval('#fillet-radius', (element) => element.value)
    if (keptRadius !== '1000') fail(`失敗後に打った値が戻っていない: "${keptRadius}"`)
    ok(
      `**失敗を握りつぶさない**: 理由が画面に出て(${afterFailure.messages[0].slice(0, 48)}…)、` +
        '選択2件と打った値がそのまま残った(すぐ打ち直せる)',
    )

    // -------------------------------------------------------------- 成功する半径
    const facesBefore = afterFailure.bodies[0].faces
    await typeInto('#fillet-radius', 2)
    await page.keyboard.press('Enter')
    await waitFor(
      (before) => (window.__duocad.parts[0]?.face_count ?? 0) > before,
      'フィレットで面が増えない',
      facesBefore,
    )
    const afterFillet = await look()
    ok(
      `**辺を2本まとめて丸めた**(面 ${facesBefore} -> ${afterFillet.bodies[0].faces}。` +
        `打ち直しは半径を打ってEnterだけ)`,
    )

    // **成功したら辺の選択は外れる**(QA指摘 2026-08-14)。形が変わると添字は別の辺を指すので、
    // 残したままだと次に半径を打った瞬間「選んだ覚えのない辺」が丸まる。
    await waitFor(
      (id) =>
        !(window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'edge',
        ),
      'フィレット成功後も古い辺の選択が残っている',
      bodyId,
    )
    ok('フィレット成功後、丸めた辺の選択は外れた(古い添字を次の操作へ持ち越さない)')

    // -------------------------------------------------------------- 検証5: undo
    await page.click('#undo')
    await waitFor(
      (expected) => window.__duocad.parts[0]?.face_count === expected,
      'undoでフィレットが戻らない',
      facesBefore,
    )
    ok(`undoでフィレットが戻った(面 ${afterFillet.bodies[0].faces} -> ${facesBefore})`)

    // ================================================================ 検証3: 面取りで円錐
    // 円筒を1本立てて、その円い辺を面取りする。**`PC_case_feet` と同じ種別の面**
    // (全周円筒・円錐・平面)が手だけで作れることの確認。
    await page.click('#sketch-new-XY')
    await waitFor(() => window.__duocad.activeSketch !== null, '3枚目のスケッチに入れない')
    await page.click('#sketch-circle')
    await clickPlane(80, 0)
    await typeInto('#sketch-radius', 12)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.regions.length === 1)
      },
      '円柱用の領域ができない',
    )
    await page.keyboard.press('Escape')
    await page.click('#sketch-look')
    await clickRegion(1)
    await page.click('#extrude-regions')
    await typeInto('#regions-distance', 20)
    await page.click('#extrude-mode-join')
    await page.keyboard.press('Enter')
    await waitFor(
      (before) => (window.__duocad.parts[0]?.volume ?? 0) > before,
      '円柱ぶんが板へ足されない',
      afterFillet.bodies[0].volume,
    )
    await page.click('#sketch-exit')
    ok('円柱を足した(離れた位置に立てたので、辺の種別で見分けられる)')

    // 円い辺(circle)を1本選んで面取り
    await page.click('#chamfer')
    await waitForEdges('面取りを起動しても辺が届かない')
    const roundEdges = await page.evaluate(
      () =>
        (window.__duocad.edges?.edges ?? [])
          .filter((edge) => edge.kind === 'circle')
          .map((edge) => edge.index),
    )
    if (roundEdges.length === 0) fail('円い辺が1本も無い(円柱を足したのに)')
    let chamferEdge = null
    for (const index of roundEdges) {
      if (await clickEdge(bodyId, index)) {
        chamferEdge = index
        break
      }
    }
    if (chamferEdge === null) fail(`円い辺を指せない(候補 ${roundEdges.join(',')})`)
    await typeInto('#chamfer-distance', 2)
    await page.keyboard.press('Enter')
    await waitFor(
      () =>
        Object.values(window.__duocad.bodyGeometry)
          .flatMap((geometry) => geometry.faces)
          .some((face) => face.kind === 'cone'),
      '面取りしても円錐面ができない',
    )
    const kinds = await page.evaluate(() => {
      const counts = {}
      for (const geometry of Object.values(window.__duocad.bodyGeometry)) {
        for (const face of geometry.faces) counts[face.kind] = (counts[face.kind] ?? 0) + 1
      }
      return counts
    })
    ok(
      `**円い辺を面取りして円錐ができた**(辺 ${chamferEdge}。面の種別 ${JSON.stringify(kinds)}` +
        ' — 平面・全周円筒・円錐が手だけで揃った)',
    )

    // ================================================================ 検証4: 反復
    await page.click('#sketch-new-XY')
    await waitFor(() => window.__duocad.activeSketch !== null, '反復用のスケッチに入れない')
    await page.click('#sketch-circle')
    await clickPlane(0, 0)
    await typeInto('#sketch-radius', 2)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === 1)
      },
      '反復する図形が置けない',
    )
    await page.keyboard.press('Escape')

    // 図形をスケッチパネルから選ぶ(**共有選択集合には入らない**引数)
    const entityId = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      return active.entities[0].entity_id
    })
    /** 図形が反復の対象に入っている状態にする(ボタンは**入り切りの切り替え**)。 */
    async function ensureEntityPicked(id) {
      const already = await page.evaluate(
        (target) => (window.__duocad.selectedEntities ?? []).includes(target),
        id,
      )
      if (!already) await page.click(`[id="entity-${id}"]`)
      await waitFor(
        (target) => (window.__duocad.selectedEntities ?? []).includes(target),
        '図形を反復の対象に選べない',
        id,
      )
    }

    await ensureEntityPicked(entityId)

    await page.click('#pattern-rectangular')
    await typeInto('#pattern-dx', 10)
    await typeInto('#pattern-dy', 8)
    await typeInto('#pattern-nx', 3)
    await typeInto('#pattern-ny', 2)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.drawn.length === 6)
      },
      '矩形反復が 3×2 = 6 にならない',
    )
    ok('矩形反復: 3×2 で図形が6個になった(元を含めた個数)')

    await page.click('#undo')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.drawn.length === 1)
      },
      'undoで反復が戻らない',
    )
    ok('undoで反復が戻った(1操作として積まれている)')

    // 円形反復: **全周8個 → 隣り合う角度は 45度**(360/8。360/7 ではない)
    await ensureEntityPicked(entityId)
    await page.click('#pattern-circular')
    await clickPlane(20, 0) // 回転中心(図形の中心と重ならない位置)
    await typeInto('#pattern-count', 8)
    await typeInto('#pattern-angle', 360)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.drawn.length === 8)
      },
      '円形反復が8個にならない',
    )
    const steps = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      const angles = active.drawn
        .map((item) => {
          const dx = item.params.cx - 20
          const dy = item.params.cy - 0
          return ((Math.atan2(dy, dx) * 180) / Math.PI + 360) % 360
        })
        .sort((a, b) => a - b)
      return angles.slice(1).map((angle, i) => angle - angles[i])
    })
    // 許容差は**度**で 1e-4(= 0.36秒角)。刻みは sin/cos を通って来るので、
    // 見たいのは「360/8 か 360/7 か」であって最終桁の一致ではない(45度と51.4度の差を見る検査)。
    const worst = Math.max(...steps.map((step) => Math.abs(step - 45)))
    if (worst > 1e-4) {
      fail(`全周8個の刻みが45度でない: ${steps.map((step) => step.toFixed(4)).join(', ')}`)
    }
    ok(`円形反復: 全周8個で隣り合う角度が45度(360/8。誤差 ${worst.toExponential(1)})`)

    if (consoleErrors.length > 0) {
      fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    }
    ok('ブラウザのコンソールにエラーなし')

    console.log('')
    console.log('smoke-features: PASS')
  } finally {
    await browser.close()
    if (server !== null) await server.stop()
  }
}

main().catch((error) => {
  console.error(`smoke-features: FAIL ${error.message}`)
  process.exit(1)
})
