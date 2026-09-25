/**
 * ローカル幽霊(`preview: 'local'`)の引数を、**コアが配った数値**へ解決する純関数群(`App.svelte` から
 * 切り出した)。手元の状態は `GhostContext` で受ける —— 辺・頂点は**版を照合した後の値**
 * (App の `freshPayload`)を返す関数で渡し、ここでは版を見ない。
 *
 * **`showLocalGhost` に参照を渡さない**のが要点である(2026-09-06。前提タスク `20260906-0100` の
 * 不変条件①) —— あちらは「解決済みの数値でしか描かない」層で、参照の解決をあちらへ入れると、
 * 省略時の既定(重心・代表点の平均)を写す誘惑が同じ場所に生まれる。ここは**コアが配った数値そのもの**
 * (辺の折れ線・頂点の座標・図形の `params`)だけを読み、読めなければ**その引数を落とす** ——
 * 落ちた引数の分だけ幽霊が出なくなる。サーバへ送るのは参照のままである(`build`)。**解決の正はコア**で、
 * ここは画面の写し。
 */
import { refsOfKind } from './commands/command'
import { MOVE_BODY } from './commands/features'
import { MOVE_SKETCH_ENTITIES } from './commands/sketch'
import { entityHandles, type GhostSource } from './entityKinds'
import type { EdgesPayload, Point2, SelectionState, SketchEntity, SketchState, VerticesPayload } from './session'
import type { BodyGhostRotation } from './viewportTypes'

/** 幽霊の解決に要る手元の状態。 */
export interface GhostContext {
  sketches: readonly SketchState[]
  /** 今編集しているスケッチの ID(モード外は null)。 */
  editingId: string | null
  selection: SelectionState | null
  /** 版が今と同じ辺の折れ線(無ければ null)。 */
  edgesOf(bodyId: string): EdgesPayload | null
  /** 版が今と同じ頂点の座標(無ければ null)。 */
  verticesOf(bodyId: string): VerticesPayload | null
}

/**
 * ローカル幽霊の**対象になる既存の図形**(`ghostCopies` の `sources`)。
 *
 * 対象の取り方がverbで違うのでここで分ける —— 反復は**引数の `entities`**、移動は
 * **共有選択集合**から取る。この違いは `entityKinds.ts` には持ち込まない(あちらは
 * 「その図形をどう変換するか」だけを知っていればよい)。
 *
 * 編集中のスケッチに実在するIDだけを返す(消えた図形・他のスケッチの図形は落ちる)。
 */
export function ghostSources(ctx: GhostContext, verb: string, args: Record<string, unknown>): GhostSource[] {
  const active = ctx.sketches.find((item) => item.sketch_id === ctx.editingId) ?? null
  if (active === null) return []
  const byId = new Map(active.entities.map((entity) => [entity.entity_id, entity]))
  if (verb === MOVE_SKETCH_ENTITIES.id) {
    // **どの点が選ばれているかまで渡す** —— この verb は「図形1つ全体(辺)」と
    // 「図形の1点」で結果が違う(全体は形を変えずに動き、1点は線分が伸びる)。
    // 図形IDだけに畳むと、点を1つ選んだだけで図形ごと動く幽霊になる。
    const grouped = new Map<string, Array<string | null>>()
    for (const ref of refsOfKind(ctx.selection?.items ?? [], 'sketch_entity')) {
      if (ref.sketch !== active.sketch_id) continue
      const points = grouped.get(ref.entity)
      if (points === undefined) grouped.set(ref.entity, [ref.point])
      else if (!points.includes(ref.point)) points.push(ref.point)
    }
    return [...grouped].flatMap(([id, points]) => {
      const entity = byId.get(id)
      return entity === undefined ? [] : [{ kind: entity.kind, params: entity.params, points }]
    })
  }
  const ids = Array.isArray(args.entities) ? (args.entities as string[]) : []
  return ids
    .map((id) => byId.get(id))
    .filter((entity): entity is SketchEntity => entity !== undefined)
    .map((entity) => ({ kind: entity.kind, params: entity.params }))
}

/** `[x, y, z]` の有限な数値3つとして読めるなら返す(読めなければ null)。 */
function vector3(value: unknown): [number, number, number] | null {
  if (!Array.isArray(value) || value.length !== 3) return null
  if (!value.every((item) => typeof item === 'number' && Number.isFinite(item))) return null
  return [value[0], value[1], value[2]] as [number, number, number]
}

/**
 * `move_body(mode="rotate")` の引数から、幽霊に掛ける回転を作る(作れなければ null)。
 *
 * **軸が数値で来ているときだけ幽霊を出す。** コアは `axis_edge`(辺への参照)や
 * 「軸の位置を省略 → 対象の合成重心」も受けるが、**そのどちらもブラウザ側では
 * 正しく解けない** —— 辺の直線はメッシュからは取れず、体積重心にいたっては
 * 三角形分割の近似からしか出せない。近似で置いた幽霊は「幽霊と確定結果が別の場所に
 * 出る」という、プレビューとして最悪の壊れ方をする。**出さないほうが正しい**
 * (2026-09-06。タスク `20260906-0100` の割り切り)。
 */
