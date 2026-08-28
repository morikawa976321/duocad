/**
 * コアとの接続 — コマンドは下り、イベントは上り(実装計画書 §2)。
 *
 * ここが持つのは「WebSocketの接続」と「途中まで入力していたコマンド」だけで、
 * 文書・操作ログ・undo履歴・**可視性**はコア側にある
 * (§2「アプリ層が落ちて失われてよいのは、途中まで入力していたコマンドだけ」)。
 *
 * **選択も可視性もコアが正**。このファイルは `modify_selection` / `commit_selection` /
 * `clear_selection` / `set_visible` を送るだけで、どちらの状態も覚えない。
 *
 * **`state` は部品ごとの配列である。** `state.bodies` は軽量
 * (geometryを含まない)で、形状は `delta.bodies`(版が変わったBodyだけ)で届く
 * (`duocad/server.py` のモジュールdocstring参照)。
 *
 * 選択の正は `state.selection`(コア側)。**ブラウザは選択を1つも覚えない。**
 * あわせて `preview`(投機的実行)を足した —— **これだけは状態を配信しない**ので、
 * 応答は `onState` ではなく `onPreview` へ来る。
 */
/** 図形の種別。**唯一の正は `entityKinds.ts` の表**(ここは型を通すだけ)。 */
import type { EntityKind } from './entityKinds'
import type { GeometryPayload } from './viewport'

export type { EntityKind }

/** 選べるものの種別。領域(region)は `RegionRef` 側で表す(`duocad/selection.py` §3.5)。 */
export type ShapeKind = 'face' | 'edge' | 'vertex'

/** 面・辺・頂点を指す参照。添字は**1始まり**(コアの `select_*` と同じ)。 */
export interface ShapeRef {
  body: string
  kind: ShapeKind
  index: number
  provenance?: unknown
}

/** スケッチの領域を指す参照(`kind` キーを持たないのが `ShapeRef` との見分け方)。 */
export interface RegionRef {
  sketch: string
  index: number
  provenance?: unknown
}

export type SelectionRef = ShapeRef | RegionRef

/**
 * 共有される選択集合(`duocad/selection.py` の `Selection.to_dict()`)。
 *
 * **origin は集合単位**。`"proposed"` はAIが提案した状態で、そのままではverbに渡せない
 * (コアが `SelectionNotCommitted` で止める)。`"user"` になる道は2つ ——
 * 人がクリックで触る(`modify_selection`)か、**人が対象集合を食うコマンドを実行する**
 * (2026-08-23。サーバが走らせる直前に確定する。`duocad/server.py` の `_commit_before_verb`)。
 *
 * **画面から `commitSelection` を呼ぶ道は無い**(同日「承認する」ボタンごと廃止した)。
 * 提案と確定を人へ伝えるのは**3Dのハイライト色だけ**なので、この値は必ず配信で追随する。
 */
export interface SelectionState {
  items: SelectionRef[]
  origin: 'user' | 'proposed'
  count: number
}

/** 1Bodyぶんの軽量な状態(geometryを含まない)。パーツ一覧はこれだけで描ける。 */
export interface BodyMeta {
  body_id: string
  name: string
  kind: string
  visible: boolean
  revision: number
  face_count: number
  volume: number
}

/** `BodyMeta` + 形状。版が変わったBodyだけ届く(`geometry` は非表示なら null)。 */
export interface BodyGeometry extends BodyMeta {
  geometry: GeometryPayload | null
}

// --------------------------------------------------------------------------------------
// C: スケッチ — **手動操作が一級の操作モード**になる場所
// --------------------------------------------------------------------------------------
/** スケッチを貼る平面(コアの PlaneRef。基準3面か、既存ソリッドの平面状の面)。 */
export type PlaneRef =
  | { kind: 'base'; name: BasePlaneName; provenance?: unknown }
  | { kind: 'face'; body: string; index: number; provenance?: unknown }

export type BasePlaneName = 'XY' | 'XZ' | 'YZ'

