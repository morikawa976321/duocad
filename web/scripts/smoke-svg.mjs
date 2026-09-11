/**
 * 実ブラウザでのSVG読み込みの検査。
 *
 * pytestが見られないのは「**画面から本当に通せるか**」だけである。verbの中身・単位・
 * 再生は `tests/test_svg_import.py` が持つので、ここは人の手順をそのまま通す
 * (「開いているスケッチへ取り込む」形で、CADの「アクティブな
 * スケッチに挿入する」に相当する):
 *
 *   1. **スケッチを作って入る**(「スケッチ」→ 基準平面をクリック。ここまでは通常の手順)
 *   2. スケッチモードの「SVG読み込み」を押す → パスを打つ → Enter で
 *      **いま開いているスケッチの図形が増える**(新しいスケッチはできない)
 *   3. **スケッチモードに入ったまま**、増えた領域を指して**そのまま押し出せる**
 *      (Escでモードを抜ける手順は要らない)
 *   4. ベジエの輪郭を押し出しても**側面が割れない**(面数で見る)
 *   5. **パラメータが増えない**(`d1` が並ばない ＝ 取り込みは名前つきの値を作らない)
 *   6. **Ctrl+Z 1回で取り込みごと消える**(ログ1行に収まっている。スケッチ自体は
 *      `sketch_create` が作ったものなので、import_svgのundoでは消えない)
 *
 *   web> node scripts/smoke-svg.mjs [--workdir <書き込めるディレクトリ>]
 *
 * **サーバはこのスクリプトが自分で立てる**(`web/scripts/server.mjs`)。既に立っている
 * ものへ繋ぐときだけ `--url` を渡す。
 */
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { findBrowser, launchBrowser, withoutBrowserPanel, pressUndo } from './browser.mjs'
import { fail, ok, parseArgs } from './args.mjs'
import { startServer } from './server.mjs'

const options = parseArgs(process.argv.slice(2))
const attachUrl = options.url ?? null
const wsPort = options.port ?? '8797'
const httpPort = options['http-port'] ?? '8798'
const timeout = Number.parseInt(options.timeout ?? '120000', 10)
const workdir = options.workdir ?? mkdtempSync(join(tmpdir(), 'duocad-svg-'))

/**
 * 検査に使うSVG。**物理単位を持たせない**(viewBoxだけ)ので 1ユーザー単位 = 1mm になる。
 *
 * 曲線を混ぜてあるのは `bezier` 種別が画面と押し出しまで通ることを見るため。丘の形
 * (3次ベジエ1本 + 閉じる直線1本)は**辺が2本しかない**ので、押し出した立体は
 * 上下2枚 + 側面2枚 = **4面**になる。折れ線化していればここが数十面に増える。
 */
const SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 60 40">
  <rect x="4" y="4" width="24" height="16"/>
  <circle cx="44" cy="12" r="7"/>
  <path d="M 8 34 C 20 22, 40 22, 52 34 Z"/>
