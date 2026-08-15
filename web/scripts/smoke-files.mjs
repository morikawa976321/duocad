/**
 * 実ブラウザでのパネルとファイル操作の検査(タスクE・2026-08-14)。
 *
 * `smoke-features.mjs` と同じく**空の文書**から始めて、人が手でやる順序を
 * 本物のクリックとキー入力で通す。見るのはタスクEの検証1〜5:
 *
 *   1. **穴の径を1箇所編集して、反復で作った全部の穴が追随する**(M3の存在理由)
 *   2. **保存 → 再読込 → 開く → 同じ形**(操作ログから再生される)
 *   3. STEPを読み込む(その時点で編集可能なボディができる)。面を選んで編集可能なコピーも作れる
 *   4. STEP/STL を書き出し、**STEPは読み戻して体積が一致**(読み戻しは pytest 側)
 *   5. 履歴パネルでundo/redoの位置が分かり、実際に戻せる
 *
 *   web> node scripts/smoke-files.mjs [--workdir <書き込めるディレクトリ>]
 *
 * **サーバはこのスクリプトが自分で立てる**(2026-08-15。`python -m duocad.server` の廃止に伴う)。
 * 既に立っているものへ繋ぐときだけ `--url` を渡す。
 *
 * **サーバは毎回起動し直す**(文書は接続より長生きするので、前回の形が残っていると
 * 「最初のソリッドが生まれる」経路を通れない)。
 */
