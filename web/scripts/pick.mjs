/**
 * 射影 → 画面座標 → クリック(実機検査が共有する「指す」手)。
 *
 * rect の取得と射影は同じ `page.evaluate` の中で行う: `getBoundingClientRect()` を単体で返すと
 * DOMRect のプロパティは構造化複製に乗らず `rect.left` が undefined になる(SMOKE_TESTING.md「環境」)。
 * 画素は持ち越さず、押す直前に射影し直す: ツールバーの行が変わるとビューポートの高さ=射影が動く
 * (SMOKE_TESTING.md「座標」)。
 *
 * 「その視点で最初に射影できた面」を選ぶ手(`pickAnyFace`)はここに置かない: 検査が視点に暗黙依存する。
 * 対象は法線や寸法で名指しする(SMOKE_TESTING.md)。
 */
import { fail } from './args.mjs'
import { waitFrames, withoutBrowserPanel } from './browser.mjs'

/**
 * `window.__duocad[kind](...args)` の画面座標(ページ絶対)。射影できなければ null。
 * `kind` は `projectSketchPoint` / `projectFace` / `projectEdge` / `projectRegion` /
 * `projectBasePlane` / `projectPoint`。
 */
export async function screenOf(page, kind, ...args) {
  return page.evaluate(
    (name, params) => {
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      const position = window.__duocad[name](...params)
      return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
    },
    kind,
    args,
  )
}

/** ページ座標をクリックする。`modifier` は押しながらのキー(`Shift` / `Control`)。 */
export async function clickAt(page, at, { modifier = null } = {}) {
  if (modifier) await page.keyboard.down(modifier)
  await page.mouse.click(at.x, at.y)
  if (modifier) await page.keyboard.up(modifier)
}

/**
 * スケッチ平面の (u,v) の画面座標。`sketchId` を渡すと通常モードでも取れる(省略時は今のスケッチ)。
 * `settle` は射影の前に2フレーム待つ(ツールバーの行が変わると、即クリックは狙いと違う場所を押す)。
 */
export async function sketchPointAt(page, u, v, { sketchId = null, settle = false } = {}) {
  if (settle) await waitFrames(page, 2)
  return sketchId === null
    ? screenOf(page, 'projectSketchPoint', u, v)
    : screenOf(page, 'projectSketchPoint', u, v, sketchId)
}

/**
 * スケッチ平面の (u,v) を押す。常にブラウザパネルを畳んでから押す(`browser.mjs` の `withoutBrowserPanel`)。
 * 射影の前に2フレーム待つのが既定(`settle`): 待たないと、直前のクリックでツールバーの行が増える前の
 * 配置で画素を決めてしまう(実測: 円の中心の直後に半径の点を押すと、4mm のはずが 6mm になった。2回に1回)。
 */
export async function clickSketchPoint(
  page,
  u,
  v,
  { sketchId = null, modifier = null, settle = true } = {},
) {
  return withoutBrowserPanel(page, async () => {
    const at = await sketchPointAt(page, u, v, { sketchId, settle })
    if (!at) fail(`スケッチ平面の点 (${u}, ${v}) が画面の中に無い`)
    await clickAt(page, at, { modifier })
    return at
  })
}

/**
 * 平らな面を法線の向きで名指す(戻り値は面の添字。形が届くまで待ち、無ければ失敗)。
 * `at` を渡すと、重心の `at.axis` 成分(0/1/2)が `at.value` の面に絞る。複数あれば面積最大。
 */
export async function planarFace(page, bodyId, normal, { at = null, timeout = 30000 } = {}) {
  try {
    const handle = await page.waitForFunction(
      (id, direction, where) => {
        const faces = window.__duocad.getGeometry(id)?.faces ?? []
        const matches = faces.filter(
          (face) =>
            face.kind === 'plane' &&
            face.normal[0] * direction[0] + face.normal[1] * direction[1] + face.normal[2] * direction[2] > 0.9 &&
            (where === null || Math.abs(face.centroid[where.axis] - where.value) < 1e-6),
        )
        if (matches.length === 0) return null
        return matches.reduce((best, face) => (face.area > best.area ? face : best)).index
      },
      { timeout, polling: 50 },
      bodyId,
      normal,
      at,
    )
    return await handle.jsonValue()
  } catch {
    fail(`ボディ ${bodyId} に法線 ${JSON.stringify(normal)} の平らな面が無い`)
  }
}