/**
 * スケッチ平面の3Dの枠。`p = origin + u*uAxis + v*vAxis` が (u,v) と3Dの往復の規則で、
 * **コアが配る**(`kernel.sketch_plane_frame`)。アプリ層で同じ計算を持たない —— 基準3面の
 * 軸の取り方と、面に貼ったときの法線反転の扱いが必ずずれる。
 */
export interface SketchFrame {
  origin: number[]
  u: number[]
  v: number[]
  normal: number[]
}

/**
 * スケッチ上の1図形。`params` は**解決済みの数値**、`refs` は同じキーの
 * **パラメータ名**(`set_parameter` で打ち直せる。名前が無ければ null)。
 *
 * この2つが揃っているのが「クリックだけで置いた後も値を打ち直せる」の実体である
 * (実装計画書§3.4 パラメトリック・プリミティブ)。
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
  positions: number[]
  indices: number[]
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
  plane: PlaneRef
  /** 平面が壊れている(貼った面が消えた等)ときだけ null。 */
  frame: SketchFrame | null
  /** 反復を展開した**実際に見えるもの**。描画は必ずこちらを使う。 */
  drawn: Array<{ kind: EntityKind; params: Record<string, number> }>
  /** 人が値を打ち直す対象(反復は展開しない)。 */
  entities: SketchEntity[]
  /** 反復の打ち直し対象(間隔・個数)。`SketchPanel.svelte` が図形の一覧に続けて出す。 */
  patterns: SketchPattern[]
  regions: SketchRegion[]
}

export interface ServerState {
  units: string
  active_body_id: string | null
  /** 全Body。**geometryを含まない**(常に軽い。27部品でも数KB)。 */
  bodies: BodyMeta[]
  log: Array<{ seq: number; verb: string; args: Record<string, unknown> }>
  can_undo: boolean
  can_redo: boolean
  applied_count: number
  /** 共有される選択集合。**ここが正**で、ブラウザは自前の選択を持たない。 */
  selection: SelectionState
  /** 文書の全スケッチ。図形も領域もここが正で、ブラウザは溜めない。 */
  sketches: SketchState[]
  /** あと何回やり直せるか。**真偽だけだと履歴の位置が出せない**。 */
  redo_count: number
  /** 名前つきの寸法値(§3.4)。パラメータ編集パネルが出す対象そのもの。 */
  parameters: Record<string, number>
}

/**
 * ファイル操作の応答。**状態の配信ではない** —— 保存・書き出しは文書を変えないので、
 * 要求した接続にだけ「どこへ何バイト書いたか」が返る(`PreviewPayload` と同じ扱い)。
 *
 * `path` は**サーバが解決した絶対パス**。相対パスの基準はサーバの作業ディレクトリで、
 * ブラウザ側では解決できない(2箇所で別々に解釈すると必ずずれる)。
 *
 * `name` は `save_document` の応答にだけ乗る**ファイル名だけ**。
 * `open_document` は文書を差し替える側なので `Applied`(`change: "document"`)の
 * `name` を見ること —— こちらは応答チャンネルが違う(`FilesPanel.svelte` 参照)。
 *
 * `files` は `list_documents` の応答にだけ乗る(`FilesPanel.svelte` が描く一覧)。
 */
export interface FilePayload {
  action: string
  path: string
  bytes: number
  name?: string
  operations?: number
  deviation?: number
  body_ids?: string[]
  /** 書き出し候補のうち非表示(visible=false)で除外されたBodyのID(export_step/export_stlのみ)。 */
  skipped_hidden?: string[]
  files?: DocumentInfo[]
}

/**
 * `models_dir` に置かれた1ファイルの一覧情報(`list_documents` の1件)。
 *
 * **壊れて操作数が数えられないファイルはサーバの一覧に出てこない**
 * (`Session.list_documents` が読めるものだけを返す)ので、ここに現れる時点で
 * `operations` は必ず読めている。
 */
export interface DocumentInfo {
  name: string
  mtime: number
  size: number
  operations: number
}

