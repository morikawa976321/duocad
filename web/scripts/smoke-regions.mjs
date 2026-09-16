/**
 * 実ブラウザでの領域ピック検査 — **通常モードで領域を指せることの機械検査**。
 *
 * 押し出しは両モードに表示し、選択可能対象だけを変える(スケッチモードでは領域、
 * 通常モードでは領域も面も選べる)。領域一覧UIは無いので、**通常モードで領域を指す道は
 * 3Dクリックだけ**である。
 *
 *   web> node scripts/smoke-regions.mjs
 *
 * 見るのは6つで、5つめまではどれも「辺は要求しているコマンドを起動している間だけ指せる」と同型:
 *
 *   1. **要求していない間は指せない** — 領域に当たる画素が1つも無い
 *   2. **要求している間は指せる** — 「領域を押し出す」を起動すると、**全スケッチ**の領域に
 *      当たる画素ができる(スケッチモードのように1枚に縛られない)
 *   3. **修飾キーの規則が面と同じ** — 置換 / Shift追加(別スケッチと同時) / Ctrl除去
 *   4. **一巡して形が変わる** — 領域クリック → 距離 → 結合 → Enter で板が生まれる(完了条件2)
 *   5. **領域が面より先に取られる / 外すと面に戻る** — 板に隠れた領域の画素が、
 *      起動中は領域になり、別のコマンドへ移ると同じ視界のクリックが面になる
 *   6. **消えた領域は押し出せない** — 添字3の領域を選んだ状態でSketchPanelから
 *      2つめの円の径を縮めると、コアが選択を空にし(a)、領域も3→2に減る。
 *      通常モードで「領域を押し出す」を起動してもその添字にはもう画素が当たらず、
 *      押し出しても文書が変わらない。あわせて `regionsStillExist`(b)のロジックも
 *      合成データで直接検算する
 *
 * 検証6は**添字3を先に選んでから**縮める順序で、選択が1件→0件になる瞬間を
 * 直接観測する((a)。`tests/test_server_selection_reset.py` に同じ筋のpytestがある)。
 * 先に値を打ち直して領域を消すだけの形だと、選ぶものが最初から無いため
 * `resets_selection` も `regionsStillExist` も実行されない「骨抜き」の検査になる。
 * ただし「引数(距離・種別)まで揃えた状態のままその場でパラメータを差し替える」という
 * 元の順序は、非アクティブな `discSketch` への再入場が `{#key sketchId === null}` で
 * `CommandHost` を作り直す(`App.svelte`)ため再現できない(受け入れた損失) ——
 * 選択(コア側の共有状態)は再入場をまたいで生き残るが、距離・種別の入力(CommandHost
 * ローカルの状態)は失われる。そのため`regionsStillExist`(b)の分岐は**通常のUI操作では
 * 到達できない**ことが構造的に確定しており(領域を変えうる全 `resets_selection` verbが例外なく
 * `resets_selection=True` を持つため。過不足の固定は
 * `tests/test_server_selection_reset.py::test_resets_selection_is_declared_on_exactly_these_verbs`)、
 * ロジック自体の正しさは `window.__duocad.regionsStillExist` を直接呼んで検算する
 * (詳細はApp.svelteの同名関数のdocstringとコード中のコメント)。
 *
 * **順序に意味がある**: スケッチ2枚を**ソリッドが1つも無いうちに**作る。基準平面の画素は
 * 「部品に隠れていない所」を探して返すが、**部品の輪郭のすぐ外側**が返ることがあり、
 * クリックが1画素ずれるだけで平面ではなく面に当たる(この検査を書く過程で実際に踏み、
 * 「XY平面に貼ったつもりが板の側面に貼れていた」まま緑になっていた)。
 * `createSketchOnBase` は貼れた平面を毎回検算するので、同じ取り違えは以後**赤で出る**。
 *
 * 検証5が「板に隠れた領域」を見られるのは、板が**その領域そのものから押し出したもの**で、
 * 領域(z=0)の真上に厚み10の板が載るからである。初回のソリッド到着でカメラが
 * `+x/+y/+z` 側へ自動フレームされる(`viewport.update` の初回だけの視点合わせ)ので、
 * 領域の点へ向かうレイは必ず板の上面を通る。
 *
 * **サーバはこのスクリプトが自分で立てる**(`web/scripts/server.mjs`)。
 * 既に立っているものへ繋ぐときだけ `--url` を渡す。
 */
