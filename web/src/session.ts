/**
 * コアとの接続 — コマンドは下り、イベントは上り(docs/DESIGN_DECISIONS.md「外してはいけない3つ」)。
 *
 * ここが持つのは「WebSocketの接続」と「途中まで入力していたコマンド」だけで、
 * 文書・操作ログ・undo履歴・可視性・選択はコア側にある(§2)。
 * `modify_selection` / `toggle_selection` / `clear_selection` / `set_visible` を送るだけで、状態は覚えない。
 *
 * `state.bodies` は軽量(geometryを含まない)で、形状は `delta.bodies`(版が変わったBodyだけ)で届く
 * (`duocad/server.py` のモジュールdocstring参照)。`state` と `preview` は1枚のバイナリフレームで届き、前回受け取った面・
 * 操作ログ・スケッチは省かれている(`wire.ts` が埋め戻す)。
 * `preview`(投機的実行)は状態を配信しないので、応答は `onState` ではなく `onPreview` へ来る。
 */
/** 図形の種別。唯一の正は `entityKinds.ts` の表。 */
import type { EntityKind } from './entityKinds'
import type { Applied, ServerStateKey } from './generated/protocol'
import type { GeometryPayload } from './viewport'
import { createDelivered, decodeFrame, DeliveryMismatch, type WireState } from './wire'

export type { Applied, EntityKind }

/** 選べるものの種別。領域(region)は `RegionRef` 側で表す(`duocad/selection.py` §3.5)。 */
export type ShapeKind = 'face' | 'edge' | 'vertex'

/** 面・辺・頂点を指す参照。添字は**1始まり**(コアの `select_*` と同じ)。 */
export interface ShapeRef {
  body: string
  kind: ShapeKind
  index: number
  provenance?: unknown
}

/**
 * ソリッドを丸ごと指す参照(形状最適化の保持/除外ソリッド)。
 *
 * 添字を持たない: コアは `index: null` を明示的に載せてくる(キーごと落とすと
 * 「まだ埋めていない」と「そもそも持たない」が見分けられない)。綴りは `duocad/selection.py` の `BODY_KIND`。
 */
export interface BodyRef {
  body: string
  kind: 'body'
  index: null
  provenance?: unknown
}

/** スケッチの領域を指す参照(`kind` キーを持たないのが `ShapeRef` との見分け方)。 */
export interface RegionRef {
  sketch: string
  index: number
  provenance?: unknown
}

/**
 * スケッチの図形(または図形上の1点)を指す参照。
 *
 * `entity` を持つことが `RegionRef` との見分け方(コアの `duocad/selection.py` の `ref_kind` と同じ規則)。
 * `point` が `null` なら図形1つ全体(= 辺)、文字列ならその点(`x0` / `cx` / `x` 等のxキー)。
 * 添字ではなく `entity_id` で指すので、他の図形を足しても平行移動しても失効しない。
 */
export interface SketchEntityRef {
  sketch: string
  entity: string
  point: string | null
  provenance?: unknown
}

/**
 * 選択集合に入りうる参照の全部(コアの `duocad/selection.py` の `SELECTABLE_KINDS` と対)。
 *
 * `BodyRef` を落とさないこと: `state.optimize` の4配列は「そのまま `modifySelection` へ渡せる」が
 * 契約なので、ここに無いと保持/除外ソリッドのプリセレクトが型の上で書けない
 * (`tests/test_optimize_setup.py` が宣言を静的に見ている)。
 */
export type SelectionRef = ShapeRef | BodyRef | RegionRef | SketchEntityRef

/** 共有される選択集合(`duocad/selection.py` の `Selection.to_dict()`)。人が選んだものもAIが選んだものも同じ集合で、出所を持たない。 */
export interface SelectionState {
  items: SelectionRef[]
  count: number
}

/**
 * 起動中のコマンド(`duocad/workspace.py`)。`null` は無起動。人の画面もAIも同じ操作(`begin_command` /
 * `set_command_args` / `end_command` / `apply_command`)で触り、出所を持たない。`args` は verb のサーバ側引数名で、
 * 入っているキーだけ。`revision` は作業空間の版(変更のたびに進む)。
 */
export interface CommandState {
  verb: string
  args: Record<string, unknown>
  revision: number
}

/** モード(`duocad/workspace.py`)。画面はこれに従う(`enter_sketch` / `leave_sketch` …)。 */
export type ModeState = { kind: 'idle' } | { kind: 'sketch'; sketch_id: string } | { kind: 'optimize' }

/** 1Bodyぶんの軽量な状態(geometryを含まない)。パーツ一覧はこれだけで描ける。 */
export interface BodyMeta {
  body_id: string
  name: string
  kind: string
  visible: boolean
  revision: number
  /** 形の版(`DocumentView.shape_revision`)。`revision` と違い粗さでは上がらない(形の差し替えと可視性でだけ上がる)。 */
  shape_revision: number
  face_count: number
  volume: number
}

/** `BodyMeta` + 形状。版が変わったBodyだけ届く(`geometry` は非表示なら null)。 */
export interface BodyGeometry extends BodyMeta {
  geometry: GeometryPayload | null
}

// --------------------------------------------------------------------------------------
// C: スケッチ
// --------------------------------------------------------------------------------------
/** スケッチを貼る平面(コアの PlaneRef。基準3面・既存ソリッドの平面状の面・そのオフセット)。 */
export type PlaneRef =
  | { kind: 'base'; name: BasePlaneName; provenance?: unknown }
  | { kind: 'face'; body: string; index: number; provenance?: unknown }
  | { kind: 'offset'; base: PlaneRef; distance: number; provenance?: unknown }

export type BasePlaneName = 'XY' | 'XZ' | 'YZ'

/**
 * スケッチ平面の3Dの枠。`p = origin + u*uAxis + v*vAxis` が (u,v) と3Dの往復の規則で、
 * コアが配る(`kernel_sketch.sketch_plane_frame`)。アプリ層で同じ計算を持たない
 * (基準3面の軸の取り方と、面に貼ったときの法線反転の扱いがずれる)。
 */
export interface SketchFrame {
  origin: number[]
  u: number[]
  v: number[]
  normal: number[]
}

/**
 * スケッチ上の1図形。`params` は解決済みの数値、`refs` は同じキーのパラメータ名
 * (`set_parameter` で打ち直せる。名前が無ければ null)。
 * (docs/DESIGN_DECISIONS.md「day 1 に払った予防」の名前つきの値)
 */
export interface SketchEntity {
  entity_id: string
  kind: EntityKind
  params: Record<string, number>
  refs: Record<string, string | null>
}

/** 計算された閉領域。`positions`/`indices` は画面へ描くための三角形(コアが測る)。 */
export interface SketchRegion {
  index: number
  area: number
  centroid: number[]
  positions: Float32Array | number[]
  indices: Uint32Array | number[]
}

/** 反復の種別。`Pattern.kind`(コア)と同じ語彙。 */
export type PatternKind = 'rectangular' | 'circular'

/**
 * スケッチ上の1反復。`params`/`refs` は `SketchEntity` と同じ形(打ち直しの対象)。
 * `sources` は元にした図形のID(反復そのものは反復の対象に取れない)。
 * `options` は寸法ではない設定(円形反復の `rotate_copies` 等。打ち直せない)。
 */
export interface SketchPattern {
  pattern_id: string
  kind: PatternKind
  sources: string[]
  params: Record<string, number>
  refs: Record<string, string | null>
  options: Record<string, unknown>
}

export interface SketchState {
  sketch_id: string
  /** 表示名(作成時の通し番号か `rename` で付けた名前)。番号の記録が無い古い文書のスケッチだけ null。 */
  name: string | null
  plane: PlaneRef
  /** 表示/非表示(`BodyMeta.visible` と同じくコアのセッション状態。保存されない)。 */
  visible: boolean
  /** 平面が壊れている(貼った面が消えた等)ときだけ null。 */
  frame: SketchFrame | null
  /** 反復を展開した実際に見えるもの。描画は必ずこちらを使う。 */
  drawn: Array<{ kind: EntityKind; params: Record<string, number> }>
  /** 人が値を打ち直す対象(反復は展開しない)。 */
  entities: SketchEntity[]
  /** 反復(間隔・個数)。 */
  patterns: SketchPattern[]
  regions: SketchRegion[]
  /** 描くもの(枠・図形・領域・取っ手)が変わったら上がる版(`DocumentView._sketch_state`)。選択・表示・名前では上がらない。 */
  revision: number
}

/**
 * 形状最適化(第3のモード)でいまどの対象にどの役割が付いているか。
 * 参照はそのまま `modifySelection` へ渡せる形: verb起動時に、既にその役割が付いている対象を
 * 選択状態にして見せる(集合代入なので、付け外しして決定した集合がそのまま新しい割り当てになる)。
 * 契約の正は `duocad/optimize.py` の `state_payload`。
 */
