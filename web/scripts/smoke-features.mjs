/**
 * 実ブラウザでのフィーチャ検査 — **手だけで形が作れることの機械検査**。
 *
 * `smoke.mjs` は27部品のSTEPを読んだ文書で「既にあるものを触る」側を見る。こちらは
 * **空の文書**から始めて、**人が手でやる順序そのもの**を本物のクリックと
 * キー入力で通す:
 *
 *   板を描く → 領域を押し出す → 辺を選んで丸める → 反復
 *
 * 分けてあるのは、`join` の押し出しが「対象が無ければ新しいソリッドを作る」verbだから
 * (27部品が居る文書では既存の部品へ融合してしまい、**最初の板が生まれる**経路が通らない)。
 *
 *   web> node scripts/smoke-features.mjs
 *
 * 見るのは画面の結線だけ(形の契約 —— 体積・切り取り・面取りの円錐・新規ボディ・undo・選択の
 * リセット —— は `tests/test_extrude.py` / `test_fillet.py` / `test_chamfer.py` / `test_pattern*.py`、
 * 幽霊と確定結果の一致は `tests/test_ghost_agreement.py` が持つ):
 *
 *   1. スケッチ → 領域をクリック → 押し出しで、最初のソリッドが手だけで生まれる
 *   2. 辺は**要求している間だけ**指せる。複数選んでフィレットし、**失敗する半径では理由が札に出て、
 *      選択と打った値が保持されたまま戻る**
 *   3. 反復: 図形を3Dのクリックで選び、確定前の幽霊が出て Esc で消える。円形反復は中心を点で指す
 */
import {
  assertNoAppErrors,
  openApp,
  pressUndo,
  typeInto as typeIntoField,
  waitOrFail,
} from './browser.mjs'
import { fail, ok } from './args.mjs'
import { runSmoke } from './harness.mjs'
import {
  clickEdge as clickEdgeOf,
  clickEntityHandle,
  clickRegion as clickRegionOf,
  clickSketchPoint,
  createSketchOnBasePlane,
} from './pick.mjs'

const PLATE = { width: 40, height: 30, thickness: 10 }