export function bodyGhostRotation(args: Record<string, unknown>): BodyGhostRotation | null {
  const point = vector3(args.axis_point)
  const dir = vector3(args.axis_dir)
  const angle = args.angle
  if (point === null || dir === null) return null
  if (typeof angle !== 'number' || !Number.isFinite(angle)) return null
  // **360の倍数はコアが ValueError にする**(回っていない姿の幽霊を出さない)。
  if (angle % 360 === 0) return null
  if (dir.every((value) => value === 0)) return null
  return { point, dir, angleDeg: angle }
}

/** 直接ピックで指した**参照を、幽霊が要る数値へ解決する**(`showLocalGhost` へ渡す前)。 */
export function resolvePicksForGhost(
  ctx: GhostContext,
  verb: string,
  args: Record<string, unknown>,
): Record<string, unknown> {
  if (args.mode !== 'rotate') return args
  if (verb === MOVE_BODY.id) {
    const { axis_edge: axisRef, center_vertex: centerRef, ...rest } = args
    const axis = pickedEdgeAxis(ctx, axisRef)
    if (axis === null) return rest // まっすぐな辺が読めない = 幽霊を出さない
    const center = centerRef === undefined ? null : pickedVertexPoint(ctx, centerRef)
    // **中心を指したのに読めないときも落とす** —— 軸だけで描くと、確定結果とは
    // 別の位置に幽霊が出る(コアは指された頂点へ軸を寄せる)。
    if (centerRef !== undefined && center === null) return rest
    return { ...rest, axis_point: center ?? axis.point, axis_dir: axis.dir }
  }
  if (verb === MOVE_SKETCH_ENTITIES.id) {
    const { center_entity: centerRef, ...rest } = args
    if (centerRef === undefined) return rest // 省略 = 既定の中心。幽霊は出さない
    const center = pickedSketchPoint(ctx, centerRef)
    return center === null ? rest : { ...rest, center }
  }
  return args
}

/** 指した辺の「軸が通る点」と「向き」(読めなければ null)。 */
function pickedEdgeAxis(
  ctx: GhostContext,
  value: unknown,
): { point: [number, number, number]; dir: [number, number, number] } | null {
  const ref = value as { body?: unknown; index?: unknown } | undefined
  if (!ref || typeof ref.body !== 'string' || typeof ref.index !== 'number') return null
  const known = ctx.edgesOf(ref.body)
  if (known === null) return null
  const edge = known.edges.find((item) => item.index === ref.index)
  // **まっすぐな辺だけ**(コアの `kernel.edge_axis` が円・splineを断るのと同じ線)。
  // `kind` はコアの `classify_edge` がそのまま載せたものなので、判定を写していない。
  if (!edge || edge.kind !== 'line' || edge.points.length < 6) return null
  const points = edge.points
  const start: [number, number, number] = [points[0], points[1], points[2]]
  const end = points.slice(points.length - 3)
  // **始点→終点**が `kernel.edge_axis` の向きと一致する(どちらも同じ
  // `BRepAdaptor_Curve` の媒介変数の順に並ぶ。2026-09-06に立方体・穴あき・フィレット・
  // 合体・L字の直線辺83本で実測)。ここが逆になると幽霊だけが逆回りに見える。
  const dir: [number, number, number] = [end[0] - start[0], end[1] - start[1], end[2] - start[2]]
  return dir.every((value) => value === 0) ? null : { point: start, dir }
}

/** 指した頂点の座標(読めなければ null)。 */
function pickedVertexPoint(ctx: GhostContext, value: unknown): [number, number, number] | null {
  const ref = value as { body?: unknown; index?: unknown } | undefined
  if (!ref || typeof ref.body !== 'string' || typeof ref.index !== 'number') return null
  const known = ctx.verticesOf(ref.body)
  if (known === null) return null
  const found = known.vertices.find((item) => item.index === ref.index)?.point
  return found === undefined ? null : [found[0], found[1], found[2]]
}

/**
 * 指したスケッチの点の座標 [u, v](読めなければ null)。
 *
 * **`point` が null なら、その種別の最初の点**(線の始点・円/弧の中心・長方形の左下・
 * ベジエの始点)。コアの `move_sketch._point_of` と同じ規則で、並びの正は
 * `entityKinds.ts` の表 1つである(`entityHandles` がその順に返す)。
 */
function pickedSketchPoint(ctx: GhostContext, value: unknown): Point2 | null {
  const ref = value as { sketch?: unknown; entity?: unknown; point?: unknown } | undefined
  if (!ref || typeof ref.sketch !== 'string' || typeof ref.entity !== 'string') return null
  const sketch = ctx.sketches.find((item) => item.sketch_id === ref.sketch)
  const entity = sketch?.entities.find((item) => item.entity_id === ref.entity)
  if (!entity) return null
  const handles = entityHandles(entity.kind, entity.params)
  const handle =
    ref.point === null || ref.point === undefined ? handles[0] : handles.find((item) => item.key === ref.point)
  return handle === undefined ? null : [handle.u, handle.v]
}