export interface OptimizeState {
  preserve_bodies: BodyRef[]
  exclude_bodies: BodyRef[]
  /** いま指せるものだけが載る(不可視・消えたボディの分は `stale` へ回る)。`modifySelection` へ渡して例外になる参照は混ぜない。 */
  fixtures: ShapeRef[]
  loads: ShapeRef[]
  /**
   * 荷重の大きさ[N]と向きを面ごとに持つ並び。荷重面が0枚なら空。
   * `loads` とは長さが違いうるので `(body, index)` で突き合わせること(荷重面のボディを隠しても
   * こちらは残る。隠しただけで打ち直しになるのを避けるため)。
   */
  load_specs: OptimizeLoadSpec[]
  /**
   * いま使えない割り当て。上の4配列に載らなかったものが理由つきで全部ここに出る
   * (指紋が合わなくなった面・消えたボディ・非表示のボディ)。
   * 文書からは何も消えていないので、形か表示を戻せば次の配信でまた載る。
   * トーストに出すかどうかは `code` で決める(`'hidden'` は人が部品を隠すたびに出る)。
   */
  stale: OptimizeStaleItem[]
  /** 条件の版(中身が変わると1つ上がる)。 */
  revision: number
}

/** 面1枚ぶんの荷重(大きさ[N]と向き)。`direction === 'vector'` のときだけ `vector` が入る。 */
export interface OptimizeLoadSpec {
  body: string
  index: number
  magnitude: number
  direction: 'normal' | '-normal' | 'vector'
  vector: [number, number, number] | null
}

/**
 * いま使えない割り当て1件(`OptimizeState.stale`)。
 *
 * `index` はボディの役割(`preserve_bodies` / `exclude_bodies`)では `null`。
 * `reason` はそのままトーストに出せる文だが、判定は `code` で行う(文言で分岐すると文を直した日に静かに壊れる)。
 */
export interface OptimizeStaleItem {
  role: 'preserve_bodies' | 'exclude_bodies' | 'fixtures' | 'loads'
  body: string
  index: number | null
  /** `'fingerprint'`=形が変わって別の面を指している / `'missing'`=ボディが無い / `'hidden'`=非表示 */
  code: 'fingerprint' | 'missing' | 'hidden'
  reason: string
}

export interface ServerState {
  units: string
  active_body_id: string | null
  /** 全Body。geometryを含まない(27部品でも数KB)。 */
  bodies: BodyMeta[]
  log: Array<{ seq: number; verb: string; args: Record<string, unknown> }>
  can_undo: boolean
  can_redo: boolean
  applied_count: number
  /** 共有される選択集合。ここが正で、ブラウザは自前の選択を持たない。 */
  selection: SelectionState
  /** 文書の全スケッチ。図形も領域もここが正。 */
  sketches: SketchState[]
  /** あと何回やり直せるか。 */
  redo_count: number
  /** 名前つきの寸法値(§3.4)。パラメータ編集パネルが出す対象そのもの。 */
  parameters: Record<string, number>
  /**
   * いま編集しているファイルの名前(`models/` 配下のファイル名だけ。無ければ `null`)。
   * サーバが正(`Session._current_name`)。ブラウザは写しを持つだけで書き換えない
   * (2箇所で持つと「ヘッダは(無題)なのに上書きは通る」食い違いが出る。リロードで
   * ブラウザの値だけ失われて実際に起きた)。
   */
  current_name: string | null
  /** 今の文書がそのままファイルに載っているか(`Session.saved`)。自動保存が書けなかったときだけ false(issue #5)。 */
  saved: boolean
  /** 文書を開いているか(`Session.document_open`)。false ならトップ画面を出し、編集の口を全部隠す。 */
  document_open: boolean
  /** 形状最適化の条件(第3のモード)。verb起動時のプリセレクトと役割の色はここが正。 */
  optimize: OptimizeState
  /** 起動中のコマンド(作業空間)。`null` は無起動。 */
  command: CommandState | null
  /** モード(作業空間)。 */
  mode: ModeState
  /** 文書の世代(`Session.generation`。開く・新規・閉じるで進む)。変わったら前の文書の記憶(スケッチの戻り先)を捨てる。 */
  generation: number
}

/**
 * `ServerState` のキーが、サーバの宣言(`duocad/catalog.py` の `STATE_KEYS`)と一致することを
 * 型検査に見させる。片方だけ直すと、ここが `never` になって落ちる。
 */
type SameKeys<A, B> = [A] extends [B] ? ([B] extends [A] ? true : never) : never
true satisfies SameKeys<keyof ServerState, ServerStateKey>

/**
 * ファイル操作の応答。状態の配信ではない(保存・書き出しは文書を変えないので、要求した接続にだけ届く)。
 * `path` はサーバが解決した絶対パス(相対パスの基準はサーバの作業ディレクトリ)。
 * `name` は `save_document` の応答にだけ乗るファイル名。`open_document` は文書を差し替える側なので
 * `Applied`(`change: "document"`)の `name` を見ること(`FilesPanel.svelte` 参照)。
 * `files` は `list_documents` の応答にだけ乗る。
 */
export interface FilePayload {
  action: string
  path: string
  name?: string
  /** 複製元のファイル名(`copy_document` のみ)。 */
  source?: string
  deviation?: number
  body_ids?: string[]
  /** 書き出し候補のうち非表示(visible=false)で除外されたBodyのID(export_step/export_stlのみ)。 */
  skipped_hidden?: string[]
  files?: DocumentInfo[]
  /** 改名前のファイル名(`rename_document` のみ。無題からの改名は null)。 */
  previous?: string | null
  /** サムネイルのPNG(base64。`get_thumbnail` のみ。無ければ null)。 */
  png?: string | null
  /** サムネイルの版(`save_thumbnail` / `get_thumbnail`。無ければ null)。 */
  thumb?: number | null
  /** サムネイルを書いたか(`save_thumbnail` のみ。文書のファイルが無ければ false)。 */
  saved?: boolean
}

/**
 * `models_dir` に置かれた1ファイルの一覧情報(`list_documents` の1件)。
 * 壊れて読めないファイルは一覧に出てこない。
 */
export interface DocumentInfo {
  name: string
  mtime: number
  /** 作成日時(UNIX秒。文書のJSONの `created`)。持たない既存ファイルは null。 */
  created: number | null
  /** サムネイルの版(付随ファイルの更新日時)。撮っていなければ null。画像は `get_thumbnail` で取る。 */
  thumb: number | null
}

/**
 * 操作を出したクライアントへの確認応答(`Applied`)は生成物 `generated/protocol.ts`。
 * 正は `duocad/catalog.py` の `Command.returns` と `CHANGE_EVENTS`。
 * `verb` と `change`(`"selection"` / `"visibility"` / `"document"`。どれも操作ログには載らない)が
 * discriminant。
 *
 * 読むときの注意(値の意味。形は生成物が持つ):
 * - `move_sketch_entities` / `move_body` は `mode` で使う値が変わるが、キーの顔ぶれは同じ
 *   (`'translate'` なら `dx`/`dy`(/`dz`) が入って `angle` と軸・中心が `null`、`'rotate'` はその逆)。
 *   `axis_point` / `axis_dir` / `axis_p0` / `plane_point` / `plane_normal` は解決後の数値で、
 *   指した辺・面・線分の参照は載らない
 * - スケッチを動かす・鏡映する verb は `changed_bodies` を持たない(ソリッドは追従しない)。
 *   `delete` も持たない(消えたボディは `bodies` から居なくなるだけ)
 * - `set_loads` の `face_count` は確定後の荷重面の総数、`applied_count` はこの操作で焼いた枚数。
 *   `direction_vector` は単位化済みで、`direction` が `'normal'` / `'-normal'` のときは `null`
 * - `change: 'document'` の `path` / `name` は、新規(`action: "new"`)のとき `null`
 */

/**
 * プレビュー(投機的実行)の応答。状態の配信ではなく、要求した接続にだけ届く
 * (サーバ側の文書は既に元へ戻っている。`Session.preview`)。
 * `preview_id` は古い応答を捨てるための番号(数値を打ち替えると要求が追い越しうるし、Escで中断した後にも届きうる)。
 */
export interface PreviewPayload {
  preview_id: number | null
  verb: string
  bodies: Array<{
    body_id: string
    name: string
    face_count: number
    volume: number
    geometry: GeometryPayload
  }>
  /**
   * スケッチに足される線の確定前の姿(`catalog.Command.preview_session` の verb だけが載せる)。
   * `pieces` は結果の領域の数(1 以外は確定すると拒まれる)。
   */
  sketch_ghost: {
    sketch_id: string
    entities: Array<{ kind: string; params: Record<string, number> }>
    pieces: number
  } | null
}

/**
 * 1本の辺の折れ線(`kernel.tessellate_edges` の1件そのまま)。
 *
 * `index` はコアの数え方の1始まりで、`fillet(edges=[...])` が食う添字と同じ
 * (ずれると指した辺と違う辺が丸まる。例外も出ない)。
 */
export interface EdgeGeometry {
  index: number
  kind: string
  length: number
  closed: boolean
  points: number[]
}

/**
 * 辺の応答。状態の配信ではない(要求した接続にだけ届く)。
 * `revision` は要求した時点のBodyの版。形が変われば上がるので、古い辺を掴んだままフィレットをかける経路が閉じる(§7)。
 */
export interface EdgesPayload {
  body_id: string
  revision: number
  edges: EdgeGeometry[]
}