/**
 * 操作を出したクライアントへの確認応答。
 *
 * **discriminated union**。全フィールドがoptionalなフラット構造だと、`verb` ごとに本来必須の
 * キー(例: `import_step` の `body_ids`)を宣言し忘れても型検査が止まらない
 * (実際、`.svelte` 側だけが `body_ids` を読んでいて宣言が無いまま素通りしていたことがある)。
 * `verb`(verbの適用)と `change`(`"selection"` = 選択の変更、`"document"` = ファイルを開いて
 * 文書ごと差し替わった。どちらも操作ログには載らない)をdiscriminantにしてあるので、
 * `applied.verb === "extrude_face"` で絞り込めば `body_id` / `face_index` / `distance` が
 * **必須**として見える(他のverbのキーは見えない)。
 *
 * `duocad/session.py` の `Session` が実際に組み立てている形と1:1。**新しいverbを足すときは
 * ここにも1メンバー足す**(`duocad/catalog.py` は応答の「形」までは持たないので、この対応は
 * 手で保つしかない)。**手で保つが、放置はされない** —— `tests/test_applied_type_sync.py` が
 * このunionをソースとして読み、catalogのverb名と、実際にverbを走らせた戻り値のキーの
 * 両方に突き合わせる(2026-08-28。それまでに `import_svg` が1つ欠けていた)。
 */

/**
 * verb適用に共通するフィールド。
 *
 * `change?: undefined` は**実行時には存在しないプレースホルダ**。discriminantが
 * `verb` と `change` の2つに分かれているので、`applied.change === "selection"` のように
 * どちらの分岐からも先に触ってよいようにするには、触る側のキーが**全メンバーに
 * (値が無くても)存在する**必要がある(無いメンバーが1つでもあると `Property 'change'
 * does not exist` でコンパイルが止まる)。値としては来ない。
 */
interface AppliedVerbBase {
  seq: number
  client_op: number | null
  change?: undefined
}

/** change系イベント(verbではない)に共通するフィールド。`verb`/`seq` 側の同じ理由。 */
interface AppliedChangeBase {
  client_op: number | null
  verb?: undefined
  seq?: undefined
}

export type Applied =
  | (AppliedVerbBase & {
      verb: 'extrude_face'
      body_id: string
      face_index: number
      distance: number
      changed_bodies: string[]
    })
  | (AppliedVerbBase & {
      verb: 'extrude'
      body_id: string
      distance: number
      mode: string
      changed_bodies: string[]
    })
  | (AppliedVerbBase & { verb: 'fillet'; body_id: string; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'chamfer'; body_id: string; changed_bodies: string[] })
  | (AppliedVerbBase & { verb: 'pattern_rectangular'; pattern_id: string })
  | (AppliedVerbBase & { verb: 'pattern_circular'; pattern_id: string })
  | (AppliedVerbBase & {
      verb: 'set_parameter'
      name: string
      value: number
      previous: number
      changed_bodies: string[]
    })
  /** C: `sketch_create` が返す新しいスケッチのID。 */
  | (AppliedVerbBase & { verb: 'sketch_create'; sketch_id: string })
  /** C: 図形追加verbが返す新しい図形のID(4つとも同じ形)。 */
  | (AppliedVerbBase & { verb: 'add_line' | 'add_circle' | 'add_rectangle' | 'add_arc'; entity_id: string })
  /**
   * SVGから取り込んだ図形。**スケッチのverb**(2026-08-28に `import_svg` はファイル操作から
   * スケッチへ移り、貼る先の `sketch_id` を必須で取るようになった)。
   *
   * `entity_ids` は**この1回が足した分だけ**で、貼った先のスケッチに元からあった図形は
   * 含まない(undoもこの分だけを戻す)。`source_name` は読んだファイル名、`entity_count` は
   * `entity_ids.length` と同じ数を操作ログ側から数えたもの。
   */
  | (AppliedVerbBase & {
      verb: 'import_svg'
      sketch_id: string
      entity_ids: string[]
      source_name: string
      entity_count: number
    })
  /**
   * STEP読み込みが作ったボディ。**読み込みは1 verbだけ**で、編集用(`body_ids`)と
   * 原本の参照(`reference_ids`)の両方が1回の応答で返る。
   */
  | (AppliedVerbBase & { verb: 'import_step'; body_ids: string[]; reference_ids: string[] })
  | (AppliedVerbBase & { verb: 'undo'; undone: string })
  | (AppliedVerbBase & { verb: 'redo'; redone: string })
  /** 選択の変更イベント。verbではない(操作ログに載らない)。 */
  | (AppliedChangeBase & { change: 'selection'; selection: SelectionState })
  /**
   * 可視性の変更イベント。**verbではない**(操作ログに載らず、Ctrl+Z / Ctrl+Y で戻らない。
   * 2026-08-26)。効果そのものは配信される `state.bodies[].visible` に載る。
   */
  | (AppliedChangeBase & {
      change: 'visibility'
      body_id: string
      visible: boolean
      previous: boolean
      changed: boolean
    })
  /**
   * 文書ごと差し替わった。verbではない(`Session.open_document` / `Session.new_document`)。
   *
   * **`path` / `name` は `null` になりうる** —— 新規(`action: "new"`)は開いたファイルが
   * 無いので、どちらも `null` で来る(触っているファイルの表示は「(未保存)」へ戻る)。
   */
  | (AppliedChangeBase & {
      change: 'document'
      action: string
      path: string | null
      name: string | null
      operations: number
    })