</svg>
`

/** 取り込んだ図形の内訳。SVGを変えたらここも変える(数が合わないと下の検査が意味を失う)。 */
const EXPECTED = {
  entities: 4 + 4 + 2, // 矩形=線4 / 円=弧4 / 丘=ベジエ1+線1
  regions: 3,
  hillFaces: 4, // 丘を押し出した立体の面数(**曲線が曲線のままなら4**)
}
const THICKNESS = 5

async function main() {
  const executablePath = findBrowser()
  console.log(`smoke-svg: ${executablePath}`)
  console.log(`  workdir: ${workdir}`)

  const svgPath = join(workdir, 'shapes.svg')
  writeFileSync(svgPath, SVG, 'utf8')

  const server =
    attachUrl === null ? await startServer({ wsPort, httpPort, modelsDir: workdir }) : null
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

    /** 画面の状態。**アクティブなスケッチを見る** —— import_svgはそこへ足す。 */
    const look = async () =>
      JSON.parse(
        await page.evaluate(() => {
          const hook = window.__duocad
          const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch) ?? null
          return JSON.stringify({
            sketches: hook.sketches.length,
            activeSketch: hook.activeSketch,
            sketchId: active?.sketch_id ?? null,
            entities: active?.entities?.length ?? 0,
            kinds: [...new Set((active?.entities ?? []).map((entity) => entity.kind))].sort(),
            drawn: active?.drawn?.length ?? 0,
            regions: (active?.regions ?? []).map((item) => ({
              index: item.index,
              area: item.area,
            })),
            parts: hook.parts.map((body) => ({ volume: body.volume, faces: body.face_count })),
            parameters: Object.keys(hook.parameters ?? {}).length,
            log: hook.state.log.length,
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

    // ============================================================ 1: スケッチを作って入る
    //
    // **座標はクリックの直前に射影し直す**(`projectBasePlane` はその画素から本当にその
    // 平面が取れることを確かめてから返す)。浮いているパネルはレイ検査に見えないので、
    // 指す間だけ畳む(`withoutBrowserPanel`)。
    await page.click('#sketch-create')
    await withoutBrowserPanel(page, async () => {
      const handle = await page.waitForFunction(
        () => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const at = window.__duocad.projectBasePlane('XY')
          return at ? { x: at.x + rect.left, y: at.y + rect.top } : null
        },
        { timeout, polling: 50 },
      )
      const at = await handle.jsonValue()
      await page.mouse.click(at.x, at.y)
    })
    await waitFor(() => window.__duocad.activeSketch !== null, 'XY平面のスケッチに入れない')
    const before = await look()
    if (before.sketches !== 1) fail(`スケッチが1枚できていない: ${JSON.stringify(before)}`)
    ok('スケッチを作って入った')

    // ============================================================ 2: SVG読み込み → パス → Enter
    await page.click('#sketch-import-svg')
    await page.click('#sketch-svg-path')
    await page.type('#sketch-svg-path', svgPath)
    await page.keyboard.press('Enter')

    await waitFor(
      (expected) => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active) && active.entities.length === expected
      },
      `取り込んだ図形が ${EXPECTED.entities} 個にならない`,
      EXPECTED.entities,
    )
    const after = await look()
    ok(`SVGを取り込んだ(図形 ${after.entities} 個 / ${after.kinds.join(', ')})`)

    // **別の新しいスケッチができていないこと** —— ここが意味の変わった点そのもの。
    // 骨抜きの検査にしないため必ず見る(タスク指示)。
    if (after.sketches !== 1) {
      fail(`新しいスケッチができている(取り込みは開いているスケッチへ足すはず): ${JSON.stringify(after)}`)
    }
    if (after.sketchId !== before.sketchId) {
      fail(`アクティブなスケッチのIDが変わった(貼る先が変わった): ${before.sketchId} -> ${after.sketchId}`)
    }
    ok('新しいスケッチはできていない(開いているスケッチへ足した)')

    if (!after.kinds.includes('bezier')) {
      fail(`ベジエが図形として入っていない: ${JSON.stringify(after.kinds)}`)
    }
    if (after.drawn !== after.entities) {
      fail(`描画用の図形が足りない: drawn=${after.drawn} entities=${after.entities}`)
    }
    if (after.regions.length !== EXPECTED.regions) {
      fail(`領域が ${EXPECTED.regions} 個にならない: ${JSON.stringify(after.regions)}`)
    }
    ok(`ベジエを含む ${after.entities} 図形が描かれ、領域が ${after.regions.length} 個できた`)

    // ============================================================ 3-4: スケッチモードのまま押し出せる
    //
    // **Escでモードを抜ける手順は要らない** —— 領域押し出しはスケッチモードのコマンドにも
    // 居る(`App.svelte` の `SKETCH_FEATURE_COMMANDS`)。取り込み → 押し出しの間に
    // モードの出入りが要らないことがここの主張。

    // 丘の領域 = 矩形(24×16)でも円(半径7)でもない残りの1つ。
    const areaOf = (region) => region.area
    const hill = after.regions.find(
      (region) =>
        Math.abs(areaOf(region) - 24 * 16) > 1 && Math.abs(areaOf(region) - Math.PI * 49) > 1,
    )
    if (!hill) fail(`丘の領域が見つからない: ${JSON.stringify(after.regions)}`)

    // **取り込み後に正面から見て寄る**(`#sketch-look` = `lookAtSketch`)。スケッチへ
    // 入った時点(`#sketch-create`直後)ではまだ図形が0個で、その時のフレーミングが
    // 空スケッチ用の既定倍率のまま残っている —— `lookAtSketch`は入るときの1回しか
    // 自動では呼ばれず、取り込みで図形が増えても再フレームされない(コンテンツが
    // 変わるたび呼び直す設計にはなっていない)。既定倍率のままだと丘の領域(直線でも
    // 円弧でもないベジエ境界)の三角形が画面上で数px以下になり、`projectRegion`の
    // 「1三角形につき4点試す」サンプリングが実際にクリックできる画素を1つも
    // 見つけられずに`null`を返し続ける(engine `duocad-region-projection-fails-on-tiny-triangles`)。
    await page.click('#sketch-look')
    await new Promise((resolve) => setTimeout(resolve, 400))

    // **スケッチモード中の領域クリックは常に指せる**(`wantsRegions` のゲートは通常モード
    // 専用。`App.svelte` の `wantsRegions` docstring)。選んでから押し出しを起動する。
    await withoutBrowserPanel(page, async () => {
      const handle = await page.waitForFunction(
        (index) => {
          const rect = document.querySelector('.viewport').getBoundingClientRect()
          const at = window.__duocad.projectRegion(index)
          return at ? { x: at.x + rect.left, y: at.y + rect.top } : null
        },
        { timeout, polling: 50 },
        hill.index,
      )
      const at = await handle.jsonValue()
      await page.mouse.click(at.x, at.y)
    })

    await page.click('#extrude-regions')
    await page.click('#regions-distance')
    await page.type('#regions-distance', String(THICKNESS))
    await page.click('#extrude-mode-join')
    await page.keyboard.press('Enter')

    await waitFor(
      (expected) => {
        const parts = window.__duocad.parts
        return parts.length === 1 && Math.abs(parts[0].volume - expected) < expected * 0.02
      },
      '取り込んだ領域を押し出せない',
      hill.area * THICKNESS,
    )
    const built = (await look()).parts[0]
    ok(`スケッチモードのまま取り込んだ領域を押し出せた(体積 ${built.volume.toFixed(1)} mm^3)`)

    if (built.faces !== EXPECTED.hillFaces) {
      fail(
        `曲線の側面が割れている(面数 ${built.faces}、期待 ${EXPECTED.hillFaces})` +
          ' —— ベジエが折れ線に落ちている',
      )
    }
    ok(`曲線の側面は1枚のまま(面数 ${built.faces})`)

    // ============================================================ 5: パラメータを増やさない
    //
    // 取り込んだ図形の座標は「人が打ち直す寸法」ではないので名前つきの値を作らない。
    // ここが増えるとSketchPanelの図形一覧がロゴ1枚で埋まる(`verbs/import_svg.py`)。
    const withSolid = await look()
    if (withSolid.parameters !== 0) {
      fail(`取り込みが名前つきの値を作っている(${withSolid.parameters}個)`)
    }
    ok('パラメータは1つも増えていない')

    // ============================================================ 6: Ctrl+Z で戻る
    //
    // ログは3行(sketch_create + import_svg + extrude)。押し出しで1回、取り込みで1回
    // 戻れば十分 —— **取り込みは何個図形があっても1回**(スケッチ自体は sketch_create が
    // 作ったものなので、import_svgのundoでは消えない)。
    if (withSolid.log !== 3) {
      fail(`ログが3行(スケッチ作成+取り込み+押し出し)になっていない: ${withSolid.log}行`)
    }
    await pressUndo(page)
    await waitFor(() => window.__duocad.parts.length === 0, 'Ctrl+Zで押し出しが戻らない')
    await pressUndo(page)
    await waitFor(
      () => {
        const hook = window.__duocad
        const active = hook.sketches.find((item) => item.sketch_id === hook.activeSketch)
        return Boolean(active) && active.entities.length === 0
      },
      'Ctrl+Z 1回で取り込みが消えない(ログがverbごとに分かれている)',
    )
    const afterUndo = await look()
    if (afterUndo.sketches !== 1) {
      fail(`取り込みのundoでスケッチ自体まで消えた(sketch_createとは別の操作のはず): ${JSON.stringify(afterUndo)}`)
    }
    ok('Ctrl+Z 2回で押し出しと取り込みが戻った(取り込みはログ1行。スケッチ自体は残る)')

    const errors = (await look()).errors
    if (errors.length > 0) fail(`画面にエラーが出ている: ${JSON.stringify(errors)}`)
    if (consoleErrors.length > 0) fail(`コンソールにエラー: ${JSON.stringify(consoleErrors)}`)
    ok('エラーは出ていない')
    console.log('smoke-svg: OK')
  } finally {
    await browser.close()
    server?.stop()
  }
}

main().catch((error) => {
  console.error(error)
  process.exit(1)
})