/**
 * 辺の連なりの応答(`get_edge_chains`)。頼んだ辺それぞれが属する連なりの添字の全部(昇順)。
 * 連なりはフィレット・面取りで1本として振る舞う辺の組(`duocad/kernel_chains.py`)。`revision` の意味は辺と同じ。
 */
export interface EdgeChainsPayload {
  body_id: string
  revision: number
  chains: number[][]
}

/**
 * 1つの頂点の3D座標(`kernel.vertex_positions` の1件そのまま)。
 *
 * `index` はコアの数え方の1始まり(`count_subshapes(shape, "vertex")`)で、
 * 選択の `ShapeRef`(`kind: "vertex"`)の添字と同じ。
 */
export interface VertexGeometry {
  index: number
  point: number[]
}

/**
 * 頂点の応答。状態の配信ではない(要求した接続にだけ届く)。
 * 全頂点が入っているが、画面に出るのは選択に入った添字だけ。`revision` の意味は `EdgesPayload` と同じ。
 */
export interface VerticesPayload {
  body_id: string
  revision: number
  vertices: VertexGeometry[]
}

// --------------------------------------------------------------------------------------
// G: アプリ内AIエージェント — verbではない
// --------------------------------------------------------------------------------------
/**
 * `measure` の2項目のあいだ(`duocad/measure.py` の `between`)。
 * キーの顔ぶれは組によらず同じ(無い値は null)。距離が出る組は `distance`・`from`・`to`、
 * 平行でない線・面どうしは `angle_deg` だけ、点と点は `delta` も。
 */
export interface MeasureBetween {
  kind: string
  parallel: boolean | null
  distance: number | null
  angle_deg: number | null
  from: number[] | null
  to: number[] | null
  delta: number[] | null
}

/**
 * `measure` の1項目(`duocad/measure.py` の `_measure_item`)。単位は mm / mm² / mm³、座標はワールド。
 * スケッチの `position` / `params` / `center_uv` だけはスケッチ座標 (u, v)。
 */
export type MeasureItem =
  | {
      kind: 'face'
      body: string
      index: number
      surface_kind: string
      area: number
      normal: number[]
      center: number[]
    }
  | {
      kind: 'edge'
      body: string
      index: number
      length: number
      start: number[]
      end: number[]
      edge_kind: string
      /** 円(円弧)の辺だけ。 */
      radius?: number
      center?: number[]
    }
  | { kind: 'vertex'; body: string; index: number; point: number[] }
  | { kind: 'region'; sketch: string; index: number; area: number; center: number[]; center_uv: number[] }
  | { kind: 'body'; body: string; name: string; volume: number; face_count: number; center: number[] | null }
  | {
      kind: 'sketch_entity'
      sketch: string
      entity: string
      entity_kind: EntityKind
      point: string | null
      /** 点を指したときだけ。 */
      position?: number[]
      /** 図形全体を指したときだけ(解決済みの素の数値)。 */
      params?: Record<string, number>
      /** 線分の図形全体を指したときだけ。 */
      length?: number
    }

/** `inspect_selection` の返事(要求した接続にだけ届く)。`item` は選択がちょうど1項目のときだけ。 */
export interface SelectionInfoPayload {
  request_id: number | null
  item: MeasureItem | null
  error: string | null
}

/** `measure` の返事(要求した接続にだけ届く。状態は変わっていない)。 */
export interface MeasurePayload {
  client_op: number | null
  count: number
  items: unknown[]
  /** ちょうど2項目のときだけ付く(`duocad/measure.py` の `measure_selection`)。 */
  between?: MeasureBetween | null
  between_error?: string | null
}

/**
 * 繋げるプロバイダの綴り(`duocad/net/ws.py` の `AI_PROVIDERS` が正)。
 * `ai_*` は文書を直接は変えない。AIがツールで文書を変えたときは通常の `state` 配信として届く
 * (`onState` の `applied` は `null`。`duocad/server.py` のモジュールdocstring)。
 */
export type AiProviderKind = 'openai_compat' | 'anthropic'

/** llama-server へ送る思考の設定(`duocad/agent/providers.py` の `THINKING_CHOICES`)。`server` は何も送らずサーバの既定に任せる。 */
export type AiThinking = 'off' | 'low' | 'medium' | 'server'

/** 画面に出してよい設定の全部。キーの値は載らない(置いてあるか・どこから読んだか・末尾4文字だけ)。 */
export interface AiStatus {
  provider: AiProviderKind
  model: string | null
  /** `anthropic` では常に `null`(送り先は固定)。 */
  baseUrl: string | null
  apiKeyConfigured: boolean
  /** `env` は環境変数(ファイルより優先され、画面からは変えられない)、`file` は `duocad_secrets.json`。 */
  apiKeySource: 'env' | 'file' | null
  /** キーの末尾4文字(どのキーかを見分けるため)。 */
  apiKeyHint: string | null
  running: boolean
  providers: AiProviderKind[]
  /** llama-server へ送る思考の設定(Claude には送らない)。 */
  thinking: AiThinking
  thinkingChoices: AiThinking[]
  /** `ai_configure` / `ai_set_thinking` を拒否したときだけ入る(例: 実行中/未知のプロバイダ)。 */
  error?: string
}

/** 形状最適化の CG を解く装置(`fem_status`。`duocad/net/ws.py` の `_fem_status`、docs/TOPOPT_GPU.md)。 */
export interface FemStatus {
  device: 'cpu' | 'gpu'
  /** GPU の部品(CuPy)が入っているか。 */
  cupyInstalled: boolean
  /** gpu を選んでいて使えるとき、その GPU の名前。 */
  gpuName: string | null
  /** gpu を選んでいるのに使えないとき、その理由(CPU で解いている)。 */
  gpuProblem: string | null
  /** `fem_set_device` を拒否したときだけ入る。 */
  error?: string
}

/** ツール呼び出し1件の進捗(`AgentLoop` の `progress` コールバックそのもの)。`name` は操作ログ・MCPツール名と同じ綴り。 */
export interface AiToolCall {
  name: string
  arguments: Record<string, unknown>
  result: unknown
  ok: boolean
}

/**
 * `run()` が止まった理由(`duocad/agent/loop.py` の `AgentRun.status` そのまま)。
 *
 * - `completed`  … ツールを呼ばずに答えた(正常終了)
 * - `max_turns`  … 往復の上限で打ち切った
 * - `cancelled`  … 中断ボタンで止めた。`tool_calls` にどこまでやったかが残る
 * - `max_tokens` … 出力が上限で切れた。そのターンのツールは実行していない(切れた引数のままでは危険なため)
 * - `refusal`    … プロバイダが安全上の理由で断った
 * - `provider_error` … HTTP・接続の失敗(`detail` に理由。APIキーは含まれない)
 * - `timeout`    … 合計時間の上限で打ち切った。実行済みのツールは `tool_calls` に残る
 * - `context_overflow` … 今の依頼だけで相手の文脈長を超えた
 * - `stopped`    … 上のどれでもない停止理由(`detail` に生値)
 * - `empty_reply` … 最後の応答が本文もツール呼び出しも無いまま返ってきた(結論を出さずに止まった)。それまでの往復で
 *   実行したツールは `tool_calls` に残る
 */
export type AiRunStatus =
  | 'completed'
  | 'empty_reply'
  | 'max_turns'
  | 'cancelled'
  | 'max_tokens'
  | 'refusal'
  | 'provider_error'
  | 'timeout'
  | 'context_overflow'
  | 'stopped'

export interface AiRunResult {
  status: AiRunStatus
  text: string
  detail: string
  turns: number
  tool_calls: Array<{ name: string; arguments: Record<string, unknown>; ok: boolean }>
  usage: Record<string, number>
}

/**
 * `AiPanel.svelte` が描く1本の時系列。コアには無い(`duocad/agent/loop.py` の `turns` はWSへ出さない)。
 * 再読込で消えるのは「途中まで入力していたコマンド」と同じ扱いで許容(docs/DESIGN_DECISIONS.md「外してはいけない3つ」の3)。
 */
export type AiTranscriptEntry =
  | { kind: 'user'; text: string }
  | { kind: 'tool'; call: AiToolCall }
  | { kind: 'result'; result: AiRunResult }
  | { kind: 'error'; message: string }

// --------------------------------------------------------------------------------------
// H: 形状最適化を走らせる — verbではない(`duocad/catalog.py` の `category="protocol"`)
// --------------------------------------------------------------------------------------
/**
 * 走らせる前の点検(`duocad/optimize_run.py` の `conditions_report`)。
 * `problems` はそのまま人に見せる文で、画面が組み立て直さない(理由と次の一手が入っている。
 * 画面で文言を作るとコアの制限が変わっても古い説明を出し続ける。
 * `docs/contracts/ui-optimize-integration.md`「最適化の配線」)。
 */
export interface OptimizeConditionsReport {
  ready: boolean
  problems: string[]
  preserve_bodies: string[]
  exclude_bodies: string[]
  fixtures: Array<Record<string, unknown>>
  loads: Array<Record<string, unknown>>
}

/**
 * 解像度の目盛り1段。添字0がいちばん粗い(＝既定)で、進むほど細かい。
 * 所要時間は付かない(走らせる前は実測が無いので、要素数だけで判断する)。
 */