import { existsSync, mkdtempSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { launch } from 'puppeteer-core'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
// `--url` を渡したときだけ既に立っているサーバへ繋ぐ(既定は自分で立てる)。
const attachUrl = options.url ?? null
const wsPort = options.port ?? '8795'
const httpPort = options['http-port'] ?? '8796'
const timeout = Number.parseInt(options.timeout ?? '120000', 10)
const workdir = options.workdir ?? mkdtempSync(join(tmpdir(), 'duocad-e-'))

// **Chromeを先に見る**(2026-08-15。理由は `smoke.mjs` の同じ箇所)。
const CANDIDATE_BROWSERS = [
  process.env.DUOCAD_BROWSER,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
].filter(Boolean)

/** 板と穴の寸法(mm)。体積の検算に使うので丸い数字。 */
const PLATE = { width: 60, height: 20, thickness: 5 }
const HOLE = { u: 10, v: 10, radius: 2, dx: 15, count: 3 }

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
  console.log(`smoke-files: ${executablePath}`)
  console.log(`  workdir: ${workdir}`)

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
      '--window-size=1280,900',
    ],
  })

  try {
    const page = await browser.newPage()
    await page.setViewport({ width: 1280, height: 900 })
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
    if ((await page.evaluate(() => window.__duocad.parts.length)) !== 0) {
      fail('空の文書で起動していない(サーバを起動し直す)')
    }
    ok('空の文書で起動した')

    // ------------------------------------------------------------------ 道具
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
              kind: body.kind,
              visible: body.visible,
            })),
            activeSketch: hook.activeSketch,
            entities: active?.entities?.length ?? 0,
            drawn: active?.drawn?.length ?? 0,
            regions: active?.regions?.length ?? 0,
            parameters: hook.parameters,
            redoCount: hook.redoCount,
            myOperations: hook.myOperations,
            lastFile: hook.lastFile,
            log: hook.state.log.length,
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

    async function typeInto(selector, value) {
      await page.click(selector)
      await page.keyboard.down('Control')
      await page.keyboard.press('KeyA')
      await page.keyboard.up('Control')
      await page.keyboard.press('Backspace')
      await page.type(selector, String(value), { delay: 15 })
    }

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

    /** 領域を全部選ぶ(反復した穴をまとめて切り取るため)。 */
    async function selectAllRegions(count) {
      await clickRegion(1)
      for (let index = 2; index <= count; index += 1) {
        const at = await page.evaluate((idx) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const position = window.__duocad.projectRegion(idx)
          return position ? { x: position.x + rect.left, y: position.y + rect.top } : null
        }, index)
        if (!at) fail(`領域 ${index} が画面の中に無い`)
        await page.keyboard.down('Shift')
        await page.mouse.click(at.x, at.y)
        await page.keyboard.up('Shift')
      }
      await waitFor(
        (expected) =>
          (window.__duocad.selection?.items ?? []).filter((item) => 'sketch' in item).length ===
          expected,
        `領域を${count}個選べない`,
        count,
      )
    }

    // ================================================================ 板を作る
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
      '板の領域ができない',
    )
    await page.keyboard.press('Escape')
    await page.click('#sketch-look')
    await clickRegion(1)
    await page.click('#extrude-regions')
    await typeInto('#regions-distance', PLATE.thickness)
    await page.click('#extrude-mode-join')
    await page.keyboard.press('Enter')
    await waitFor(() => window.__duocad.parts.length === 1, '板ができない')
    await page.click('#sketch-exit')
    await page.click('#view-fit')
    const plateVolume = (await look()).bodies[0].volume
    ok(`板を作った(体積 ${plateVolume})`)

    // ================================================================ 検証1: 追随
    // 穴を1つ描いて矩形反復で3つにし、まとめて切り取ってから**径を1箇所だけ**打ち直す。
    await page.click('#sketch-new-XY')
    await waitFor(() => window.__duocad.sketches.length === 2, '穴のスケッチに入れない')
    await page.click('#sketch-circle')
    await clickPlane(HOLE.u, HOLE.v)
    await typeInto('#sketch-radius', HOLE.radius)
    await page.keyboard.press('Enter')
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.entities.length === 1)
      },
      '穴の図形が置けない',
    )
    await page.keyboard.press('Escape')

    const entityId = await page.evaluate(() => {
      const hook = window.__duocad
      const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
      return active.entities[0].entity_id
    })
    await page.click(`[id="entity-${entityId}"]`)
    await waitFor(
      (id) => (window.__duocad.selectedEntities ?? []).includes(id),
      '図形を反復の対象に選べない',
      entityId,
    )
    await page.click('#pattern-rectangular')
    await typeInto('#pattern-dx', HOLE.dx)
    await typeInto('#pattern-dy', 0)
    await typeInto('#pattern-nx', HOLE.count)
    await typeInto('#pattern-ny', 1)
    await page.keyboard.press('Enter')
    await waitFor(
      (expected) => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active && active.regions.length === expected)
      },
      '反復した穴の領域が3つにならない',
      HOLE.count,
    )
    await page.click('#sketch-look')
    await selectAllRegions(HOLE.count)
    await page.click('#extrude-regions')
    await typeInto('#regions-distance', PLATE.thickness)
    await page.click('#extrude-mode-cut')
    await page.keyboard.press('Enter')
    const holesVolume = Math.PI * HOLE.radius ** 2 * PLATE.thickness * HOLE.count
    await waitFor(
      (before, expected) => {
        const body = window.__duocad.parts[0]
        return Boolean(body && Math.abs(before - body.volume - expected) < expected * 1e-3)
      },
      '3つの穴が開かない',
      plateVolume,
      holesVolume,
    )
    await page.click('#sketch-exit')
    await page.click('#view-fit')
    const withHoles = (await look()).bodies[0]
    ok(`反復した穴を3つまとめて開けた(体積 ${plateVolume} -> ${withHoles.volume.toFixed(2)})`)

    // **パラメータパネルで径を1箇所だけ打ち直す**
    const radiusName = await page.evaluate((radius) => {
      const entries = Object.entries(window.__duocad.parameters)
      const hit = entries.find(([, value]) => Math.abs(value - radius) < 1e-9)
      return hit ? hit[0] : null
    }, HOLE.radius)
    if (!radiusName) fail(`半径 ${HOLE.radius} の名前つきの値がパネルに出ていない`)
    await typeInto(`[id="parameter-${radiusName}"]`, 3)
    await page.keyboard.press('Enter')
    const biggerHoles = Math.PI * 3 ** 2 * PLATE.thickness * HOLE.count
    await waitFor(
      (before, expected) => {
        const body = window.__duocad.parts[0]
        return Boolean(body && Math.abs(before - body.volume - expected) < expected * 1e-3)
      },
      '径を打ち直しても穴が太くならない(追随していない)',
      plateVolume,
      biggerHoles,
    )
    const afterResize = (await look()).bodies[0]
    ok(
      `**径を1箇所だけ打ち直したら、反復した穴が全部追随した**(半径 ${HOLE.radius}→3 で` +
        ` 体積 ${withHoles.volume.toFixed(2)} -> ${afterResize.volume.toFixed(2)})`,
    )

    // ================================================================ 検証5: 履歴の位置
    const beforeUndo = await look()
    await page.click('#undo')
    await waitFor(
      (expected) => window.__duocad.redoCount === expected,
      'undoしてもやり直せる件数が増えない',
      beforeUndo.redoCount + 1,
    )
    const undone = await look()
    if (undone.log !== beforeUndo.log - 1) {
      fail(`undoで履歴の有効件数が減っていない: ${beforeUndo.log} -> ${undone.log}`)
    }
    const position = await page.$eval('#history-position', (element) => element.textContent.trim())
    if (!position.includes('やり直せる')) fail(`履歴パネルに位置が出ていない: "${position}"`)
    ok(`履歴パネルに位置が出た("${position}")`)

    // **溢れた行までスクロールで到達できる**(QA指摘 2026-08-14)。`flex-direction: column-reverse`
    // で新しい順に並べると、溢れた分は `scrollHeight === clientHeight` のままスクロールできず、
    // 「一覧で見せる」が最新数行のリストに縮退する(実測で踏んだ)。**最古の行が読めること**を見る。
    const history = await page.evaluate(() => {
      const list = document.querySelector('#history-list')
      const rows = [...list.querySelectorAll('.item')]
      const oldest = rows[rows.length - 1]
      list.scrollTop = list.scrollHeight
      const listBox = list.getBoundingClientRect()
      const rowBox = oldest.getBoundingClientRect()
      return {
        rows: rows.length,
        scrollable: list.scrollHeight > list.clientHeight,
        oldestSeq: oldest.dataset.seq,
        oldestVisible: rowBox.top >= listBox.top - 1 && rowBox.bottom <= listBox.bottom + 1,
      }
    })
    if (!history.oldestVisible) {
      fail(`履歴の最古の行(${history.oldestSeq})までスクロールで到達できない: ${JSON.stringify(history)}`)
    }
    ok(
      `履歴 ${history.rows} 行のうち最古(seq ${history.oldestSeq})までスクロールで到達できた` +
        `(溢れている: ${history.scrollable})`,
    )

    await page.click('#redo')
    await waitFor(
      (expected) => window.__duocad.redoCount === expected,
      'redoで位置が戻らない',
      beforeUndo.redoCount,
    )
    const mine = (await look()).myOperations.length
    if (mine === 0) fail('この画面が出した操作が1つも印されていない')
    ok(`redoで戻った。この画面が出した操作 ${mine} 件に「自分」の印が付いている`)

    // ================================================================ 検証2: 保存と再現
    const documentPath = join(workdir, 'model.json')
    await page.click('#file-save')
    await typeInto('#file-path', documentPath)
    await page.keyboard.press('Enter')
    await waitFor(
      () => window.__duocad.lastFile?.action === 'save',
      '保存の応答が返らない',
    )
    const saved = (await look()).lastFile
    if (!existsSync(documentPath)) fail(`保存したはずのファイルが無い: ${documentPath}`)
    ok(`保存した(${saved.path} / ${saved.bytes} B / 操作 ${saved.operations} 件)`)

    const shapeBefore = (await look()).bodies[0]

    // **アプリを再読込してから開く**(タスクEの検証2「再起動 → 開く → 同じ形状」)。
    // 画面の状態は全部捨てられ、形は操作ログの再生だけから戻ることになる。
    await page.reload({ waitUntil: 'load', timeout })
    await page.waitForFunction(() => Boolean(window.__duocad && window.__duocad.ready), {
      timeout,
      polling: 100,
    })

    // **開く前に形を変えておく**。サーバは長寿命で、再読込しても文書はそのまま残る ——
    // 保存した形と同じままで「開く」を押すと、**開くのが失敗しても体積が一致してしまい
    // 検査が素通りする**(2026-08-14に実際に踏んだ: open_document が例外で落ちていたのに
    // このチェックが緑だった)。undoで形を変えてから開き、戻ることを見る。
    await page.click('#undo')
    await waitFor(
      (before) => Math.abs((window.__duocad.parts[0]?.volume ?? 0) - before) > 1e-6,
      'undoしても形が変わらない(開く検査の前提が作れない)',
      shapeBefore.volume,
    )
    ok('開く前に undo で形を変えた(開いて戻ることを見るため)')

    await page.click('#file-open')
    await typeInto('#file-path', documentPath)
    await page.keyboard.press('Enter')
    await waitFor(
      (expectedVolume) => {
        const body = window.__duocad.parts[0]
        return Boolean(body && Math.abs(body.volume - expectedVolume) < 1e-6)
      },
      '開いても同じ形にならない',
      shapeBefore.volume,
    )
    const reopened = (await look()).bodies[0]
    if (reopened.faces !== shapeBefore.faces) {
      fail(`開いた形の面数が違う: ${shapeBefore.faces} -> ${reopened.faces}`)
    }
    ok(
      `**保存 → 再読込 → 開く で同じ形が戻った**(体積 ${reopened.volume.toFixed(4)} / 面 ${reopened.faces}` +
        ' — 操作ログから再生された)',
    )

    // ================================================================ 検証4: 書き出し
    const stepPath = join(workdir, 'out.step')
    await page.click('#file-export-step')
    await typeInto('#file-path', stepPath)
    await page.keyboard.press('Enter')
    await waitFor(
      () => window.__duocad.lastFile?.action === 'export_step',
      'STEP書き出しの応答が返らない',
    )
    if (!existsSync(stepPath) || statSync(stepPath).size === 0) fail('STEPが書けていない')
    ok(`STEPを書き出した(${statSync(stepPath).size} B)`)

    const stlPath = join(workdir, 'out.stl')
    await page.click('#file-export-stl')
    await typeInto('#file-path', stlPath)
    await typeInto('#file-deviation', 0.05)
    await page.keyboard.press('Enter')
    await waitFor(
      () => window.__duocad.lastFile?.action === 'export_stl',
      'STL書き出しの応答が返らない',
    )
    const stl = (await look()).lastFile
    if (stl.deviation !== 0.05) fail(`偏差が渡っていない: ${stl.deviation}`)
    ok(`STLを書き出した(${statSync(stlPath).size} B / 偏差 ${stl.deviation}mm)`)

    // ================================================================ 検証3: 参照部品
    // 読み込んだ時点で編集可能なボディができ、参照ボディは隠れて画面から指せない
    // (隠したBodyは形状が配信されないので `projectFace` が null を返す)。
    const originalVolume = (await look()).bodies[0].volume
    const idsBefore = await page.evaluate(() =>
      window.__duocad.parts.map((body) => body.body_id),
    )
    const partsBefore = idsBefore.length
    await page.click('#file-import-step')
    await typeInto('#file-path', stepPath)
    await page.keyboard.press('Enter')
    await waitFor(
      (previous) => window.__duocad.parts.length === previous + 1,
      'STEPを読み込めない',
      partsBefore,
    )
    const importedId = await page.evaluate((before) => {
      const added = window.__duocad.parts.filter((body) => !before.includes(body.body_id))
      return added[0]?.body_id ?? null
    }, idsBefore)
    if (!importedId) fail('読み込んでできた編集用ボディが特定できない')

    const references = await page.evaluate(() => {
      const refs = window.__duocad.state.bodies.filter((body) => body.kind === 'reference')
      return {
        count: refs.length,
        hidden: refs.filter((body) => body.visible === false).length,
      }
    })
    if (references.count < 1) fail('参照ボディが増えていない')
    if (references.hidden !== references.count) {
      fail(`参照ボディが隠れていない: ${references.count} 個中 非表示 ${references.hidden}`)
    }
    // **UIから出したSTEPを読み戻して体積が一致する**(タスクEの検証4の「UIから出しても同じ」)。
    // 許容差は相対 1e-6 —— §7の訂正どおり `==` では成立しない(再読込で数値積分の順序が変わる)。
    const importedVolume = await page.evaluate(
      (id) => window.__duocad.parts.find((body) => body.body_id === id).volume,
      importedId,
    )
    if (Math.abs(importedVolume - originalVolume) > Math.abs(originalVolume) * 1e-6) {
      fail(`STEP往復で体積が変わった: ${originalVolume} -> ${importedVolume}`)
    }
    ok(
      `STEPを読み込んだ(読み込んだボディはそのまま編集できる)。**読み戻した体積が一致**` +
        `(${originalVolume.toFixed(6)} vs ${importedVolume.toFixed(6)})`,
    )

    // 「編集可能にする」は複製の経路。対象は参照ではなく、読み込んでできた編集用ボディ。
    // **先に他を隠して対象だけ表示に戻す** —— 読み込んだのは自分を書き出したSTEPなので、
    // 元のソリッドと同じ場所に重なっている(「手前を隠すと奥が選べる」= M3.5の可視性)。
    await page.click('#parts-hide-all')
    await waitFor(
      () => window.__duocad.parts.every((body) => !body.visible),
      '編集用ソリッドを隠せない',
    )
    await page.click(`.parts-row[data-body-id="${importedId}"] input.visible-toggle`)
    await waitFor(
      (id) => {
        const body = window.__duocad.parts.find((item) => item.body_id === id)
        if (!body?.visible) return false
        for (let index = 1; index <= 30; index += 1) {
          if (window.__duocad.projectFace(id, index)) return true
        }
        return false
      },
      '読み込んだボディの面が画面から指せない',
      importedId,
    )
    const facePoint = await page.evaluate((id) => {
      const rect = document.querySelector('.viewport').getBoundingClientRect()
      for (let index = 1; index <= 30; index += 1) {
        const position = window.__duocad.projectFace(id, index)
        if (position) return { x: position.x + rect.left, y: position.y + rect.top, index }
      }
      return null
    }, importedId)
    if (!facePoint) fail('読み込んだボディの面が画面から指せない')
    await page.mouse.click(facePoint.x, facePoint.y)
    await waitFor(
      (id) =>
        (window.__duocad.selection?.items ?? []).some(
          (item) => item.body === id && item.kind === 'face',
        ),
      '読み込んだボディの面を選べない',
      importedId,
    )
    const solidsBefore = (await look()).bodies.length
    await page.click('#file-make-editable')
    await page.keyboard.press('Enter')
    await waitFor(
      (expected) => window.__duocad.parts.length === expected,
      '編集可能なコピーができない',
      solidsBefore + 1,
    )
    ok('面を選んで**編集可能なコピー**を作れた(複製の経路)')

    // **画面に出たエラーを見落とさない**。サーバのエラーは通知欄へ出るだけで、
    // ブラウザのコンソールには出ない —— ここを見ないと「失敗しているのに緑」が起きる
    // (2026-08-14に open_document で実際に起きた)。
    const errors = (await look()).messages.filter((message) => message.startsWith('エラー['))
    if (errors.length > 0) fail(`画面にエラーが出ている: ${errors.join(' | ')}`)
    ok('画面の通知にエラーなし')

    if (consoleErrors.length > 0) {
      fail(`ブラウザのコンソールにエラー: ${consoleErrors.slice(0, 3).join(' | ')}`)
    }
    ok('ブラウザのコンソールにエラーなし')

    console.log('')
    console.log('smoke-files: PASS')
  } finally {
    await browser.close()
    if (server !== null) await server.stop()
  }
}

main().catch((error) => {
  console.error(`smoke-files: FAIL ${error.message}`)
  process.exit(1)
})