// `--url` を渡したときだけ既に立っているサーバへ繋ぐ(既定は自分で立てる)。
runSmoke('smoke-features', { attach: true }, async ({ browser, url, timeout }) => {
  {
    const { page, consoleErrors } = await openApp(browser, url, { timeout })
    /** わざと出させたエラーの札(受け取った文字列そのもの。最後の「エラーは出ていない」から除く)。 */
    const expectedErrors = []
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
          const active = hook.sketch
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
            // **今まさに画面に出ているエラーの札**(`Toast.svelte`。8秒で消える)。
            // 文言の正はこのDOMであって、控えの `errors` ではない。
            toasts: Array.from(document.querySelectorAll('#toasts .toast-text')).map((node) =>
              node.textContent.trim(),
            ),
            // 消えた後も残る控え(`Toast.svelte` の `errorLog`)。走行中に出たエラーの履歴。
            errors: hook.errors,
          })
        }),
      )

    const waitFor = (predicate, what, ...args) =>
      waitOrFail(page, predicate, what, { args, timeout, dump: look })

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

    // 指す手は `pick.mjs`(射影は押す直前・パネルは畳む)。この本の呼び方に合わせた薄い皮。
    const typeInto = (selector, value) => typeIntoField(page, selector, value)
    const createSketchOnBase = (name = 'XY') => createSketchOnBasePlane(page, name, { timeout })
    const clickPlane = (u, v) => clickSketchPoint(page, u, v)
    const clickRegion = (index) => clickRegionOf(page, index)
    /** 辺を本物のクリックで選ぶ(`shift` で追加)。外れたら false(呼び出し側が次の辺を試す)。 */
    const clickEdge = (bodyId, edgeIndex, shift = false) =>
      clickEdgeOf(page, bodyId, edgeIndex, { modifier: shift ? 'Shift' : null })

    // ================================================================ 検証1: 板と穴
    await createSketchOnBase('XY')
    await waitFor(() => window.__duocad.activeSketch !== null, 'XY平面のスケッチに入れない')

    await page.click('#sketch-rectangle')
    await clickPlane(0, 0)
    await typeInto('#sketch-width', PLATE.width)
    await typeInto('#sketch-height', PLATE.height)
    await page.keyboard.press('Enter')
    await waitFor(
      () => window.__duocad.sketch?.regions.length === 1,
      '長方形を描いても領域が1つにならない',
    )
    ok(`長方形を手で描いた(${PLATE.width}×${PLATE.height})`)

    // 「選ぶ」へ戻して領域をクリック(描くコマンドが起動している間は点が優先される)
    await page.keyboard.press('Escape')
    await page.click('#sketch-look')
    await clickRegion(1)
    await waitFor(
      // **領域だけ**(`sketch_entity` も `sketch` を持つ。`refKind` 相当の判定)
      () =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => 'sketch' in item && !('entity' in item),
        ),
      '領域を選べない',
    )
    ok('領域を本物のクリックで選んだ')

    // 領域押し出し(結合)。**空の文書なので、ここで最初のソリッドが生まれる**
    await page.click('#extrude')
    await typeInto('#distance', PLATE.thickness)
    await page.click('#extrude-mode-join')
    await page.keyboard.press('Enter')
    await waitFor(() => window.__duocad.parts.length === 1, '領域押し出しでソリッドができない')
    const plate = (await look()).bodies[0]
    ok('領域を押し出して板ができた')

    await page.click('#sketch-exit')
    await waitFor(() => window.__duocad.activeSketch === null, 'スケッチモードを抜けられない')

    // ================================================================ 検証2: フィレット
    const bodyId = plate.id

    // ---------------------------------------------------------- 無起動では辺が指せない
    // **無起動では辺を要求しない**ことをここで固定する —— この下の「フィレットを起動」
    // でしか辺を要求しないはずで、`onWants` が退行して常時辺を要求するようになると、
    // この時点で辺のジオメトリがサーバから届いてしまう。
    const idleEdges = await look()
    if (idleEdges.wantsEdges) {
      fail('起動していないのに辺を要求している(既定のコマンドは「押し出す」=面)')
    }
    if (idleEdges.edges.length !== 0) {
      fail(`要求していないのに辺のジオメトリが届いている: ${idleEdges.edges.length}本`)
    }
    // **実際にクリックでも選べない**ことも見る(旗だけでなく、本物のクリック経路を通す)。
    // 添字は総当たりで構わない —— 揃っていなければ `projectEdge` がその場でnullを返すので
    // `clickEdge` はクリックそのものを起こさない。
    let idleEdgePicked = false
    for (let index = 1; index <= 40; index += 1) {
      if (await clickEdge(bodyId, index)) {
        idleEdgePicked = true
        break
      }
    }
    if (idleEdgePicked) fail('起動していないのに辺が本物のクリックで選べてしまった')
    ok('**要求していない間は指せない**: 通常モードで辺へ当たる画素が1つも無く、クリックでも選べない')

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

    // Escで一度降りる(選択の正はコア: 空になる配信を待ってから次へ進む)
    await page.keyboard.press('Escape')
    await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, 'Escで選択が空にならない')

    // -------------------------------------------------------------- 失敗する半径
    // §7: OCCTのフィレットは例外を投げずに `IsDone()=False` で失敗し、半径に対して単調でない。
    // **握りつぶさず理由を出し、選択を保持したまま戻す**ことがタスクDの要件。
    await page.click('#fillet')
    await waitForEdges('フィレットを起動し直しても辺が届かない')
    // 起動中は**素のクリックで足せる**(Escで空になったので2本とも素のクリック)。
    for (const index of picked) {
      if (!(await clickEdge(bodyId, index))) fail(`起動中の素のクリックで辺 ${index} を選べない`)
    }
    await typeInto('#fillet-radius', 1000)
    await page.keyboard.press('Enter')
    // **理由はトーストで出る**(ステータスバーの1行では拒否に気づけない)。
    // ここは控えの `errors` ではなく**実際に描かれた札**を見る —— 人間が見るのはDOMの方で、
    // 控えだけ見ていると「記録はされたが画面には出ていない」を緑にしてしまう。
    await waitFor(
      () =>
        Array.from(document.querySelectorAll('#toasts .toast-text')).some((node) =>
          node.textContent.includes('fillet'),
        ),
      '失敗する半径なのに理由がトーストに出ない',
    )
    const afterFailure = await look()
    expectedErrors.push(afterFailure.errors[0])
    // **エラーがステータスバーへ二重に出ていない**(トースト一本化の実体)。
    // 両方に出すと「気づける場所」がまた2つになり、片方を直し忘れる形へ戻る。
    const strayed = (afterFailure.messages ?? []).filter((message) => message.startsWith('エラー['))
    if (strayed.length > 0) fail(`エラーがステータスバーにも出ている: ${strayed.join(' | ')}`)
    if (afterFailure.selection.count !== 2) {
      fail(`失敗で選択が消えた: ${afterFailure.selection.count}(24本選び直しになる)`)
    }
    const keptRadius = await page.$eval('#fillet-radius', (element) => element.value)
    if (keptRadius !== '1000') fail(`失敗後に打った値が戻っていない: "${keptRadius}"`)
    ok(
      `**失敗を握りつぶさない**: 理由がトーストに出て(${(afterFailure.toasts[0] ?? '').slice(0, 48)}…)、` +
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

    // ================================================================ 検証3: 反復
    await page.keyboard.press('Escape')
    await createSketchOnBase('XY')
    await waitFor(() => window.__duocad.activeSketch !== null, '反復用のスケッチに入れない')
    await page.click('#sketch-circle')
    await clickPlane(0, 0)
    await typeInto('#sketch-radius', 2)
    await page.keyboard.press('Enter')
    await waitFor(
      () => window.__duocad.sketch?.entities.length === 1,
      '反復する図形が置けない',
    )
    await page.keyboard.press('Escape')

    // 図形を**3Dのクリックで**選ぶ。反復の `entities` 引数は共有選択集合の
    // `sketch_entity` から導かれる。
    const entityId = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketch
      return active.entities[0].entity_id
    })
    /** 図形が反復の対象に入っている状態にする(既に入っていれば何もしない)。 */
    async function ensureEntityPicked(id) {
      const already = await page.evaluate(
        (target) => (window.__duocad.selectedEntities ?? []).includes(target),
        id,
      )
      if (!already) await clickEntityHandle(page, id)
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

    // ---------------------------------------------------------------- 確定前の幽霊
    // **`preview: 'local'`** —— 打っている値のとおりの複製が、確定する前に
    // 半透明で見えている。数えるのは「増える分」= nx*ny - 1(元の位置には本物が居る)。
    await waitFor(
      () => window.__duocad.ghostInfo().sketch.length === 5,
      '矩形反復の幽霊が 3×2-1 = 5 個にならない',
    )
    // **Escで消えて、文書は一切変わらない**(ローカル方式の契約。サーバへ何も送っていない)
    await page.keyboard.press('Escape')
    await waitFor(
      () => window.__duocad.ghostInfo().sketch.length === 0,
      'Escで矩形反復の幽霊が消えない',
    )
    const afterEscape = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketch
      return active.drawn.length
    })
    if (afterEscape !== 1) fail(`Escで中断したのに図形が増えている: ${afterEscape}`)
    ok('矩形反復: 確定前に複製5個が幽霊で見え、Escで消えて文書は変わらない')

    // 打ち直して今度は確定する。
    // Escで選択は空になる。その配信が届いてから選び直す(届く前に「選択済み」と読むと選び直しを飛ばす)。
    await waitFor(() => (window.__duocad.selection?.items ?? []).length === 0, 'Escで選択が空にならない(矩形反復)')
    await ensureEntityPicked(entityId)
    await page.click('#pattern-rectangular')
    await typeInto('#pattern-dx', 10)
    await typeInto('#pattern-dy', 8)
    await typeInto('#pattern-nx', 3)
    await typeInto('#pattern-ny', 2)
    await waitFor(
      () => window.__duocad.ghostInfo().sketch.length === 5,
      '打ち直した矩形反復の幽霊が出ない',
    )
    await page.keyboard.press('Enter')
    await waitFor(
      () => window.__duocad.sketch?.drawn.length === 6,
      '矩形反復が 3×2 = 6 にならない',
    )
    ok('矩形反復: 3×2 で図形が6個になった(元を含めた個数)')

    await pressUndo(page)
    await waitFor(
      () => window.__duocad.sketch?.drawn.length === 1,
      'undoで反復が戻らない',
    )

    // 円形反復は中心をスケッチ平面の点で指す。矩形反復は戻してある(同じ図形は2回反復できない)。
    await ensureEntityPicked(entityId)
    await page.click('#pattern-circular')
    await clickPlane(20, 0) // 回転中心(図形の中心と重ならない位置)
    await typeInto('#pattern-count', 8)
    await typeInto('#pattern-angle', 360)
    // 確定前の幽霊は**増える分 = count - 1 = 7 個**。
    await waitFor(
      () => window.__duocad.ghostInfo().sketch.length === 7,
      '円形反復の幽霊が 8-1 = 7 個にならない',
    )
    await page.keyboard.press('Enter')
    await waitFor(
      () => window.__duocad.sketch?.drawn.length === 8,
      '円形反復が8個にならない',
    )
    ok('円形反復: 中心を点で指し、幽霊7個が出て、確定で8個になった')

    // わざと失敗させた札(半径1000のフィレット)だけは出ていてよい。
    await assertNoAppErrors(page, consoleErrors, { allow: expectedErrors })
  }
})
