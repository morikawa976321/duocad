/**
 * 実ブラウザでの領域ピック検査(2026-08-23) — **通常モードで領域を指せることの機械検査**。
 *
 * 2026-08-23の本人裁定5「押し出しは両モードに表示し、選択可能対象だけ変える。
 * スケッチモードでは領域、通常モードではスケッチ(領域)も面も選択できるようにする」の対。
 * 領域一覧UIを削除した(裁定1)ので、**通常モードで領域を指す道は3Dクリックだけ**になった。
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
 *   6. **消えた領域は押し出せない** — 通常モードで領域を選び引数を揃えた後、パラメータ編集で
 *      その領域を消すと、Enterを押しても文書が変わらない(`runCommand` の `regionsStillExist`
 *      と、コアの `resets_selection` の二枚重ね。下記「防具の重なり」)
 *
 * **防具の重なり(2026-08-23に変異検査で実測)**: 消えた領域を押し出させない防具は3枚ある。
 *
 *   (a) コアの `resets_selection`(`duocad/catalog.py`。`set_parameter` は `True`)——
 *       パラメータを変えた時点で**選択が空になる**ので、確定の引数がそもそも揃わない
 *   (b) `App.svelte` の `regionsStillExist` —— 選んだ領域が今のスケッチに無ければ送らず、
 *       **何が起きたかを画面に出す**
 *   (c) サーバ —— 消えた添字を送れば拒否する(文書は変わらないが、理由は素の添字エラー)
 *
 * **今のUIで効いているのは (a) であり、(b) の枝には到達しない**。(a) を外すと (b) が働き、
 * 検証6の判定は緑のまま「選択が残り regionsStillExist が止めた」に変わる(そのぶん札が
 * 1枚出るので、走行全体は最後のエラー検査で赤になる)。(a)(b) の両方を外すと (c) だけが残り、
 * **文書は依然変わらない** —— つまり `regionsStillExist` が守っているのは文書ではなく
 * **理由の見え方**である。検証6が固定するのは「どれが効いても押し出されない」という
 * 外から見える性質のほうで、(b) 単独に歯を立てられる緑の検査は今の骨格では書けない。
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
import { findBrowser, launchBrowser } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
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
 * 同じスケッチに置く2つめの円。**1つめと重ねる**(中心の距離7 < 5+6)ので領域は3つに割れ、
 * 半径を1.5へ縮めると重なりが解けて2つに戻る ——「選んでいた領域(添字3)が消える」を
 * パラメータ編集だけで作れる最小の形。検証6が使う。
 */
const DISC_B = { u: -8, v: 15, radius: 6, shrunk: 1.5 }

async function main() {
  const executablePath = findBrowser()
  console.log(`smoke-regions: ${executablePath}`)

  const server = attachUrl === null ? await startServer({ wsPort, httpPort }) : null
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

    /** 領域を本物のクリックで指す(**射影はクリックの直前にやり直す**)。 */
    async function clickRegion(sketchId, index, modifier = null) {
      await settleLayout()
      const at = await regionPixel(index, sketchId)
      if (!at) fail(`領域 ${sketchId}:${index} に当たる画素が無い`)
      await clickAt(at, modifier)
      return at
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
              .filter((item) => 'sketch' in item)
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
    await waitFor(() => window.__duocad.wantsRegions === true, '確定後にコマンドが外れている')
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
    // タスク文の「既存の防具(`regionsStillExist`)が通常モードでも効くこと」の対。
    // **添字3の領域を選んで引数まで揃え**、そのうえで2つめの円を縮めて重なりを解く
    // (領域は3つ -> 2つになり、選んでいた添字3が消える)。ここでEnterを押しても
    // 文書が1つも変わらないことを見る。
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
    await typeInto('#regions-distance', PLATE.thickness)
    await page.click('#extrude-mode-join')
    // **押せば通る状態だったこと**を先に固定する。これが無いと、この後の
    // 「押しても通らない」は最初から引数が足りなかっただけかもしれない。
    await waitFor(
      () => (document.querySelector('#command-prompt')?.textContent ?? '').includes('Enterで確定'),
      '引数が揃わない(この後の「押しても通らない」が意味を持たない)',
    )

    await typeInto(`#parameter-${radiusParam}`, DISC_B.shrunk)
    await page.keyboard.press('Enter')
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
    // **どちらの防具が効いたか**も見える形で残す。今のコアは `set_parameter` が
    // `resets_selection=True` なので選択が空になり、`regionsStillExist` の枝までは行かない。
    const guard = await page.evaluate(
      () => (window.__duocad.selection?.items ?? []).length === 0,
    )

    const before = await look()
    const beforeVolume = before.bodies.reduce((sum, body) => sum + body.volume, 0)
    const beforeOps = await page.evaluate(() => window.__duocad.state.log.length)
    // **入力欄からフォーカスを外してから押す** —— 欄の中のEnterはコマンドではなく
    // その欄が受ける(`CommandHost.onKeydown` は INPUT を除く)ので、外さないと
    // 「押したのに何も起きなかった」を検査したことにならない。
    await page.click('header strong')
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
    const prompt = await page.evaluate(
      () => document.querySelector('#command-prompt')?.textContent ?? '',
    )
    // **確定できる状態には戻っていないこと**だけを見る(文言そのものは見ない) ——
    // どちらの防具が止めたかで残る文言が変わる(選択が空なら「押し出す領域を1つ選ぶ」、
    // 選択が残って `regionsStillExist` が止めたなら確定の途中まで進んだ跡が残る)。
    if (prompt.includes('Enterで確定')) {
      fail(`消えた領域なのに「Enterで確定」のままになっている: ${JSON.stringify(prompt)}`)
    }
    ok(
      `**消えた領域は押し出せない**: 引数が揃った状態から領域を消してEnterしても文書は不変` +
        `(${guard ? 'コアが選択を空にした' : '選択が残り regionsStillExist が止めた'})`,
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