/**
 * プレビュー(投機的実行)の応答。**状態の配信ではない** —— 要求した接続にだけ届き、
 * サーバ側の文書は既に元へ戻っている(`Session.preview`)。
 *
 * `preview_id` は要求時に付けた番号。**古い応答を捨てるために要る** ——
 * 数値を打ち替えると要求が追い越しうるし、Escで中断した後に届くこともある。
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
}

/**
 * 1本の辺の折れ線(`kernel.tessellate_edges` の1件そのまま)。
 *
 * `index` は**コアの数え方の1始まり**で、`fillet(edges=[...])` が食う添字と同じ。
 * ここがずれると「指した辺と違う辺が丸まる」という、例外も出ない事故になる。
 */
export interface EdgeGeometry {
  index: number
  kind: string
  length: number
  closed: boolean
  points: number[]
}

/**
 * 辺の応答。**状態の配信ではない**(要求した接続にだけ届く。`PreviewPayload` と同じ扱い)。
 *
 * `revision` は要求した時点のBodyの版。**形が変われば版が上がる**ので、古い辺を
 * 掴んだままフィレットをかける経路が閉じる(§7 添字は形状変更で壊れる、の辺版)。
 */
export interface EdgesPayload {
  body_id: string
  revision: number
  edges: EdgeGeometry[]
}

/**
 * 1つの頂点の3D座標(`kernel.vertex_positions` の1件そのまま)。
 *
 * `index` は**コアの数え方の1始まり**(`count_subshapes(shape, "vertex")`)で、
 * 選択の `ShapeRef`(`kind: "vertex"`)が持つ添字と同じ。
 */
export interface VertexGeometry {
  index: number
  point: number[]
}

/**
 * 頂点の応答。**状態の配信ではない**(要求した接続にだけ届く。`EdgesPayload` と同じ扱い)。
 *
 * 全頂点が入っているが、**画面に出るのは選択に入った添字だけ**である(辺と違い、
 * 頂点を要求するコマンドが無いので候補を見せる意味が無い)。`revision` は要求した
 * 時点のBodyの版で、形が変わった後の古い座標を描き続ける経路を閉じる。
 */
export interface VerticesPayload {
  body_id: string
  revision: number
  vertices: VertexGeometry[]
}