export interface OptimizeResolutionStep {
  index: number
  element_size_mm: number
  /** 設計空間の要素数の見積もり(領域の体積 ÷ 要素の体積。実数ではない)。 */
  design_elements_estimate: number
  /** この段を選んだときの最小幅の下限[mm](= 2mm 以上で最小の m(m ≥ 2)× 要素サイズ)。サーバーが計算する(`optimize_run.min_width_cell_range`)。 */
  min_width_floor_mm: number
  /** この段を選んだときの最小幅の上限[mm](= floor(格子の最短の辺のセル数 × 1/4)× 要素サイズ)。下限を下回れば選べる段は無い。 */
  min_width_ceiling_mm: number
  /** この段で選べる最小幅の段(下限の m から上限の m までの `m × 要素サイズ`)。渡すと、ちょうどその m になる(`optimize_run.min_width_steps_mm`)。 */
  min_width_steps_mm: number[]
}

/**
 * 要素サイズの上限・下限・目盛り(`optimize_run.resolution_limits`)。
 * `error` が入っていたら他のキーは無い(条件面がまだ1枚も無い、旧引数 `rmin_mm` が届いた等、
 * 「この版では見積もれない」状態。サーバは赤い札にせずここへ入れて返す)。
 */
export interface OptimizeResolutionPayload {
  error?: string
  max_element_size_mm?: number
  min_element_size_mm?: number
  default_element_size_mm?: number
  from_condition_faces_mm?: number
  /** 要素サイズの上限の固定値(`optimize_run.MAX_ELEMENT_SIZE_MM`)。上限は条件面からの要求とこれの細かい方。 */
  max_element_size_cap_mm?: number
  /** `optimizeResolution(値)` で問い合わせた値(自由入力)。`selected`・`selected_error` はこの値についてのもの。 */
  requested_element_size_mm?: number
  /** 問い合わせた値を選んだときの段(実格子へ揃えた要素サイズ。`index` は -1)。 */
  selected?: OptimizeResolutionStep
  /** 問い合わせた値が範囲外・不正だった理由(人に見せる文)。 */
  selected_error?: string
  max_design_elements?: number
  feasible?: boolean
  steps?: OptimizeResolutionStep[]
}

/**
 * 進捗1件(`optimize_run.run()` の `progress`)。`phase` ごとにキーが増える
 * (`docs/contracts/ui-optimize-integration.md`「最適化の配線」)ので、共通の2つだけを必須にする。
 *
 * `phase: 'search'` は探索を始めるときの1件に続けて、評価(FEM 1回)ごとに1件来る
 * (`evaluation` 以下のキーを持つ。`binary_search._reduce_volume` の `on_evaluation`)。
 * 安全率は探索の格子での値で、比べる相手は `target_safety_factor`(探索時の目標)。
 */
export interface OptimizeProgress {
  phase: string
  message: string
  design_elements?: number
  evaluation?: number
  outcome?: 'initial' | 'accepted' | 'rejected'
  failed?: string[]
  candidate_safety_factor?: number | null
  candidate_cells?: number
  safety_factor?: number
  cells?: number
  target_safety_factor?: number
  /** 合格形が増えたとき(`outcome` が `'initial'` / `'accepted'`)だけ、そのステップ。 */
  step?: OptimizeStepMeta
  /** 実行の識別子(`OptimizeStepMeta.run` と同じ)。前の実行の進捗が後から届いたら画面が捨てる。 */
  run?: string
}

/**
 * 探索の合格形1つ(ステップ。`duocad/optimize_steps.py` の `OptimizationSteps.append`)。`index` は 0 から。
 * `safety_factor` は探索の格子での値(探索の目標と比べる)、`seconds` は実行の開始からの秒。
 */
export interface OptimizeStepMeta {
  /** 実行の識別子。前の実行の遅れた返事を見分ける。 */
  run: string
  index: number
  evaluation: number
  seconds: number
  material_mm3: number
  safety_factor: number
  cells: number | null
}

/** ステップ1つの形(表面のセル。`OptimizationSteps.voxels`)。`error` があれば形は無い。 */
export interface OptimizeStepVoxels extends Partial<OptimizeStepMeta> {
  index: number
  grid?: OptimizeDebugGrid
  elements?: number[]
  total?: number
  shown?: number
  stride?: number
  error?: string
}

/**
 * 実行の結果。`state` で分岐する(`docs/contracts/ui-optimize-integration.md`「最適化の配線」)。
 * `no_feasible_found` / `input_contradiction` / `resolution_insufficient` / `boundary_mapping_failed` は
 * `binary_search.SearchResult.status` をそのまま渡したもの(`docs/TOPOPT.md`「状態・最初の合格モデル・島除去・応力評価」。
 * `boundary_mapping_failed` は探索の前の確定表の検証からも来る)。
 * `"done"` 以外では文書は1ビットも変わっていない。
 */
export interface OptimizeResultPayload {
  state:
    | 'done'
    | 'empty_condition_faces'
    | 'unrepresented_regions'
    | 'boundary_mapping_failed'
    | 'no_feasible_found'
    | 'input_contradiction'
    | 'resolution_insufficient'
    /** 探索後に平滑化で潰れる 1×1 の空隙を埋めた形が6条件に不合格(`optimize_run` の `thin_voids`)。 */
    | 'fill_infeasible'
    /** 完成形の表面の応力(面積の上位値)で見た安全率が本来の目標を割る(結果は足さない。#129)。 */
    | 'final_unsafe'
  warning: string | null
  body_id: string | null
  name: string | null
  triangle_count: number | null
  vertex_count: number | null
  safety_factor: number | null
  /** 探索が合格させた形の安全率(空隙を埋める前)。埋めなかったときは `safety_factor` と同じ。 */
  search_safety_factor?: number | null
  /** 探索後に埋めた 1×1 の空隙のセル数。 */
  filled_cells?: number | null
  /** 完成形(平滑化・保持/除外の後)を細かい格子で解き直した、部材域の表面の応力(面積の上位値)での安全率。測れなかったときと、完成形まで進まなかったときは `null`。 */
  final_safety_factor?: number | null
  /** 完成形の検査(`topopt.final_check.inspect`)。完成形まで進まなかったときは `null`。 */
  final_check?: FinalCheck | null
  /** 全材料が安全率だけで落ちたとき、探索を始められる荷重(荷重の大きさの合計 × `factor`)。それ以外は `null`。文面は `warning` にもある。 */
  full_material_limit_load?: {
    factor: number
    load_total_n: number
    limit_load_total_n: number
    safety_factor: number
    search_target_safety_factor: number
  } | null
  /** 探索の最終形が完成形検査で目標を割り、探索の合格形をさかのぼったときの記録(`topopt.complete.backtrack`)。
   *  `steps_back` は足した形が最終形から何手前か(どれも通らなければ `null` で、結果は足さない)。文面は `warning` にもある。 */
  backtrack?: {
    reason: 'passed' | 'exhausted' | 'check_limit' | 'nothing_to_go_back'
    steps_back: number | null
    checks: Array<{
      index: number
      steps_back: number
      search_safety_factor: number
      material_mm3: number
      final_safety_factor: number | null
      verdict: string
    }>
    first_final_safety_factor: number | null
    first_material_mm3: number
  } | null
  /** 利用者が指定した実際の目標。 */
  target_safety_factor: number
  /** 探索に渡した目標(= `target_safety_factor` × `search_target_factor`)。`safety_factor` はこれに対する探索の格子での値。 */
  search_target_safety_factor: number
  search_target_factor: number
  material_mm3: number | null
  evaluations: number
  /** 探索の終了理由(`converged` / `evaluation_limit` / `stopped_by_user`(仕上げで止めた)/ `finished_after_run`(実行の後に仕上げた)/ ...)。 */
  termination: string | null
  /** 仕上げたステップの番号と、その実行のステップの数(`state` が `done` / `final_unsafe` / `fill_infeasible` のとき)。 */
  finished_step?: number
  steps?: number
  element_size_mm: number
  /** 最小幅の解決結果(`optimize_run._resolve_min_width`)。未指定なら選べるいちばん細い段を採用し `source` が `'default'`
   *  (`requested_mm` は既定のときも実際に使った値で `null` にならない)。明示すれば `'requested'`。 */
  min_width: {
    requested_mm: number
    spacing_mm: number
    cells: number
    guaranteed_mm: number
    source: 'default' | 'requested'
  }
  design_elements: number
  seconds: number
  empty_condition_faces?: Array<Record<string, unknown>>
  unrepresented_regions?: Array<Record<string, unknown>>
  /** 確定表の検証(`anchor.condition_mapping`)。確定表を作る前に返った状態では `null`。 */
  boundary_mapping?: Record<string, unknown> | null
}

/** 完成形の検査(`duocad/topopt/final_check.py`)。つなぎ目は診断であって保証ではない(適合は出さない)。 */
export interface FinalCheck {
  safety_factor: number | null
  neck: NeckDiagnosis
}

