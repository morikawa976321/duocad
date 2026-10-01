/**
 * 実機での確認 — **カメラ**(画角と座標系)。three.js の描画結果でしか分からないものだけを見る。
 *
 *   node scripts/smoke-camera.mjs [--port 8885] [--http-port 8886]
 *
 * 1. **画角(FOV)**: どの画角でも `frameAll` が図形を画面に収める / 画角を変えても注視点付近の
 *    見かけの大きさが変わらない(距離補正) / 0度が本物の正投影である / 正投影でも寄り引きと
 *    `frameAll` が噛み合う / ビューキューブが画角に追従する / UI は正投影のチェックボックス1つ
 * 2. **座標系(Z-up 右手系)**: `camera.up` が +Z で初期視点が front-top-right / +Z が画面で上 /
 *    オービットの極が +Z / スケッチに入って出ても Y-up へ戻らない
 *
 * 場面は MCP で作る板1枚・穴3つ。ブラウザを触るのはカメラの操作とスケッチの出入りだけ。
 */
import { assertNoAppErrors, frameAll, openApp, sleep, waitFrames, waitOrFail, withoutBrowserPanel } from './browser.mjs'
import { ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import { addExtrudedCircle, addExtrudedRect } from './mcp-client.mjs'
import { createSketchOnBasePlane } from './pick.mjs'

runSmoke('smoke-camera', { mcp: true }, async ({ mcp, browser, url, timeout }) => {
  const { page, consoleErrors } = await openApp(browser, url, { timeout, height: 900 })
  const waitFor = (predicate, what, ...args) => waitOrFail(page, predicate, what, { args, timeout })

  // **スケッチの最初の退出を、ボディを作る前に済ませる。** 既定の視点へ戻す処理は「最初の退出」と
  // 「2回目以降」で経路が違う(後者だけが控えた up を復元する)ので、両方を見るには最初の1回を
  // ここで控える。基準平面のグリッドはボディに覆われると指せないので、空の文書のうちに貼る。
  await createSketchOnBasePlane(page, 'XY', { timeout })
  const sketchId = await page.evaluate(() => window.__duocad.activeSketch)
  await page.click('#sketch-exit')
  await waitFor(() => window.__duocad.activeSketch === null, 'スケッチを抜けられない')
  const upAfterFirstExit = await page.evaluate(() => window.__duocad.cameraPose().up)

  const scene = await addExtrudedRect(mcp, { corner: [-40, -25], width: 80, height: 50, distance: 8 })
  for (const center of [[-22, 0], [0, 0], [22, 0]]) {
    await addExtrudedCircle(mcp, { center, radius: 6, distance: 8, mode: 'cut' })
  }
  // 板(6面)+ 穴3つの側面 = 9面。穴が開いていなければ以降の「面の広がり」が別の場面の値になる。
  await waitFor(
    (bodyId) => window.__duocad.parts.find((part) => part.body_id === bodyId)?.face_count === 9,
    '板1枚・穴3つの場面が画面に届かない',
    scene.bodyId,
  )

  /** 設定ダイアログの開閉(DOMに反映されるまで待つ)。 */
  async function setSettingsOpen(open) {
    const now = await page.evaluate(() => document.querySelector('#settings-dialog')?.open === true)
    if (now === open) return
    await page.click(open ? '#settings-open' : '#settings-close')
    await page.waitForFunction(
      (want) => document.querySelector('#settings-dialog')?.open === want,
      { timeout: 30000, polling: 20 },
      open,
    )
  }

  /**
   * 画角を `value` にする。**UIを経由せず内部API(`window.__duocad.setFov`)を直呼び**
   * する(UIは正投影/透視30度の2値なので、30〜90の連続値はUIから到達できない ——
   * ここで検査しているのはビューポートの責務(距離補正・`frameAll`・正投影の性質)で
   * あってUIの責務ではないので、経路をAPIへ移すだけで検査の中身は変えない)。
   */
  async function setFovTo(value) {
    await page.evaluate((deg) => window.__duocad.setFov(deg), value)
    await page.waitForFunction(
      (want) => window.__duocad.fov === want,
      { timeout: 30000, polling: 20 },
      value,
    )
    return value
  }

  /**
   * 注視点の手前(-1)・注視点上(0)・奥(+1)に**同じ長さの世界の線分**を視線と直角に置き、
   * それぞれ画面で何画素に写るかを測る(`ratio` は手前÷奥)。
   *
   * **これが正投影の検査の要**である。透視投影ではこの比は `(d+L)/(d-L)` ——
   * **距離だけで決まり画角に依らない**ので、画角をいくら絞っても1にならない。
   * つまり「比が1」は近似では作れず、本物の平行投影であることの証拠になる
   * (面の重心のような図形依存の点ではなく、視点から作った世界座標を射影するので、
   * どんなシーンでも同じように測れる)。
   *
   * **`scale` を渡すと線分の長さ(＝物差し)を世界座標で固定する。** 渡さないと注視点までの
   * 距離から作るので、カメラが前後した瞬間に物差しごと変わり、別の時点の測定値と
   * 比べられない(返り値の `scale` をそのまま次へ渡すこと)。
   *
   * **前後のずらしだけは固定しない。** こちらを世界座標で固定すると、カメラが遠ざかる
   * 実装(画角を絞って正投影を真似る等)では前後の差が距離に対して相対的に消え、比が1へ
   * 近づく —— 検査の検出力がカメラの遠さで薄まる。画角1度の偽物で実測すると
   * 比 1.0127(しきい値1.01のすぐ外)しか出ず、もう一声絞られたら通ってしまう。
   * よって**常に今の距離から作る**(本物の透視なら距離によらず約1.86)。
   */
  async function depthProbe(scale = null) {
    return await page.evaluate((given) => {
      const hook = window.__duocad
      const pose = hook.cameraPose()
      if (pose === null) return null
      const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
      const len = (v) => Math.hypot(v[0], v[1], v[2])
      const unit = (v) => {
        const l = len(v)
        return [v[0] / l, v[1] / l, v[2] / l]
      }
      const cross = (a, b) => [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
      ]
      const toTarget = sub(pose.target, pose.position)
      const distance = len(toTarget)
      if (!(distance > 0)) return null
      const dir = unit(toTarget)
      const right = unit(cross(dir, pose.up))
      if (!right.every((value) => Number.isFinite(value))) return null
      const half = given === null ? distance * 0.05 : given.half // 線分の半分の長さ(物差し)
      const depth = distance * 0.3 // 注視点からの前後のずらし(常に今の距離から作る)
      const widthAt = (k) => {
        const center = [0, 1, 2].map((i) => pose.target[i] + dir[i] * depth * k)
        const a = hook.projectPoint(center.map((value, i) => value + right[i] * half))
        const b = hook.projectPoint(center.map((value, i) => value - right[i] * half))
        if (a === null || b === null) return null
        return Math.hypot(a.x - b.x, a.y - b.y)
      }
      const near = widthAt(-1)
      const at = widthAt(0)
      const far = widthAt(1)
      if (near === null || at === null || far === null) return null
      return { near, at, far, ratio: near / far, scale: { half } }
    }, scale)
  }

  /**
   * 今この瞬間に画面へ射影できる面を全部集める(ビューポート左上を原点とする画素)。
   * `fromCenter` はビューポート中心からの距離 —— **中心付近か外周かで期待する挙動が
   * 逆になる**ので、判定はこの値で振り分ける。
   */
  async function faceScreenPoints() {
    return await page.evaluate(() => {
      const hook = window.__duocad
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      const center = { x: rect.width / 2, y: rect.height / 2 }
      const points = []
      for (const part of hook.parts) {
        const geometry = hook.getGeometry(part.body_id)
        if (geometry === null) continue
        for (const face of geometry.faces) {
          // **面の重心を、固定した世界座標として`projectPoint`で射影する**
          // (`projectFace`は使わない)。`projectFace`は「面の三角形を順に走査して
          // 最初にクリックが通る画素」を返す実装で、代表点ではなく**たまたま条件を
          // 満たした三角形の重心**を返す。視点・画角を変えると勝ち残る三角形が入れ替わり、
          // 同じ面でも画面座標が数十px跳ぶ(engine
          // `projected-face-point-jumps-between-triangles`。実測: 中心から
          // 211pxの面で27.3px対3.5pxの差)。ここは「視点・画角の違いを比べる」検査なので、
          // 固定した世界座標(面の重心)をそのまま射影する`projectPoint`を使う
          // (`projectFace`が要るのは「本当にそこを押せるか」を見る手だけ)。
          const at = hook.projectPoint(face.centroid)
          if (at === null) continue
          if (at.x < 0 || at.y < 0 || at.x > rect.width || at.y > rect.height) continue
          points.push({
            key: `${part.body_id}#${face.index}`,
            x: at.x,
            y: at.y,
            fromCenter: Math.hypot(at.x - center.x, at.y - center.y),
          })
        }
      }
      return { points, width: rect.width, height: rect.height }
    })
  }

  /**
   * 次の描画が終わるまで待つ(2フレーム分)。`setFovTo` は
   * `window.__duocad.fov` の値が変わったことしか待たないので、その直後に
   * スクリーンショットを撮ると更新前のcanvasを拾う恐れがある(`tick`のrAFループが
   * 実際に1回まわったことを保証するため2回分待つ)。
   */
  const waitTwoFrames = () => waitFrames(page, 2)

  /**
   * ビューキューブの矩形(ページ絶対座標・CSSピクセル)。`viewCubeRect()` は
   * `.viewport` コンテナ内相対(左上原点)を返すので、コンテナの位置を足す
   * (`clearSelectionByClickingNothing`と同じ変換。`smoke-files.mjs`参照)。
   */
  async function cubeRectAbsolute() {
    return await page.evaluate(() => {
      const box = document.querySelector('.viewport').getBoundingClientRect()
      const cube = window.__duocad.viewCubeRect()
      return { x: box.left + cube.x, y: box.top + cube.y, width: cube.w, height: cube.h }
    })
  }

  /** ビューキューブの矩形だけを切り取ったスクリーンショット(PNGのバイト列)。 */
  async function cubeShot(rect) {
    return await page.screenshot({ clip: rect, encoding: 'binary' })
  }

  /**
   * 2枚のPNGバイト列がどれだけ違うか。**画素単位のデコードはしない**(依存を増やさない)
   * —— 単純なバイト単位の不一致数(+長さの差)を「見え方がどれだけ違うか」の粗い代理指標
   * として使う。Chromeのheadlessスクリーンショットは同じ画素からは決定論的にエンコードされる
   * ので、同一カメラ・同一FOVの制御群同士なら理論上0になる(タイムスタンプ等の非決定要素は
   * PNGチャンクに含まれない)。
   */
  function pngByteDiff(a, b) {
    const len = Math.min(a.length, b.length)
    let diff = Math.abs(a.length - b.length)
    for (let i = 0; i < len; i += 1) if (a[i] !== b[i]) diff += 1
    return diff
  }

  /** 減衰(`enableDamping`)が効いているので、カメラが止まってから読む。 */
  async function settledPose() {
    let previous = null
    for (let guard = 0; guard < 200; guard += 1) {
      const pose = await page.evaluate(() => window.__duocad.cameraPose())
      if (
        previous !== null &&
        previous.position.every((value, i) => Math.abs(value - pose.position[i]) < 1e-4)
      ) {
        return pose
      }
      previous = pose
      await sleep(30)
    }
    throw new Error('カメラが止まらない(減衰が終わらない)')
  }

  // ================================================================ 画角(FOV)
  //
  // **動かないことを見るのは画面中心に近い点、動くことを見るのは一番外の点**にする。両方見ないと
  // 検出力が無い —— `setFov` が丸ごと効いていない実装でも「中心が動かない」だけなら緑になる。
  //
  // **ダイアログは各手の前に閉じる。** 非モーダル(`show()`)なので開いたままでもビューポートの
  // クリックは効くが、ダイアログの矩形が覆っている画素だけは取れない。
  {
    await setSettingsOpen(false)
    await setFovTo(45)
    await frameAll(page)

    // (1) 30 と 90 のどちらでも、frameAll のあとに図形が画面の中にある(完了条件8・9)。
    //
    //     **「1面でも射影できた」では検出力が無い。** `frameToContent` が画角を見ずに
    //     距離を決めていても、中央付近の面はどのみち画面に残るので緑になってしまう
    //     (実際に距離式を `diagonal * 1.2` へ戻して確かめた)。そこで**画面に占める
    //     大きさ**を見る —— `fitDistance` は画角によらず同じ外接球を画面いっぱいに
    //     収めるので、射影された面の広がりは画角を変えてもほぼ変わらないはずである。
    //     画角を見ない距離式なら、90度では 3.7 倍ほど小さく写る(=シーンが豆粒になる)。
    const framed = {}
    const captured = {}
    // **0(正投影)も同じ枠で見る。** 正投影は距離では収まらず視錐台で収めるので、
    // `frameToContent` が透視のときの距離式だけを直していると、ここで豆粒になるか
    // 画面からはみ出す。
    for (const angle of [45, 30, 90, 0]) {
      const shown = await setFovTo(angle)
      await frameAll(page)
      const { points } = await faceScreenPoints()
      if (points.length === 0) {
        throw new Error(`画角${angle}で frameAll しても面が1つも画面の中に無い`)
      }
      captured[angle] = new Map(points.map((point) => [point.key, point]))
      framed[angle] = { shown, faces: points.length }
    }
    // **広がりは同じ面の集合で測る。** 射影できる面は画角によって増えたり減ったりするので、
    // 拾えた面の顔ぶれの違いを広がりの差と取り違えない。
    const commonKeys = [...captured[45].keys()].filter(
      (key) => captured[30].has(key) && captured[90].has(key) && captured[0].has(key),
    )
    if (commonKeys.length < 3) {
      throw new Error(`0/30/45/90 のどれでも射影できる面が ${commonKeys.length} 個しか無い(広がりを比べられない)`)
    }
    const spreadOf = (angle) => {
      const xs = commonKeys.map((key) => captured[angle].get(key).x)
      const ys = commonKeys.map((key) => captured[angle].get(key).y)
      return Math.hypot(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys))
    }
    for (const angle of [45, 30, 90, 0]) framed[angle].spread = Number(spreadOf(angle).toFixed(1))
    for (const angle of [30, 90, 0]) {
      const ratio = framed[angle].spread / framed[45].spread
      framed[angle].ratio = Number(ratio.toFixed(3))
      if (ratio < 0.6 || ratio > 1.5) {
        throw new Error(
          `画角${angle}で frameAll した図形の広がりが45度の ${ratio.toFixed(2)} 倍` +
            `(${framed[angle].spread} / ${framed[45].spread} 画素。` +
            '収める大きさが投影に追随していない)',
        )
      }
    }

    // (2) 距離補正。**frameAll を挟まずに**画角だけを 45 → 60 へ変え、注視点の平面に置いた
    //     同じ物差しの見かけの長さを比べる。補正が正しければ場面に依らず不変で、補正が無ければ
    //     tan(22.5°)/tan(30°) ≒ 0.717 倍に縮む(実測: 正しい実装で 1.0000 倍、`setFov` の補正を
    //     5% ずらすと 0.952 倍)。
    //
    //     **面の重心では測らない。** 注視点の平面から奥行きのずれた点は補正が正しくてもパース差で
    //     動くので、しきい値が場面ごとに変わる(実測: 59面の場面で外周 1.8px、板1枚の場面で 12.2px)。
    await setFovTo(45)
    await frameAll(page)
    const rulerAt45 = await depthProbe()
    if (rulerAt45 === null) throw new Error('画角45で注視点の物差しが測れない')
    await setFovTo(60)
    const rulerAt60 = await depthProbe(rulerAt45.scale)
    if (rulerAt60 === null) throw new Error('画角60で注視点の物差しが測れない')
    const keptAcrossFov = rulerAt60.at / rulerAt45.at
    if (Math.abs(keptAcrossFov - 1) > 0.02) {
      throw new Error(
        `画角45→60で注視点の見かけの大きさが ${keptAcrossFov.toFixed(3)} 倍になる` +
          '(距離補正が効いていない。補正が無ければ 0.717 倍)',
      )
    }
    // (3) 画角そのものが本当に変わっていること(`setFov` が丸ごと効いていない実装を落とす)。
    //     **45→90で見る** —— 45→60のパース差はこのシーンの薄さでは10画素に届かず、
    //     境目を引ける幅が無い。45→90なら拡大率が 2.414 倍になり、注視点から奥行きの
    //     ずれた面は数十画素動く。
    await setFovTo(45)
    await frameAll(page)
    const wideBefore = await faceScreenPoints()
    await setFovTo(90)
    const wideAfter = await faceScreenPoints()
    const wideByKey = new Map(wideAfter.points.map((point) => [point.key, point]))
    const wideShifts = wideBefore.points
      .filter((point) => wideByKey.has(point.key))
      .map((point) => {
        const now = wideByKey.get(point.key)
        return Math.hypot(now.x - point.x, now.y - point.y)
      })
    if (wideShifts.length === 0) throw new Error('画角45と90の両方で射影できる面が1つも無い')
    const wideMaxShift = Math.max(...wideShifts)
    if (wideMaxShift < 20) {
      throw new Error(`画角45→90でどの面も ${wideMaxShift.toFixed(1)} 画素しか動かない(画角が実際には変わっていない)`)
    }

    // (4) **完全正投影(0度)。** 見るのは2点 —— (a) 手前と奥で見かけの大きさが変わらない
    //     こと(`depthProbe` の説明のとおり、これは「画角のとても小さい透視」では作れない)、
    //     (b) 切り替えても注視点の見かけの大きさが変わらないこと(30〜90の距離補正と同じ
    //     約束を、投影の切り替えでも守る)。
    await setFovTo(45)
    await frameAll(page)
    const perspProbe = await depthProbe()
    if (perspProbe === null) {
      throw new Error('透視での前後の比が測れない(線分が画面の外か、視線が上方向と平行)')
    }
    if (perspProbe.ratio < 1.5) {
      throw new Error(
        `画角45なのに手前と奥の見かけが ${perspProbe.ratio.toFixed(3)} 倍しか違わない` +
          '(この検査自体が効いていない。想定は約1.86倍)',
      )
    }
    const scale = perspProbe.scale // 以降は同じ物差しで測る(カメラが前後しても比べられる)
    /**
     * **見かけの大きさが動かなくなるまで測り続ける。** ホイールの効きは次の rAF まで
     * 出ないので、送った直後に1回測るだけでは寄る前の値を読む。「変わるまで待つ」では
     * なく「動かなくなるまで待つ」にしてあるのは、1回の操作が複数のイベントに分かれて
     * 届いたときに途中の値を掴まないため(**この分割が実際に起きるかは確かめていない**。
     * 上限200回で、正常時は数回で抜ける)。
     */
    async function settledProbe() {
      let previous = null
      let stable = 0
      for (let guard = 0; guard < 200 && stable < 3; guard += 1) {
        const now = await depthProbe(scale)
        if (now === null) return null
        if (previous !== null && Math.abs(now.at - previous.at) < 0.01) stable += 1
        else stable = 0
        previous = now
      }
      return previous
    }
    const orthoShown = await setFovTo(0)
    if (orthoShown !== 0) throw new Error(`正投影のfovが${orthoShown}になっている`)
    const orthoProbe = await depthProbe(scale)
    if (orthoProbe === null) throw new Error('正投影での前後の比が測れない')
    if (Math.abs(orthoProbe.ratio - 1) > 0.01) {
      throw new Error(
        `正投影なのに手前と奥の見かけが ${orthoProbe.ratio.toFixed(4)} 倍違う` +
          `(手前 ${orthoProbe.near.toFixed(1)}px / 奥 ${orthoProbe.far.toFixed(1)}px。パースが残っている)`,
      )
    }
    const orthoKeptSize = orthoProbe.at / perspProbe.at
    if (Math.abs(orthoKeptSize - 1) > 0.02) {
      throw new Error(
        `正投影へ切り替えると注視点の見かけの大きさが ${orthoKeptSize.toFixed(3)} 倍になる` +
          '(切り替えがズームになっている)',
      )
    }

    // (5) **正投影の寄り引きと、そこからの `frameAll`。** 正投影のドリーは `zoom` を
    //     動かすだけで**カメラを動かさない**ので、`frameAll` が位置しか直していないと
    //     寄った倍率のまま居座る。上の(1)の枠ではこれを検出できない —— 画角を切り替えた
    //     直後の見かけの大きさは `setFov` 側が保つので、`frameAll` が何もしなくても
    //     たまたま正しい値になっているためである。ここは**先にホイールで倍率を崩してから**
    //     `frameAll` を呼ぶので、視錐台を作り直していない実装は戻れない。
    const zoomedIn = await withoutBrowserPanel(page, async () => {
      const box = await page.evaluate(() => {
        const rect = document.querySelector('.viewport').getBoundingClientRect()
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }
      })
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
      await page.mouse.wheel({ deltaY: -600 })
      return await settledProbe()
    })
    if (zoomedIn === null || !(zoomedIn.at > orthoProbe.at * 1.2)) {
      throw new Error(
        `正投影でホイールが効かない(見かけ ${zoomedIn === null ? 'null' : zoomedIn.at.toFixed(1)}px /` +
          ` 寄る前 ${orthoProbe.at.toFixed(1)}px)`,
      )
    }
    // 寄っても平行投影のままであること(`zoom` の道でパースが混ざらない)。
    if (Math.abs(zoomedIn.ratio - 1) > 0.01) {
      throw new Error(`正投影で寄ったら手前と奥の見かけが ${zoomedIn.ratio.toFixed(4)} 倍違う`)
    }
    await frameAll(page)
    const refit = await settledProbe()
    if (refit === null) throw new Error('正投影の frameAll 後に前後の比が測れない')
    const refitRatio = refit.at / orthoProbe.at
    if (Math.abs(refitRatio - 1) > 0.05) {
      throw new Error(
        `正投影で寄ったあと frameAll しても倍率が戻らない(戻り ${refitRatio.toFixed(3)} 倍。` +
          `${refit.at.toFixed(1)}px / ${orthoProbe.at.toFixed(1)}px)`,
      )
    }

    // (6) **ビューキューブがFOVに追従してひずむこと**(専用の正投影カメラをやめ、
    //     本編と同じ画角で描く作りになっている)。カメラの**向きは
    //     動かさず**(`frameAll`は最初の1回だけ)、FOVだけを変えてキューブ矩形の画素を
    //     比べる —— 画角を変えている最中の実運用と同じ操作列にすることで、
    //     `setFov`から明示的に通知しなくても毎フレームの`updateCubeCamera`が追随する
    //     ことも一緒に確かめる。
    //
    //     **制御群(同一FOV同士の差分)を挟む** —— スクリーンショットのPNGエンコードや
    //     アンチエイリアスの揺らぎだけを「変わった」と誤検出しないため、30度↔90度の差分が
    //     制御群の差分より明確に大きいことを見る(byte-diffの絶対値ではなく比で判定する)。
    await setFovTo(45)
    await frameAll(page) // 角視点(HOME_VIEW_OFFSETの向き=前・上・右)に固定
    const cubeRect = await cubeRectAbsolute()
    await waitTwoFrames()
    const cubeAt45a = await cubeShot(cubeRect)
    // **frameAllは呼ばない** —— 向きはそのまま、FOVだけを変える(画角を変えている
    // 最中と同じ操作。frameAllを挟むと向きも寄り引きも変わり、キューブの追従だけを見る
    // 検査にならない)。
    await setFovTo(30)
    await waitTwoFrames()
    const cubeAt30 = await cubeShot(cubeRect)
    await setFovTo(90)
    await waitTwoFrames()
    const cubeAt90 = await cubeShot(cubeRect)
    await setFovTo(45)
    await waitTwoFrames()
    const cubeAt45b = await cubeShot(cubeRect) // 制御群: 最初と同じ向き・同じFOV

    const cubeControlDiff = pngByteDiff(cubeAt45a, cubeAt45b)
    const cubeFovDiff = pngByteDiff(cubeAt30, cubeAt90)
    // しきい値は実測から作った。同一FOV制御群の差分は実測0バイト
    // (決定論的な描画・エンコード)なので、1バイトでも大きければ検出できるはずだが、
    // 将来の非決定要素(タイムスタンプ入りメタデータ等)の混入に備えて8倍の余裕を見る。
    if (!(cubeFovDiff > 200 && cubeFovDiff > cubeControlDiff * 8)) {
      throw new Error(
        `キューブの見え方がFOV30→90でほとんど変わらない(差分 ${cubeFovDiff}バイト。` +
          `同一FOVの制御群の差分は ${cubeControlDiff}バイト)`,
      )
    }
    ok(`ビューキューブ: FOV30↔90でPNG差分 ${cubeFovDiff}バイト(同一FOV制御群 ${cubeControlDiff}バイト)`)

    // (7) **2値のUIそのもの。** `#settings-fov`(スライダー)は要素ごと消えている。
    //     `#settings-ortho` を入れると `fov===0`、外すと常に `FOV_DEFAULT`(30) に戻る
    //     ——「戻る角度を覚える仕組み」を消した設計なので、内部APIで選べない値(70)を
    //     入れてあっても戻り先は変わらない。
    await setFovTo(70)
    await setSettingsOpen(true)
    const sliderGone = await page.$('#settings-fov')
    if (sliderGone !== null) throw new Error('スライダー(#settings-fov)がまだ存在する')
    const orthoBefore = await page.$eval('#settings-ortho', (element) => element.checked)
    if (orthoBefore) throw new Error('画角70(透視)なのに正投影チェックボックスが入っている')
    await page.click('#settings-ortho')
    await page.waitForFunction(() => window.__duocad.fov === 0, { timeout: 30000, polling: 20 })
    await page.click('#settings-ortho')
    await page.waitForFunction(() => window.__duocad.fov === 30, { timeout: 30000, polling: 20 })
    await setSettingsOpen(false)
    ok('画角UI: スライダーは存在せず、チェックボックスの入切だけで 0 ↔ 30 が切り替わる')

    await setFovTo(45)
    await frameAll(page)

    ok(
      `画角: frameAll の広がり(共通の${commonKeys.length}面) 45度=${framed[45].spread}px / 30度=${framed[30].spread}px(${framed[30].ratio}倍)` +
        ` / 90度=${framed[90].spread}px(${framed[90].ratio}倍) / 正投影=${framed[0].spread}px(${framed[0].ratio}倍)。` +
        ` 正投影の手前÷奥=${orthoProbe.ratio.toFixed(4)}(透視45度なら ${perspProbe.ratio.toFixed(2)})・` +
        `切り替えでの見かけ ${orthoKeptSize.toFixed(3)}倍・` +
        `ホイールで ${(zoomedIn.at / orthoProbe.at).toFixed(2)}倍に寄って frameAll で ${refitRatio.toFixed(3)}倍へ戻る。` +
        ` 45→60: 注視点の見かけ ${keptAcrossFov.toFixed(4)}倍(補正無しなら 0.717倍)。` +
        `45→90では面が最大 ${wideMaxShift.toFixed(1)}px 動く`,
    )
  }

  // ================================================================ 座標系: Z-up 右手系
  //
  // **上下面の比較に `projectFace` は使えない。** あれは「その画素が本当にその面を取れる」ことまで
  // 確かめて返すので、上から見えている限り下面は常に null になり、比べる相手が存在しない。
  // 遮蔽と無関係な投影そのものを問う `projectPoint` を使う。
  //
  // **オービットの極は画面の見た目からは分からない。** `OrbitControls` はコンストラクタで生成時の
  // `object.up` を `_quat` に焼き込み `update()` で再計算しないので、up を後から書き換えた実装では
  // 画面の上方向だけが正しく、回転の極は+Yのままになる。判定は「水平ドラッグしても注視点からの高さ
  // (`position.z - target.z`)が変わらない」＋「回転軸上の点の画面位置が動かない」で見る。
  {
    await setSettingsOpen(false)
    await frameAll(page)
    const pose = await settledPose()

    // (1) 上方向そのもの(完了条件1)。
    const up = pose.up
    if (Math.abs(up[0]) > 1e-6 || Math.abs(up[1]) > 1e-6 || Math.abs(up[2] - 1) > 1e-6) {
      throw new Error(`camera.up が [${up.map((v) => v.toFixed(3)).join(', ')}](+Zでない)`)
    }

    // (2) 初期視点が front-top-right(完了条件4)。**X と Y に等量、Zは控えめ**の
    //     (d, -d, 0.6d) —— `+Y` から見ると背面になるので Y は負である。
    const offset = pose.position.map((value, i) => value - pose.target[i])
    const home = { x: offset[0], y: offset[1], z: offset[2] }
    if (!(home.x > 0 && home.y < 0 && home.z > 0)) {
      throw new Error(
        `初期視点が front-top-right でない(注視点からのオフセット ` +
          `${offset.map((v) => v.toFixed(1)).join(', ')}。+X / −Y / +Z の側から見るはず)`,
      )
    }
    const xyRatio = Math.abs(home.y) / home.x
    const zRatio = home.z / home.x
    if (Math.abs(xyRatio - 1) > 0.05 || Math.abs(zRatio - 0.6) > 0.05) {
      throw new Error(
        `初期視点の比が (1, -1, 0.6) でない(|y|/x=${xyRatio.toFixed(3)} z/x=${zRatio.toFixed(3)})`,
      )
    }

    // (3) **+Z が画面で上**(完了条件2)。まず注視点とその真上40mmの点で見る。
    const axisPoint = [pose.target[0], pose.target[1], pose.target[2] + 40]
    const screen = await page.evaluate(
      (below, above) => ({
        below: window.__duocad.projectPoint(below),
        above: window.__duocad.projectPoint(above),
      }),
      pose.target,
      axisPoint,
    )
    if (screen.below === null || screen.above === null) {
      throw new Error('注視点とその真上40mmの点が画面に射影できない')
    }
    const axisRise = screen.below.y - screen.above.y // 画面yは下向きなので、上なら正
    if (axisRise < 10) {
      throw new Error(
        `+Zへ40mm動かしても画面で ${axisRise.toFixed(1)} 画素しか上がらない(画面の上方向が+Zでない)`,
      )
    }

    // (4) **押し出した部品の上面と下面**(完了条件2の本番)。法線 +Z / −Z の面の重心を
    //     射影して、上面のほうが画面で上(y が小さい)ことを見る。
    const plate = await page.evaluate(() => {
      const hook = window.__duocad
      for (const part of hook.parts) {
        const geometry = hook.getGeometry(part.body_id)
        if (geometry === null) continue
        const top = geometry.faces.find((face) => face.normal[2] > 0.9)
        const bottom = geometry.faces.find((face) => face.normal[2] < -0.9)
        if (top === undefined || bottom === undefined) continue
        return {
          bodyId: part.body_id,
          top: { index: top.index, at: hook.projectPoint(top.centroid), z: top.centroid[2] },
          bottom: {
            index: bottom.index,
            at: hook.projectPoint(bottom.centroid),
            z: bottom.centroid[2],
          },
        }
      }
      return null
    })
    if (plate === null) throw new Error('法線が +Z と −Z の面を両方持つボディが1つも無い')
    if (plate.top.at === null || plate.bottom.at === null) {
      throw new Error('上面・下面の重心が画面に射影できない')
    }
    if (!(plate.top.z > plate.bottom.z)) {
      throw new Error(
        `上面の重心のZ(${plate.top.z})が下面(${plate.bottom.z})より上でない(前提が崩れている)`,
      )
    }
    const faceRise = plate.bottom.at.y - plate.top.at.y
    if (faceRise <= 0) {
      throw new Error(
        `上面(面${plate.top.index})が画面で下面(面${plate.bottom.index})より下にある` +
          `(y ${plate.top.at.y.toFixed(1)} vs ${plate.bottom.at.y.toFixed(1)})`,
      )
    }

    // (5) **オービットの極が +Z**(完了条件3)。ビューポートを水平にドラッグして、
    //     注視点からの高さと、回転軸上の点の画面位置が動かないことを見る。
    const rect = await page.evaluate(() => {
      const box = document.querySelector('.viewport').getBoundingClientRect()
      return { left: box.left, top: box.top, width: box.width, height: box.height }
    })
    const midY = rect.top + rect.height / 2
    const centerX = rect.left + rect.width / 2
    const swing = Math.min(220, rect.width / 3)
    // **右ボタンで回す**(左ドラッグはウィンドウ選択、中ドラッグはパン、回転は右)。
    // 始点が浮いているブラウザの下に入らないよう、畳んでから引く(ブラウザの高さは行数で変わる)。
    await withoutBrowserPanel(page, async () => {
      await page.mouse.move(centerX - swing, midY)
      await page.mouse.down({ button: 'right' })
      for (let i = 1; i <= 10; i += 1) {
        await page.mouse.move(centerX - swing + (2 * swing * i) / 10, midY)
      }
      await page.mouse.up({ button: 'right' })
    })
    const turned = await settledPose()
    const turnedOffset = turned.position.map((value, i) => value - turned.target[i])
    const azimuthBefore = Math.atan2(offset[1], offset[0])
    const azimuthAfter = Math.atan2(turnedOffset[1], turnedOffset[0])
    let azimuthDelta = Math.abs(azimuthAfter - azimuthBefore)
    if (azimuthDelta > Math.PI) azimuthDelta = 2 * Math.PI - azimuthDelta
    const radius = Math.hypot(...offset)
    if (azimuthDelta < 0.5) {
      throw new Error(
        `水平に${(2 * swing).toFixed(0)}画素**右ドラッグ**しても方位が ${azimuthDelta.toFixed(3)} rad しか回らない(オービットが効いていない)`,
      )
    }
    const heightDrift = Math.abs(turnedOffset[2] - offset[2])
    if (heightDrift > 0.01 * radius) {
      throw new Error(
        `水平ドラッグで注視点からの高さが ${offset[2].toFixed(1)} → ${turnedOffset[2].toFixed(1)} へ動いた` +
          `(${heightDrift.toFixed(2)}mm。回転の極が+Zでない ——` +
          ` OrbitControls が生成時の up を焼き込んでいる)`,
      )
    }
    const axisAfter = await page.evaluate(
      (point) => window.__duocad.projectPoint(point),
      axisPoint,
    )
    if (axisAfter === null) throw new Error('回転軸上の点が回した後に射影できない')
    const axisShift = Math.hypot(axisAfter.x - screen.above.x, axisAfter.y - screen.above.y)
    if (axisShift > 3) {
      throw new Error(
        `水平ドラッグで回転軸(注視点の真上)の点が ${axisShift.toFixed(1)} 画素動いた(その軸で回っていない)`,
      )
    }

    // (6) **スケッチに入って出ても Y-up へ戻らない**。まず最初の退出(復帰の走らない経路。
    //     冒頭の `upAfterFirstExit`)、続いて2回目以降の出入り(控えた up の復元が走る経路)。
    const firstUp = upAfterFirstExit
    if (
      Math.abs(firstUp[0]) > 1e-6 ||
      Math.abs(firstUp[1]) > 1e-6 ||
      Math.abs(firstUp[2] - 1) > 1e-6
    ) {
      throw new Error(
        `最初にスケッチを抜けた直後の camera.up が [${firstUp.map((v) => v.toFixed(3)).join(', ')}]` +
          `(既定へ戻す処理が Y-up を直書きしている)`,
      )
    }
    await page.click(`[data-sketch-id="${sketchId}"] .sketch-name`)
    await waitFor((id) => window.__duocad.activeSketch === id, 'スケッチへ再入場できない', sketchId)
    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'スケッチを抜けられない')
    const afterSketch = await settledPose()
    const backUp = afterSketch.up
    if (Math.abs(backUp[0]) > 1e-6 || Math.abs(backUp[1]) > 1e-6 || Math.abs(backUp[2] - 1) > 1e-6) {
      throw new Error(
        `スケッチを1往復したら camera.up が [${backUp.map((v) => v.toFixed(3)).join(', ')}] になった(Y-upへ戻っている)`,
      )
    }

    ok(
      `座標系: up=[${up.join(', ')}] / 視点は front-top-right` +
        `(${offset.map((v) => v.toFixed(0)).join(', ')}。|y|/x=${xyRatio.toFixed(2)} z/x=${zRatio.toFixed(2)}) /` +
        ` +Zへ40mmで画面 ${axisRise.toFixed(0)}px 上がる /` +
        ` 上面(面${plate.top.index})は下面(面${plate.bottom.index})より ${faceRise.toFixed(0)}px 上 /` +
        ` 水平ドラッグで方位 ${azimuthDelta.toFixed(2)}rad 回しても高さのずれ ${heightDrift.toFixed(3)}mm・` +
        `軸上の点 ${axisShift.toFixed(1)}px / スケッチは最初の退出も1往復の後も up は +Z`,
    )
  }

  await assertNoAppErrors(page, consoleErrors)
})
