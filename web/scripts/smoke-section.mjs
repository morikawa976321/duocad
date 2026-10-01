/**
 * 実機での確認 — **断面表示(issue #6)**。本物のクリック・ドラッグ・キー入力で見る:
 *
 *   1. ブラウザの「断面」を入れると、内容の外接箱の中心で切れ、ホームビューのカメラ側が消える
 *   2. 切られた側の面は指せず、切り口を本物でクリックしても面は選ばれない(見えるもの＝指せるもの)
 *   3. 切り口が塗られ(ステンシルが効いている)、板が断面の外へはみ出さない
 *   4. ハンドルのドラッグ・数式の入力・反転・平面の切り替えで状態が動く
 *   5. 切るのをやめると元どおり指せる
 *
 *   node scripts/smoke-section.mjs
 *
 * 形は2つ: A=40×30×20 の箱(原点の隅から)、B=40×30×5 の低い箱(A の奥 y=50..80)。
 * 断面の既定位置は外接箱の中心 z=10 なので、A は上半分が消え、B(高さ5)は丸ごと残る。
 */
import { inflateSync } from 'node:zlib'
import { assertNoAppErrors, frameAll, openApp, sleep, waitFrames } from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedRect } from './mcp-client.mjs'

// `viewportConstants.ts` と揃える(変えたらここも変える)
const BASE_COLOR = 0x9aa6b2
const SECTION_CAP_COLOR = 0x6b7682

/** 1×1 の PNG の画素。1×1 は左・上の画素が無いので、どのフィルタ型でも生の値がそのまま入っている。 */
function decodeSinglePixelPng(png) {
  let offset = 8
  let header = null
  const data = []
  while (offset < png.length) {
    const length = png.readUInt32BE(offset)
    const type = png.toString('ascii', offset + 4, offset + 8)
    const body = png.subarray(offset + 8, offset + 8 + length)
    if (type === 'IHDR') header = { width: body.readUInt32BE(0), bitDepth: body[8], colorType: body[9] }
    if (type === 'IDAT') data.push(body)
    offset += 12 + length
  }
  if (header?.width !== 1 || header.bitDepth !== 8 || ![2, 6].includes(header.colorType)) {
    fail(`想定外のPNG: ${JSON.stringify(header)}`)
  }
  const raw = inflateSync(Buffer.concat(data))
  return [raw[1], raw[2], raw[3]]
}

const toLinear = (c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4)
const toSrgb = (c) => (c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055)
const channels = (hex) => [(hex >> 16) & 255, (hex >> 8) & 255, hex & 255].map((c) => toLinear(c / 255))