/** つなぎ目の幅の診断(`duocad/topopt/neck_persistence.py`、docs/TOPOPT_MIN_WIDTH.md 3章)。 */
export interface NeckDiagnosis {
  status: 'suspect' | 'undetermined' | 'not_observed'
  required_mm: number
  sampling_mm: number
  epsilon_mm: number
  events: Array<{
    kind: 'loop_neck' | 'tree_neck'
    diameter_mm: [number, number]
    lifetime_mm: number | null
    verdict: 'suspect' | 'undetermined'
    reason: string | null
    position_mm: [number, number, number]
  }>
  micro_pieces: number
  thinning_reference: Array<{ diameter_mm: number; position_mm: number[] }>
  /** 端の参考値(探針の最小の直径)。探針ゼロなら `null`。 */
  end_probe_mm: number | null
  reason: string | null
}

/** 実行の摘み。省いたものはコアの既定(いちばん粗くて安い設定)。 */
export interface OptimizeRunOptions {
  elementSizeMm?: number | null
  /** 最小幅[mm](選んだ段の値)。`null` または省略 = 既定(選べるいちばん細い段)をサーバーが使う。 */
  minWidthMm?: number | null
  /**
   * 開発用の可視化を頼む。既定は空で、空のときサーバは配列を1本も作らない。
   * 頼んだぶんが `onOptimizeDebug` へ届く。決めるのは `debugFlags.ts`(URLパラメータ)。
   */
  debug?: OptimizeDebugKind[]
}

/** 開発用の可視化の種類(`duocad/optimize_run.py` の `DEBUG_KINDS`)。 */
export type OptimizeDebugKind = 'space' | 'anchors' | 'rho'

/** 格子(`duocad/topopt/voxelize.py` の `Grid.as_dict`)。セル中心の規約の変換点は `optimizeDebug.ts` の `buildDebugVoxels` だけ。 */
export interface OptimizeDebugGrid {
  origin: number[]
  spacing: number
  shape: number[]
}

/**
 * 開発用の可視化1件(`duocad/optimize_run.py` の `_DebugSink`)。頼んだときしか来ない。
 * `stage` は `'derive'`(導出直後の塊。1回しか届かない)、`'mapping'`(境界条件の確定直後の面アンカー。
 * 探索・FEMが実際に使う確定表の節点)、`'final'`(探索の後の、実際に解いた密度場 `rho`)。
 * 同じ `kind` が届いたら後から来たほうで置き換えること。
 * `elements` は立っている要素の通し添字(C順 `i*(ny*nz) + j*nz + k`)。
 * `total` が本当の数、`shown` が配った数、`stride` が間引きの幅(1なら間引いていない)。
 */
export interface OptimizeDebugPayload {
  kind: OptimizeDebugKind
  stage: 'derive' | 'mapping' | 'final'
  grid: OptimizeDebugGrid
  /** `kind` が `'space'` / `'rho'` のとき。 */
  elements?: number[]
  total?: number
  shown?: number
  stride?: number
  threshold?: number
  /** `kind` が `'anchors'` のとき。`points` はワールドmmの平坦な [x,y,z,...]。 */
  fixed?: OptimizeDebugAnchor
  load?: OptimizeDebugAnchor
}

export interface OptimizeDebugAnchor {
  /** その側の条件面すべて(拘束側なら全拘束面)。点群は側ごとに1つへまとめてある。 */
  faces: number[]
  count: number
  points: number[]
  area_mm2: number
}

export type ConnectionStatus = 'connecting' | 'open' | 'closed'

export interface SessionHooks {
  /**
   * `changed` は今回の版で届いたBodyだけ(geometry付き。非表示になったものは
   * `geometry: null`)。それ以外のBodyは前回のまま描き直さない(差分更新)。
   */
  onState(state: ServerState, changed: BodyGeometry[], applied: Applied | null): void
  /** `clientOp` は失敗した要求に付いていた番号(サーバの `error` だけが持つ。送れなかった・切れたときは無い)。 */
  onError(message: string, where: string, clientOp?: number | null): void
  onWarning(message: string): void
  onStatus(status: ConnectionStatus): void
  /**
   * ビューポートの変化を待たせている下りの、いちばん古い送信時刻(`performance.now()`)。無くなれば null。
   * 待たせるかどうかは送る口ごとに決めてある(`send` の `wait`)。
   */
  onWaiting(oldestSentAt: number | null): void
  /** プレビューが返ってきた(状態は変わっていない)。 */
  onPreview(payload: PreviewPayload): void
  /** 頼んだ辺が返ってきた(状態は変わっていない)。 */
  onEdges(payload: EdgesPayload): void
  /** 頼んだ辺の連なりが返ってきた(状態は変わっていない)。 */
  onEdgeChains(payload: EdgeChainsPayload): void
  /** 頼んだ頂点が返ってきた(状態は変わっていない)。 */
  onVertices(payload: VerticesPayload): void
  /** 測った(状態は変わっていない)。要求した接続にだけ届く。 */
  onMeasure(payload: MeasurePayload): void
  /** 選択情報の表示の返事(状態は変わっていない)。要求した接続にだけ届く。 */
  onSelectionInfo(payload: SelectionInfoPayload): void
  /** ファイルを書いた(状態は変わっていない)。 */
  onFile(payload: FilePayload): void
  // ---------------------------------------------------------------- G: アプリ内AIエージェント
  /** 設定が変わった/`ai_get_status` の返事/実行の開始・終了で `running` が変わったとき。 */
  onAiStatus(status: AiStatus): void
  /** ツールを1つ呼んだ(進行中に逐次届く)。 */
  onAiToolCall(call: AiToolCall): void
  /** `run()` が止まった(理由は `status`)。 */
  onAiResult(result: AiRunResult): void
  /** 空メッセージ・実行中の2通目・プロバイダ未設定など、繋がらない理由。 */
  onAiError(message: string): void
  // ---------------------------------------------------------------- H: 形状最適化を走らせる
  // ---------------------------------------------------------------- I: 計算の設定
  /** 装置の設定が変わった/`fem_get_status` の返事。 */
  onFemStatus(status: FemStatus): void
  /** 走らせる前の点検が返ってきた(要求した接続にだけ届く)。 */
  onOptimizeConditions(report: OptimizeConditionsReport): void
  /** 解像度の上限・下限・目盛りが返ってきた(要求した接続にだけ届く)。 */
  onOptimizeResolution(payload: OptimizeResolutionPayload): void
  /** 走っているかどうかが変わった(全接続へ配られる)。`finishing` は「このステップで仕上げる」を伝えた後。 */
  /** `available` はこの版で形状最適化を使えるか(安定版は false。`duocad/channel.py` の `HIDDEN_IN_STABLE`)。 */
  onOptimizeStatus(running: boolean, cancelling: boolean, finishing: boolean, available: boolean): void
  /** いま開いている文書の直近の実行のステップの一覧(`optimizeSteps()` の返事)。`finishable` は実行の後に仕上げられるか。 */
  onOptimizeSteps(steps: OptimizeStepMeta[], finishable: boolean): void
  /** ステップ1つの形(`optimizeStepVoxels()` の返事)。 */
  onOptimizeStepVoxels(payload: OptimizeStepVoxels): void
  /** 進捗1件(反復ごと・候補材料量ごと)。 */
  onOptimizeProgress(event: OptimizeProgress): void
  /** 開発用の可視化が届いた(頼んだときだけ。`OptimizeDebugPayload`)。 */
  onOptimizeDebug(payload: OptimizeDebugPayload): void
  /** 走り終わった。`state` が "done" でなければ文書は変わっていない。 */
  onOptimizeResult(result: OptimizeResultPayload): void
  /**
   * 走らせられなかった / 途中で落ちた / 中断した。`problems` は条件が足りないときだけ入る。
   * `cancelled` は失敗ではない(人が中断を押した結果)。
   * `alreadyRunning` は「別の実行が走っているので断った」で、走っている実行の表示は変えない。
   */
  onOptimizeError(message: string, problems: string[], cancelled: boolean, alreadyRunning: boolean): void
}

