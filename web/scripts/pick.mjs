/**
 * 射影 → 画面座標 → クリック(実機検査が共有する「指す」手)。
 *
 * `page` と対象を引数で受けて値を返す形(`web/src/planeGrid.ts` と同じ)。**rect の取得と射影を
 * 同じ `page.evaluate` の中で行う** —— `getBoundingClientRect()` を単体で返すと DOMRect の
 * プロパティは構造化複製に乗らず `rect.left` が undefined になる(SMOKE_TESTING.md「環境」)。
 * **画素は持ち越さず、押す直前に射影し直す**(コマンドの起動や引数の確定でツールバーの行が
 * 変わり、ビューポートの高さ=射影が動く。SMOKE_TESTING.md「座標」)。
 *
 * **「その視点で最初に射影できた面」を選ぶ手(`pickAnyFace`)はここに置かない** —— 検査が視点に
 * 暗黙依存する形そのもので、対象は法線や寸法で名指しする(SMOKE_TESTING.md)。
 */
import { fail } from './args.mjs'
import { sleep, viewportCenter, waitFrames, withoutBrowserPanel } from './browser.mjs'

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
 * `settle` は射影の前に2フレーム待つ —— コマンドを起動したり引数が1つ埋まったりするとツールバーの
 * 行が変わり、射影して即クリックすると狙いと違う場所を押す(`smoke-regions.mjs` の作法)。
 */
export async function sketchPointAt(page, u, v, { sketchId = null, settle = false } = {}) {
  if (settle) await waitFrames(page, 2)
  return sketchId === null
    ? screenOf(page, 'projectSketchPoint', u, v)
    : screenOf(page, 'projectSketchPoint', u, v, sketchId)
}

/**
 * スケッチ平面の (u,v) を押す。**常に浮いているブラウザを畳んでから**押す —— (u,v) は画面の
 * 1画素しかなく、そこがパネルの下に来ると逃げ場が無い(`browser.mjs` の `withoutBrowserPanel`)。
 *
 * **射影の前に2フレーム待つのが既定**(`settle`)。直前のクリックで引数が1つ埋まるとツールバーの
 * 行が増え、待たずに射影すると増える前の配置で画素を決めてしまう(実測: 円の中心を押した直後に
 * 半径の点を押すと、4mm のはずの半径が 6mm になった。2回に1回)。
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
 * 平らな面を**法線の向きで名指す**(戻り値は面の添字。形が届くまで待ち、無ければ失敗)。
 * `at` を渡すと、重心の `at.axis` 成分(0/1/2)が `at.value` の面に絞る。同じ向きの面が複数
 * あるときは面積が最大のもの。
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

/** 面の画面座標(射影できなければ null)。 */
export async function faceAt(page, bodyId, index) {
  return screenOf(page, 'projectFace', bodyId, index)
}

/**
 * **視点が落ち着いてから**面の画面座標を返す。`lookAtSketch` / `sketch-exit` の直後は
 * OrbitControls のダンピングでカメラが数十ms動き続け、射影した座標をそのまま押すと別の部品の
 * 面に当たる。2回続けて同じ画素に射影できたら止まったとみなす。
 */
export async function faceAtWhenStill(page, bodyId, index, { attempts = 40, interval = 50 } = {}) {
  let previous = null
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const at = await faceAt(page, bodyId, index)
    if (at && previous && Math.hypot(at.x - previous.x, at.y - previous.y) < 1) return at
    previous = at
    await sleep(interval)
  }
  return null
}

/**
 * 面を押す。面は逃げ場がある(候補の三角形を複数持ち、パネルに覆われた画素は候補から落ちる)ので、
 * パネルを畳むかは選べる(`hidePanel`)。
 */
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
 * 領域の画面座標。`sketchId` を渡すと通常モードでも射影できる(スケッチモードの外では「今の
 * スケッチ」が無い)。指せない状態なら null —— それ自体がゲートの検査になる。
 */
export async function regionAt(page, index, { sketchId = null, settle = false } = {}) {
  if (settle) await waitFrames(page, 2)
  return sketchId === null
    ? screenOf(page, 'projectRegion', index)
    : screenOf(page, 'projectRegion', index, sketchId)
}

export async function clickRegion(
  page,
  index,
  { sketchId = null, modifier = null, hidePanel = false, settle = false } = {},
) {
  const run = async () => {
    const at = await regionAt(page, index, { sketchId, settle })
    if (!at) fail(`領域 ${sketchId === null ? '' : `${sketchId}:`}${index} に当たる画素が無い`)
    await clickAt(page, at, { modifier })
    return at
  }
  return hidePanel ? withoutBrowserPanel(page, run) : run()
}