// --------------------------------------------------------------------------------------
// G: アプリ内AIエージェント — **verbではない**
// --------------------------------------------------------------------------------------
/**
 * `ai_*` は文書を直接は変えない(操作ログに載らずundoの単位でもない)。AIがツールを呼んで
 * 文書を変えたときは、その変更は**通常の `state` 配信**として別途届く(`onState` の
 * `applied` は `null` —— MCP起点の変更と同じ扱い。`duocad/server.py` のモジュールdocstring)。
 * ここにある4つはその会話そのものの配線でしかない。
 */
/**
 * 繋げるプロバイダの綴り(`duocad/server.py` の `AI_PROVIDERS` が正)。
 *
 * **2026-08-27に `'anthropic'` が消えて1つになった** —— ClaudeをAPIキーで叩く経路の廃止。
 * 型を潰さず union のまま残してあるのは、次のプロバイダの差込口だからである。
 */
export type AiProviderKind = 'openai_compat'

/** 画面に出してよい設定の全部。**鍵の類は載らない**(2026-08-27にAPIキー経路ごと廃止)。 */
export interface AiStatus {
  provider: AiProviderKind
  model: string | null
  baseUrl: string | null
  running: boolean
  providers: AiProviderKind[]
  /** `ai_configure` を拒否したときだけ入る(例: 実行中/未知のプロバイダ)。 */
  error?: string
}

/** ツール呼び出し1件の進捗(`AgentLoop` の `progress` コールバックそのもの)。
 * **`name` は操作ログ・MCPツール名と同じ綴り** —— 操作ログと突き合わせられる。 */
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
 * - `max_tokens` … 出力が上限で切れた。**そのターンのツールは実行していない**
 *                  (切れた引数のままでは危険なので、実行せず止める設計)
 * - `refusal`    … プロバイダが安全上の理由で断った
 * - `provider_error` … HTTP・接続の失敗(`detail` に理由。APIキーは含まれない)
 * - `stopped`    … 上のどれでもない停止理由(`detail` に生値)
 */
export type AiRunStatus =
  | 'completed'
  | 'max_turns'
  | 'cancelled'
  | 'max_tokens'
  | 'refusal'
  | 'provider_error'
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
 * `AiPanel.svelte` が描く1本の時系列。**コアには無い**(会話の再構成はここが唯一の場所。
 * `duocad/agent/loop.py` の `turns` はサーバ側に留まり、WSへは出さない)——再読込で消えるのは
 * 「途中まで入力していたコマンド」と同じ扱いで許容している(実装計画書§2予防3)。
 */
export type AiTranscriptEntry =
  | { kind: 'user'; text: string }
  | { kind: 'tool'; call: AiToolCall }
  | { kind: 'result'; result: AiRunResult }
  | { kind: 'error'; message: string }

export type ConnectionStatus = 'connecting' | 'open' | 'closed'

export interface SessionHooks {
  /**
   * `changed` は今回の版で届いたBodyだけ(geometry付き。非表示になったものは
   * `geometry: null`)。それ以外のBodyは前回のまま描き直さない(差分更新)。
   */
  onState(state: ServerState, changed: BodyGeometry[], applied: Applied | null): void
  onError(message: string, where: string): void
  onWarning(message: string): void
  onStatus(status: ConnectionStatus): void
  /** プレビューが返ってきた(状態は変わっていない)。 */
  onPreview(payload: PreviewPayload): void
  /** 頼んだ辺が返ってきた(状態は変わっていない)。 */
  onEdges(payload: EdgesPayload): void
  /** 頼んだ頂点が返ってきた(状態は変わっていない)。 */
  onVertices(payload: VerticesPayload): void
  /** ファイルを書いた(状態は変わっていない)。 */
  onFile(payload: FilePayload): void
  // ---------------------------------------------------------------- G: アプリ内AIエージェント
  /** 設定が変わった/`ai_get_status` の返事/実行の開始・終了で `running` が変わったとき。 */
  onAiStatus(status: AiStatus): void
  /** ツールを1つ呼んだ(進行中に逐次届く。**verb配信ではない**)。 */
  onAiToolCall(call: AiToolCall): void
  /** `run()` が止まった(理由は `status`)。 */
  onAiResult(result: AiRunResult): void
  /** 空メッセージ・実行中の2通目・プロバイダ未設定など、**繋がらない理由**。 */
  onAiError(message: string): void
}

