/**
 * スケッチ図形の種別ごとの純フロントエンド処理(表) — kind分岐のテーブル化(2026-08-20)。
 *
 * 以前は「描画用の折れ線化」(`viewport.ts` の `entityPoints`)と「幽霊(確定前の姿)の組み立て」
 * (`commands/sketch.ts` の `ghostEntity`)がそれぞれ独自に kind/verb で分岐しており、`EntityKind`
 * の宣言も `session.ts` の3箇所目だった。ここへ**図形種別ごとに1行**へ集約する
 * (コアの `duocad/sketch.py` の `_ENTITY_KIND_TABLE` と対になる、TS側の唯一の宣言)。
 *
 * **表に無い種別は即時エラー**(`entityPoints` は throw。`ghostEntity` は元々「知らない
 * verbなら描かない」契約なので null のまま — 幽霊はスケッチのverb以外にも呼ばれるため)。
 */

/** 図形の種別。**この表のキーが唯一の正**(新種別は表に1行足すだけでよい)。 */
export type EntityKind = 'line' | 'circle' | 'rectangle' | 'arc'

interface EntityKindSpec {
  /** この種別を作るverb名(`add_*`)。`ghostEntity` の逆引きに使う。 */
  verb: string
  /** verbの `build()` が作った引数から、コアの `Entity.params` と同じキーへ移し替える。 */
  fromArgs(args: Record<string, unknown>): Record<string, number>
  /** `params` を (u,v) の折れ線へ(円・円弧は分割線で近似)。コアの `Entity.params` のキーを
   *  そのまま読む(`duocad/sketch.py` の `Entity` docstring)。 */
  toPoints(params: Record<string, number>): Array<[number, number]>
}

/** 円・円弧を折れ線へ近似するときの分割数。 */
const ARC_SEGMENTS = 64

function arcPoints(cx: number, cy: number, radius: number, start: number, end: number): Array<[number, number]> {
  const steps = Math.max(8, Math.round((ARC_SEGMENTS * Math.abs(end - start)) / 360))
  const points: Array<[number, number]> = []
  for (let step = 0; step <= steps; step += 1) {
    const angle = ((start + ((end - start) * step) / steps) * Math.PI) / 180
    points.push([cx + radius * Math.cos(angle), cy + radius * Math.sin(angle)])
  }
  return points
}

const ENTITY_KIND_TABLE: Record<EntityKind, EntityKindSpec> = {
  line: {
    verb: 'add_line',
    fromArgs(args) {
      const p0 = args.p0 as [number, number]
      const p1 = args.p1 as [number, number]
      return { x0: p0[0], y0: p0[1], x1: p1[0], y1: p1[1] }
    },
    toPoints(params) {
      return [
        [params.x0, params.y0],
        [params.x1, params.y1],
      ]
    },
  },
  circle: {
    verb: 'add_circle',
    fromArgs(args) {
      const center = args.center as [number, number]
      return { cx: center[0], cy: center[1], radius: args.radius as number }
    },
    toPoints(params) {
      // 反時計回り・0度をまたいでよい(`add_arc` の契約と揃える。円は0〜360固定)。
      return arcPoints(params.cx, params.cy, params.radius, 0, 360)
    },
  },
  rectangle: {
    verb: 'add_rectangle',
    fromArgs(args) {
      const corner = args.corner as [number, number]
      return { x: corner[0], y: corner[1], width: args.width as number, height: args.height as number }
    },
    toPoints(params) {
      const { x, y, width, height } = params
      return [
        [x, y],
        [x + width, y],
        [x + width, y + height],
        [x, y + height],
        [x, y],
      ]
    },
  },
  arc: {
    verb: 'add_arc',
    fromArgs(args) {
      const center = args.center as [number, number]
      return {
        cx: center[0],
        cy: center[1],
        radius: args.radius as number,
        start_deg: args.start_deg as number,
        end_deg: args.end_deg as number,
      }
    },
    toPoints(params) {
      // end<=start は一周分足す(0度またぎ。`add_arc` の契約)。
      let end = params.end_deg
      if (end <= params.start_deg) end += 360
      return arcPoints(params.cx, params.cy, params.radius, params.start_deg, end)
    },
  },
}

/** 図形の種別の一覧(唯一の正は上の表)。 */
export const ENTITY_KINDS: readonly EntityKind[] = Object.keys(ENTITY_KIND_TABLE) as EntityKind[]

const VERB_TO_KIND: Record<string, EntityKind> = Object.fromEntries(
  (Object.entries(ENTITY_KIND_TABLE) as Array<[EntityKind, EntityKindSpec]>).map(([kind, spec]) => [spec.verb, kind]),
)

function entityKindSpec(kind: string): EntityKindSpec {
  const spec = (ENTITY_KIND_TABLE as Record<string, EntityKindSpec>)[kind]
  if (!spec) throw new Error(`unknown entity kind: ${kind}`)
  return spec
}

/** 図形を (u,v) の折れ線にする。表に無い種別は例外(黙って空の折れ線を返さない)。 */
export function entityPoints(kind: string, params: Record<string, number>): Array<[number, number]> {
  return entityKindSpec(kind).toPoints(params)
}

/**
 * 確定前の姿(幽霊)を、コアの `Entity` と同じ `{kind, params}` へ翻訳する。
 *
 * `verb` がどの図形種別の追加でもなければ null(スケッチ以外のverbは幽霊を描かない —
 * これは「表に無いkindのエラー」ではなく「そもそも図形を作るverbではない」という正常系)。
 */
export function ghostEntity(
  verb: string,
  args: Record<string, unknown>,
): { kind: EntityKind; params: Record<string, number> } | null {
  const kind = VERB_TO_KIND[verb]
  if (!kind) return null
  return { kind, params: entityKindSpec(kind).fromArgs(args) }
}
