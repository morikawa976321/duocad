/**
 * 形状最適化の本が共有する題材と条件(`page` と `mcp` を引数で受けて値を返す形)。
 *
 * 条件を**本物のクリックで置ける**ことは `smoke-optimize-mode.mjs` が見る。ほかの本は走らせる前の
 * 下ごしらえとして MCP で置く。
 */
import { addExtrudedRect } from './mcp-client.mjs'
import { planarFace } from './pick.mjs'

/** 12mm の立方体を2つ、48mm 離して置く(**離す**ので、あいだを繋ぐ材料を最適化が起こす)。 */
export async function addBoxPair(mcp) {
  const add = async (corner) =>
    (await addExtrudedRect(mcp, { corner, width: 12, height: 12, distance: 12 })).bodyId
  return { bodyA: await add([0, 0]), bodyB: await add([48, 0]) }
}

/**
 * 4条件を MCP で置く: 保持 = A と B、拘束 = A の外側の面(−X)、荷重 = B の上面へ 200N の下向き。
 * 面は添字を決め打ちせず、画面が持っている形から法線で名指す。
 */
export async function placeConditions(mcp, page, { bodyA, bodyB }) {
  const fixtureFace = await planarFace(page, bodyA, [-1, 0, 0])
  const loadFace = await planarFace(page, bodyB, [0, 0, 1])
  await mcp.callTool('set_selection', {
    refs: [bodyA, bodyB].map((id) => ({ body: id, kind: 'body', index: null })),
  })
  await mcp.callTool('set_preserve_bodies')
  await mcp.callTool('set_selection', { refs: [{ body: bodyA, kind: 'face', index: fixtureFace }] })
  await mcp.callTool('set_fixtures')
  await mcp.callTool('set_selection', { refs: [{ body: bodyB, kind: 'face', index: loadFace }] })
  await mcp.callTool('set_loads', { magnitude: 200, direction: [0, 0, -1] })
  await mcp.callTool('clear_selection')
  return { fixtureFace, loadFace }
}
