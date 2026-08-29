/**
 * スケッチ図形の種別ごとの純フロントエンド処理(表) — kind分岐のテーブル化。
 *
 * 「描画用の折れ線化」(`viewport.ts` の `entityPoints`)と「幽霊(確定前の姿)の組み立て」
 * (`commands/sketch.ts` の `ghostEntity`)を**図形種別ごとに1行**へ集約してある
 * (コアの `duocad/sketch.py` の `_ENTITY_KIND_TABLE` と対になる、TS側の唯一の宣言)。
 *
 * **表に無い種別は即時エラー**(`entityPoints` は throw。`ghostEntity` は「知らないverbなら
 * 描かない」契約なので null のまま —— 幽霊はスケッチのverb以外にも呼ばれるため)。
 */

import {
  DEFAULT_RESOLUTION,
  arcSegments,
  bezierSegments,
  type ViewResolution,
} from './adaptiveResolution'

/** 図形の種別。**この表のキーが唯一の正**(新種別は表に1行足すだけでよい)。 */
export type EntityKind = 'line' | 'circle' | 'rectangle' | 'arc' | 'bezier'

interface EntityKindSpec {
  /**
   * この種別を作るverb名(`add_*`)。`ghostEntity` の逆引きに使う。
   *
   * **省略できる** —— `bezier` はこれを作るverbが無い(取り込み `import_svg` だけが作り、
   * 人もAIもベジエを1本ずつは足せない。2026-08-26本人裁定)。verbが無い種別は
   * 幽霊(確定前の姿)も持たないので、`fromArgs` ではなく表ごと持たない形にしてある。
   */
  verb?: string
  /** verbの `build()` が作った引数から、コアの `Entity.params` と同じキーへ移し替える。
   *  **`verb` があるときだけ要る**(幽霊を描くのはverbで起動したコマンドだけ)。 */
  fromArgs?(args: Record<string, unknown>): Record<string, number>
  /** `params` を (u,v) の折れ線へ(円・円弧は分割線で近似)。コアの `Entity.params` のキーを
   *  そのまま読む(`duocad/sketch.py` の `Entity` docstring)。
   *
   *  **`resolution` は「いまの画面でどれくらい細かく折るか」**(`adaptiveResolution.ts`)。
   *  以前は分割数が固定(円1周64)で、寄ると多角形の角が見えていた。 */
  toPoints(params: Record<string, number>, resolution: ViewResolution): Array<[number, number]>
}

/**
 * 円弧を折れ線へ。`start` / `end` は**度**(コアの `Entity.params` がそう持っている)、
 * 分割数の計算は**ラジアン**で行う(`adaptiveResolution.ts` の単位に合わせる)。
 */
function arcPoints(
  cx: number,
  cy: number,
  radius: number,
  start: number,
  end: number,
  resolution: ViewResolution,
): Array<[number, number]> {
  const sweep = ((end - start) * Math.PI) / 180
  const steps = arcSegments(radius, sweep, resolution)
  const points: Array<[number, number]> = []
  for (let step = 0; step <= steps; step += 1) {
    const angle = ((start + ((end - start) * step) / steps) * Math.PI) / 180
    points.push([cx + radius * Math.cos(angle), cy + radius * Math.sin(angle)])
  }
  return points
}

/** 3次ベジエを折れ線へ。**描画と当たり判定だけの話**で、
 *  コア側(`kernel._edges_bezier`)は曲線のままOCCTへ渡すので形は近似されない。 */
function bezierPoints(
  x0: number, y0: number,
  cx1: number, cy1: number,
  cx2: number, cy2: number,
  x1: number, y1: number,
  resolution: ViewResolution,
): Array<[number, number]> {
  const steps = bezierSegments([x0, y0], [cx1, cy1], [cx2, cy2], [x1, y1], resolution)
  const points: Array<[number, number]> = []
  for (let step = 0; step <= steps; step += 1) {
    const t = step / steps
    const u = 1 - t
    points.push([
      u * u * u * x0 + 3 * u * u * t * cx1 + 3 * u * t * t * cx2 + t * t * t * x1,
      u * u * u * y0 + 3 * u * u * t * cy1 + 3 * u * t * t * cy2 + t * t * t * y1,
    ])
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
    toPoints(params, resolution) {
      // 反時計回り・0度をまたいでよい(`add_arc` の契約と揃える。円は0〜360固定)。
      return arcPoints(params.cx, params.cy, params.radius, 0, 360, resolution)
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
    toPoints(params, resolution) {
      // end<=start は一周分足す(0度またぎ。`add_arc` の契約)。
      let end = params.end_deg
      if (end <= params.start_deg) end += 360
      return arcPoints(params.cx, params.cy, params.radius, params.start_deg, end, resolution)
    },
  },
  bezier: {
    // **`verb` を持たない唯一の種別**(上の `EntityKindSpec.verb` のコメント)。
    toPoints(params, resolution) {
      return bezierPoints(
        params.x0, params.y0,
        params.cx1, params.cy1,
        params.cx2, params.cy2,
        params.x1, params.y1,
        resolution,
      )
    },
  },
}

/** 図形の種別の一覧(唯一の正は上の表)。 */
export const ENTITY_KINDS: readonly EntityKind[] = Object.keys(ENTITY_KIND_TABLE) as EntityKind[]

// **`verb` を持つ種別だけ**を入れる。持たない種別(`bezier`)を `undefined` キーで
// 入れると、verbの綴りを間違えた呼び出しがそこへ落ちて幽霊が別の図形として描かれる。
const VERB_TO_KIND: Record<string, EntityKind> = Object.fromEntries(
  (Object.entries(ENTITY_KIND_TABLE) as Array<[EntityKind, EntityKindSpec]>)
    .filter(([, spec]) => spec.verb !== undefined)
    .map(([kind, spec]) => [spec.verb as string, kind]),
)

function entityKindSpec(kind: string): EntityKindSpec {
  const spec = (ENTITY_KIND_TABLE as Record<string, EntityKindSpec>)[kind]
  if (!spec) throw new Error(`unknown entity kind: ${kind}`)
  return spec
}

/**
 * 図形を (u,v) の折れ線にする。表に無い種別は例外(黙って空の折れ線を返さない)。
 *
 * `resolution` を省くと**ズーム連動を入れる前と同じ既定の粗さ**になる。呼ぶのは
 * `viewport.ts` だけで、そちらは常に今のカメラから求めた値を渡す。
 */
export function entityPoints(
  kind: string,
  params: Record<string, number>,
  resolution: ViewResolution = DEFAULT_RESOLUTION,
): Array<[number, number]> {
  return entityKindSpec(kind).toPoints(params, resolution)
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
  const spec = entityKindSpec(kind)
  // `VERB_TO_KIND` は verb を持つ種別だけなので、ここへ来る種別は必ず `fromArgs` を持つ。
  if (!spec.fromArgs) return null
  return { kind, params: spec.fromArgs(args) }
}