/**
 * スケッチの図形を**その最初の点**(円・円弧は中心、線分・ベジエは始点、長方形は左下)のクリックで
 * 選ぶ。点は必ずその図形の上にあり、当たり判定も点が最優先なので確実に掴める。
 */
export async function clickEntityHandle(page, entityId) {
  return withoutBrowserPanel(page, async () => {
    const at = await page.evaluate((id) => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
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
 * 辺を本物のクリックで選ぶ。射影できなければ false、**選択に入るまで待って**入らなければ false
 * (狙いが外れて隣の辺に取られた等。呼び出し側が次の辺を試す)。例外にしない。
 * 選択の正はコアなので、押した直後の `selection` は読まず次の配信を待つ。
 */
export async function clickEdge(page, bodyId, index, { modifier = null, timeout = 5000 } = {}) {
  const at = await screenOf(page, 'projectEdge', bodyId, index)
  if (!at) return false
  await clickAt(page, at, { modifier })
  try {
    await page.waitForFunction(
      (id, edgeIndex) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'edge' && item.index === edgeIndex,
        ),
      { timeout, polling: 50 },
      bodyId,
      index,
    )
    return true
  } catch {
    return false
  }
}

/**
 * **スケッチを作る2手**: ツールバーの「スケッチ」を起動 → 基準平面をクリック(Enter は無い:
 * 対象1つ・how の入力なし → 即適用)。戻り値は作られた sketch_id。
 *
 * 起動してから、その平面を指せる画素が出るまで待つ(`projectBasePlane` は「その画素からのレイが
 * 本当にその平面に当たり、手前に面が無い」ことを確かめてから返す)。`settle` は起動の反映を待って
 * 2フレーム置いてから射影する(`smoke-regions.mjs` の作法)。
 * **貼れた先が本当にその基準平面かを毎回検算する**(`verifyFrame`) —— `projectBasePlane` が
 * 返す画素は部品の輪郭のすぐ外側になることがあり、1画素ずれると側面に貼られ、後続のアサーションは
 * それで全部通ってしまう(SMOKE_TESTING.md「座標」)。
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

/**
 * 編集中のスケッチの (u,v) が**押せる画素**になるまで、実際にホイールでズームアウトする。
 *
 * 空のスケッチへ入った直後のカメラは前の視点の距離を引き継ぐので、原点から離れた点は画面の外か、
 * 右上のビューキューブの下に来ることがある(キューブの矩形は押下を引き取るので
 * `projectSketchPoint` が null を返す)。スケッチの視点は原点を画面中央に置くので、引けば点は
 * 中央へ寄ってキューブから外れる。
 */
export async function zoomOutUntilSketchPointClickable(page, u, v, { maxNotches = 40, delay = 40 } = {}) {
  for (let i = 0; i < maxNotches; i += 1) {
    if (await sketchPointAt(page, u, v, { settle: true })) return
    const center = await viewportCenter(page)
    await page.mouse.move(center.x, center.y)
    await page.mouse.wheel({ deltaY: 120 }) // 正で引く(ズームアウト)
    await sleep(delay)
  }
  fail(`スケッチ平面の点 (${u}, ${v}) が、ズームアウトしても押せる画素にならない`)
}

/**
 * 指定のワールド座標が画面に映るまで実際にホイールでズームアウトする(`frameAll` は内容の外接箱に
 * 合わせるだけで、ピック範囲全体は画面に入らない)。**`projectPoint` は z だけを見て x/y は
 * クリップしない**ので、映ったかは `.viewport` の rect と突き合わせる。カーソルは刻みごとに
 * ビューポート中央へ置く(ホイールはカーソル位置で発火する)。
 */
export async function zoomOutUntilVisible(page, worldPoint, { maxNotches = 200, delay = 40 } = {}) {
  for (let i = 0; i < maxNotches; i += 1) {
    const visible = await page.evaluate((point) => {
      const box = document.querySelector('.viewport').getBoundingClientRect()
      const at = window.__duocad.projectPoint(point)
      return Boolean(at && at.x >= 0 && at.x <= box.width && at.y >= 0 && at.y <= box.height)
    }, worldPoint)
    if (visible) return true
    const center = await viewportCenter(page)
    await page.mouse.move(center.x, center.y)
    await page.mouse.wheel({ deltaY: 480 }) // 正で引く(ズームアウト)
    await sleep(delay)
  }
  return false
}
