/**
 * 干渉チェック(issue #186)の返事の型。コアの `duocad/interference.py` の `run` の戻り値(`{type:'interference', client_op, ...}`)そのもの。
 * 形は docs/INTERFERENCE.md「返すもの」。画面へだけ `render`(描く三角形・折れ線・点)が付く。
 */

export type InterferenceRelation = 'overlap' | 'tiny_overlap' | 'contact' | 'separated' | 'unknown'

/** `short`: 要求未満(触れている組は全部) / `at`: 要求と一致(比較帯の中) / `ok`: 余裕 / `null`: 最短距離が測れなかった(理由は `clearance_reason`)。隙間を問わなかったときは欄ごと無い。 */
export type InterferenceClearance = 'short' | 'at' | 'ok' | null

export interface RenderMesh {
  /** x,y,z を平らに並べた頂点[mm]。 */
  positions: number[]
  indices: number[]
}

export interface InterferenceRender {
  /** overlap / tiny_overlap の重なりの立体。 */
  solid?: RenderMesh
  /** 面の接触の共有面。 */
  faces?: RenderMesh
  /** 線の接触の共有辺(折れ線)。 */
  edges?: number[][][]
  /** 点の接触の点。 */
  points?: number[][]
}

export interface InterferencePair {
  a: string
  b: string
  a_name: string
  b_name: string
  relation: InterferenceRelation
  /** unknown の理由。 */
  reason?: string
  volume?: number
  thickness?: number
  obb_min?: number
  center?: number[]
  bbox?: { min: number[]; max: number[] }
  contact?: 'face' | 'edge' | 'vertex' | 'mesh'
  area?: number
  distance?: number
  /** 最短距離の2点(B-rep どうしで測れたとき)。 */
  points?: number[][]
  /** 縁の点に頼った値(真の最小以上)。 */
  distance_upper_bound?: boolean
  /** 探索幅で頭打ちになった値(実際はこれ以上)。 */
  distance_lower_bound?: boolean
  clearance?: InterferenceClearance
  clearance_reason?: string
  /** メッシュを含む組の近似の印。 */
  approximate?: string
  warnings: string[]
  render?: InterferenceRender
}

export interface InterferenceCounts {
  overlap: number
  tiny_overlap: number
  contact: number
  separated: number
  unknown: number
  short: number
  at: number
  ok: number
  clearance_null: number
}

export interface InterferencePayload {
  /** 要求に付けた `client_op` の写し。古い返事を捨てる印。 */
  client_op: number | null
  request_id?: number | string
  /** 開始時の文書の版(`世代.適用回数.ログ本数`)。いまの版と違えば古い。 */
  document_revision?: string
  units?: string
  target?: {
    mode: 'visible_all' | 'one_vs_rest' | 'selected_set'
    selected: string[]
    bodies: string[]
    excluded_hidden: number
    pairs: number
  }
  clearance?: number
  method?: string
  seconds?: number
  complete?: boolean
  not_started?: number
  child_restarts?: number
  counts?: InterferenceCounts
  pairs?: InterferencePair[]
  /** 計算が例外で終わった(結果は無い)。 */
  error?: string
}

/** ビューポートの層が描くもの(`interferenceLayer.ts`)。座標はワールド[mm]。 */
export interface InterferenceVisual {
  /** 重なりの立体(半透明の赤)。 */
  solids: RenderMesh[]
  /** 面の接触(共有面)。 */
  faces: RenderMesh[]
  /** 線の接触(折れ線)。 */
  edges: number[][][]
  /** 点の接触。 */
  points: number[][]
  /** 最短距離の線と札。 */
  distance: { from: number[]; to: number[]; label: string } | null
}
