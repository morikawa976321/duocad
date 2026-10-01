/**
 * 実ブラウザでのメッシュボディ検査(20260904-1703) —— **三角形メッシュが画面に出ることの証明**。
 *
 * pytest 側(`tests/test_mesh_body.py`)は配りものの**形**がB-repと同じであることまでしか
 * 見られない。ここが見るのは、その配りものを**本物のブラウザが実際に受け取って描いたか**
 * である —— 形が同じでも、`web/src/App.svelte` が種別で濾していれば画面には出ない
 * (実際、部品一覧のコメントは「常に kind==="solid" だけ」と書いてあった。**濾していない**
 * ことをここで固定する)。
 *
 *   web> node scripts/smoke-mesh-body.mjs
 *
 * 見るもの:
 *
 *   1. **MCPの `add_mesh_body`** が通り、`describe_document` が kind="mesh" を返す
 *   2. **部品一覧に出る**(種別で濾されていない)
 *   3. **viewportが三角形を持っている**(`getGeometry` の positions/indices が渡した通り)
 *   4. **画面に投影できる**(`projectFace` が座標を返す = THREE.Mesh が実在して描かれている)
 *   5. **表示の切り替えが効く**(隠すと geometry が消える)
 *   6. ブラウザのコンソールにエラーが出ていない
 *
 * 編集が断られること・STLへ書けること・消せることはブラウザに現れないので、ここでは見ない
 * (`tests/test_mesh_body.py`)。
 *
 * **サーバはこのスクリプトが自分で立てる**(`web/scripts/server.mjs`)。
 * 既に立っているものへ繋ぐときだけ `--url` を渡す(そのときは `--port` も合わせること)。
 */
import path from 'node:path'

import { fail, ok } from './args.mjs'
import { assertNoAppErrors, openApp } from './browser.mjs'
import { runSmoke } from './harness.mjs'

/**
 * 一辺10mmの正八面体。**画面で見える大きさ**にしてある(四面体1mmだと描けていても
 * 投影の検査が画素の丸めに埋もれる)。体積は 4/3 * 10^3 = 1333.33mm^3。
 */
const OCTA_POSITIONS = [
  10, 0, 0, -10, 0, 0, 0, 10, 0, 0, -10, 0, 0, 0, 10, 0, 0, -10,
]
const OCTA_INDICES = [
  0, 2, 4, 2, 1, 4, 1, 3, 4, 3, 0, 4, 2, 0, 5, 1, 2, 5, 3, 1, 5, 0, 3, 5,
]
const OCTA_VOLUME = (4 / 3) * 10 ** 3
const OCTA_TRIANGLES = OCTA_INDICES.length / 3