export interface Session {
  start(): void
  /**
   * コマンドを1つ送る。文書を変える verb に限らない(保存・改名・一覧などファイル操作もここを通る)。
   * 引数のdictをそのまま流す(`cmd` と `client_op` を足すだけ)。引数を作るのは `CommandDef.build` の仕事で、
   * verbごとにメソッドを書き写すと、キー名を打ち間違えた経路がそのverbでだけ静かに壊れる。
   * 受け取れるverbの一覧はコアの宣言(`duocad/catalog.py` の `COMMANDS`)が正で、無い文字列は `unknown command` で返る。
   */
  sendCommand(cmd: string, args: Record<string, unknown>, clientOp: number): void
  setVisible(bodyId: string, visible: boolean, clientOp: number): void
  setSketchVisible(sketchId: string, visible: boolean, clientOp: number): void
  /** 差分編集(足す・外す)。 */
  modifySelection(add: SelectionRef[], remove: SelectionRef[], clientOp: number): void
  /** 参照ごとに、あれば外し無ければ足す(素のクリック。AI の `toggle_selection` と同じ操作)。 */
  toggleSelection(refs: SelectionRef[], clientOp: number): void
  clearSelection(clientOp: number): void
  // ---------------------------------------------------------------- B2: 作業空間(起動中のコマンド・モード。issue #153)
  /**
   * verb を起動して引数を集め始める(ツールバーのクリック)。応答は状態配信の `command`。AI の `begin_command` と同じ操作で、
   * 画面は起動中の verb を自分で持たず、配信された `command` に従う(`CommandHost.svelte`)。
   */
  beginCommand(verb: string, args: Record<string, unknown>, clientOp: number): void
  /** 起動中の verb の引数を併合する(入力欄の値。`null` で外す)。 */
  setCommandArgs(args: Record<string, unknown>, clientOp: number): void
  /** 起動中の verb を確定せずに畳む(Esc)。選択も空になる(サーバの規則)。 */
  endCommand(clientOp: number): void
  /** 既存のスケッチの編集モードへ入る(応答は状態配信の `mode`)。 */
  enterSketch(sketchId: string, clientOp: number): void
  leaveSketch(clientOp: number): void
  enterOptimize(clientOp: number): void
  leaveOptimize(clientOp: number): void
  /**
   * verbを投機的に実行して形だけ測らせる。文書は変わらない(サーバが元へ戻す)。
   * 応答は `onPreview` へ。`previewId` は古い応答を捨てるための番号。
   */
  preview(verb: string, args: Record<string, unknown>, previewId: number): void
  // ---------------------------------------------------------------- D: フィーチャ
  /**
   * 1Bodyの辺を折れ線で取りに行く(応答は `onEdges`)。状態配信ではない。
   * フィレット・面取りを起動している間だけ呼ぶ(常時配ると27部品ぶんの辺が乗る。`Session.edges` のdocstring)。
   */
  getEdges(bodyId: string): void
  /**
   * 辺の連なりを取りに行く(応答は `onEdgeChains`)。状態配信ではない。連なりは辺の折れ線に入れず、
   * フィレット・面取りで指した辺の分だけ頼む(全辺ぶんは大きな部品で 0.5 秒かかる。`DocumentView.edge_chains`)。
   */
  getEdgeChains(bodyId: string, edges: number[]): void
  /**
   * 1Bodyの頂点の座標を取りに行く(応答は `onVertices`)。状態配信ではない。
   * 呼ぶのは選択に頂点が入っているときだけ(`duocad/server.py` の `Session.vertices`)。
   */
  getVertices(bodyId: string): void
  /** 選択がちょうど1項目ならその寸法を取りに行く(応答は `onSelectionInfo`)。状態配信ではない。 */
  inspectSelection(requestId: number): void
  /**
   * 画面の見え方をサーバへ知らせ、ズームに合わせた粗さでソリッド・辺・領域を測り直させる(可変解像度)。
   * 応答は通常の状態配信で、変わらなければ何も来ない。
   * 導出済みの偏差ではなくカメラの事実を送る: 「画面の許容誤差 → 目標偏差」の変換式は
   * `web/src/adaptiveResolution.ts` と `duocad/resolution.py` の両方にあり(同じ定数・検査あり)、
   * 偏差を送るとサーバが任意の粗さをクライアントから押し込まれる。
   * 呼ぶのは `viewport.onResolutionChange` の中だけ(ズーム中の連打はそちらが止める)。
   *
   * @param worldPerPixel 画面1画素ぶんの世界長さ[mm]
   * @param sizeHint いま見えているものの外接対角[mm](上下限の丸めにだけ使う)
   */
  setViewResolution(worldPerPixel: number, sizeHint: number, focus?: Record<string, number[] | null> | null): void
  undo(): void
  redo(): void
  // ---------------------------------------------------------------- G: アプリ内AIエージェント
  /** 今の設定(応答は `onAiStatus`)。 */
  aiGetStatus(): void
  /** プロバイダ・エンドポイントを差し替える。**キーは渡さない**(渡す経路が無い)。
   * モデルは送らない —— llama-server は1モデルで起動すると `model` を無視するので、
   * サーバ側の既定値に任せる(`ai_configure` 自体は `model` を受け付けたまま)。 */
  aiConfigure(provider: AiProviderKind, baseUrl?: string): void
  /** 思考の設定を差し替える。会話は保ったまま次の往復から効く(`ai_configure` と違い会話を作り直さない)。 */
  aiSetThinking(thinking: AiThinking): void
  /** ClaudeのAPIキーを書く(`null` なら消す)。返事は `ai_status`(値は載らない)。書くと Claude に切り替わる。 */
  aiSetApiKey(key: string | null): void
  /** ツール定義を先に読ませておく(返事は無い。1つの会話につき1回だけ効く)。 */
  aiPrepare(): void
  /** 1つの発話でエージェントを起こす。**待たずに返る** —— 進捗は `onAiToolCall`、
   * 最終応答は `onAiResult` へ届く(`duocad/server.py` の `_ai_start` のdocstring)。 */
  aiSend(message: string): void
  /** 中断する。走っているツールの結果は `onAiResult` の `tool_calls` に残る。 */
  aiCancel(): void
  // ---------------------------------------------------------------- I: 計算の設定
  /** 今の装置の設定(応答は `onFemStatus`)。 */
  femGetStatus(): void
  /** 装置を選ぶ。次に準備する計算から効き、この clone の設定ファイルに残る。 */
  femSetDevice(device: 'cpu' | 'gpu'): void
  // ---------------------------------------------------------------- H: 形状最適化を走らせる
  /** 4条件がそろって走らせられるかを聞く(応答は `onOptimizeConditions`)。 */
  optimizeConditions(): void
  /**
   * 要素サイズの上限・下限・目盛りを聞く(応答は `onOptimizeResolution`)。
   * **軽い**(条件面のbboxを数枚測るだけ)ので、条件が変わるたびに呼んでよい。
   */
  /** 要素サイズの上限・下限・目盛り。`elementSizeMm` を渡すとその値の段(`selected`)も返る。 */
  optimizeResolution(elementSizeMm?: number | null): void
  /**
   * 走らせる。**待たずに返る** —— 進捗は `onOptimizeProgress`、結果は `onOptimizeResult`
   * へ届き、結果が文書へ入ったことは通常の状態配信として別途来る。
   */
  runOptimization(options: OptimizeRunOptions): void
  /**
   * 中断の旗を立てる。**実際に降りるのは次に進捗が出た時点**で、
   * 進捗を出さない短い処理の途中では止まらない(`docs/contracts/ui-optimize-integration.md`「最適化の配線」)。
   */
  cancelOptimization(): void
  /**
   * 「このステップで仕上げる」。走っている間は探索を止めて `step`(`null` は最新)を仕上げる(降りるのは次の候補を作る前)。
   * 走っていなければ、いま開いている文書の直近の実行のステップを、探索をやり直さずに仕上げて結果を足す。
   */
  finishOptimization(step: number | null, debug?: OptimizeDebugKind[]): void
  /** いま開いている文書の直近の実行のステップの一覧を聞く(応答は `onOptimizeSteps`)。 */
  optimizeSteps(): void
  /** ステップ1つの形を聞く(応答は `onOptimizeStepVoxels`)。 */
  optimizeStepVoxels(index: number): void
  dispose(): void
}

/** スケッチ平面上の点 [u, v](mm)。 */
export type Point2 = [number, number]

const RECONNECT_DELAY_MS = 2000
/**
 * `sendCommand` で送るもののうち、結果がビューポートに出ないもの(待ちバーを出さない)。
 * ファイルパネル・一覧・書き出し・サムネイルの往復で、人はビューポートの変化を待っていない。
 */
const NOT_SHOWN_IN_VIEWPORT = new Set([
  'list_documents',
  'save_document',
  'rename_document',
  'copy_document',
  'delete_document',
  'export_step',
  'export_stl',
  'save_thumbnail',
  'get_thumbnail',
])
/**
 * 繋がらないことをトーストで言うまでの連続失敗回数。
 *
 * **試行のたびには出さない**。切断は黙って自動再接続し、**開けなかった試行**だけを数える。
 * 一度も繋がっていない間は3回目、一度繋がったあとの再接続失敗は1回目 —— サーバより先に
 * タブを開いたとき、黙ったままでは繋がらない理由が画面のどこにも出ない。
 *
 * **数えるのは回数であって時間ではない。** 試行の間隔は `RECONNECT_DELAY_MS` では
 * 決まらない —— 繋がらないソケットが閉じるまでに実測2秒かかり、さらにChromeが同じ
 * ホストへの失敗を重ねると自前で遅らせる(実測: 開いたばかりの画面で5秒周期、
 * 失敗を積んだ画面では26秒周期)。時間で切ると、この幅がそのまま無音の幅になる。
 */
const CONNECT_FAILURE_NOTICE_AT = 3
/** 一度でも繋がったあと、再接続が開けなかったときは1回目で知らせる(初回接続待ちは3回)。 */
const RECONNECT_FAILURE_NOTICE_AT = 1

/**
 * 一度サーバから教わった WebSocket の URL。**繋ぎ直しはこれを使い、設定を読み直さない** —— 読み直しに失敗して既定(開発版の 8765)へ
 * 落ちると、別の版のサーバへ繋ぎに行く(開発版は安定版の画面の Origin を断るので普通は繋がらないが、繋ぎ先が変わりうると
 * 形状最適化を使えるかの答え(`optimize_status`)を接続をまたいで持てない。sol の指摘)。
 */
let knownWebSocketUrl: string | null = null