export interface Session {
  start(): void
  /**
   * verbを1つ送る。**引数のdictをそのまま流す**(`cmd` と `client_op` を足すだけ)。
   *
   * 引数を作るのは `CommandDef.build` の仕事で、ここは運ぶだけ —— verbごとにメソッドを
   * 書き写すと、キー名を1文字打ち間違えた経路が**そのverbでだけ**静かに壊れる。
   * **受け取れるverbの一覧はコアの宣言(`duocad/catalog.py` の `COMMANDS`)が正**で、
   * そこに無い文字列は `unknown command` で返る
   * (任意のメソッドを呼べる作りにしない、はサーバ側で担保する)。
   */
  runVerb(verb: string, args: Record<string, unknown>, clientOp: number): void
  setVisible(bodyId: string, visible: boolean, clientOp: number): void
  /** 人が直す経路。差分編集で `origin` は `"user"` になる(触ること自体が承認)。 */
  modifySelection(add: SelectionRef[], remove: SelectionRef[], clientOp: number): void
  /**
   * AIの提案を中身を変えずに承認する。
   *
   * **今は画面から呼んでいない**(2026-08-23に「承認する」ボタンを廃止し、verbの実行を
   * 承認とみなすようサーバ側で自動化した)。口はコアの3つの選択操作の1つとして残る
   * (`duocad/selection.py` のモジュールdocstring)。
   */
  commitSelection(clientOp: number): void
  clearSelection(clientOp: number): void
  /**
   * verbを投機的に実行して形だけ測らせる。**文書は変わらない**(サーバが元へ戻す)。
   * 応答は `onPreview` へ。`previewId` は古い応答を捨てるための番号。
   */
  preview(verb: string, args: Record<string, unknown>, previewId: number): void
  // ---------------------------------------------------------------- D: フィーチャ
  /**
   * 1Bodyの辺を折れ線で取りに行く(応答は `onEdges`)。**状態配信ではない**ので
   * 他のクライアントには何も起きない。フィレット・面取りを起動している間だけ呼ぶ
   * (常時配ると27部品ぶんの辺が乗る。`Session.edges` のdocstring)。
   */
  getEdges(bodyId: string): void
  /**
   * 1Bodyの頂点の座標を取りに行く(応答は `onVertices`)。**状態配信ではない**。
   *
   * 呼ぶのは**選択に頂点が入っているとき**だけ。辺のように「コマンドを起動している間」
   * ではない —— 頂点を引数に取るコマンドが無く、要るのは「AIが選んだ頂点が今どれか」を
   * 人が見て外せることだけである(`duocad/server.py` の `Session.vertices`)。
   */
  getVertices(bodyId: string): void
  undo(): void
  redo(): void
  // ---------------------------------------------------------------- G: アプリ内AIエージェント
  /** 今の設定(応答は `onAiStatus`)。 */
  aiGetStatus(): void
  /** プロバイダ・モデル・エンドポイントを差し替える。**キーは渡さない**(渡す経路が無い)。 */
  aiConfigure(provider: AiProviderKind, model?: string, baseUrl?: string): void
  /** 1つの発話でエージェントを起こす。**待たずに返る** —— 進捗は `onAiToolCall`、
   * 最終応答は `onAiResult` へ届く(`duocad/server.py` の `_ai_start` のdocstring)。 */
  aiSend(message: string): void
  /** 中断する。走っているツールの結果は `onAiResult` の `tool_calls` に残る。 */
  aiCancel(): void
  dispose(): void
}

/** スケッチ平面上の点 [u, v](mm)。 */
export type Point2 = [number, number]