// `--url` には対応しない(MCPのstdioが要るのでサーバを自分で立てる。渡すと harness が断る)。
runSmoke('smoke-mesh-body', { mcp: true }, async ({ mcp, browser, url, timeout, workdir }) => {
  {
    // 書き出し先は文書の置き場と同じ使い捨てディレクトリ(走行後に harness が消す)。
    const outDir = workdir

    // ------------------------------------------------------------ 1. 足す
    const added = await mcp.callTool('add_mesh_body', {
      positions: OCTA_POSITIONS,
      indices: OCTA_INDICES,
      name: 'octahedron',
    })
    if (added.verb !== 'add_mesh_body') fail(`verb が一致しない: ${added.verb}`)
    const meshBodyId = added.body_id
    ok(`MCPの add_mesh_body: ${meshBodyId}`)

    const described = await mcp.callTool('describe_document')
    const meshEntry = described.bodies.find((body) => body.body_id === meshBodyId)
    if (!meshEntry) fail('describe_document にメッシュボディが出ない')
    if (meshEntry.kind !== 'mesh') fail(`kind が mesh でない: ${meshEntry.kind}`)
    if (meshEntry.face_count !== 1) fail(`面は1枚として数える約束: ${meshEntry.face_count}`)
    if (Math.abs(meshEntry.volume - OCTA_VOLUME) > 1e-6) {
      fail(`体積が合わない(期待 ${OCTA_VOLUME}): ${meshEntry.volume}`)
    }
    ok(`describe_document: kind=mesh / face_count=1 / volume=${meshEntry.volume.toFixed(2)}mm^3`)

    // ------------------------------------------------------------ ブラウザを開く
    const { page, consoleErrors } = await openApp(browser, url, {
      timeout,
      until: () => (window.__duocad.parts ?? []).length > 0,
    })

    // ------------------------------------------------------------ 2. 部品一覧に出る
    const parts = JSON.parse(
      await page.evaluate(() =>
        JSON.stringify(window.__duocad.parts.map((body) => [body.body_id, body.name, body.kind])),
      ),
    )
    const listed = parts.find((entry) => entry[0] === meshBodyId)
    if (!listed) fail(`部品一覧にメッシュボディが出ない: ${JSON.stringify(parts)}`)
    if (listed[1] !== 'octahedron' || listed[2] !== 'mesh') {
      fail(`部品一覧の中身が違う: ${JSON.stringify(listed)}`)
    }
    ok('部品一覧に出る(種別で濾されていない)')

    // ------------------------------------------------------------ 3. viewportが三角形を持つ
    const geometry = JSON.parse(
      await page.evaluate((bodyId) => {
        const payload = window.__duocad.getGeometry(bodyId)
        if (payload === null) return JSON.stringify(null)
        return JSON.stringify({
          faceCount: payload.faces.length,
          positions: payload.faces[0].positions.length,
          indices: payload.faces[0].indices.length,
          firstIndices: payload.faces[0].indices.slice(0, 6),
        })
      }, meshBodyId),
    )
    if (geometry === null) fail('viewportがメッシュのgeometryを持っていない')
    if (geometry.faceCount !== 1) fail(`面1枚として配る約束: ${geometry.faceCount}`)
    if (geometry.positions !== OCTA_POSITIONS.length || geometry.indices !== OCTA_INDICES.length) {
      fail(`三角形が渡した通りでない: ${JSON.stringify(geometry)}`)
    }
    ok(`viewportが三角形を持っている(${OCTA_TRIANGLES}枚・頂点${OCTA_POSITIONS.length / 3}個)`)

    // ------------------------------------------------------------ 4. 画面に投影できる
    await page.evaluate(() => window.__duocad.frameAll())
    await page.waitForFunction(() => window.__duocad.projectFace !== undefined, { timeout })
    const projected = JSON.parse(
      await page.evaluate((bodyId) => JSON.stringify(window.__duocad.projectFace(bodyId, 1)), meshBodyId),
    )
    if (projected === null) fail('メッシュが画面へ投影できない(THREE.Meshが無い=描かれていない)')
    if (
      !(projected.x > 0 && projected.x < 1280 && projected.y > 0 && projected.y < 860)
    ) {
      fail(`投影先が画面の外: ${JSON.stringify(projected)}`)
    }
    ok(`画面に出ている(投影先 x=${projected.x.toFixed(0)} y=${projected.y.toFixed(0)})`)
    await page.screenshot({ path: path.join(outDir, 'mesh-body.png') })

    // ------------------------------------------------------------ 5. 表示の切り替え
    const epochBefore = await page.evaluate(() => window.__duocad.stateEpoch)
    await mcp.callTool('set_visible', { body_id: meshBodyId, visible: false })
    await page.waitForFunction((before) => window.__duocad.stateEpoch > before, { timeout }, epochBefore)
    const hidden = await page.evaluate(
      (bodyId) => window.__duocad.parts.find((body) => body.body_id === bodyId).visible,
      meshBodyId,
    )
    if (hidden !== false) fail('隠したのに可視のまま')
    await mcp.callTool('set_visible', { body_id: meshBodyId, visible: true })
    ok('表示の切り替えが効く')

    await assertNoAppErrors(page, consoleErrors)
  }
})