runSmoke('smoke-section', { timeout: 60000, mcp: true }, async ({ mcp, browser, url, timeout }) => {
  {
    const box = async (corner, width, height, distance) =>
      (await addExtrudedRect(mcp, { corner, width, height, distance })).bodyId
    const bodyA = await box([0, 0], 40, 30, 20)
    const bodyB = await box([0, 50], 40, 30, 5)
    ok(`箱A(40×30×20, body=${bodyA})と低い箱B(40×30×5, body=${bodyB})を作った`)

    const { page, consoleErrors } = await openApp(browser, url, {
      timeout,
      until: () => window.__duocad.state.bodies.length === 2,
    })
    await frameAll(page)

    /** 状態配信(カメラを動かすと可変解像度で形が配り直される)が止まるまで待つ。 */
    async function settle() {
      let epoch = await page.evaluate(() => window.__duocad.stateEpoch)
      for (;;) {
        await sleep(700)
        const next = await page.evaluate(() => window.__duocad.stateEpoch)
        if (next === epoch) break
        epoch = next
      }
      await waitFrames(page, 2)
    }
    await settle()

    const rect = await page.evaluate(() => {
      const box = document.querySelector('.viewport').getBoundingClientRect()
      return { left: box.left, top: box.top }
    })
    const section = () => page.evaluate(() => window.__duocad.section())
    const selectedFaces = () =>
      page.evaluate(() =>
        (window.__duocad.selection?.items ?? []).filter((item) => item.kind === 'face').map((item) => `${item.body}:${item.index}`),
      )
    /** 全頂点が `axis` 成分 = `value` の面(箱の平らな面を座標で名指す)。 */
    const faceWhere = (bodyId, axis, value) =>
      page.evaluate(
        (id, component, target) => {
          const geometry = window.__duocad.getGeometry(id)
          const face = geometry?.faces.find((candidate) => {
            const values = candidate.positions.filter((_, at) => at % 3 === component)
            return values.length > 0 && values.every((v) => Math.abs(v - target) < 1e-6)
          })
          return face?.index ?? null
        },
        bodyId,
        axis,
        value,
      )
    const projectFace = (bodyId, faceIndex) =>
      page.evaluate((id, index) => window.__duocad.projectFace(id, index), bodyId, faceIndex)
    /** 世界座標の画面位置。**キャンバスが実際にクリックを受ける画素か**も確かめる(パネルの下は使えない)。 */
    async function screenOf(point, label) {
      const at = await page.evaluate((p) => window.__duocad.projectPoint(p), point)
      if (at === null) fail(`${label} がカメラの裏`)
      const onCanvas = await page.evaluate(
        (x, y) => document.elementFromPoint(x, y)?.tagName === 'CANVAS',
        rect.left + at.x,
        rect.top + at.y,
      )
      if (!onCanvas) fail(`${label} の画素(${at.x.toFixed(0)},${at.y.toFixed(0)})がパネルに覆われている`)
      return at
    }
    async function pixel(at) {
      const shot = await page.screenshot({
        clip: { x: Math.round(rect.left + at.x), y: Math.round(rect.top + at.y), width: 1, height: 1 },
      })
      return decodeSinglePixelPng(Buffer.from(shot))
    }
    async function clickAt(at) {
      await page.mouse.click(rect.left + at.x, rect.top + at.y)
    }

    const topA = await faceWhere(bodyA, 2, 20)
    const frontA = await faceWhere(bodyA, 1, 0)
    if (topA === null || frontA === null) fail(`箱Aの上面/手前の面が見つからない: top=${topA} front=${frontA}`)

    // ---------------------------------------------------------------- 0. 切る前
    const initial = await section()
    if (initial.on || initial.stencilBodies !== 0) fail(`起動直後に断面が入っている: ${JSON.stringify(initial)}`)
    const topBefore = await projectFace(bodyA, topA)
    if (topBefore === null) fail('切る前に箱Aの上面が指せない(カメラの前提が崩れている)')
    // 断面の中(P)・断面の外で板の範囲内(Q)・切られない面(T: Bの上面)
    const pointP = await screenOf([34, 5, 10], '切り口の点P')
    const pointQ = await screenOf([44, 20, 10], '板の範囲内で断面の外の点Q')
    const pointT = await screenOf([20, 70, 5], '箱Bの上面の点T')
    const pixelQOff = await pixel(pointQ)
    ok(`切る前: 断面なし・箱Aの上面は (${topBefore.x.toFixed(0)},${topBefore.y.toFixed(0)}) で指せる`)

    // ---------------------------------------------------------------- 1. 入れる
    await page.click('#section-toggle')
    await page.waitForFunction(() => window.__duocad.section()?.on === true, { timeout, polling: 50 })
    await settle()
    const on = await section()
    if (on.plane !== 'XY') fail(`既定の平面がXYでない: ${on.plane}`)
    if (Math.abs(on.position - 10) > 1e-6) fail(`位置が外接箱の中心(z=10)でない: ${on.position}`)
    if (on.keepSign !== -1) fail(`ホームビューのカメラ側(+Z)を消していない: keepSign=${on.keepSign}`)
    if (on.stencilBodies !== 2) fail(`切り口の判定メッシュが2ボディぶん無い: ${on.stencilBodies}`)
    if (on.handle === null) fail('ハンドルが画面に無い')
    const shownPosition = await page.$eval('#section-position', (el) => el.value)
    if (shownPosition !== '10') fail(`位置の欄が10でない: ${shownPosition}`)
    ok('断面を入れた: XY・z=10・+Z側を消す・判定メッシュ2本・欄に10')

    // ---------------------------------------------------------------- 2. 切られた側は指せない
    if ((await projectFace(bodyA, topA)) !== null) fail('切られた上面(z=20)がまだ指せる')
    const frontKept = await projectFace(bodyA, frontA)
    if (frontKept === null) fail('残った手前の面(z<10の部分)が指せない')
    await clickAt(frontKept)
    await page.waitForFunction(
      (face) => (window.__duocad.selection?.items ?? []).some((item) => `${item.body}:${item.index}` === face),
      { timeout: 5000, polling: 50 },
      `${bodyA}:${frontA}`,
    )
    // 切り口(P)をクリック → 面に当たらない=何も無い所のクリックなので選択が外れる
    await clickAt(pointP)
    await page.waitForFunction(() => (window.__duocad.selection?.items ?? []).length === 0, {
      timeout: 5000,
      polling: 50,
    })
    // 上面が居た画素をクリックしても上面は選ばれない
    await clickAt(topBefore)
    await sleep(400)
    const afterTopClick = await selectedFaces()
    if (afterTopClick.includes(`${bodyA}:${topA}`)) fail('切られた上面が居た画素のクリックで上面が選ばれた')
    ok('切られた上面は指せない・残った面はクリックで選べる・切り口のクリックは何も選ばない')

    // ---------------------------------------------------------------- 3. 切り口が塗られている
    const handle = (await section()).handle
    for (const [label, at] of [['P', pointP], ['Q', pointQ], ['T', pointT]]) {
      if (Math.hypot(at.x - handle.x, at.y - handle.y) < 70) fail(`点${label}がハンドルに近すぎて画素を比べられない`)
    }
    const pixelP = await pixel(pointP)
    const pixelT = await pixel(pointT)
    // P(板・法線+Z)と T(Bの上面・法線+Z)は同じ光を受けるので、線形空間で色の比だけ違う
    const base = channels(BASE_COLOR)
    const capColor = channels(SECTION_CAP_COLOR)
    const predicted = pixelT.map((value, at) =>
      Math.round(toSrgb((capColor[at] * toLinear(value / 255)) / base[at]) * 255),
    )
    const capError = Math.max(...pixelP.map((value, at) => Math.abs(value - predicted[at])))
    if (capError > 6) {
      fail(`切り口が板の色で塗られていない: P=${pixelP} 予想=${predicted} (T=${pixelT})`)
    }
    const pixelQOn = await pixel(pointQ)
    const outsideError = Math.max(...pixelQOn.map((value, at) => Math.abs(value - pixelQOff[at])))
    if (outsideError > 3) fail(`断面の外に板がはみ出している: Q 切る前=${pixelQOff} 切った後=${pixelQOn}`)
    ok(`切り口は板の色(P=${pixelP}・予想${predicted})、断面の外は切る前と同じ(Q=${pixelQOn})`)

    // ---------------------------------------------------------------- 4a. ハンドルのドラッグ
    const poseBefore = await page.evaluate(() => JSON.stringify(window.__duocad.cameraPose()))
    const drag = 60
    await page.mouse.move(rect.left + handle.x, rect.top + handle.y)
    await page.mouse.down()
    for (let step = 1; step <= 6; step += 1) {
      await page.mouse.move(
        rect.left + handle.x + (handle.dir.x * drag * step) / 6,
        rect.top + handle.y + (handle.dir.y * drag * step) / 6,
      )
    }
    await page.mouse.up()
    const dragged = await section()
    if (!(dragged.position > on.position + 1)) {
      fail(`ハンドルを+Z方向へ ${drag}px ドラッグしても位置が増えない: ${on.position} → ${dragged.position}`)
    }
    const poseAfter = await page.evaluate(() => JSON.stringify(window.__duocad.cameraPose()))
    if (poseAfter !== poseBefore) fail('ハンドルのドラッグでカメラが動いた')
    if ((await selectedFaces()).length !== 0) fail('ハンドルのドラッグで選択が変わった(矩形選択に漏れた)')
    await page.waitForFunction(
      (expected) => document.querySelector('#section-position').value === expected,
      { timeout: 5000, polling: 50 },
      String(Number(dragged.position.toFixed(2))),
    )
    ok(`ハンドルのドラッグで z=${on.position} → ${dragged.position.toFixed(2)}(カメラ・選択は動かない・欄も追従)`)

    // ---------------------------------------------------------------- 4b. 数式で打つ
    await page.click('#section-position')
    await page.keyboard.down('Control')
    await page.keyboard.press('KeyA')
    await page.keyboard.up('Control')
    await page.keyboard.type('5+1')
    await page.keyboard.press('Enter')
    await page.waitForFunction(() => window.__duocad.section().position === 6, { timeout: 5000, polling: 50 })
    const typed = await page.$eval('#section-position', (el) => el.value)
    if (typed !== '6') fail(`式の確定後に欄が評価値へ戻らない: ${typed}`)
    ok('位置の欄に 5+1 を打って Enter → z=6')

    // ---------------------------------------------------------------- 4c. 反転
    await page.click('#section-invert')
    await page.waitForFunction(() => window.__duocad.section().inverted === true, { timeout: 5000, polling: 50 })
    const inverted = await section()
    if (inverted.keepSign !== 1) fail(`反転しても残す側が+Zにならない: ${inverted.keepSign}`)
    await settle()
    if ((await projectFace(bodyA, topA)) === null) fail('反転したのに上面(z=20)が指せない')
    ok('反転で +Z 側が残り、上面が指せる')

    // ---------------------------------------------------------------- 4d. 平面の切り替え
    await page.click('#section-plane-YZ')
    await page.waitForFunction(() => window.__duocad.section().plane === 'YZ', { timeout: 5000, polling: 50 })
    const yz = await section()
    if (Math.abs(yz.position - 20) > 1e-6) fail(`YZへ替えた位置が外接箱の中心(x=20)でない: ${yz.position}`)
    ok('YZ へ切り替えると x=20(外接箱の中心)で切る')

    // ---------------------------------------------------------------- 5. やめる
    await page.click('#section-toggle')
    await page.waitForFunction(() => window.__duocad.section().on === false, { timeout: 5000, polling: 50 })
    await settle()
    const off = await section()
    if (off.stencilBodies !== 0 || off.handle !== null) fail(`やめても判定メッシュ・ハンドルが残る: ${JSON.stringify(off)}`)
    if ((await projectFace(bodyA, topA)) === null) fail('やめたのに上面が指せない')
    if ((await page.$('#section-position')) !== null) fail('やめたのに位置の欄が出ている')
    ok('やめると判定メッシュ・ハンドル・操作子が消え、上面がまた指せる')

    await assertNoAppErrors(page, consoleErrors)
  }
})