import { findBrowser, launchBrowser, withoutBrowserPanel } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { McpClient } from './mcp-client.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const attachUrl = options.url ?? null
const wsPort = options.port ?? '8815'
const httpPort = options['http-port'] ?? '8816'
const timeout = Number.parseInt(options.timeout ?? '120000', 10)

/** 板の寸法(mm)。体積の検算に使うので丸い数字にしてある。 */
const PLATE = { width: 40, height: 30, thickness: 10 }
/**
 * 2枚目のスケッチに置く円。**板の footprint (u,v) = (0..40, 0..30) の外**に置く ——
 * 同じXY平面の領域どうしが重なると、レイ距離が同じでどちらが取れるか決まらない
 * (検査を不安定にするだけで、見たい性質はそこではない)。
 */
const DISC = { u: -15, v: 15, radius: 5 }
/**
 * 同じスケッチに置く2つめの円。**1つめと重ねる**(中心の距離15 < 5+13)ので領域は3つに割れ、
 * 半径を1.5へ縮めると重なりが解けて2つに戻る ——「選んでいた領域(添字3)が消える」を
 * パラメータ編集だけで作れる最小の形。検証6が使う。
 *
 * **中心のクリック点は1つめの円の輪郭から10mm以上離す**(実測)。`63a30a7` の
 * ホバー/スナップ導入後、既存図形の輪郭から数mm以内をクリックすると新しい点が
 * その輪郭へ吸い付く(実測: このカメラ距離で輪郭から4mmはスナップし5mmはしない)。
 * 以前の値(輪郭から2mm)は無関係な円へ吸い付いて中心がずれ、検証6が壊れていた。
 */
const DISC_B = { u: -30, v: 15, radius: 13, shrunk: 1.5 }