export async function faceAt(page, bodyId, index) {
  return screenOf(page, 'projectFace', bodyId, index)
}

/** 面を押す。面は覆われた画素を候補から落として逃げられるので、パネルを畳むかは選べる(`hidePanel`)。 */
export async function clickFace(page, bodyId, index, { modifier = null, hidePanel = false } = {}) {
  const run = async () => {
    const at = await faceAt(page, bodyId, index)
    if (!at) fail(`ボディ ${bodyId} の面 ${index} が画面の中に無い`)
    await clickAt(page, at, { modifier })
    return at
  }
  return hidePanel ? withoutBrowserPanel(page, run) : run()
}

/**
 * スケッチの図形をその最初の点(円・円弧は中心、線分・ベジエは始点、長方形は左下)のクリックで選ぶ。
 * 当たり判定は点が最優先なので確実に掴める。
 */
export async function clickEntityHandle(page, entityId) {
  return withoutBrowserPanel(page, async () => {
    const at = await page.evaluate((id) => {
      const hook = window.__duocad
      const active = hook.sketch
      const entity = active?.entities.find((item) => item.entity_id === id)
      if (!entity) return null
      const p = entity.params
      const uv = 'cx' in p ? [p.cx, p.cy] : 'x0' in p ? [p.x0, p.y0] : [p.x, p.y]
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      const position = hook.projectSketchPoint(uv[0], uv[1])
      return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
    }, entityId)
    if (!at) fail(`図形 ${entityId} の点が画面の中に無い`)
    await clickAt(page, at)
    return at
  })
}

/**
 * スケッチを作る2手: ツールバーの「スケッチ」を起動 → 基準平面をクリック(即適用なので Enter は無い)。
 * 戻り値は作られた sketch_id。平面を指せる画素が出るまで待ち、`settle` は起動の反映を待って
 * 2フレーム置いてから射影する。
 * 貼れた先が本当にその基準平面かを毎回検算する(`verifyFrame`): `projectBasePlane` の画素は部品の
 * 輪郭のすぐ外側になることがあり、1画素ずれると側面に貼られ、後続のアサーションが全部通ってしまう
 * (SMOKE_TESTING.md「座標」)。
 */
export async function createSketchOnBasePlane(
  page,
  name = 'XY',
  { timeout = 60000, verifyFrame = true, settle = false, hidePanel = false } = {},
) {
  const run = async () => {
    const before = await page.evaluate(() => window.__duocad.sketches.length)
    await page.click('#sketch-create')
    let at = null
    try {
      if (settle) {
        await page.waitForFunction(() => window.__duocad.wantsBasePlanes === true, {
          timeout,
          polling: 50,
        })
        await waitFrames(page, 2)
      }
      const handle = await page.waitForFunction(
        (plane) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectBasePlane(plane)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        },
        { timeout, polling: 50 },
        name,
      )
      at = await handle.jsonValue()
    } catch {
      fail(`基準平面 ${name} を指せる画素が無い(「スケッチ」が起動しているか)`)
    }
    await clickAt(page, at)
    try {
      await page.waitForFunction(
        (want) => window.__duocad.sketches.length === want,
        { timeout, polling: 50 },
        before + 1,
      )
    } catch {
      fail(`基準平面 ${name} にスケッチを貼れない`)
    }
    const created = await page.evaluate(() => window.__duocad.activeSketch)
    if (verifyFrame) {
      const frame = JSON.parse(
        await page.evaluate(
          (id) =>
            JSON.stringify(
              window.__duocad.sketches.find((item) => item.sketch_id === id)?.frame ?? null,
            ),
          created,
        ),
      )
      const axis = { XY: 2, XZ: 1, YZ: 0 }[name]
      if (
        !frame ||
        frame.origin.some((value) => Math.abs(value) > 1e-6) ||
        Math.abs(Math.abs(frame.normal[axis]) - 1) > 1e-6
      ) {
        fail(`基準平面 ${name} ではない所に貼れた: ${JSON.stringify(frame)}`)
      }
    }
    return created
  }
  return hidePanel ? withoutBrowserPanel(page, run) : run()
}