const RECONNECT_DELAY_MS = 2000
/**
 * 繋がらないことをトーストで言うまでの連続失敗回数。
 *
 * **試行のたびには出さない**(`App.svelte` の `onStatus` が「繋がっていたのに切れた」落差
 * だけを見るのと同じ規律)。ただし**一度も繋がっていない間は落差が生まれない**ので、
 * それだけをここが1回言う —— サーバより先にタブを開いたとき、黙ったままでは
 * 繋がらない理由が画面のどこにも出ない。
 *
 * **数えるのは回数であって時間ではない。** 試行の間隔は `RECONNECT_DELAY_MS` では
 * 決まらない —— 繋がらないソケットが閉じるまでに実測2秒かかり、さらにChromeが同じ
 * ホストへの失敗を重ねると自前で遅らせる(実測: 開いたばかりの画面で5秒周期、
 * 失敗を積んだ画面では26秒周期)。時間で切ると、この幅がそのまま無音の幅になる。
 */
const CONNECT_FAILURE_NOTICE_AT = 3

async function resolveWebSocketUrl(): Promise<string> {
  // サーバが `/duocad-config.json` で自分のWebSocketのURLを教える。
  // ポートを推測しない(静的配信とWebSocketでポートが別であり、既定値を2箇所に書くと必ずずれる)。
  try {
    const response = await fetch(new URL('duocad-config.json', location.href), {
      cache: 'no-store',
    })
    if (response.ok) {
      const config = (await response.json()) as { ws_url?: string }
      if (config.ws_url) return config.ws_url
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
  /** 開く前に閉じた回数。開いたら0へ戻す(`CONNECT_FAILURE_NOTICE_AT`)。 */
  let failures = 0

  /**
   * 1本送る。**繋がっていなければ送らずに、その場でトーストへ回す**。
   *
   * 2026-08-27にヘッダの常設ステータスを外したので、**繋がっていないことに人間が
   * 気づく機会は「操作したのに送れなかった」この瞬間**になった。ここは画面からの操作が
   * 必ず通る1本道なので、各パネルに同じ判定を書き写さずに済む。
   *
   * **間引かない。** 同じ文言が続けて来ても、まとめるのはトースト側(`Toast.svelte` の
   * 「同じ文言は積まずに数える」)であって、送る側が黙る話ではない —— ここで最初の1回だけ
   * にすると、**2回目の操作が黙って消える**(押した本人には成功と区別がつかない)。
   */
  function send(payload: Record<string, unknown>): void {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      hooks.onError('サーバに繋がっていないので操作を送れなかった(自動で繋ぎ直しを試みている)', 'send')
      return
    }
    socket.send(JSON.stringify(payload))
  }

  function handle(raw: string): void {
    let message: {
      type?: string
      state?: ServerState
      delta?: { bodies?: BodyGeometry[] }
      applied?: Applied
      message?: string
      where?: string
      preview_id?: number | null
      verb?: string
      bodies?: PreviewPayload['bodies']
      body_id?: string
      revision?: number
      edges?: EdgeGeometry[]
      vertices?: VertexGeometry[]
      action?: string
      path?: string
      bytes?: number
      operations?: number
      deviation?: number
      body_ids?: string[]
      skipped_hidden?: string[]
      files?: DocumentInfo[]
      // ---------------------------------------------------------------- G
      provider?: AiProviderKind
      model?: string | null
      base_url?: string | null
      running?: boolean
      providers?: AiProviderKind[]
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
      text?: string
    }
    try {
      message = JSON.parse(raw)
    } catch (error) {
      hooks.onError(String(error), 'parse')
      return
    }
    if (message.type === 'state' && message.state) {
      hooks.onState(message.state, message.delta?.bodies ?? [], message.applied ?? null)
    } else if (message.type === 'preview') {
      hooks.onPreview({
        preview_id: message.preview_id ?? null,
        verb: message.verb ?? '',
        bodies: message.bodies ?? [],
      })
    } else if (message.type === 'edges') {
      hooks.onEdges({
        body_id: message.body_id ?? '',
        revision: message.revision ?? 0,
        edges: message.edges ?? [],
      })
    } else if (message.type === 'vertices') {
      hooks.onVertices({
        body_id: message.body_id ?? '',
        revision: message.revision ?? 0,
        vertices: message.vertices ?? [],
      })
    } else if (message.type === 'file') {
      hooks.onFile({
        action: message.action ?? '',
        path: message.path ?? '',
        bytes: message.bytes ?? 0,
        name: message.name,
        operations: message.operations,
        deviation: message.deviation,
        body_ids: message.body_ids,
        skipped_hidden: message.skipped_hidden,
        files: message.files,
      })
    } else if (message.type === 'error') {
      hooks.onError(message.message ?? '(理由なし)', message.where ?? '?')
    } else if (message.type === 'warning') {
      hooks.onWarning(message.message ?? '')
    } else if (message.type === 'ai_status') {
      hooks.onAiStatus({
        provider: message.provider ?? 'openai_compat',
        model: message.model ?? null,
        baseUrl: message.base_url ?? null,
        running: message.running ?? false,
        providers: message.providers ?? ['openai_compat'],
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
    }
  }

  async function connect(): Promise<void> {
    if (disposed) return
    hooks.onStatus('connecting')
    const url = await resolveWebSocketUrl()
    const next = new WebSocket(url)
    socket = next
    // この接続が一度でも開いたか。**開いてから切れた**のは `App.svelte` の `onStatus` が
    // 落差として拾うので、ここで数えるのは開く前に閉じたぶんだけでよい。
    let opened = false
    next.onopen = () => {
      opened = true
      failures = 0
      hooks.onStatus('open')
    }
    next.onmessage = (event) => handle(String(event.data))
    // **`onerror` にトーストを繋がない。** 繋ぐと再接続の試行1回につき1枚出ることになり、
    // 「試行は毎回トーストにしない」という規律が、その規律が対象にしていた経路そのものに
    // 破られる(サーバより先にタブを開いていて15回ぶん数え上がった)。繋がらないことは
    // 下の `onclose` が1回だけ言う。
    next.onclose = () => {
      hooks.onStatus('closed')
      if (disposed) return
      if (!opened) {
        failures += 1
        if (failures === CONNECT_FAILURE_NOTICE_AT) {
          hooks.onError(`サーバへ繋がらない (${url})。自動で繋ぎ直しを続ける`, 'socket')
        }
      }
      retry = setTimeout(() => void connect(), RECONNECT_DELAY_MS)
    }
  }

  return {
    start() {
      void connect()
    },
    runVerb(verb, args, clientOp) {
      send({ cmd: verb, ...args, client_op: clientOp })
    },
    setVisible(bodyId, visible, clientOp) {
      send({ cmd: 'set_visible', body_id: bodyId, visible, client_op: clientOp })
    },
    modifySelection(add, remove, clientOp) {
      send({ cmd: 'modify_selection', add, remove, client_op: clientOp })
    },
    commitSelection(clientOp) {
      send({ cmd: 'commit_selection', client_op: clientOp })
    },
    clearSelection(clientOp) {
      send({ cmd: 'clear_selection', client_op: clientOp })
    },
    preview(verb, args, previewId) {
      send({ cmd: 'preview', verb, args, preview_id: previewId })
    },
    getEdges(bodyId) {
      send({ cmd: 'get_edges', body_id: bodyId })
    },
    getVertices(bodyId) {
      send({ cmd: 'get_vertices', body_id: bodyId })
    },
    undo() {
      send({ cmd: 'undo' })
    },
    redo() {
      send({ cmd: 'redo' })
    },
    aiGetStatus() {
      send({ cmd: 'ai_get_status' })
    },
    aiConfigure(provider, model, baseUrl) {
      send({ cmd: 'ai_configure', provider, model: model ?? null, base_url: baseUrl ?? null })
    },
    aiSend(message) {
      send({ cmd: 'ai_send', message })
    },
    aiCancel() {
      send({ cmd: 'ai_cancel' })
    },
    dispose() {
      disposed = true
      if (retry) clearTimeout(retry)
      socket?.close()
      socket = null
    },
  }
}