async function main() {
  const executablePath = findBrowser()
  console.log(`smoke-regions: ${executablePath}`)

  const server = attachUrl === null ? await startServer({ wsPort, httpPort }) : null
  // **MCPの口**(`set_parameter` の1手だけをMCP側から打つ。詳細は `smoke-files.mjs` 参照)。
  // `--attach` で外のサーバへ繋いだときは子プロセスを持たないので `null` になる。
  const mcp = server === null ? null : new McpClient(server.child, { timeout })
  const url = server?.url ?? attachUrl
  console.log(`  server : ${url}${server === null ? ' (既に立っているサーバへ接続)' : ''}`)

  const browser = await launchBrowser(executablePath)

  try {
    const page = await browser.newPage()
    await page.setViewport({ width: 1280, height: 900 })
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
            })),
            activeSketch: hook.activeSketch,
            sketches: hook.sketches.map((item) => ({
              id: item.sketch_id,
              regions: item.regions.map((region) => region.index),
            })),
            wantsRegions: hook.wantsRegions,
            selection: hook.selection?.items ?? [],
            errors: hook.errors,
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
     * **描画が落ち着くまで待つ**。コマンドを起動したり引数が1つ埋まったりすると
     * ツールバーの行が変わり、ビューポートの高さ＝射影が動く。射影して即クリックすると
     * 狙いと違う場所を押すので、射影の前に必ず挟む。
     */
    const settleLayout = () =>
      page.evaluate(
        () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
      )

    /**
     * **起きないことを見る前の猶予**。適用が走るなら「キー入力 → WS往復 → 状態配信」で
     * 済む時間を過ぎてから数える —— 待たずに数えると、走っていても
     * 「まだ返ってきていないだけ」を緑と読んでしまう(`smoke-files` と同じ道具)。
     */
    const settle = () => new Promise((resolve) => setTimeout(resolve, 1000))

    async function typeInto(selector, value) {
      await page.click(selector)
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyA')
      await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await page.type(selector, String(value), { delay: 15 })
    }

    async function clickAt(at, modifier = null) {
      if (modifier) await page.keyboard.down(modifier)
      await page.mouse.click(at.x, at.y)
      if (modifier) await page.keyboard.up(modifier)
    }

    /** そのスケッチの平面(法線と原点)。**貼る先を取り違えていないかの検算に使う**。 */
    const frameOf = async (sketchId) =>
      JSON.parse(
        await page.evaluate(
          (id) =>
            JSON.stringify(
              window.__duocad.sketches.find((item) => item.sketch_id === id)?.frame ?? null,
            ),
          sketchId,
        ),
      )

    /**
     * **スケッチを作る2手**: ツールバーの「スケッチ」を起動 → 貼る先をクリック(Enterは無い)。
     *
     * 貼れた先が本当にその基準平面かを**毎回検算する** —— 外すと部品の面に貼れてしまい、
     * それでも後続のアサーションは通る(=検査が黙って別の状況を見る)。
     */
    async function createSketchOnBase(name = 'XY') {
      const before = await page.evaluate(() => window.__duocad.sketches.length)
      await page.click('#sketch-create')
      await waitFor(() => window.__duocad.wantsBasePlanes === true, '「スケッチ」が起動しない')
      await settleLayout()
      const at = await page.evaluate((plane) => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        const position = window.__duocad.projectBasePlane(plane)
        return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
      }, name)
      if (!at) fail(`基準平面 ${name} を指せる画素が無い(「スケッチ」が起動しているか)`)
      await clickAt(at)
      await waitFor(
        (want) => window.__duocad.sketches.length === want,
        `基準平面 ${name} にスケッチを貼れない`,
        before + 1,
      )
      const created = await page.evaluate(() => window.__duocad.activeSketch)
      const frame = await frameOf(created)
      const axis = { XY: 2, XZ: 1, YZ: 0 }[name]
      if (
        !frame ||
        frame.origin.some((value) => Math.abs(value) > 1e-6) ||
        Math.abs(Math.abs(frame.normal[axis]) - 1) > 1e-6
      ) {
        fail(`基準平面 ${name} ではない所に貼れた: ${JSON.stringify(frame)}`)
      }
      return created
    }

    async function clickPlane(u, v) {
      // **浮いているブラウザを畳んでから**押す —— スケッチ平面の (u,v) は画面の1画素しかなく、
      // そこがパネルの下に来ると逃げ場が無い(理由は `browser.mjs` の `withoutBrowserPanel`)。
      return withoutBrowserPanel(page, async () => {
        await settleLayout()
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
        await clickAt(at)
      })
    }

    /**
     * 領域の画素。**`sketchId` を渡すと通常モードでも射影できる**(スケッチモードの外では
     * 「今のスケッチ」が無い)。指せない状態なら null が返る —— それ自体がゲートの検査になる。
     */
    const regionPixel = async (index, sketchId = null) =>
      page.evaluate(
        (idx, sid) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectRegion(idx, sid ?? undefined)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        index,
        sketchId,
      )

    /**
     * 領域を本物のクリックで指す(**射影はクリックの直前にやり直す**)。浮いているブラウザは
     * `clickPlane` と同じく畳んでから押す(ブラウザの行が増えると、領域の画素がその下に入る)。
     */
    async function clickRegion(sketchId, index, modifier = null) {
      return withoutBrowserPanel(page, async () => {
        await settleLayout()
        const at = await regionPixel(index, sketchId)
        if (!at) fail(`領域 ${sketchId}:${index} に当たる画素が無い`)
        await clickAt(at, modifier)
        return at
      })
    }

    /**
     * スケッチ平面上の点 (u,v) の画素。**スケッチを指定すれば通常モードでも取れる**。
     * 画面の寸法が変わっても**同じ世界座標**を指し直せるので、コマンドを移った前後で
     * 「同じ場所」を押し比べられる(画素そのものは持ち越せない)。
     */
    const sketchPixel = async (sketchId, u, v) => {
      await settleLayout()
      return page.evaluate(
        (sid, uu, vv) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectSketchPoint(uu, vv, sid)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        sketchId,
        u,
        v,
      )
    }

    /** 選択にある領域だけを `sketch:index` の形で並べる。 */
    const pickedRegions = async () =>
      JSON.parse(
        await page.evaluate(() =>
          JSON.stringify(
            (window.__duocad.selection?.items ?? [])
              // **領域だけ**(`sketch_entity` も `sketch` を持つ)
              .filter((item) => 'sketch' in item && !('entity' in item))
              .map((item) => `${item.sketch}:${item.index}`),
          ),
        ),
      )

    // ================================================================ 下ごしらえ: スケッチ2枚
    // **ソリッドが1つも無いうちに作る**(冒頭の注記。基準平面のクリックが部品に取られない)。
    const plateSketch = await createSketchOnBase('XY')
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
      '板の領域ができない',
    )
    await page.keyboard.press('Escape')
    await page.click('#sketch-exit')

    const discSketch = await createSketchOnBase('XY')
    await page.click('#sketch-circle')
    await clickPlane(DISC.u, DISC.v)
    await typeInto('#sketch-radius', DISC.radius)
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

    // 2つめの円(1つめと重ねる)。**パラメータの名前は打った値から引く** ——
    // `d?` の採番はそれまでの図形の数で決まるので、決め打ちにすると図形を1つ足すたびに壊れる。
    const beforeParams = await page.evaluate(() => Object.keys(window.__duocad.parameters))
    await page.click('#sketch-circle')
    await clickPlane(DISC_B.u, DISC_B.v)
    await typeInto('#sketch-radius', DISC_B.radius)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.regions.length === 3)
      },
      '2つの円が重なって領域が3つにならない',
    )
    const radiusParam = await page.evaluate(
      (known, radius) => {
        const parameters = window.__duocad.parameters
        const added = Object.keys(parameters).filter((name) => !known.includes(name))
        const found = added.filter((name) => Math.abs(parameters[name] - radius) < 1e-9)
        return found.length === 1 ? found[0] : null
      },
      beforeParams,
      DISC_B.radius,
    )
    if (!radiusParam) fail('2つめの円の半径のパラメータ名を一意に引けない')
    await page.keyboard.press('Escape')
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'スケッチモードを抜けられない')
    if (plateSketch === discSketch) fail('スケッチが2枚に分かれていない')
    ok(`XY平面にスケッチを2枚(長方形・重なる円2つ=領域3つ)置き、通常モードへ戻った(半径は ${radiusParam})`)

    // ============================================== 検証1: 要求していない間は領域を指せない
    if ((await look()).wantsRegions !== false) {
      fail('起動していないのに領域を要求している(既定のコマンドは「押し出す」=面)')
    }
    if ((await regionPixel(1, plateSketch)) !== null || (await regionPixel(1, discSketch)) !== null) {
      fail('領域を要求していないのに、領域へ当たる画素がある(常時ピック可能になっている)')
    }
    ok('**要求していない間は指せない**: 通常モードで領域へ当たる画素が1つも無い')

    // ============================================== 検証2: 起動中は全スケッチの領域が指せる
    await page.click('#extrude-regions')
    await waitFor(() => window.__duocad.wantsRegions === true, '「領域を押し出す」で領域を要求しない')
    await settleLayout()
    if ((await regionPixel(1, plateSketch)) === null) fail('起動中なのに1枚目の領域を指せない')
    if ((await regionPixel(1, discSketch)) === null) {
      fail('起動中なのに2枚目の領域を指せない(active固定のままになっている)')
    }
    ok('**起動している間は指せる**: 全スケッチ(2枚)の領域に当たる画素ができた')

    // ============================================== 検証3: 修飾キーの規則が面と同じ
    await clickRegion(discSketch, 1)
    await waitFor(
      (sketch) => {
        const items = window.__duocad.selection?.items ?? []
        return items.length === 1 && items[0].sketch === sketch && items[0].index === 1
      },
      '素のクリックで領域が選べない',
      discSketch,
    )
    await clickRegion(plateSketch, 1, 'Shift')
    await waitFor(
      () => (window.__duocad.selection?.items ?? []).length === 2,
      'Shift+クリックで領域が追加されない',
    )
    const both = await pickedRegions()
    if (!both.includes(`${plateSketch}:1`) || !both.includes(`${discSketch}:1`)) {
      fail(`2枚のスケッチの領域が同時に選べない: ${JSON.stringify(both)}`)
    }
    await clickRegion(discSketch, 1, 'Control')
    await waitFor(
      (sketch) => {
        const items = window.__duocad.selection?.items ?? []
        return items.length === 1 && items[0].sketch === sketch
      },
      'Ctrl+クリックで領域が外れない',
      plateSketch,
    )
    const left = await pickedRegions()
    if (left.length !== 1 || left[0] !== `${plateSketch}:1`) {
      fail(`Ctrl除去の後に残る領域が違う: ${JSON.stringify(left)}`)
    }
    ok('**修飾キーは面と同じ**: 置換 / Shift追加(別スケッチと同時) / Ctrl除去')

    // ============================================== 検証4: 一巡して形が変わる(完了条件2)
    // ここで残っている選択は1枚目(長方形)の領域だけ。距離と向きを打って Enter。
    await typeInto('#regions-distance', PLATE.thickness)
    await page.click('#extrude-mode-join')
    await page.keyboard.press('Enter')
    const plateVolume = PLATE.width * PLATE.height * PLATE.thickness
    await waitFor(
      (expected) => {
        const parts = window.__duocad.parts
        return parts.length === 1 && Math.abs(parts[0].volume - expected) < expected * 1e-3
      },
      '通常モードの「領域を押し出す」で板ができない',
      plateVolume,
    )
    // **送られた操作そのものを見る**。体積だけでは「向きが結合として渡ったか」を分けられない
    // (この文書にはまだソリッドが無いので、cutでも未指定でも同じ体積になりうる)。
    const applied = JSON.parse(
      await page.evaluate(() => {
        const log = window.__duocad.state.log
        return JSON.stringify(log[log.length - 1] ?? null)
      }),
    )
    if (
      !applied ||
      applied.verb !== 'extrude' ||
      applied.args.mode !== 'join' ||
      applied.args.distance !== PLATE.thickness
    ) {
      fail(`確定した操作が「距離10の結合の領域押し出し」ではない: ${JSON.stringify(applied)}`)
    }
    ok(`**一巡して形が変わった**: 通常モードで 領域クリック → 距離 → 結合 → Enter (体積 ${plateVolume})`)

    // ============================================== 検証5: 領域が面より先 / 外すと面に戻る
    // 板は**その領域から押し出したもの**なので、領域(z=0)の真上に厚み10の板が載っている。
    // 初回のソリッド到着でカメラは +x/+y/+z 側へ自動フレームされる(`viewport.update`)ので、
    // 領域の点へ向かうレイは必ず板の上面を通る ——「板に隠れている領域」を押す状況になる。
    // **確定成功でコマンドは無起動へ戻る**(`commandSucceeded()`)ので、
    // 検証4のEnterで既に降りている。ここで改めて起動し直す(:377/:533と同じ形)。
    await page.click('#extrude-regions')
    await waitFor(() => window.__duocad.wantsRegions === true, '「領域を押し出す」で領域を要求しない')
    // **指す場所は板の真ん中**(スケッチ座標で (20,15) = 長方形の中心)。領域の内側であり、
    // かつ真上に厚み10の板が載っている ——「面と領域が同じ場所で競合する」1点である。
    const middle = [PLATE.width / 2, PLATE.height / 2]
    const withGate = await sketchPixel(plateSketch, middle[0], middle[1])
    if (!withGate) fail('板の中心が画面の中に無い(視点が外れている)')
    await clickAt(withGate)
    await waitFor(
      (sketch) => {
        const items = window.__duocad.selection?.items ?? []
        return items.length === 1 && items[0].sketch === sketch && items[0].index === 1
      },
      '板に隠れた領域の場所で領域が取れない(面に取られている)',
      plateSketch,
    )
    ok('**領域が面より先**: 板に隠れている領域の場所で、面ではなく領域が入った')

    await page.keyboard.press('Escape')
    await page.click('#extrude')
    await waitFor(
      () => window.__duocad.wantsRegions === false,
      '面のコマンドへ移っても領域を要求したままになる',
    )
    if ((await regionPixel(1, plateSketch)) !== null) {
      fail('面のコマンドへ移ったのに領域へ当たる画素が残っている(ゲートが閉じない)')
    }
    // **同じ世界座標をもう一度押す**。これが「領域が面より先」の対で、2つ揃って初めて
    // 「そこには面も居た(=競合していた)」が言える。コマンドを移るとツールバーの高さが
    // 変わって画素は動くので、画素ではなくスケッチ座標を持ち越して射影し直す。
    const withoutGate = await sketchPixel(plateSketch, middle[0], middle[1])
    if (!withoutGate) fail('板の中心が画面の中に無い(視点が外れている)')
    await clickAt(withoutGate)
    await waitFor(
      () => {
        const items = window.__duocad.selection?.items ?? []
        return items.length === 1 && !('sketch' in items[0])
      },
      'ゲートを閉じた後、同じ場所のクリックで面が取れない',
    )
    ok('**外すと面に戻る**: 同じ場所が、要求中は領域・外すと面になった(=そこで競合していた)')

    // ============================================== 検証6: 消えた領域は押し出せない
    // 「既存の防具(`regionsStillExist`)が通常モードでも効くこと」の対。
    //
    // 先に消してから選べないことを見るだけの形だと、`resets_selection`(コア)も
    // `regionsStillExist`(`App.svelte`)もどちらも実行されない(選ぶものが最初から
    // 無いので防具の出番が無い「骨抜き」の検査になる)。ここでは両方を実地で踏む:
    //
    //   (a) `resets_selection` —— **先に添字3を選んでから**縮める。選択が
    //       1件→0件になることを直接確認し、コアが実際に選択を空にしたことを見る
    //       (`tests/test_server_selection_reset.py::test_set_parameter_clears_a_selected_region_that_the_edit_makes_vanish`
    //       がpytest側でも同じ筋を固定している)。
    //   (b) `regionsStillExist` —— **通常のUI操作では構造的に到達できない**(理由は
    //       下記コメントと `App.svelte` の同名関数のdocstring)。ロジックそのものの
    //       正しさは `window.__duocad.regionsStillExist(selection, sketches)` を
    //       合成データで直接呼んで検算する。

    // 2つめの円のentity_idを引く(`refs.radius` が `radiusParam` と一致するもの)。
    const circleBEntityId = await page.evaluate(
      (sketchId, name) => {
        const sketch = window.__duocad.sketches.find((item) => item.sketch_id === sketchId)
        const entity = sketch.entities.find((item) => item.refs.radius === name)
        return entity ? entity.entity_id : null
      },
      discSketch,
      radiusParam,
    )
    if (!circleBEntityId) fail('2つめの円のentity_idを引けない')

    // **(a-1) 先に添字3を選ぶ**(まだ実在する領域を、通常モードで本物のクリックで選ぶ)。
    // **距離・種別はあえて打たない** —— `EXTRUDE_REGIONS` は `preview: 'server'` で、
    // 引数が揃うとデバウンス後にプレビュー要求を自動で投げる。直後にSketchPanelへ
    // 再入場して選択を空にすると、飛んでいる途中のプレビュー要求が空の選択で
    // サーバに届いて `SelectionEmpty` の警告トーストを出す(実測)。ここで見たいのは
    // 「選択が1件→0件になること」だけなので、プレビューを誘発する残り2つの引数は
    // 埋めずに済ませる。
    await page.click('#extrude-regions')
    await waitFor(() => window.__duocad.wantsRegions === true, '「領域を押し出す」で領域を要求しない')
    await clickRegion(discSketch, 3)
    await waitFor(
      (sketch) => {
        const items = window.__duocad.selection?.items ?? []
        return items.length === 1 && items[0].sketch === sketch && items[0].index === 3
      },
      'これから消える領域(添字3)を選べない',
      discSketch,
    )
    const selectedBeforeShrink = await page.evaluate(
      () => (window.__duocad.selection?.items ?? []).length,
    )
    if (selectedBeforeShrink !== 1) fail(`縮める前の選択が1件でない: ${selectedBeforeShrink}`)

    // **(a-2) 再入場してSketchPanelから縮める**(人間の実際の手順=「再開」。
    // BrowserPanelの名前クリック)。再入場は `{#key sketchId === null}` で
    // `CommandHost` を作り直すので、起動していた `extrude-regions` コマンド自体は
    // ここで失われる(受け入れた損失。SketchPanelへの統合タスクの帰結)。
    // **失われないのはコアが持つ共有選択集合**(`selection`)であり、添字3への参照は
    // ここでもまだ生きている ——だからこそ直後の `set_parameter` がそれを空にする
    // 瞬間を観測できる。
    await page.click(`[data-sketch-id="${discSketch}"] .sketch-name`)
    await waitFor(
      (id) => window.__duocad.activeSketch === id,
      'discSketchへ再入場できない',
      discSketch,
    )
    // **径の打ち直しはAI(MCP)の口だけである**。
    // ここで見たいのは「選んだ領域が消える形の変更で `resets_selection` が効くか」で、
    // それは**どの経路から打っても同じコアの契約**である(MCPもWSも `Session` を通る)。
    const radiusName = await page.evaluate((id) => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      return active.entities.find((entity) => entity.entity_id === id).refs.radius
    }, circleBEntityId)
    if (!radiusName) fail('2つめの円の半径に名前つきの値が付いていない')
    if (mcp === null) fail('--attach では径を縮める操作を打てない(MCPの口が要る)')
    await mcp.handshake('duocad-smoke-regions')
    await mcp.callTool('set_parameter', { name: radiusName, value: DISC_B.shrunk })
    await waitFor(
      (sketch) => {
        const target = window.__duocad.sketches.find((item) => item.sketch_id === sketch)
        return Boolean(
          target &&
            target.regions.length === 2 &&
            !target.regions.some((region) => region.index === 3),
        )
      },
      '2つめの円を縮めても領域が2つに戻らない(添字3が消えない)',
      discSketch,
    )
    // **(a-3) resets_selection が実際に選択を空にしたことを見る**(1件→0件)。
    const selectedAfterShrink = await page.evaluate(
      () => (window.__duocad.selection?.items ?? []).length,
    )
    if (selectedAfterShrink !== 0) {
      fail(
        `set_parameterの後も選択が残っている(resets_selectionが効いていない): ` +
          `${selectedAfterShrink}件`,
      )
    }
    ok(
      '**(a) resets_selection**: 添字3を選んだ状態で径を縮めたら、' +
        'コアが選択を1件→0件に空にした(領域は3→2、添字3は実在しなくなった)',
    )

    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'discSketchを抜けられない')

    // **(b) regionsStillExist のロジックを合成データで直接検査する。**
    //
    // なぜ通常のUI操作では到達できないか: 領域を変えうる全 `resets_selection` verb(`add_line` 等の作図・
    // `set_parameter`・`pattern_rectangular`/`pattern_circular`・`undo`/`redo`)は
    // `duocad/catalog.py` で例外なく `resets_selection=True` を持つ
    // (`tests/test_server_selection_reset.py::test_resets_selection_is_declared_on_exactly_these_verbs`
    // が過不足なく固定)。したがって「選んだ領域が消える」操作が起きた瞬間、コアの選択は
    // 必ず同時に空になる。`CommandHost` は生きた `selection` を見て確定を許すかどうかを
    // 決めるので、選択が空になった時点で `runCommand` 自体が呼ばれなくなり、
    // その内側にある `regionsStillExist` のfalse分岐へ**構造的に到達できない**
    // (直上の(a)がまさにこれを実証している)。
    //
    // そこでロジック自体の正しさ(実在しない添字はfalse・実在する添字はtrueと判定するか)
    // を、本物の関数 `App.svelte` の `regionsStillExist` を直接呼んで検算する
    // (`window.__duocad.regionsStillExist`。再実装ではなく本体そのもの)。
    const regionCheck = await page.evaluate((sketchId) => {
      const sketches = window.__duocad.sketches
      const target = sketches.find((item) => item.sketch_id === sketchId)
      const liveIndex = target.regions[0]?.index ?? null
      const missingIndex = Math.max(0, ...target.regions.map((r) => r.index)) + 100
      const stale = { items: [{ sketch: sketchId, index: missingIndex }], origin: 'user', count: 1 }
      const live = { items: [{ sketch: sketchId, index: liveIndex }], origin: 'user', count: 1 }
      return {
        liveIndex,
        missingIndex,
        staleResult: window.__duocad.regionsStillExist(stale, sketches),
        liveResult: window.__duocad.regionsStillExist(live, sketches),
      }
    }, discSketch)
    if (regionCheck.liveIndex === null) fail('discSketchに領域が1つも無い(検査の前提が崩れている)')
    if (regionCheck.staleResult !== false) {
      fail(
        `regionsStillExist: 実在しない添字${regionCheck.missingIndex}をfalseと判定しない` +
          '(ガードの実装そのものが壊れている)',
      )
    }
    if (regionCheck.liveResult !== true) {
      fail(`regionsStillExist: 実在する添字${regionCheck.liveIndex}をtrueと判定しない`)
    }
    ok(
      '**(b) regionsStillExist**: 実在しない添字はfalse・実在する添字はtrueと、' +
        '本体のロジックを直接呼んで確認した(通常操作では(a)が先回りするため到達しない分岐)',
    )

    // ============================================== 仕上げ: 通常モードでも消えた領域は押し出せない
    const before = await look()
    const beforeVolume = before.bodies.reduce((sum, body) => sum + body.volume, 0)
    const beforeOps = await page.evaluate(() => window.__duocad.state.log.length)

    await page.click('#extrude-regions')
    await waitFor(() => window.__duocad.wantsRegions === true, '「領域を押し出す」で領域を要求しない')
    if ((await regionPixel(3, discSketch)) !== null) {
      fail('消えたはずの添字3の領域にまだ画素が当たる')
    }
    // **入力欄からフォーカスを外してから押す** —— 欄の中のEnterはコマンドではなく
    // その欄が受ける(`CommandHost.onKeydown` は INPUT を除く)ので、外さないと
    // 「押したのに何も起きなかった」を検査したことにならない。選ぶものが無いので
    // そもそも引数は揃わない(=消えた領域を押し出す道が無い)。
    // **押す先はヘッダの中の押しても何も起きない字**(常にあるファイル名の札)。
    // ボタンを押すとその操作が始まってしまう。
    await page.click('#current-file')
    await page.keyboard.press('Enter')
    await settle()

    const after = await look()
    const afterVolume = after.bodies.reduce((sum, body) => sum + body.volume, 0)
    const afterOps = await page.evaluate(() => window.__duocad.state.log.length)
    if (afterOps !== beforeOps || Math.abs(afterVolume - beforeVolume) > 1e-6) {
      fail(
        `消えた領域なのに押し出しが通った: 操作 ${beforeOps} -> ${afterOps} / ` +
          `体積 ${beforeVolume} -> ${afterVolume}`,
      )
    }
    ok(
      '**消えた領域は押し出せない**: 添字3が消えた後は通常モードでもその領域へ当たる画素が無く、' +
        '押し出しを起動しても文書は不変だった',
    )

    // ============================================== 走行中にエラーが出ていないこと
    // **札(DOM)ではなく控えを見る** —— トーストは8秒で消えるので、最後に画面を見ると
    // 途中で出て消えたエラーを取りこぼす(`App.svelte` の `errorLog`)。
    const errors = (await look()).errors
    if (!Array.isArray(errors)) fail(`__duocad.errors が配列で渡らない: ${JSON.stringify(errors)}`)
    if (errors.length > 0) fail(`走行中にエラーが出た: ${errors.join(' | ')}`)
    ok('走行中にエラーが1件も出ていない')

    if (consoleErrors.length > 0) {
      fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    }
    ok('ブラウザのコンソールにエラーなし')

    console.log('')
    console.log('smoke-regions: PASS')
  } finally {
    await browser.close()
    if (server !== null) await server.stop()
  }
}

main().catch((error) => {
  console.error(`smoke-regions: FAIL ${error.message}`)
  process.exit(1)
})