async function resolveWebSocketUrl(): Promise<string> {
  if (knownWebSocketUrl !== null) return knownWebSocketUrl
  // サーバが `/duocad-config.json` で自分のWebSocketのURLを教える。
  // ポートを推測しない(静的配信とWebSocketでポートが別であり、既定値を2箇所に書くと必ずずれる)。
  try {
    const response = await fetch(new URL('duocad-config.json', location.href), {
      cache: 'no-store',
    })
    if (response.ok) {
      const config = (await response.json()) as { ws_url?: string }
      if (config.ws_url) {
        knownWebSocketUrl = config.ws_url
        return config.ws_url
      }
    }
  } catch {
    // 取れなければ既定へ落ちる(dev サーバから開いた場合など)
  }
  return `ws://${location.hostname || '127.0.0.1'}:8765`
}

export function createSession(hooks: SessionHooks): Session {
  let socket: WebSocket | null = null
  let disposed = false
  let retry: ReturnType<typeof setTimeout> | null = null
  /** 開く前に閉じた回数。開いたら0へ戻す。 */
  let failures = 0
  /** このタブで一度でも `open` したか(初回接続待ちと再接続失敗でトーストの出し方を分ける)。 */
  let everOpened = false
  /** ビューポートの変化を待たせている下り(`ack_id` -> 送った時刻)。挿入順が番号順・時刻順。 */
  const waits = new Map<number, number>()
  let lastAckId = 0
  /** この接続で受け取ったもの(省かれて届いた面・ログ・スケッチの埋め戻し)。繋ぎ直すたびに作り直す(サーバも忘れる)。 */
  let delivered = createDelivered()

  function reportWaits(): void {
    hooks.onWaiting(waits.size === 0 ? null : waits.values().next().value!)
  }

  /**
   * 1本送る。**繋がっていなければ送らずに、その場でトーストへ回す**。
   *
   * ヘッダの常設ステータスは無いので、**繋がっていないことに人間が
   * 気づく機会は「操作したのに送れなかった」この瞬間**になった。ここは画面からの操作が
   * 必ず通る1本道なので、各パネルに同じ判定を書き写さずに済む。
   *
   * **間引かない。** 同じ文言が続けて来ても、まとめるのはトースト側(`Toast.svelte` の
   * 「同じ文言は積まずに数える」)であって、送る側が黙る話ではない —— ここで最初の1回だけ
   * にすると、**2回目の操作が黙って消える**(押した本人には成功と区別がつかない)。
   */
  function send(payload: Record<string, unknown>, wait = false): void {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      hooks.onError('サーバに繋がっていないので操作を送れなかった(自動で繋ぎ直しを試みている)', 'send')
      return
    }
    if (!wait) {
      socket.send(JSON.stringify(payload))
      return
    }
    lastAckId += 1
    socket.send(JSON.stringify({ ...payload, ack_id: lastAckId }))
    waits.set(lastAckId, performance.now())
    reportWaits()
  }

  function handle(raw: string | ArrayBuffer): void {
    let message: {
      type?: string
      state?: WireState
      delta?: { bodies?: BodyGeometry[] }
      applied?: Applied
      message?: string
      where?: string
      preview_id?: number | null
      verb?: string
      bodies?: PreviewPayload['bodies']
      sketch_ghost?: PreviewPayload['sketch_ghost']
      body_id?: string
      revision?: number
      edges?: EdgeGeometry[]
      chains?: number[][]
      vertices?: VertexGeometry[]
      action?: string
      path?: string
      source?: string
      deviation?: number
      body_ids?: string[]
      skipped_hidden?: string[]
      files?: DocumentInfo[]
      previous?: string | null
      png?: string | null
      thumb?: number | null
      saved?: boolean
      // ---------------------------------------------------------------- G
      provider?: AiProviderKind
      model?: string | null
      base_url?: string | null
      api_key_configured?: boolean
      api_key_source?: 'env' | 'file' | null
      api_key_hint?: string | null
      running?: boolean
      providers?: AiProviderKind[]
      thinking?: AiThinking
      thinking_choices?: AiThinking[]
      error?: string
      name?: string
      arguments?: Record<string, unknown>
      result?: unknown
      ok?: boolean
      status?: AiRunStatus
      detail?: string
      turns?: number
      tool_calls?: Array<{ name: string; arguments: Record<string, unknown>; ok: boolean }>
      usage?: Record<string, number>
      // ---------------------------------------------------------------- I
      device?: 'cpu' | 'gpu'
      cupy_installed?: boolean
      gpu_name?: string | null
      gpu_problem?: string | null
      text?: string
      // ---------------------------------------------------------------- H
      // **形状最適化の応答は丸ごと型変換して渡す**(`optimize_result` だけで20キー近くあり、
      // ここへ書き写すと同じ宣言が2箇所になる)。分岐に要るキーだけをここに置く。
      cancelling?: boolean
      finishing?: boolean
      available?: boolean
      cancelled?: boolean
      already_running?: boolean
      problems?: string[]
      steps?: unknown[]
      finishable?: boolean
      // ---------------------------------------------------------------- 測定
      client_op?: number | null
      count?: number
      items?: unknown[]
      between?: MeasureBetween | null
      between_error?: string | null
      // ---------------------------------------------------------------- 選択情報
      request_id?: number | null
      item?: MeasureItem | null
      // ---------------------------------------------------------------- 待ちバー
      ack_id?: number
    }
    try {
      message = typeof raw === 'string' ? JSON.parse(raw) : (decodeFrame(raw) as typeof message)
    } catch (error) {
      hooks.onError(String(error), 'parse')
      return
    }
    if (message.type === 'state' && message.state) {
      const bodies = message.delta?.bodies ?? []
      let state: ServerState
      try {
        state = delivered.restore(message.state, bodies)
      } catch (error) {
        if (!(error instanceof DeliveryMismatch)) throw error
        // 起きないはずの食い違い。繋ぎ直せばサーバも写しも忘れ、全部を送り直してもらえる
        hooks.onError(`${error.message}(繋ぎ直す)`, 'wire')
        socket?.close()
        return
      }
      hooks.onState(state, bodies, message.applied ?? null)
    } else if (message.type === 'preview') {
      hooks.onPreview({
        preview_id: message.preview_id ?? null,
        verb: message.verb ?? '',
        bodies: message.bodies ?? [],
        sketch_ghost: message.sketch_ghost ?? null,
      })
    } else if (message.type === 'edges') {
      hooks.onEdges({
        body_id: message.body_id ?? '',
        revision: message.revision ?? 0,
        edges: message.edges ?? [],
      })
    } else if (message.type === 'edge_chains') {
      hooks.onEdgeChains({
        body_id: message.body_id ?? '',
        revision: message.revision ?? 0,
        chains: message.chains ?? [],
      })
    } else if (message.type === 'vertices') {
      hooks.onVertices({
        body_id: message.body_id ?? '',
        revision: message.revision ?? 0,
        vertices: message.vertices ?? [],
      })
    } else if (message.type === 'measure') {
      hooks.onMeasure({
        client_op: message.client_op ?? null,
        count: message.count ?? 0,
        items: message.items ?? [],
        between: message.between,
        between_error: message.between_error,
      })
    } else if (message.type === 'selection_info') {
      hooks.onSelectionInfo({
        request_id: message.request_id ?? null,
        item: message.item ?? null,
        error: message.error ?? null,
      })
    } else if (message.type === 'file') {
      hooks.onFile({
        action: message.action ?? '',
        path: message.path ?? '',
        name: message.name,
        source: message.source,
        deviation: message.deviation,
        body_ids: message.body_ids,
        skipped_hidden: message.skipped_hidden,
        files: message.files,
        previous: message.previous,
        png: message.png,
        thumb: message.thumb,
        saved: message.saved,
      })
    } else if (message.type === 'done') {
      // サーバは1接続を直列に捌くので、この番号以下は全部済んでいる。
      const done = message.ack_id ?? 0
      for (const ackId of waits.keys()) if (ackId <= done) waits.delete(ackId)
      reportWaits()
    } else if (message.type === 'error') {
      hooks.onError(message.message ?? '(理由なし)', message.where ?? '?', message.client_op ?? null)
    } else if (message.type === 'warning') {
      hooks.onWarning(message.message ?? '')
    } else if (message.type === 'ai_status') {
      hooks.onAiStatus({
        provider: message.provider ?? 'openai_compat',
        model: message.model ?? null,
        baseUrl: message.base_url ?? null,
        apiKeyConfigured: message.api_key_configured ?? false,
        apiKeySource: message.api_key_source ?? null,
        apiKeyHint: message.api_key_hint ?? null,
        running: message.running ?? false,
        providers: message.providers ?? ['openai_compat'],
        thinking: message.thinking ?? 'server',
        thinkingChoices: message.thinking_choices ?? [],
        error: message.error,
      })
    } else if (message.type === 'fem_status') {
      hooks.onFemStatus({
        device: message.device ?? 'cpu',
        cupyInstalled: message.cupy_installed ?? false,
        gpuName: message.gpu_name ?? null,
        gpuProblem: message.gpu_problem ?? null,
        error: message.error,
      })
    } else if (message.type === 'ai_tool_call') {
      hooks.onAiToolCall({
        name: message.name ?? '',
        arguments: message.arguments ?? {},
        result: message.result,
        ok: message.ok ?? false,
      })
    } else if (message.type === 'ai_result') {
      hooks.onAiResult({
        status: message.status ?? 'stopped',
        text: message.text ?? '',
        detail: message.detail ?? '',
        turns: message.turns ?? 0,
        tool_calls: message.tool_calls ?? [],
        usage: message.usage ?? {},
      })
    } else if (message.type === 'ai_error') {
      hooks.onAiError(message.message ?? '(理由なし)')
    } else if (message.type === 'optimize_conditions') {
      hooks.onOptimizeConditions(message as unknown as OptimizeConditionsReport)
    } else if (message.type === 'optimize_resolution') {
      hooks.onOptimizeResolution(message as unknown as OptimizeResolutionPayload)
    } else if (message.type === 'optimize_status') {
      hooks.onOptimizeStatus(
        message.running ?? false,
        message.cancelling ?? false,
        message.finishing ?? false,
        message.available !== false,
      )
    } else if (message.type === 'optimize_steps') {
      hooks.onOptimizeSteps((message.steps ?? []) as OptimizeStepMeta[], message.finishable ?? false)
    } else if (message.type === 'optimize_step_voxels') {
      hooks.onOptimizeStepVoxels(message as unknown as OptimizeStepVoxels)
    } else if (message.type === 'optimize_progress') {
      hooks.onOptimizeProgress(message as unknown as OptimizeProgress)
    } else if (message.type === 'optimize_debug') {
      hooks.onOptimizeDebug(message as unknown as OptimizeDebugPayload)
    } else if (message.type === 'optimize_result') {
      hooks.onOptimizeResult(message as unknown as OptimizeResultPayload)
    } else if (message.type === 'optimize_error') {
      hooks.onOptimizeError(
        message.message ?? '(理由なし)',
        message.problems ?? [],
        message.cancelled ?? false,
        message.already_running ?? false,
      )
    }
  }

  async function connect(): Promise<void> {
    if (disposed) return
    hooks.onStatus('connecting')
    const url = await resolveWebSocketUrl()
    // 待っている間に破棄された(初回の設定取得中の再マウントなど)。作ると、閉じる者のいないソケットがフックを呼び続ける。
    if (disposed) return
    const next = new WebSocket(url)
    next.binaryType = 'arraybuffer'
    socket = next
    delivered = createDelivered()
    // このソケットが開いたか。開いてから切れたときは `failures` を増やさない(キープアライブ等)。
    let opened = false
    next.onopen = () => {
      opened = true
      everOpened = true
      failures = 0
      hooks.onStatus('open')
    }
    next.onmessage = (event) => handle(event.data instanceof ArrayBuffer ? event.data : String(event.data))
    // **`onerror` にトーストを繋がない。** 繋ぐと再接続の試行1回につき1枚出ることになり、
    // 「試行は毎回トーストにしない」という規律が、その規律が対象にしていた経路そのものに
    // 破られる(サーバより先にタブを開いていて15回ぶん数え上がった)。繋がらないことは
    // 下の `onclose` が1回だけ言う。
    next.onclose = () => {
      hooks.onStatus('closed')
      // 待たせていた下りの `done` はもう来ない。黙って消すと、押した操作が成功したのか分からないまま終わる。
      if (waits.size > 0) {
        waits.clear()
        reportWaits()
        if (!disposed) hooks.onError('応答を待っている間にサーバとの接続が切れた(自動で繋ぎ直しを試みている)', 'socket')
      }
      if (disposed) return
      if (!opened) {
        failures += 1
        const noticeAt = everOpened ? RECONNECT_FAILURE_NOTICE_AT : CONNECT_FAILURE_NOTICE_AT
        if (failures === noticeAt) {
          const message = everOpened
            ? 'サーバへ再接続できない。自動で繋ぎ直しを続ける'
            : `サーバへ繋がらない (${url})。自動で繋ぎ直しを続ける`
          hooks.onError(message, 'socket')
        }
      }
      retry = setTimeout(() => void connect(), RECONNECT_DELAY_MS)
    }
  }

  return {
    start() {
      void connect()
    },
    sendCommand(cmd, args, clientOp) {
      send({ cmd, ...args, client_op: clientOp }, !NOT_SHOWN_IN_VIEWPORT.has(cmd))
    },
    setVisible(bodyId, visible, clientOp) {
      send({ cmd: 'set_visible', body_id: bodyId, visible, client_op: clientOp }, true)
    },
    setSketchVisible(sketchId, visible, clientOp) {
      send({ cmd: 'set_sketch_visible', sketch_id: sketchId, visible, client_op: clientOp }, true)
    },
    modifySelection(add, remove, clientOp) {
      send({ cmd: 'modify_selection', add, remove, client_op: clientOp }, true)
    },
    toggleSelection(refs, clientOp) {
      send({ cmd: 'toggle_selection', refs, client_op: clientOp }, true)
    },
    clearSelection(clientOp) {
      send({ cmd: 'clear_selection', client_op: clientOp }, true)
    },
    beginCommand(verb, args, clientOp) {
      send({ cmd: 'begin_command', verb, args, client_op: clientOp })
    },
    setCommandArgs(args, clientOp) {
      send({ cmd: 'set_command_args', args, client_op: clientOp })
    },
    endCommand(clientOp) {
      send({ cmd: 'end_command', client_op: clientOp })
    },
    enterSketch(sketchId, clientOp) {
      send({ cmd: 'enter_sketch', sketch_id: sketchId, client_op: clientOp })
    },
    leaveSketch(clientOp) {
      send({ cmd: 'leave_sketch', client_op: clientOp })
    },
    enterOptimize(clientOp) {
      send({ cmd: 'enter_optimize', client_op: clientOp })
    },
    leaveOptimize(clientOp) {
      send({ cmd: 'leave_optimize', client_op: clientOp })
    },
    preview(verb, args, previewId) {
      send({ cmd: 'preview', verb, args, preview_id: previewId }, true)
    },
    getEdges(bodyId) {
      send({ cmd: 'get_edges', body_id: bodyId }, true)
    },
    getEdgeChains(bodyId, edges) {
      send({ cmd: 'get_edge_chains', body_id: bodyId, chain_of: edges })
    },
    getVertices(bodyId) {
      send({ cmd: 'get_vertices', body_id: bodyId }, true)
    },
    inspectSelection(requestId) {
      send({ cmd: 'inspect_selection', request_id: requestId })
    },
    setViewResolution(worldPerPixel, sizeHint, focus) {
      if (focus == null) {
        send({ cmd: 'set_view_resolution', world_per_pixel: worldPerPixel, size_hint: sizeHint })
      } else {
        send({
          cmd: 'set_view_resolution',
          world_per_pixel: worldPerPixel,
          size_hint: sizeHint,
          focus,
        })
      }
    },
    undo() {
      send({ cmd: 'undo' }, true)
    },
    redo() {
      send({ cmd: 'redo' }, true)
    },
    aiGetStatus() {
      send({ cmd: 'ai_get_status' })
    },
    aiConfigure(provider, baseUrl) {
      send({ cmd: 'ai_configure', provider, ...(baseUrl === undefined ? {} : { base_url: baseUrl }) })
    },
    aiSetThinking(thinking) {
      send({ cmd: 'ai_set_thinking', thinking })
    },
    aiSetApiKey(key) {
      send({ cmd: 'ai_set_api_key', key })
    },
    aiPrepare() {
      send({ cmd: 'ai_prepare' })
    },
    aiSend(message) {
      send({ cmd: 'ai_send', message })
    },
    aiCancel() {
      send({ cmd: 'ai_cancel' })
    },
    femGetStatus() {
      send({ cmd: 'fem_get_status' })
    },
    femSetDevice(device) {
      send({ cmd: 'fem_set_device', device })
    },
    optimizeConditions() {
      send({ cmd: 'optimize_conditions' })
    },
    optimizeResolution(elementSizeMm) {
      send(
        elementSizeMm === undefined || elementSizeMm === null
          ? { cmd: 'optimize_resolution' }
          : { cmd: 'optimize_resolution', element_size_mm: elementSizeMm },
      )
    },
    runOptimization(options) {
      send({
        cmd: 'run_optimization',
        element_size_mm: options.elementSizeMm ?? null,
        min_width_mm: options.minWidthMm ?? null,
        // **空配列は送らない。** 古いサーバは知らないキーを無視するだけだが、
        // 「頼んでいない」ことが送信の中身からも読めるようにしておく。
        ...(options.debug && options.debug.length > 0 ? { debug: options.debug } : {}),
      })
    },
    cancelOptimization() {
      send({ cmd: 'cancel_optimization' })
    },
    finishOptimization(step, debug) {
      send({
        cmd: 'finish_optimization',
        ...(step === null ? {} : { step }),
        // 実行の後の仕上げでもデバッグ表示(`?voxel=1` など)を新しい結果に合わせる(`runOptimization` と同じ)。
        ...(debug && debug.length > 0 ? { debug } : {}),
      })
    },
    optimizeSteps() {
      send({ cmd: 'optimize_steps' })
    },
    optimizeStepVoxels(index) {
      send({ cmd: 'optimize_step_voxels', index }, true)
    },
    dispose() {
      disposed = true
      if (retry) clearTimeout(retry)
      socket?.close()
      socket = null
    },
  }
}
