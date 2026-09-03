/**
 * ビューポート — three.js をここに閉じ込める(実装計画書 §4.2 予防2)。
 *
 * **three.js の型を外へ漏らさない**。公開するのは下の `Viewport` インタフェースだけで、
 * three.js のオブジェクトは1つも戻り値・引数に出さない。three.js は semver を持たず
 * r番号でほぼ毎月リリースし非推奨APIを定期的に削除するため、**このスタックで最も churn が
 * 激しい依存**である。1ファイルに閉じておけば破壊的変更の影響がここで止まる。
 *
 * **canvas はフレームワークの外**(実装計画書 §4「守るべき唯一のルール」)。Svelte から
 * 受け取るのは空の `HTMLElement` 1つだけで、canvas の生成・破棄・再描画は全部この中で行う。
 * Svelte はこのコンテナの中身を二度と触らない。
 *
 * **Bodyごとの差分更新に対応している。** 文書はSTEPの読み込みなどでソリッドごとに
 * 分かれる(パーツ一覧UI)ので、「変わったBodyだけ差し替える」`update()` が要る。
 * **ここが27部品ぶんを毎回全部作り直すと、サーバ側を速くした意味(差分送信)が描画で
 * 消える**(モジュールの利用側 `App.svelte` 参照)。面の当たり判定・強調表示・射影は
 * body_id + face_index の組で指す(1個のBodyの中でしか一意でない `faceIndex` 単独では
 * 指さない)。
 */
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

// 辺の折れ線の形は `session.ts` の `EdgeGeometry` が正(サーバが配る形そのもの)。
import { entityHandles, entityPoints } from './entityKinds'
import { DEFAULT_RESOLUTION, viewResolution, type ViewResolution } from './adaptiveResolution'
import { sampleFaces, selectFineFaces, type BodySamples } from './faceCulling'
import {
  BASE_EXTENT,
  TIERS,
  extentFor,
  mmPerPixel,
  snap125,
  tierAlphas,
  type CameraProjection,
} from './gridScale'
import { createPlaneGrid, type PlaneFrame, type PlaneGrid } from './planeGrid'
import { cellBox, cellDirection, regionCells, upForDirection } from './viewCube'
import type { BasePlaneName, EdgeGeometry } from './session'

/** kernel.tessellate() が返す1面ぶん(サーバのJSONそのまま)。 */
export interface FaceGeometry {
  index: number
  kind: string
  area: number
  centroid: number[]
  normal: number[]
  positions: number[]
  indices: number[]
  /** この面を細かく測ったか粗く測ったか(サーバが付与。古いサーバは無い)。 */
  detail?: 'fine' | 'coarse'
}

export interface GeometryPayload {
  deviation: number
  face_count: number
  bbox: { min: number[]; max: number[]; center: number[]; diagonal: number }
  faces: FaceGeometry[]
  untriangulated: number[]
  /** 粗い側の偏差[mm](古いサーバは無い)。 */
  coarse_deviation?: number
  /** 細かく測った面の数(古いサーバは無い)。 */
  fine_face_count?: number
  /** 閉じた立体か。開いたシェルではバックフェイス判定を使わない(古いサーバは無い)。 */
  closed?: boolean
}

/**
 * 「いまの画面はどれくらい細かく描くべきか」。**カメラから導く値であって設定ではない。**
 *
 * `worldPerPixel` と `sizeHint` が**事実**(画面1画素ぶんの世界長さ[mm]と、いま見えている
 * ものの外接対角[mm])で、`deviation` / `angularDeflection` はそこから
 * `adaptiveResolution.ts` の式で導いた**目標精度**である。サーバへ送るのは事実の方だけで
 * (`set_view_resolution`)、同じ式が `duocad/resolution.py` にもある。
 */
export interface ViewportResolution extends ViewResolution {
  /** 画面1画素ぶんの世界長さ[mm](注視点の面で測る)。 */
  worldPerPixel: number
  /** いま見えているものの外接対角[mm](上下限の丸めにだけ使う)。 */
  sizeHint: number
  /** face単位の高精度化の指定。`null` は「全Body全面を細かく」(サーバへ送らない)。
   *  値は `{body_id: 細かく測る面の添字[] | null}`。 */
  focus: Record<string, number[] | null> | null
}

/** 面を一意に指す(1個のBodyの中でしか一意でない `faceIndex` 単独指定はしない)。 */
export interface FaceRef {
  bodyId: string
  faceIndex: number
}

/** 辺を一意に指す(添字はコアの数え方の1始まり)。 */
export interface EdgeRef {
  bodyId: string
  edgeIndex: number
}

/** 荷重の大きさ[N]と向き。契約は `duocad/optimize.py` の `state_payload`(`session.ts` の
 *  `OptimizeLoad` と同じ形)。`setOptimizeState` はこれをそのまま受け取る。 */
export interface OptimizeLoadVisual {
  magnitude: number
  direction: 'normal' | '-normal' | 'vector'
  vector: [number, number, number] | null
}

/** `setOptimizeState` が受け取る、形状最適化(第3のモード)の見た目の元。 */
export interface OptimizeVisualState {
  preserveBodies: string[]
  excludeBodies: string[]
  fixtures: FaceRef[]
  loads: FaceRef[]
  load: OptimizeLoadVisual | null
}

/** 頂点を一意に指す(添字はコアの数え方の1始まり)。 */
export interface VertexRef {
  bodyId: string
  vertexIndex: number
}

/**
 * 画面に出す頂点1個。**参照と座標を一緒に渡す** —— 頂点は選択に入っているものしか
 * 描かないので、「どれを描くか」と「どこに描くか」を別の呼び出しに分ける意味が無い。
 */
export interface VertexMarker extends VertexRef {
  /** 世界座標 [x, y, z](`kernel.vertex_positions` の `point`)。 */
  point: number[]
}

/** 1Bodyぶんの形状の差し替え。`geometry: null` は非表示・削除(メッシュを取り除く)。 */
export interface BodyGeometryUpdate {
  bodyId: string
  geometry: GeometryPayload | null
}

/**
 * クリックが選択集合に対して何をするか。修飾キーから決まる。
 *
 * **2026-08-30に `add` / `remove` を `toggle` の1つへ統一した**(本人明言:
 * 「間違えたらもう一回クリックすればいいので、除外専用の機能は実質無意味」)。
 * Shift も Ctrl(Mac は Meta)も同じ `toggle` で、既存の選択を消さずに
 * 「入っていれば外す・入っていなければ足す」。修飾なしは今までどおり `replace`。
 */
export type PickMode = 'replace' | 'toggle'

export interface PickEvent {
  /** 当たった面(Body+添字)。外したら null。**スケッチモード中は常に null**(下記)。 */
  hit: FaceRef | null
  /**
   * 当たった辺。**辺を出しているときだけ** null 以外になる
   * (`showEdges`)。辺が当たったときは `hit` を見ない —— 辺は面の上に重なっているので、
   * 両方返すと呼び出し側が毎回優先順位を書くことになる。
   */
  edge: EdgeRef | null
  /**
   * 当たった頂点。**選択に入っている頂点を出しているときだけ** null 以外になる
   * (`showVertices`)。辺より後・面より先に見る —— 頂点は必ず辺の端点に乗っているので
   * 辺より先に見ると、フィレットを起動している間に辺の端が掴めなくなる。逆に面より
   * 後だと、最小の的である頂点マーカーがその下の面に取られて二度と外せない。
   */
  vertex: VertexRef | null
  /** Shift / Ctrl(Mac は Meta)=トグル / 修飾なし=置き換え。 */
  mode: PickMode
  /**
   * 当たったスケッチの図形(点または辺)。**スケッチモード中だけ** null 以外になる。
   *
   * `point` が文字列ならその点(コアの参照が持つ `point` と同じ**xキー**)、`null` なら
   * 図形1つ全体(= 辺)である。点は辺より**先に**見る —— 点は必ず辺の上に乗っているので、
   * 後に見ると端点を掴めない(頂点マーカーを辺より先に見るのと同じ理由)。
   */
  sketchEntity: SketchEntityHit | null
  /**
   * スケッチモード中の、クリックした点のスケッチ平面上の座標。
   * 平面がカメラの裏なら null。**これが「手で描く」の入力そのもの**。
   */
  point: { sketchId: string; u: number; v: number } | null
  /**
   * 当たった領域(スケッチ+添字)。
   *
   * **スケッチモード中は常に**(そのスケッチの領域だけ。領域は面より手前で拾う)、
   * **通常モードでは `pickRegions(true)` の間だけ**(全スケッチの領域)非nullになる。
   * 通常モードの領域は辺と同じ「要求しているコマンドを起動している間だけ指せる」規律で、
   * 常時指せるとスケッチ平面と重なる面のクリックが領域に取られる。
   */
  region: { sketchId: string; index: number } | null
  /**
   * 当たった基準平面(XY/XZ/YZ)。**`showBasePlanes(true)` の間だけ** null 以外になる
   * (辺と同じ「要求しているコマンドを起動している間だけ指せる」規律)。
   *
   * 面と平面の両方に当たったときは**レイ距離が近い方**だけが返る(通常のオクルージョン)
   * ので、`plane` が非nullなら `hit` は null である。
   */
  plane: BasePlaneName | null
}

/** スケッチの図形(点または辺)への当たり。`point` が null なら図形全体。 */
export interface SketchEntityHit {
  sketchId: string
  entityId: string
  point: string | null
}

/**
 * 左ドラッグのウィンドウ選択の結果(**矩形に完全包含されたものだけ**)。
 *
 * 種別ごとに配列で返すのは、呼び出し側(`App.svelte`)が参照の形へ翻訳するときに
 * 種別の分岐を1回で済ませるため。**空の配列でも呼ぶ** —— 何も囲まなかったドラッグは
 * 「選択を空にする」であって「何も起きない」ではない(修飾なしのとき)。
 */
export interface BoxSelectEvent {
  faces: FaceRef[]
  edges: EdgeRef[]
  vertices: VertexRef[]
  regions: Array<{ sketchId: string; index: number }>
  sketchEntities: SketchEntityHit[]
  mode: PickMode
}

/**
 * スケッチを貼った平面。**基準3面か、既存ソリッドの面か**(session.ts の `PlaneRef` と
 * 同じ語彙。`face` 側は `FaceRef` と同じ形にして、面メッシュの索きをそのまま使えるようにする)。
 * グリッドの表示平面決定(`desiredGridPlanes`)と範囲の見積もり(`faceUvExtent`)が読む。
 */
export type SketchPlaneRef = { kind: 'base' } | ({ kind: 'face' } & FaceRef)

/** 1枚のスケッチの描画指示(three.js の型は出さない)。 */
export interface SketchDraw {
  sketchId: string
  /** `p = origin + u*uAxis + v*vAxis`(コアが配る枠。session.ts の `SketchFrame`)。 */
  frame: { origin: number[]; u: number[]; v: number[]; normal: number[] }
  /** 貼った平面(基準面か、既存ソリッドの面か)。 */
  plane: SketchPlaneRef
  /** 反復を展開した描画対象。`params` はコアの `Entity.params` と同じキー。 */
  entities: Array<{ kind: string; params: Record<string, number> }>
  regions: Array<{ index: number; positions: number[]; indices: number[] }>
  /** 選択中の領域の添字(強調して描く)。 */
  selectedRegions: number[]
  /**
   * **反復を展開していない**図形(`entity_id` つき)。点・辺のピックと点の表示に使う。
   *
   * `entities`(= コアの `drawn`)と分けてあるのは、あちらが反復を展開した「見えるもの」で
   * `entity_id` を持たないからである。選択の参照は `entity_id` で指すので、指せるのは
   * ここに載っている元の図形だけ(反復で増えた複製は指せない —— 複製は導出物であって
   * 独立した図形ではない)。
   */
  handles: Array<{ entityId: string; kind: string; params: Record<string, number> }>
  /** 選択中の図形(強調して描く)。`point` が null なら図形全体。 */
  selectedHandles: Array<{ entityId: string; point: string | null }>
}

export interface Viewport {
  /**
   * Bodyの形状をまとめて反映する。**初回(まだ何も描いていない状態)だけ、反映後の
   * シーン全体にカメラを合わせる**。以後の呼び出しは視点を保ったまま差分だけ描き直す。
   * 変更のないBodyには触れない — 27部品のうち1つだけ変わった押し出しでは、
   * 呼び出し側(App.svelte)はその1件だけを渡せばよい。
   */
  update(bodies: BodyGeometryUpdate[]): void
  /**
   * 直近に `update()` で渡した生の `GeometryPayload` を読む(無ければ null)。
   *
   * **実機検査フック専用**。ここに寄せてあるので、形の正は「three.jsが今描いている形」の
   * 1箇所になる(App.svelte が写しを持つと、コアの状態の第3のコピーになる)。
   */
  getGeometry(bodyId: string): GeometryPayload | null
  /**
   * `validBodyIds` に無いBodyの描画を取り除く(文書から消えたBodyのメッシュを掃除する)。
   * `duocad/server.py` の `Session.delta` の契約(「知っているのに `bodies` にも
   * `unchanged` にも現れないIDは消えた」)に対応する側。**実際に呼べるverbからは
   * 到達しない**(undoは文書の土台より前へ戻せない)が、呼び出し側(App.svelte)は
   * 契約どおりに毎回呼ぶ — 到達しない経路のためのコードを書かないと、次にBody削除が
   * 起きるverbが増えたときに同じ「画面から消えない」不具合が再発する。
   */
  prune(validBodyIds: string[]): void
  /** 面のクリック(当たり・外れとも)を受け取る。 */
  onPick(listener: (event: PickEvent) => void): void
  /**
   * スケッチモード中、ポインタがスケッチ平面のどこを指しているか。
   * モード外・平面の裏では null が来る。**確定前の線を見せるためだけ**に使う。
   */
  onPlaneMove(listener: (point: { u: number; v: number } | null) => void): void
  /**
   * 選択中の面を強調する(空配列で解除)。
   *
   * **複数選択と出所(origin)に対応している**。`origin` が `"proposed"`(AIの提案)なら
   * 確定した選択と**明確に違う色**で描く —— これが「交互操作が人間側から見える」唯一の
   * 場所である(実装計画書§8b)。
   *
   * **領域は3Dでは色分けしない**(スケッチ側で強調する)。**辺と頂点は同じ規律に
   * 揃えてある** —— 辺は `showEdges` で折れ線を渡してある間だけ `highlightEdges` が、
   * 頂点は `showVertices` が、どちらも user=橙 / proposed=水色で色を変える。
   */
  highlight(refs: FaceRef[], origin: 'user' | 'proposed'): void
  /**
   * 選択中の**ボディ**を強調する(`highlight` のボディ版。空配列で解除)。
   *
   * 形状最適化(第3のモード)で保持/除外ソリッドを決めているあいだだけ非空になる ——
   * その他のverbはボディを選択できない(`App.svelte` の `wantedKinds`)。**選択は役割色
   * (`setOptimizeState` の緑/赤)に勝つ**——そのボディの全面がハイライト色になる。
   */
  highlightBodies(bodyIds: string[], origin: 'user' | 'proposed'): void
  /**
   * 形状最適化(第3のモード)の見た目 —— 保持/除外ソリッドの色、拘束面の色分けと
   * 固定記号、荷重の矢印+ラベル。**`null` はモードを抜けたとき**(何も描かない・
   * ソリッドの色も素へ戻す。タスク仕様「モードを抜けているあいだは描かない」)。
   *
   * 記号・矢印は**指せない**(装飾)——「見えて、指した先が分かればよい」で、
   * ピックの対象は増やさない(`allFaceMeshes` は変わらない)。
   */
  setOptimizeState(state: OptimizeVisualState | null): void
  // ------------------------------------------------------------------ D: 辺
  /**
   * 辺の折れ線を出す(`bodyId` に null を渡すと消える)。**出している間だけ辺を指せる**。
   *
   * 常時出さないのは2つの理由による。(1)27部品ぶんの辺はサーバから取るだけでも重い
   * (`Session.edges` のdocstring)。(2)辺は面の手前に重なるので、常時出すと
   * 面を狙うクリックが辺に取られ、「面を選んで押し出す」が遅くなる。
   *
   * **奥の辺も見え、奥の辺も指せる**(深度検査をしない)。three.js の線のレイキャストは
   * 面による遮蔽を見ないので、**見えているものと指せるものを一致させる**にはこちらへ
   * 揃えるしかない(見た目だけ隠すと「見えない辺が選ばれる」になる)。手前の辺のほうが
   * レイに近いので、素直にクリックすれば手前が取れる。
   */
  showEdges(bodyId: string | null, edges: EdgeGeometry[]): void
  // ------------------------------------------------------------------ 基準3平面(スケッチを貼る先)
  /**
   * 基準3平面(XY/XZ/YZ)を**指せるようにする**(`false` で指せなくなる)。
   *
   * 辺と同じ規律 —— **その対象を要求しているコマンドを起動している間だけ指せる**。
   * 常時指せると、面を狙うクリックが手前のグリッドに取られる(グリッドは原点を跨いで
   * ±100mmに広がっているので、ほとんどの視点で部品の手前に1枚は居る)。
   *
   * **当たり判定に新しいメッシュは足していない** —— `raycaster.ray.intersectPlane()` を
   * 3枚ぶん回すだけの解析交差で、描画中のグリッド(`gridRoot`)は判定に一切使わない
   * (グリッドは3平面の線が1つの `BufferGeometry` に混在しており、どの平面の線かを
   * 当たった線から復元できない)。出している間はグリッドの色を変えて「今これが指せる」を出す。
   */
  showBasePlanes(on: boolean): void
  // ------------------------------------------------------------------ 通常モードの領域
  /**
   * **通常モードで**スケッチの領域を指せるようにする(`false` で指せなくなる)。
   *
   * 辺・基準平面と同じ規律 —— **その対象を要求しているコマンド(「領域を押し出す」)を
   * 起動している間だけ指せる**。常時指せると、スケッチ平面と重なる面(押し出した板の
   * 底面など)を狙うクリックが、手前に浮いている領域に取られる。
   *
   * **新しいジオメトリは足していない** —— 領域のメッシュは既に全スケッチぶんが
   * `showSketches` で置かれており(`userData.sketchId` を持つ)、ここが変えるのは
   * `regionAt` が見る対象の範囲だけである。スケッチモード中(`setActiveSketch`)は
   * このゲートに関係なく、**そのスケッチの領域だけ**が対象になる(描いている最中に
   * 別のスケッチの領域を掴まない)。
   */
  pickRegions(on: boolean): void
  /** 選択中の辺を強調する(面の `highlight` と同じ規律)。 */
  highlightEdges(refs: EdgeRef[], origin: 'user' | 'proposed'): void
  // ------------------------------------------------------------------ 頂点(選択中のものだけ)
  /**
   * **選択に入っている頂点だけ**を点(画面上で一定の大きさ)で出す(空配列で消える)。
   *
   * 辺の `showEdges` + `highlightEdges` と違って**1つの呼び出し**なのは、頂点では
   * 「出す」と「選ばれている」が同じことだからである —— 頂点を引数に取るコマンドが無く、
   * 候補を全部見せて選ばせる場面が存在しない。目的は「AIが選んだ頂点が今どれかを人が見て、
   * クリックで外せる」ことだけ。
   *
   * 色は面・辺と同じ規律(user=橙 / proposed=水色)。**領域の単色方式は採らない** ——
   * `SelectionPanel` を廃した後、提案と確定の区別を人へ伝える手段は3Dの色だけになる。
   *
   * **奥のマーカーも見え、奥のマーカーも指せる**(辺と同じく深度検査をしない)。
   */
  showVertices(markers: VertexMarker[], origin: 'user' | 'proposed'): void
  /**
   * 今そのハイライトに使っている色(16進)。出していない種別は null。
   *
   * **実機検査フック専用**。`SelectionPanel` を廃した後、AIの提案(水色)と確定(橙)の
   * 区別を人へ伝えるのは**3Dの色だけ**になったので、その色が実際に変わっていることを
   * 機械で見られるようにする(`getGeometry` と同じ species の穴)。
   */
  highlightColors(): { face: number | null; edge: number | null; vertex: number | null }
  /**
   * 指した面が**いま実際に塗られている色**(16進。無ければ null)。**実機検査フック専用**
   * —— 形状最適化(第3のモード)の役割色(緑/赤/青)が本当に切り替わっていることを、
   * `highlightColors`(選択の色だけを見る)では確かめられないので別に置く。
   */
  faceColor(ref: FaceRef): number | null
  /**
   * 形状最適化(第3のモード)の装飾(拘束記号+荷重矢印・ラベル)の数。**実機検査フック専用**
   * —— 「拘束と荷重が見える」を、個々のメッシュの形までは検証せず「何か描かれているか」の
   * 数で機械的に確かめる(`setOptimizeState` が空にすると0に戻る)。
   *
   * **種別を区別しない粗い数**なので、拘束と荷重の取り違え・向きの逆転・ラベル欠落までは
   * 検出できない(2026-08-31 QA指摘)。そこまで見るには `optimizeAnnotations()` を使うこと。
   */
  optimizeAnnotationCount(): number
  /**
   * 形状最適化(第3のモード)の装飾の**中身**。**実機検査フック専用**。
   *
   * `fixtures` は固定記号を置いた面の参照、`loads` は矢印ごとの単位ベクトルとラベル文字列
   * (`"250N"` のような表示テキストそのもの)。**種別を区別して数える・向きとラベルを
   * 突き合わせる**ところまで実機検査で確かめられるようにするための穴
   * (`optimizeAnnotationCount()` の粗さを補う。2026-08-31 QA指摘)。
   */
  optimizeAnnotations(): {
    fixtures: FaceRef[]
    loads: Array<{ bodyId: string; faceIndex: number; direction: [number, number, number]; label: string }>
  }
  /**
   * 今ホバーで明るくしているもの(**実機検査フック専用**)。
   *
   * ホバーは色を足さず明度だけを上げるので、`highlightColors` では変化を読めない
   * (橙のままである)。**「クリックすれば取れるもの」と実際にクリックした結果が
   * 一致していること**を機械で確かめるための穴で、頂点は対象外なので返さない。
   */
  hoveredRef(): {
    face: FaceRef | null
    edge: EdgeRef | null
    region: { sketchId: string; index: number } | null
    plane: BasePlaneName | null
    /** そのものに**今実際に当たっている**色(16進)。何も指していなければ null。 */
    color: number | null
  }
  /**
   * 頂点マーカーの画面座標(無理なら null)。**実機検査が本物のクリックで頂点を外す**
   * ために要る穴で、面の `project` / 辺の `projectEdge` と同じ流儀 ——
   * 返す前にその画素からのレイが実際にその頂点へ当たることを確かめる。
   */
  projectVertex(bodyId: string, vertexIndex: number): { x: number; y: number } | null
  /**
   * 辺の上の点の画面座標(無理なら null)。面の `project` の辺版で、
   * **実機検査が本物のクリックで辺を選ぶ**ために要る穴。
   * 返す前に**その画素からのレイが実際にこの辺へ当たること**を確かめる。
   */
  projectEdge(bodyId: string, edgeIndex: number): { x: number; y: number } | null
  /**
   * 確定前の形(プレビュー)を重ねる。**本物のBodyは隠し、幽霊を代わりに描く**。
   *
   * 幽霊は**ピックの対象にしない**(まだ存在しない形は指せない)。本物の状態が
   * 1つでも届いたら(`update`)プレビューは無効になるので、自動的に消える。
   */
  showPreview(bodies: BodyGeometryUpdate[]): void
  /** プレビューを消して本物のBodyを出し直す。 */
  clearPreview(): void
  /**
   * 面の表面上の点の画面座標(コンテナ内のCSSピクセル)。無理なら null。
   *
   * これは自動テスト(`scripts/smoke.mjs`)が**本物のクリックで面を選ぶ**ために必要な穴で、
   * 「動くはず」で納品しないための道具。重心ではなく三角形の上の点を返し、
   * さらに**その画素からのレイが実際にこの面へ当たることを確かめてから**返す
   * (重心は円環や円筒では面の外にあり、隠れている面もあるため)。**他のBodyに隠れている
   * 面は、そのBodyを隠さない限り null になる**(それ自体が可視性=選択機構の一部であることの
   * 検査になる)。
   */
  project(bodyId: string, faceIndex: number): { x: number; y: number } | null
  // ------------------------------------------------------------------ C: スケッチ
  /**
   * スケッチを描き直す(渡さなかったスケッチの描画は消える)。**図形は線、領域は面**で
   * 描く。ソリッドの描画(`update`)とは別のグループに持つので、押し出しで形が変わっても
   * スケッチは消えない。
   */
  showSketches(sketches: SketchDraw[]): void
  /**
   * 確定前の図形(スケッチの幽霊)を描く。`entity` が null で消える。
   *
   * **サーバへ投げない**。図形の形はブラウザ側で分かっており、コアで投機実行すると
   * 文書に一度は入る(戻すとはいえ)。Escで中断しても文書が変わらないことを、
   * 「そもそも送らない」で成立させる。
   */
  showSketchGhost(sketchId: string, entity: { kind: string; params: Record<string, number> } | null): void
  /**
   * スケッチモードの対象を決める。`null` でモードを抜ける。
   *
   * モード中のクリックは**面ではなく**「領域」または「平面上の点」になる
   * (`PickEvent.point` / `PickEvent.region`)。描いている最中に奥のソリッドの面を
   * 掴んでしまうと、線を引くつもりのクリックが選択の置き換えになる。
   */
  setActiveSketch(sketchId: string | null): void
  /** スケッチ平面を正面から見る視点へ移す(無いと描きにくい)。 */
  lookAtSketch(sketchId: string): void
  /**
   * 今あるもの全体が入る視点へ戻す。
   *
   * **`lookAtSketch` の戻り道ではない**(2026-08-26)。抜けたときの復帰は
   * `setActiveSketch(null)` が「入る前の視点」へ戻す形で担う。こちらは
   * 「今あるもの全体」という別の視点で、実機検査が図形を画面内へ収めるために呼ぶ。
   */
  frameAll(): void
  /**
   * 新規文書にしたときに呼ぶ。**カメラは動かさない**(新規は空の文書で、合わせる内容が
   * 無いため`frameAll`を呼ぶ理由が無い)。基準グリッドの縮小だけを許可する
   * (`frameAll`と同じヒステリシスの「許可」を、姿勢を動かさずに与える。2026-08-30)。
   */
  resetGrid(): void
  /**
   * 左ドラッグのウィンドウ選択が終わったときに呼ばれる(矩形に**完全包含**されたものだけ)。
   *
   * `onPick` と別の入口にしてあるのは、返すものが「1つの当たり」ではなく**集合**だから。
   * ドラッグの向きは見ない(左右どちらへ引いても同じ判定。本人明言)。
   */
  onBoxSelect(listener: (event: BoxSelectEvent) => void): void
  /**
   * カメラの画角[度]を変える。**`FOV_ORTHO`(0) は完全正投影**で、そのとき裏では
   * `OrthographicCamera` へ丸ごと差し替わる(近似ではない)。0以外は
   * `FOV_MIN`〜`FOV_MAX` に丸め、NaN・∞は無視する。
   *
   * **画面の設定であって文書の状態ではない。** コアにも操作ログにも行かず、undoの
   * 対象にもならない。値の持ち主はブラウザ側(`App.svelte` + `viewSettings.ts`)である。
   *
   * **見かけの大きさの補正が入る** —— 注視点の面で画面の縦に収まる世界長さを切り替えの
   * 前後で保つ(透視なら距離、正投影なら視錐台の大きさで作る)。**正投影への切り替えでも
   * 同じ**で、変わるのはパースの強さだけである。これをしないと画角の変更がただの
   * 「ズーム」になり、比べたいものが比べられない。
   */
  setFov(deg: number): void
  // ------------------------------------------------------------------ 可変解像度(ズーム連動)
  /**
   * 今の画面が要求している解像度。**実機検査と `App.svelte` の両方が読む**
   * (前者は「寄ったら本当に細かくなったか」を数値で見るため、後者は送る値のため)。
   */
  viewResolution(): ViewportResolution
  /**
   * face単位の高精度化(視錐台+バックフェイスカリング)を**切る/入れる**。
   *
   * **実機検査のための穴である**(既定は入り)。「導入前後で三角形数と測り直しの時間が
   * どれだけ変わったか」は、同じモデル・同じ視点で両方を測って初めて言える ——
   * 版を戻して測り直すのでは、機械が後から回せる証拠にならない
   * (`web/scripts/smoke-face-culling.mjs`)。切ると `focus` を送らなくなるので、
   * サーバは2026-08-29時点の「文書全体で1つの精度」と同じ振る舞いに戻る。
   */
  setFaceCulling(enabled: boolean): void
  /**
   * 解像度が**意味のある幅で**変わったときに呼ばれる(ズーム・画角変更・寸法変更)。
   *
   * **落ち着いてから1回だけ呼ぶ。** ホイールを回している間ずっと呼ぶと、27部品ぶんの
   * 再テセレーションがサーバで連続して走る —— カメラが止まったことを確かめ(減衰が
   * 収まるまで待つ)、かつ偏差が一定比以上動いたときだけ通す。
   *
   * スケッチの折れ線(ブラウザ内で完結する経路)は**この呼び出しと同じ瞬間に**
   * ここが自分で描き直す。呼び出し側がやるのはサーバへ知らせることだけである。
   */
  onResolutionChange(listener: (resolution: ViewportResolution) => void): void
  /**
   * スケッチの図形1本ごとの折れ線の点数(`sketch_id` -> 図形順の点数)。
   *
   * **実機検査フック専用**。「寄ったら円が滑らかになった」は目でしか分からないので、
   * 代わりに**分割数そのもの**を機械で見られるようにする(`getGeometry` と同じ species の穴。
   * ソリッド側は `getGeometry().deviation` と三角形数がその役をする)。
   */
  sketchPolylineCounts(): Record<string, number[]>
  /**
   * 今のカメラの姿勢(ワールド座標)。**実機検査が視点の向きそのものを見るための穴**で、
   * 描画にも操作にも使わない。
   *
   * ここから確かめたいのは2点: `up` がワールド+Zであること、および**水平ドラッグで
   * オービットさせても `position.z - target.z` が変わらない**こと(＝回転の極が+Zであること)。
   * 後者は `OrbitControls` が生成時の up を `_quat` へ焼き込む仕様のせいで**静かに壊れる**
   * 種類の不具合で、画面の見た目からは分からない。
   *
   * `near`/`far` も返す(2026-08-30)。こちらも実機検査の穴で、文書の状態ではないので
   * コアへは何も飛ばさない(`App.svelte` の `window.__duocad` と同じ方針)。
   */
  cameraPose(): {
    position: [number, number, number]
    target: [number, number, number]
    up: [number, number, number]
    near: number
    far: number
  }
  /**
   * 基準平面の**ピック範囲**の今の半幅[mm](`gridScale.ts` の `extentFor` が導いた値)。
   * near/farの下限と面外の軸線の長さも同じ値から引く。**描画の範囲ではない** ——
   * 格子はシェーダーで無限に描く(2026-09-03)。
   *
   * **実機検査フック専用**(2026-08-30) —— 「指せる範囲が大きなモデルに合わせて広がった」
   * ことは見た目からは何mmかまで分からないので、値そのものを機械で見られるようにする。
   * 文書の状態ではないので、コアへは何も飛ばさない(`cameraPose` と同じ方針)。
   */
  gridScale(): { extent: number }
  /**
   * グリッドの3段(1/10/100mm)の今の見え方。**実機検査フック専用**(2026-09-03に
   * `gridScale().step` を置き換えたもの —— 刻みが固定になり `step` は何も語らなくなった
   * ので、代わりに「いま各段がどれだけ濃いか」と「どの平面を出しているか」を出す)。
   *
   * `planes` は表示中の平面の名前で、基準平面(`XY`/`XZ`/`YZ`)か、任意面へのスケッチ中は
   * `sketch-face`(名前は `desiredGridPlanes` が付ける)。
   * `axis` は**面外に線として描いている軸**(無ければ null)。
   * `sketchFaceExtent` は任意面グリッドの表示範囲の半幅[mm](`faceUvExtent` の目安。
   * 無限のまま・任意面でないときは null)。
   */
  gridTiers(): {
    mmPerPixel: number
    tiers: Array<{ step: number; alpha: number }>
    planes: string[]
    axis: 'X' | 'Y' | 'Z' | null
    sketchFaceExtent: number | null
  }
  /**
   * ワールド座標の点が画面のどこに写るか(ビューポート左上を原点とする画素)。
   * カメラの前後のクリップ範囲の外なら null。
   *
   * **`project*` の仲間ではない** —— あちらは「その画素が本当にそれを取れる」ことまで
   * 確かめて返すピック用の穴だが、こちらは投影そのものを問う。**「+Zが画面で上か」は
   * 遮蔽と無関係に決まる**(真上から見れば下面は隠れて `project` が null を返すが、
   * 上下関係は変わらない)ので、ピックできるかを条件にすると測りたいものが測れない。
   */
  projectPoint(point: readonly [number, number, number]): { x: number; y: number } | null
  /** スケッチ平面上の (u,v) の画面座標。実機検査が**本物のクリックで描く**ために要る。 */
  projectSketchPoint(sketchId: string, u: number, v: number): { x: number; y: number } | null
  /**
   * 基準平面の上の点の画面座標(無理なら null)。**実機検査が本物のクリックで
   * 基準平面を指す**ために要る穴で、面の `project` と同じ流儀:
   * 返す前に**その画素からのレイが実際にその平面へ当たり、かつ手前に面が無いこと**を
   * 確かめる(部品に隠れている所を返すと、クリックが面に取られる)。
   *
   * **`showBasePlanes(true)` の間しか返らない** —— 指せない状態の座標を返しても、
   * そこをクリックした結果は平面のピックにならないため。
   */
  projectBasePlane(name: BasePlaneName): { x: number; y: number } | null
  /**
   * 領域の画面座標(実機検査が領域を本物のクリックで選ぶために要る)。
   *
   * **今その領域が指せる状態でなければ null** —— 返す前に `regionAt` で当たることを
   * 確かめるので、通常モードでは `pickRegions(true)` の間しか返らない
   * (`projectBasePlane` と同じ流儀。指せない状態の座標を返しても、そこをクリックした
   * 結果は領域のピックにならない)。
   */
  projectRegion(sketchId: string, index: number): { x: number; y: number } | null
  /**
   * ビューポート右上に常設するビューキューブの矩形(コンテナ内のCSSピクセル、左上原点)。
   * **実機検査フック専用**(タスク仕様の落とし穴3) —— 寸法が変わると右上アンカーごと
   * 矩形も動く([[viewport-resize-silently-changes-what-a-click-grabs]] と同型)ので、
   * 検査が決め打ちの座標を使わずここから読めるようにする。
   */
  viewCubeRect(): { x: number; y: number; w: number; h: number }
  dispose(): void
}

/**
 * ワールドの上方向 —— **X=右 / Y=奥 / Z=上の右手系**(Fusion 360・Blender・スライサと
 * 同じ、CADの標準)。カーネル(OCCT/build123d)は元から Z-up で、`_BASE_PLANE_AXES` の
 * "XY" の法線も `(0,0,1)` である。**ずれていたのはビューアだけだった**(2026-08-28に是正)。
 *
 * **`(0,0,1)` をこのファイルの他の場所へ直書きしないこと。** up軸の値が2か所以上に散ると
 * 次の改修で必ず片方が取り残される —— 直前まで `camera.up.set(0, 1, 0)` の直書きが2か所
 * あり、**スケッチに1回入って出るだけで Y-up へ戻っていた**。
 */
const WORLD_UP = new THREE.Vector3(0, 0, 1)

/**
 * **three.js の既定の上方向そのものを、モジュールの読み込み時に差し替える。**
 *
 * `camera.up` を後から書き換える形では効かない箇所が2つあるので、**カメラ・
 * OrbitControls・ライトを1つも `new` する前に**ここで確定させる(いずれも three 0.185.1 の
 * 実物で確認):
 *
 * - `OrbitControls` はコンストラクタで `_quat` に**生成時の `object.up`** を焼き込み、
 *   `update()` で再計算しない。ここが Y のままだと、見た目の上方向は正しいのに
 *   **水平ドラッグの回転軸だけが Y軸まわりのまま静かに残る**。
 * - `HemisphereLight` はコンストラクタで `position` に `DEFAULT_UP` を複写し、
 *   **その position が空(sky)の向きになる**。ここが Y のままだと天地が横倒しになる。
 */
THREE.Object3D.DEFAULT_UP.copy(WORLD_UP)

const BASE_COLOR = 0x9aa6b2
const HIGHLIGHT_COLOR = 0xffb454
/** スケッチの線・確定前の線・領域。ソリッドの色と混ざらない色にする。 */
const SKETCH_COLOR = 0xe8eef6
const SKETCH_GHOST_COLOR = 0x7ee081
//: スケッチの点マーカー(未選択)。線(`SKETCH_COLOR`)より暗くして線を主役に保つ。
const SKETCH_HANDLE_COLOR = 0x9fb3c8
//: 選択中のスケッチの点・辺。**選択の色は1つ**(面・辺・領域と同じ `HIGHLIGHT_COLOR`)。
const SKETCH_SELECTED_COLOR = HIGHLIGHT_COLOR
const REGION_COLOR = 0x4d7ea8
const REGION_SELECTED_COLOR = 0xffb454
/** AIの提案(`origin === "proposed"`)。確定(橙)と**色相で**離す。 */
const PROPOSED_COLOR = 0x36d1ff
/** 確定前の形。半透明で「まだ無いもの」に見せる。 */
const PREVIEW_COLOR = 0x7ee081
/**
 * 形状最適化(第3のモード)の役割色。**選択が勝つ**(本人決定) —— 選ばれているあいだは
 * `HIGHLIGHT_COLOR` / `PROPOSED_COLOR` になり、役割色は一時的に見えなくなる
 * (`applyFaceMaterials` が実体。役割は `state !== 'plain'` のとき無視する)。
 */
const OPTIMIZE_PRESERVE_COLOR = 0x3f9b58
const OPTIMIZE_EXCLUDE_COLOR = 0xc75448
/** 拘束面(青系に塗る)。 */
const OPTIMIZE_FIXTURE_COLOR = 0x3f7fc2
/** 拘束の固定記号(四角錐)と、荷重の矢印・ラベルの色。面の塗りとは別に見分ける。 */
const OPTIMIZE_FIXTURE_MARK_COLOR = 0x1c3550
const OPTIMIZE_LOAD_COLOR = 0xffc857
/** 面の境界線。背景(`BACKGROUND_COLOR`)に埋もれないよう、2026-08-23に`GRID_COLOR`と
    同程度の明るさへ上げた。`PICK_EDGE_COLOR`とは見分けがつく明るさを保つ(下記コメント)。 */
const EDGE_COLOR = 0x3f4a56
/** 選べる辺。面の境界線(`EDGE_COLOR`)と**見分けがつく明るさ**にする。 */
const PICK_EDGE_COLOR = 0xbfd4ea
/** 辺の当たり判定の太さ[画素]。細すぎると掴めず、太すぎると隣の辺が取れる。 */
const EDGE_PICK_PIXELS = 6
//: スケッチの点マーカーの当たり半径[画素]。**辺より小さく**しておく
//: (点は辺の上に乗っているので、同じ太さだと辺を狙えない)。
const SKETCH_HANDLE_PIXELS = 5
//: ウィンドウ選択と見なすドラッグの最小距離[画素]。**クリックの上限と同じ値**にして、
//: 「クリックでもドラッグでもない画素」が生まれないようにする(下の `CLICK_MOVE_LIMIT_PX`)。
const BOX_SELECT_MIN_PX = 6
/** 頂点マーカーの半径[画素]。辺の当たり判定(`EDGE_PICK_PIXELS`)より細くする ——
    太いと、辺を狙うクリックが端点のマーカーに吸われる(頂点は必ず辺の端に乗っている)。 */
const VERTEX_PIXELS = 4
const BACKGROUND_COLOR = 0x161a1f
/**
 * 平面グリッドの格子線。単位は mm。**刻みは 1/10/100mm の3段固定**(`gridScale.ts` の
 * `TIERS`)で、内容の大きさでも視点でも変わらない —— 変わるのは段ごとの不透明度だけで、
 * 線の太さがそのまま寸法の意味になる(2026-09-03本人決定)。描くのは `planeGrid.ts` の
 * 無限グリッドで、範囲という概念を持たない。`root` には入れない(初回フレーミングが
 * グリッドの箱に引っ張られるのを避ける)。
 */
const GRID_COLOR = 0x3a4450
/**
 * スケッチのコマンドを起動している間のグリッド。**「今これが指せる」が分かればよく、
 * 見た目は作り込まない**(当たり判定の感触と見た目は後日ユーザーが対話で詰める)。
 */
const GRID_ARMED_COLOR = 0x7a90ab

/**
 * 基準3平面の解析的な定義(**描いているグリッドとは別物**)。ピック範囲は ±`gridExtent`
 * (可変)。3枚とも原点を通るので three.js の `Plane` の `constant` は 0 でよい。
 *
 * **u/v はコアの `_BASE_PLANE_AXES`(`duocad/kernel.py`)と同じ取り方**にしてある ——
 * スケッチのframeがどの基準平面のものかは、この表との一致で判定する
 * (`basePlaneForFrame`)。法線の符号だけはコアと違う(XZがコアでは -Y)が、
 * 解析交差にも一致判定にも符号は効かないので揃えていない。
 */
const BASE_PLANES: ReadonlyArray<{
  name: BasePlaneName
  normal: readonly [number, number, number]
  u: readonly [number, number, number]
  v: readonly [number, number, number]
}> = [
  { name: 'XY', normal: [0, 0, 1], u: [1, 0, 0], v: [0, 1, 0] },
  { name: 'XZ', normal: [0, 1, 0], u: [1, 0, 0], v: [0, 0, 1] },
  { name: 'YZ', normal: [1, 0, 0], u: [0, 1, 0], v: [0, 0, 1] },
]

/**
 * `projectBasePlane` が順に試す平面上の点[mm]。
 *
 * **u も v も 0 にしない** —— 3平面は原点と3本の軸で交わっており、どちらかが0の点は
 * 2枚に同時に乗る(例: XY平面上の (60, 0) は世界座標で y=0 なので XZ 平面の上でもある)。
 * そこを返すと、レイ距離が同じ2枚のどちらが取れるかが並び順で決まってしまう。
 *
 * **座標は固定でよい** —— 最大でも85で、`gridExtent` は `BASE_EXTENT`(=100)を
 * 下限に**広がる方向にしか動かない**ので、この点は常に描画範囲の内側にある。
 */
const BASE_PLANE_PROBES: ReadonlyArray<readonly [number, number]> = [
  [30, 30], [-30, 30], [30, -30], [-30, -30],
  [60, 60], [-60, 60], [60, -60], [-60, -60],
  [15, 45], [-45, 15], [45, -15], [-15, -45],
  [85, 85], [-85, -85], [10, 10], [-10, -10],
]
/** 軸は色で見分ける(X赤 / Y緑 / Z青)。グリッド本体より少し明るく、選択色とは被らない。 */
const AXIS_X_COLOR = 0xc45c5c
const AXIS_Y_COLOR = 0x5cad6e
const AXIS_Z_COLOR = 0x5a8ec4

/**
 * 基準平面ごとの「面内の2軸」と「面外の軸」。**面内の軸は平面グリッドのシェーダーが描き、
 * 面外の軸だけ線ジオメトリで描く**(`planeGrid.ts` は平面の中しか知らないため)。
 *
 * 3平面を同時に出すとき(スケッチの対象待ち)は、同じ世界軸を2枚が二重に描かないよう
 * `desiredGridPlanes` が面内の軸を割り振る —— この表はあくまで「その平面の u/v は
 * どの軸か」を持つだけである。
 */
const BASE_PLANE_AXES: Record<
  BasePlaneName,
  {
    uColor: number
    vColor: number
    /** 面外の軸(その平面に直交する world 軸)。 */
    outOfPlane: { name: 'X' | 'Y' | 'Z'; dir: readonly [number, number, number]; color: number }
  }
> = {
  XY: { uColor: AXIS_X_COLOR, vColor: AXIS_Y_COLOR, outOfPlane: { name: 'Z', dir: [0, 0, 1], color: AXIS_Z_COLOR } },
  XZ: { uColor: AXIS_X_COLOR, vColor: AXIS_Z_COLOR, outOfPlane: { name: 'Y', dir: [0, 1, 0], color: AXIS_Y_COLOR } },
  YZ: { uColor: AXIS_Y_COLOR, vColor: AXIS_Z_COLOR, outOfPlane: { name: 'X', dir: [1, 0, 0], color: AXIS_X_COLOR } },
}

/**
 * **描く順序の層番号(1箇所にまとめる)。** 生の数値をコード中に書かない
 * (engine `duocad-depth-overlay-rule` の併記事項)。
 *
 * 負の3つは装飾(B群。深度検査あり・深度書き込み無し)、0以上は既存の階段
 * (A群＝ピック対象は `depthTest: false` なので、順序がそのまま前後関係になる)。
 */
const LAYER = {
  planeGrid: -3,
  gridAxisLine: -2,
  basePlaneHover: -1,
  solid: 0,
  optimize: 1,
  edge: 10,
  region: 11,
  sketchLine: 12,
  sketchPoint: 13,
} as const

/**
 * **B群の深度バイアスの単位量**(クリップ空間。`gl_Position.z -= 層番号 * これ * w`)。
 * 法線方向にジオメトリを動かして逃がすことは禁止 —— 描画のズレをレイ検査へ持ち込まない
 * (engine `duocad-depth-overlay-rule`)。
 *
 * 1e-4 は「共平面の面より確実に手前」かつ「1mm程度の段差なら順序が入れ替わらない」
 * 大きさとして選んだ(near=0.1・far=数千mmの視錐台での実測レンジ)。
 */
const DEPTH_BIAS_UNIT = 1.0e-4

/**
 * B群の層番号(大きいほどカメラ側)。**`sketchFaceGrid` は別枠**(2026-09-03) ——
 * 任意面のスケッチ平面は貼り付けた面(A群のソリッド。深度書き込みあり)と完全に同一平面で
 * 争うので、床・基準平面グリッド(`planeGrid`。通常は開いた空間に浮いていて争う相手がいない)
 * と同じバイアス経路を使いつつ、層番号だけ分けて併記事項の規約(層は名前付き定数表)に従う。
 */
const DEPTH_BIAS_LAYER = { planeGrid: 1, gridAxisLine: 2, basePlaneHover: 3, sketchFaceGrid: 4 } as const

/**
 * カメラの画角[度]の下限・上限・既定。**UIとlocalStorage(`viewSettings.ts`)も
 * ここを import する** —— 範囲の正を2か所に書かない。
 *
 * 90を超えると端の歪みが強すぎて形の判断に使えない。**下限が30なのは、そこから下に
 * 欲しいのが「弱いパース」ではなく正投影そのものだから** —— 画角を絞って正投影を
 * 真似ると距離補正でカメラが際限なく遠ざかり(30度で既に注視点距離が3.7倍)、深度の
 * 精度を捨てるわりに平行投影にはならない(手前と奥の見かけの比は**画角ではなく距離で
 * 決まる**ので、画角をいくら絞っても1にならない)。そこで**1〜29は選べる値に無く**、
 * 0(`FOV_ORTHO`)は近似ではなく本物の `OrthographicCamera` へ切り替える。
 */
export const FOV_MIN = 30
export const FOV_MAX = 90
export const FOV_DEFAULT = 45

/**
 * 完全正投影を表す画角(2026-08-28本人指示で追加)。**「とても小さい画角」ではない**
 * —— これを渡すと `PerspectiveCamera` ではなく `OrthographicCamera` が使われ、
 * パースが完全に消える(同じ長さの辺は奥行きが違っても同じ長さに写る)。
 */
export const FOV_ORTHO = 0

/**
 * 選べる画角か。**0ちょうど、または30〜90**の2択で、あいだの1〜29は無い。
 * UI(`SettingsDialog.svelte`)とlocalStorage(`viewSettings.ts`)はこれを呼ぶ ——
 * 「選べる値」の正をこの1か所に閉じるため。
 */
export function isSelectableFov(value: number): boolean {
  return Number.isFinite(value) && (value === FOV_ORTHO || (value >= FOV_MIN && value <= FOV_MAX))
}

/**
 * `lookAtSketch` が図形の外接半径に掛ける余白。
 * **45度で従来の `radius * 3` と一致させるための余白**(`1.243/tan(22.5°) ≈ 3.00`)。
 */
const SKETCH_FIT_MARGIN = 1.243

/**
 * 既定の視点の向き(front-top-right)。**距離に掛けて注視点へ足す**ためのベクトルで、
 * **長さは1ではなく 1.5362**(= `hypot(1, 1, 0.6)`)。初期視点と `frameToContent` の
 * 2か所が使う。
 *
 * **掛けた値がそのまま注視点距離になるわけではない** —— `frameToContent` は
 * `fitDistance` の 1.5362倍の位置にカメラを置くので、収まるぎりぎりより一回り引いた絵に
 * なる。**この余白は既定の見え方そのもの**なので直さない(直すと全画角で見かけが1.54倍に
 * なる)。ただし正投影は視錐台で大きさを作るため、この係数を掛けないと透視と揃わない
 * (`frameToContent`)。
 *
 * **X と Y に等量、上方向(Z)は控えめ** —— Y-up 時代の `(d, 0.6d, d)` の構図をそのまま
 * Z-up へ移したもので、注視点からの距離は以前と変わらない(距離を決めるのは `fitDistance`
 * であり、こちらは向きだけを持つ)。**Y が負**なのは duocad では Y=奥 だからで、`+Y` から
 * 見ると背面になる —— Fusion / SolidWorks の既定アイソメと同じ
 * front(−Y) / top(+Z) / right(+X) の側から見る。
 */
const HOME_VIEW_OFFSET = new THREE.Vector3(1, -1, 0.6)

/** クリックとドラッグ(視点回転)の境目。これ以上動いたら選択ではなく回転とみなす。 */
const CLICK_MOVE_LIMIT_PX = 6
const CLICK_TIME_LIMIT_MS = 500

/**
 * ホバー強調で上げる明度(HSLのL)。**3色目は足さない**(2026-08-27本人決定) ——
 * 色相が「誰の選択か」(user=橙 / proposed=水色)、明度が「今カーソルの下にあるか」を
 * 表す2軸にする。未選択にも選択済みにも同じ幅で掛けるので、規則は
 * 「カーソルの下にあるものは明るい」の1行で済む。
 */
const HOVER_LIGHTEN = 0.16
/** ホバー中の基準平面に重ねる板の濃さ。グリッドの線が透けて見える程度に留める。 */
const BASE_PLANE_HOVER_OPACITY = 0.14

/**
 * ビューキューブ(常設・26領域)。一辺[画素]と右上の余白[画素](タスク仕様: 120px角、余白12px)。
 * ビューポート左上には浮くブラウザがある([[duocad-ui-moved-to-fusion-layout]])ので右上は空いている。
 */
const CUBE_SIZE_PX = 120
const CUBE_MARGIN_PX = 12
/** `pointerdown` からこれだけ動いたらドラッグ(それ未満で `pointerup` ならクリック=スナップ)。 */
const CUBE_DRAG_THRESHOLD_PX = 4
/** クリックでスナップするときの視点アニメーション時間[ms](タスク仕様: 300ms前後)。 */
const CUBE_SNAP_MS = 300
/** キューブ用の独立した正投影カメラの視錐台半幅。キューブは半幅1の立方体(対角 `sqrt(3)`)なので、
 *  角が切れない程度に広く取る。 */
const CUBE_ORTHO_HALF = 1.8
/** キューブ用カメラを置く距離。正投影なので見かけの大きさには影響しない(near/farに収まればよい)。 */
const CUBE_CAMERA_DISTANCE = 4
/** 面セルの日本語ラベル。キーは `"i,j,k"`。Z-up右手系(タスク仕様: 上=+Z/下=-Z/前=-Y/後=+Y/右=+X/左=-X)。 */
const CUBE_FACE_LABELS: Readonly<Record<string, string>> = {
  '1,0,0': '右',
  '-1,0,0': '左',
  '0,1,0': '後',
  '0,-1,0': '前',
  '0,0,1': '上',
  '0,0,-1': '下',
}

/** 色相・彩度はそのままに明度だけ上げる(`HOVER_LIGHTEN` の実体)。 */
function lighten(hex: number, amount: number): number {
  const color = new THREE.Color(hex)
  const hsl = { h: 0, s: 0, l: 0 }
  color.getHSL(hsl)
  color.setHSL(hsl.h, hsl.s, Math.min(1, hsl.l + amount))
  return color.getHex()
}

/** 1個のBodyが画面に持つメッシュ(面ごとの三角形メッシュ+境界線)。 */
interface BodyMeshes {
  /** ピック判定の対象(面の三角形メッシュだけ)。 */
  faceMeshes: THREE.Mesh[]
  /** `root` に足した全オブジェクト(面メッシュ+境界線)。clearBodyで**これを**取り除く
   * (面メッシュだけを外すと境界線(LineSegments)がシーンに残り続け、dispose済みの
   * ジオメトリを参照するゴースト線としてメモリリークする)。 */
  sceneObjects: THREE.Object3D[]
  disposables: Array<{ dispose(): void }>
}

/**
 * B群(装飾。ピック対象でない)の深度バイアスを材質へ仕込む —— クリップ空間で
 * `gl_Position.z -= 層番号 * 単位量 * w` だけカメラ側へ寄せる
 * (engine `duocad-depth-overlay-rule`)。**ジオメトリは1mmも動かさない**ので、
 * 描画のズレがレイ検査(`basePlaneAt` の解析交差・面のピック)へ漏れない。
 *
 * `customProgramCacheKey` を層ごとに変える —— 変えないと、同じ材質種で層の違う
 * プログラムが1つに共有されて**後から作った方のバイアスが効かない**。
 *
 * `#include <project_vertex>` が無い材質へ掛けたら例外にする(黙って素通しにしない)。
 */
function withDepthBias<T extends THREE.Material>(material: T, layer: number): T {
  const bias = (layer * DEPTH_BIAS_UNIT).toFixed(8)
  material.onBeforeCompile = (shader) => {
    const marker = '#include <project_vertex>'
    if (!shader.vertexShader.includes(marker)) {
      throw new Error(`深度バイアスを掛けられない材質(project_vertexが無い): ${material.type}`)
    }
    shader.vertexShader = shader.vertexShader.replace(
      marker,
      `${marker}
	gl_Position.z -= ${bias} * gl_Position.w;`,
    )
  }
  material.customProgramCacheKey = () => `duocad-depth-bias-${bias}`
  return material
}

export function createViewport(container: HTMLElement): Viewport {
  const renderer = new THREE.WebGLRenderer({ antialias: true })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
  renderer.domElement.style.display = 'block'
  renderer.domElement.style.width = '100%'
  renderer.domElement.style.height = '100%'
  renderer.domElement.style.touchAction = 'none'
  container.appendChild(renderer.domElement)

  const scene = new THREE.Scene()
  scene.background = new THREE.Color(BACKGROUND_COLOR)

  /**
   * **カメラは2つあり、`camera` が今どちらを使っているかを指す。**
   *
   * 正投影は「画角0度の透視」ではなく別種の投影なので、1つのカメラでは表せない
   * (`FOV_ORTHO` の説明を参照)。**姿勢(position / up / near / far)は切り替えのたびに
   * 写す**ので、どちらを使っていても視点は同じ ——「どちらのカメラか」は投影の作り方
   * だけの違いに閉じている。
   *
   * **`camera` を直接見てよいのは姿勢と投影の共通部分だけ**。`fov` / `aspect` は
   * 透視にしか無く、`left`/`right`/`top`/`bottom`/`zoom` は正投影にしか意味が無いので、
   * それらを触る処理は `perspective` / `orthographic` を名指しすること。
   */
  const perspective = new THREE.PerspectiveCamera(FOV_DEFAULT, 1, 0.1, 10000)
  const orthographic = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 10000)
  let camera: THREE.PerspectiveCamera | THREE.OrthographicCamera = perspective
  // 空の文書でも原点まわりが見える位置。部品が届いたら `frameToContent` が上書きする。
  // **`BASE_EXTENT` を使う**(可変の `gridExtent` ではない) —— ここは起動直後の1回きりで、
  // `gridExtent` はまだ宣言されておらず(下の平面グリッドの節より前)、その初期値は
  // 常に `BASE_EXTENT` だから同じ値になる。
  {
    const diagonal = Math.hypot(BASE_EXTENT * 2, BASE_EXTENT * 2, BASE_EXTENT * 2)
    const distance = diagonal * 1.2
    camera.position.copy(HOME_VIEW_OFFSET).multiplyScalar(distance)
  }
  scene.add(camera)
  // 光をカメラに付ける(視点を回しても面が真っ黒にならない。見た目の作り込みではなく可視性)
  const headlight = new THREE.DirectionalLight(0xffffff, 1.6)
  headlight.position.set(0.4, 0.7, 1)
  camera.add(headlight)
  scene.add(new THREE.HemisphereLight(0xdfe7f2, 0x191c21, 1.0))

  // **型引数を明示する** —— 省くと生成に渡した `perspective` で固まり、正投影へ
  // 差し替えるときに `controls.object` が受け取れない(`activateCamera`)。
  const controls: OrbitControls<THREE.PerspectiveCamera | THREE.OrthographicCamera> =
    new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.dampingFactor = 0.15
  // **マウスの割り当て(2026-08-30本人決定)**: 左=ウィンドウ選択(このファイルが自前で
  // 実装する)/ 中=パン / 右=カメラ回転。
  //
  // `OrbitControls` の既定は LEFT=ROTATE / MIDDLE=DOLLY / RIGHT=PAN なので、**3つとも
  // 変える**(中ボタンは「既定のままパン」ではなくドリーだった)。左を `null` にするのは、
  // 左ドラッグを丸ごとこちらへ渡すため —— 左に何か割り当てたままだと、矩形を引く間に
  // カメラが動いて「囲ったつもりの範囲」と実際の範囲がずれる。
  controls.mouseButtons = {
    LEFT: null,
    MIDDLE: THREE.MOUSE.PAN,
    RIGHT: THREE.MOUSE.ROTATE,
  }

  const root = new THREE.Group()
  scene.add(root)

  // ------------------------------------------------------------------ 形状最適化(第3のモード)の注記
  //
  // 拘束の固定記号・荷重の矢印+ラベル。**`root` には入れない**(グリッドと同じ理由 ——
  // 外接直方体に混ざると初回フレーミングやnear/far計算を歪める)。**指せない**(装飾)ので
  // ピックの対象にも足さない。`setOptimizeState` が唯一の書き手。
  const optimizeRoot = new THREE.Group()
  optimizeRoot.renderOrder = LAYER.optimize
  scene.add(optimizeRoot)
  let optimizeDisposables: Array<{ dispose(): void }> = []

  // ------------------------------------------------------------------ 平面グリッド(3段固定・無限)
  //
  // **「ある平面frame(原点・u軸・v軸)を受け取って描くもの」**(`planeGrid.ts`)。XY床は
  // その一利用者にすぎず、どの平面を出すかは状態が決める(`desiredGridPlanes`)。
  // `root` には入れない —— 入れると初回の `frameToContent` がグリッドの箱に引っ張られる。
  // ピック対象にもしない(判定は `basePlaneAt` の解析交差が別に持つ)。
  const gridRoot = new THREE.Group()
  // Group の `renderOrder` は three.js では groupOrder として子全体に効く。中身どうしの
  // 前後は各オブジェクトの `LAYER.*` が決める。
  gridRoot.renderOrder = -1
  scene.add(gridRoot)

  /**
   * 基準平面のピック範囲の半幅[mm]。**描画範囲ではない**(格子は無限) ——
   * `basePlaneAt` のピック範囲・`updateClipPlanes` のnear/far下限・面外の軸線の長さが
   * ここを見る。`applyGridScale` だけが動かす。
   */
  let gridExtent = BASE_EXTENT

  /** 基準3平面を指せるか(`showBasePlanes`)。**出していない間は当たり判定を回さない**。 */
  let basePlanesOn = false

  /**
   * スケッチ中のスケッチ平面のframe(モード外は null)。**書き手は `setGridSketchFrame`
   * だけ**で、`setActiveSketch` と `showSketches` の両方から呼ぶ —— モードに入った時点では
   * まだ描画指示が届いておらず、frameを知らないことがある。
   */
  let sketchGridFrame: PlaneFrame | null = null

  /**
   * `sketchGridFrame` が任意面(基準平面でない)のときの表示範囲の半幅[mm]。
   * `faceUvExtent` が測れなかった・基準平面のときは `null`(無限)。**書き手は
   * `setGridSketchFrame` だけ**(`sketchGridFrame` と対で更新する)。
   */
  let sketchGridExtent: number | null = null

  /** 実機検査フック(`gridTiers`)へ出す、いま表示している平面の名前。 */
  let gridPlanesShown: string[] = []

  /**
   * 同時に出しうる平面は最大3枚(対象待ちの XY/XZ/YZ)。**作り直さず使い回す** ——
   * frameを差し替えるだけで別の平面になる。
   */
  const planeGrids: PlaneGrid[] = [createPlaneGrid(), createPlaneGrid(), createPlaneGrid()]
  for (const grid of planeGrids) {
    grid.object.renderOrder = LAYER.planeGrid
    // 深度バイアスは `applyGridPlanes()`(直後に必ず呼ぶ)が状態ごとに入れ直す
    // (`GridPlaneSpec.depthBiasLayer`) —— ここでは初期化しない。
    grid.setVisible(false)
    gridRoot.add(grid.object)
  }

  /** 面外の軸(1本)。面内の軸はグリッドのシェーダーが描くので、線にするのはこれだけ。 */
  const gridAxisMaterial = withDepthBias(
    new THREE.LineBasicMaterial({
      color: AXIS_Z_COLOR,
      // 不透明だが**透明パスへ入れる** —— 実体より後に描かないと、共平面の面に隠れる。
      transparent: true,
      depthWrite: false,
    }),
    DEPTH_BIAS_LAYER.gridAxisLine,
  )
  let gridAxisLine: THREE.Line | null = null
  let gridAxisShown: 'X' | 'Y' | 'Z' | null = null

  /**
   * ホバー中の基準平面に重ねる板(**同時に見えるのは多くても1枚**)。ピック範囲が変わる
   * たびに作り直す(`rebuildBasePlaneHover`)。
   *
   * 平面ごとの色分けを線でやる代わりに半透明の板を重ねて「今カーソルが指しているのは
   * この平面」を出す(2026-08-27本人決定)。**当たり判定には使わない** —— 判定は
   * 今までどおり `basePlaneAt` の解析交差である。
   */
  const basePlaneHoverMeshes = new Map<BasePlaneName, THREE.Mesh>()
  let basePlaneHoverDisposables: Array<{ dispose(): void }> = []

  function basePlaneSpec(name: BasePlaneName): (typeof BASE_PLANES)[number] {
    const found = BASE_PLANES.find((spec) => spec.name === name)
    if (found === undefined) throw new Error(`基準平面が表に無い: ${name}`)
    return found
  }

  function baseFrame(name: BasePlaneName): PlaneFrame {
    const spec = basePlaneSpec(name)
    return { origin: [0, 0, 0], u: spec.u, v: spec.v }
  }

  /**
   * frameがどの基準平面のものか(u/vの一致で見る)。基準平面でなければ null ——
   * **任意面のスケッチはここで null になる**。`desiredGridPlanes` はそれを見て、
   * 通常(XY床)へ落とすのではなく、そのframeへ直接グリッドを乗せる(2026-09-03)。
   */
  function basePlaneForFrame(frame: PlaneFrame): BasePlaneName | null {
    const same = (a: readonly number[], b: readonly number[]): boolean =>
      Math.abs(a[0] - b[0]) < 1e-6 && Math.abs(a[1] - b[1]) < 1e-6 && Math.abs(a[2] - b[2]) < 1e-6
    if (!same(frame.origin, [0, 0, 0])) return null
    for (const spec of BASE_PLANES) {
      if (same(frame.u, spec.u) && same(frame.v, spec.v)) return spec.name
    }
    return null
  }

  interface GridPlaneSpec {
    /** 実機検査フック(`gridTiers`)へ出す名前。 */
    name: string
    frame: PlaneFrame
    /** 面内の軸の色。`null` の側は描かない(同じ世界軸を2枚が二重に描かないため)。 */
    uColor: number | null
    vColor: number | null
    /** 表示範囲の半幅[mm]。`null` は無限(指せない・面の輪郭が測れない装飾)。 */
    extent: number | null
    /** クリップ空間の深度バイアスの層番号(`DEPTH_BIAS_LAYER`)。 */
    depthBiasLayer: number
  }

  /**
   * いま出す平面と、面外に線で描く軸(完了条件4の表そのもの)。
   *
   * | 状態 | 表示 |
   * |---|---|
   * | 通常(スケッチ外) | XY平面 + Z軸 |
   * | スケッチ起動中(対象待ち・`showBasePlanes(true)`) | 3平面すべて |
   * | 基準平面へのスケッチ中 | その平面 + それに直交する軸 |
   * | 任意面へのスケッチ中 | その面のframeのグリッドのみ(直交軸なし。本人指定) |
   *
   * 対象待ちを3平面のままにするのは、基準平面も**クリックして指す**から
   * (engine `duocad-sketch-start-is-two-steps-by-pointing`)。ここでXZ/YZを隠すと
   * 指せなくなる。3枚のときは3本の軸が全部どれかの面内に出るので、面外の軸は要らない。
   */
  function desiredGridPlanes(): {
    planes: GridPlaneSpec[]
    axis: (typeof BASE_PLANE_AXES)[BasePlaneName]['outOfPlane'] | null
  } {
    if (basePlanesOn) {
      return {
        planes: [
          { name: 'XY', frame: baseFrame('XY'), uColor: AXIS_X_COLOR, vColor: AXIS_Y_COLOR, extent: gridExtent, depthBiasLayer: DEPTH_BIAS_LAYER.planeGrid },
          { name: 'XZ', frame: baseFrame('XZ'), uColor: null, vColor: AXIS_Z_COLOR, extent: gridExtent, depthBiasLayer: DEPTH_BIAS_LAYER.planeGrid },
          { name: 'YZ', frame: baseFrame('YZ'), uColor: null, vColor: null, extent: gridExtent, depthBiasLayer: DEPTH_BIAS_LAYER.planeGrid },
        ],
        axis: null,
      }
    }
    const sketchPlane = sketchGridFrame === null ? null : basePlaneForFrame(sketchGridFrame)
    if (sketchGridFrame !== null && sketchPlane === null) {
      // 任意面(3基準平面のいずれでもない)へのスケッチ中。その面のframeへそのまま乗せる ——
      // 原点・u/v軸はスケッチ平面のもの(完了条件4)。直交軸は出さない(完了条件3・本人指定)。
      // 貼り付けた面(A群のソリッド)と完全同一平面になるので、バイアスの層番号は別枠
      // (`sketchFaceGrid`)を使う([[duocad-depth-overlay-rule]] B群・タスク指示)。
      return {
        planes: [
          {
            name: 'sketch-face',
            frame: sketchGridFrame,
            uColor: null,
            vColor: null,
            extent: sketchGridExtent,
            depthBiasLayer: DEPTH_BIAS_LAYER.sketchFaceGrid,
          },
        ],
        axis: null,
      }
    }
    const name: BasePlaneName = sketchPlane ?? 'XY'
    const axes = BASE_PLANE_AXES[name]
    return {
      planes: [
        {
          name,
          frame: baseFrame(name),
          uColor: axes.uColor,
          vColor: axes.vColor,
          extent: null,
          depthBiasLayer: DEPTH_BIAS_LAYER.planeGrid,
        },
      ],
      axis: axes.outOfPlane,
    }
  }

  function rebuildGridAxisLine(
    axis: (typeof BASE_PLANE_AXES)[BasePlaneName]['outOfPlane'] | null,
  ): void {
    if (gridAxisLine !== null) {
      gridRoot.remove(gridAxisLine)
      gridAxisLine.geometry.dispose()
      gridAxisLine = null
    }
    gridAxisShown = axis === null ? null : axis.name
    if (axis === null) return
    gridAxisMaterial.color.setHex(axis.color)
    const geometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-axis.dir[0], -axis.dir[1], -axis.dir[2]).multiplyScalar(gridExtent),
      new THREE.Vector3(axis.dir[0], axis.dir[1], axis.dir[2]).multiplyScalar(gridExtent),
    ])
    const line = new THREE.Line(geometry, gridAxisMaterial)
    line.renderOrder = LAYER.gridAxisLine
    line.raycast = () => {}
    gridRoot.add(line)
    gridAxisLine = line
  }

  /**
   * ホバー板をいまのピック範囲で作り直す。**`basePlaneAt` の判定範囲と同じ `gridExtent`
   * から作る** —— ここを取りこぼすと「板は見えているのに指せない(逆も)」という原因の
   * 遠い壊れ方をする。
   *
   * `applyBasePlaneHover()` はここでは呼ばない —— 初回呼び出しはまだ `hoverPlane` が
   * 宣言される前(TDZ)に走るため。作り直し後にホバー状態を反映するのは呼び出し元
   * (`applyGridScale`)の仕事にしてある。
   */
  function rebuildBasePlaneHover(extent: number): void {
    for (const item of basePlaneHoverDisposables) item.dispose()
    basePlaneHoverDisposables = []
    for (const mesh of basePlaneHoverMeshes.values()) gridRoot.remove(mesh)
    basePlaneHoverMeshes.clear()

    const hoverMaterial = withDepthBias(
      new THREE.MeshBasicMaterial({
        color: GRID_ARMED_COLOR,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: BASE_PLANE_HOVER_OPACITY,
        // 半透明どうしで真っ黒にならないように奥行きは書かない(`previewMaterial` と同じ)。
        depthWrite: false,
      }),
      DEPTH_BIAS_LAYER.basePlaneHover,
    )
    basePlaneHoverDisposables.push(hoverMaterial)
    for (const spec of BASE_PLANES) {
      const corners = ([[-1, -1], [1, -1], [1, 1], [-1, 1]] as const).map(
        ([su, sv]) =>
          new THREE.Vector3(
            (su * spec.u[0] + sv * spec.v[0]) * extent,
            (su * spec.u[1] + sv * spec.v[1]) * extent,
            (su * spec.u[2] + sv * spec.v[2]) * extent,
          ),
      )
      const geometry = new THREE.BufferGeometry().setFromPoints([
        corners[0], corners[1], corners[2],
        corners[0], corners[2], corners[3],
      ])
      const mesh = new THREE.Mesh(geometry, hoverMaterial)
      mesh.renderOrder = LAYER.basePlaneHover
      mesh.visible = false
      gridRoot.add(mesh)
      basePlaneHoverDisposables.push(geometry)
      basePlaneHoverMeshes.set(spec.name, mesh)
    }
  }

  /** 出す平面・面内の軸・色・面外の軸線を、いまの状態から入れ直す。 */
  function applyGridPlanes(): void {
    const wanted = desiredGridPlanes()
    const color = basePlanesOn ? GRID_ARMED_COLOR : GRID_COLOR
    planeGrids.forEach((grid, index) => {
      const spec: GridPlaneSpec | undefined = wanted.planes[index]
      if (spec === undefined) {
        grid.setVisible(false)
        return
      }
      grid.setFrame(spec.frame)
      grid.setAxisColors(spec.uColor, spec.vColor)
      grid.setColor(color)
      // **指せるときだけピック範囲で切る**(「見えるもの＝指せるもの」)。指せない
      // 装飾のときは無限 —— 3枚を無限に重ねると画面が線で埋まって何も読めなくなる、
      // という実測もある(2026-09-03のスクリーンショット確認)。任意面のスケッチ平面は
      // 指せる対象ではないが、面の外接範囲の外まで無限に伸びると画面を横切って見えるので
      // (タスク仕様)、`spec.extent`(`faceUvExtent` の目安)で切る。
      grid.setExtent(spec.extent)
      // 任意面は貼り付けた面と同一平面になるので、バイアスの層番号を別枠にする
      // (`desiredGridPlanes` が選ぶ)。他の状態は従来どおり `planeGrid` のまま。
      grid.setDepthBias(spec.depthBiasLayer * DEPTH_BIAS_UNIT)
      grid.setVisible(true)
    })
    gridPlanesShown = wanted.planes.map((spec) => spec.name)
    rebuildGridAxisLine(wanted.axis)
  }

  /**
   * スケッチ平面のframeを差し替える(モード外は null)。表示する平面がこれで決まる。
   * `extent` は任意面のときだけ効く(基準平面・モード外は `desiredGridPlanes` が無視する)。
   */
  function setGridSketchFrame(frame: PlaneFrame | null, extent: number | null): void {
    sketchGridFrame = frame
    sketchGridExtent = extent
    applyGridPlanes()
  }

  /**
   * 段ごとの不透明度とカメラ行列を毎フレーム入れ直す。**CPUの仕事はこれだけ**で、
   * ジオメトリには一切触らない(格子はシェーダーが作る)。
   *
   * 不透明度は `gridScale.ts` の `tierAlpha`(＝Nodeから検査できる純関数)が決める。
   * シェーダーはそれを上限に、1画素ごとの局所的な線間隔でさらに落とす。
   */
  let gridMmPerPixel = 0
  let gridTierAlphaValues: [number, number, number] = [0, 0, 0]
  const gridViewProjection = new THREE.Matrix4()
  const gridInverseViewProjection = new THREE.Matrix4()

  function updateGridUniforms(): void {
    gridMmPerPixel = worldPerPixel()
    gridTierAlphaValues = tierAlphas(gridMmPerPixel)
    camera.updateMatrixWorld()
    gridViewProjection.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse)
    gridInverseViewProjection.copy(gridViewProjection).invert()
    for (const grid of planeGrids) {
      grid.setTierAlphas(gridTierAlphaValues)
      grid.setCamera(gridViewProjection, gridInverseViewProjection)
    }
  }

  rebuildBasePlaneHover(gridExtent)
  applyGridPlanes()

  // ------------------------------------------------------------------ ビューキューブ(常設・26領域)
  //
  // 同一canvasへの**第2パス**(`renderCubePass`。`tick` が毎フレーム呼ぶ)。**別のDOM要素で
  // 浮かせない**(engine [[duocad-floating-panel-eats-viewport-clicks]] の逆向きの落とし穴 ——
  // 同じcanvasに描くぶん、`elementFromPoint` は常にcanvasを返すので、メインのポインタ経路
  // (`clickable`)の側でキューブの矩形を明示的に除外する)。
  //
  // 独立した `THREE.Scene`+`OrthographicCamera`+専用の光を持つ。**キューブ自体はワールドに
  // 固定し、カメラの方だけをメインカメラの向きに追従させる**(タスク仕様 ——
  // キューブ側を回転させると、正投影/透視の切替やupの変更で崩れる回転行列の逆算が要る)。
  const cubeScene = new THREE.Scene()
  const cubeCamera = new THREE.OrthographicCamera(
    -CUBE_ORTHO_HALF, CUBE_ORTHO_HALF, CUBE_ORTHO_HALF, -CUBE_ORTHO_HALF, 0.1, 10,
  )
  cubeScene.add(cubeCamera)
  // 専用の光1つ(本編の照明は共有しない —— 本編の照明構成は別タスクで変わる予定があるため)。
  // カメラの子にする(本編の `headlight` と同じ技法) —— キューブがどの向きでも視点側から当たる。
  const cubeLight = new THREE.DirectionalLight(0xffffff, 1.6)
  cubeLight.position.set(0.4, 0.7, 1)
  cubeCamera.add(cubeLight)

  const cubeGroup = new THREE.Group()
  const cubeLabelGroup = new THREE.Group()
  cubeScene.add(cubeGroup, cubeLabelGroup)

  const cubeDisposables: Array<{ dispose(): void }> = []
  /** `"i,j,k"` -> そのセルの材質(ホバーで明度を変えるためインスタンスを分ける)。 */
  const cubeMaterials = new Map<string, THREE.MeshLambertMaterial>()

  for (const [i, j, k] of regionCells()) {
    const { center, size } = cellBox(i, j, k)
    const geometry = new THREE.BoxGeometry(size[0], size[1], size[2])
    const material = new THREE.MeshLambertMaterial({ color: BASE_COLOR })
    const mesh = new THREE.Mesh(geometry, material)
    mesh.position.set(center[0], center[1], center[2])
    mesh.userData = { i, j, k }
    cubeGroup.add(mesh)
    cubeMaterials.set(`${i},${j},${k}`, material)
    cubeDisposables.push(geometry, material)
  }

  /**
   * 面セルの外側に立てる、指せない日本語ラベル板。**セル側(`cubeGroup`)だけがピックの対象**
   * ——`mesh.raycast` を無効化し(タスク仕様)、かつ `cubeHitAt` は `cubeGroup.children` しか
   * 見ないので二重に確実。板の向き(`lookAt`の技法)は、法線を「向く先」・`upForDirection`を
   * 「上」として、テキストがその面を正面から見たときに正しい向きで読める基底を作る。
   */
  function buildCubeLabel(i: number, j: number, k: number, text: string): void {
    const normal = new THREE.Vector3(i, j, k) // 面セルなので既に単位ベクトル
    const up = upForDirection([i, j, k])
    const basis = new THREE.Matrix4().lookAt(normal, new THREE.Vector3(0, 0, 0), new THREE.Vector3(...up))
    const canvas = document.createElement('canvas')
    canvas.width = 128
    canvas.height = 128
    const ctx = canvas.getContext('2d')
    if (ctx) {
      ctx.fillStyle = '#e8eef6'
      ctx.font = 'bold 84px sans-serif'
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillText(text, canvas.width / 2, canvas.height / 2 + 4)
    }
    const texture = new THREE.CanvasTexture(canvas)
    const material = new THREE.MeshBasicMaterial({ map: texture, transparent: true })
    const labelSize = 0.55
    const geometry = new THREE.PlaneGeometry(labelSize, labelSize)
    const mesh = new THREE.Mesh(geometry, material)
    mesh.quaternion.setFromRotationMatrix(basis)
    // 面セルの外側へ +0.001 だけ出す(タスク仕様。z-fightingを避ける程度の薄い浮かせ)。
    // **面セルの外側の面は、どのセルでも必ず立方体の表面(±1)にちょうど一致する**
    // (`cellBox`の不変条件 —— `tests/eval_view_cube.mjs`が検査済み)ので、`normal`(単位
    // ベクトル)を`1 + 0.001`倍するだけでよい。`cellBox`の`size`から出す必要は無い
    // (一度それで書いて、EDGE_T側の薄い寸法と取り違えて立方体の中に埋もれる回帰を作った)。
    mesh.position.copy(normal).multiplyScalar(1.001)
    mesh.raycast = () => {} // ピックされるのは面セル(cubeGroup)の側
    cubeLabelGroup.add(mesh)
    cubeDisposables.push(geometry, material, texture)
  }
  for (const [i, j, k] of regionCells()) {
    const label = CUBE_FACE_LABELS[`${i},${j},${k}`]
    if (label !== undefined) buildCubeLabel(i, j, k, label)
  }

  /** ビューポート右上のキューブの矩形(コンテナ内のCSSピクセル、左上原点。`offsetX`/`offsetY` と同じ)。 */
  function viewCubeRect(): { x: number; y: number; w: number; h: number } {
    const { width } = size()
    return { x: width - CUBE_MARGIN_PX - CUBE_SIZE_PX, y: CUBE_MARGIN_PX, w: CUBE_SIZE_PX, h: CUBE_SIZE_PX }
  }

  /** その画素がキューブの矩形の内側か。 */
  function insideCubeRect(x: number, y: number): boolean {
    const rect = viewCubeRect()
    return x >= rect.x && x <= rect.x + rect.w && y >= rect.y && y <= rect.y + rect.h
  }

  const cubeRaycaster = new THREE.Raycaster()
  const cubeNdc = new THREE.Vector2()

  /** 画面座標(キューブ矩形の内側)から、当たった領域の `(i,j,k)`(外れなら null)。 */
  function cubeHitAt(x: number, y: number): { i: number; j: number; k: number } | null {
    const rect = viewCubeRect()
    if (x < rect.x || x > rect.x + rect.w || y < rect.y || y > rect.y + rect.h) return null
    cubeNdc.set(((x - rect.x) / rect.w) * 2 - 1, -(((y - rect.y) / rect.h) * 2 - 1))
    cubeRaycaster.setFromCamera(cubeNdc, cubeCamera)
    const hits = cubeRaycaster.intersectObjects(cubeGroup.children, false)
    if (hits.length === 0) return null
    const data = hits[0].object.userData as { i: number; j: number; k: number }
    return { i: data.i, j: data.j, k: data.k }
  }

  /**
   * キューブ用カメラを、いまのメインカメラの向きへ追従させる(位置とupだけ)。
   * `dir = normalize(camera.position - controls.target)` を距離 `CUBE_CAMERA_DISTANCE` に置き、
   * `up` を写して `lookAt(0,0,0)` する —— これでロール(傾き)まで自動的に一致する
   * (タスク仕様。透視・正投影のどちらがアクティブでも `camera`/`controls` を読むだけなので同じに動く)。
   */
  function updateCubeCamera(): void {
    const offset = camera.position.clone().sub(controls.target)
    if (offset.lengthSq() < 1e-12) offset.copy(HOME_VIEW_OFFSET) // 退化(注視点に重なる)の逃げ場
    const dir = offset.normalize()
    cubeCamera.position.copy(dir).multiplyScalar(CUBE_CAMERA_DISTANCE)
    cubeCamera.up.copy(camera.up)
    cubeCamera.lookAt(0, 0, 0)
  }

  // ---------------------------------------------------------------- ホバー(明るくなる)
  /** ポインタがキューブの矩形の内側にあるときだけ非null(main側の `hoverAt` と対になる)。 */
  let cubeHoverAt: { x: number; y: number } | null = null
  let cubeHoverKey = ''

  function applyCubeMaterials(): void {
    for (const [key, material] of cubeMaterials) {
      material.color.setHex(key === cubeHoverKey ? lighten(BASE_COLOR, HOVER_LIGHTEN) : BASE_COLOR)
    }
  }

  function updateCubeHover(): void {
    const hit = cubeHoverAt === null ? null : cubeHitAt(cubeHoverAt.x, cubeHoverAt.y)
    const key = hit === null ? '' : `${hit.i},${hit.j},${hit.k}`
    if (key === cubeHoverKey) return
    cubeHoverKey = key
    applyCubeMaterials()
  }

  // ---------------------------------------------------------------- クリック=スナップ(視点アニメーション)
  interface CubeSnap {
    start: number
    fromDir: THREE.Vector3
    fromUp: THREE.Vector3
    dirQuat: THREE.Quaternion
    upQuat: THREE.Quaternion
    distance: number
  }
  let cubeSnap: CubeSnap | null = null

  /**
   * カメラを `dir` 方向(注視点からの距離は保つ)・`up` 姿勢へアニメーションで移す。
   * 位置は**方向を球面上で補間して距離を掛ける**(直線補間だと注視点の近くを一瞬めり込む。
   * タスク仕様)。upも合わせて補間し、最後に正規化する。
   *
   * **再利用できる形にしてある**(タスク仕様 —— 後続が `lookAtSketch` 等から呼べるように)。
   * three.js の型だけに依存し、キューブ固有の状態(`cubeSnap` 以外)には触れない。
   */
  function animateCameraTo(dir: THREE.Vector3, up: THREE.Vector3): void {
    const distance = Math.max(camera.position.distanceTo(controls.target), 1e-6)
    const fromDir = camera.position.clone().sub(controls.target).normalize()
    const fromUp = camera.up.clone().normalize()
    const toDir = dir.clone().normalize()
    const toUp = up.clone().normalize()
    cubeSnap = {
      start: performance.now(),
      fromDir,
      fromUp,
      // **球面補間の実体**: `setFromUnitVectors` は反対方向(dot≈-1)も安全に扱う
      // (three.js側が任意の直交軸を選んで処理する)ので、対蹠点への遷移でも壊れない。
      dirQuat: new THREE.Quaternion().setFromUnitVectors(fromDir, toDir),
      upQuat: new THREE.Quaternion().setFromUnitVectors(fromUp, toUp),
      distance,
    }
  }

  /** アニメーションを1フレームぶん進める(`tick` が毎フレーム呼ぶ。動いていなければ何もしない)。 */
  function stepCubeSnap(now: number): void {
    if (cubeSnap === null) return
    const t = Math.min(1, (now - cubeSnap.start) / CUBE_SNAP_MS)
    // 軽いease-in-out(見た目だけの調整。等速でも瞬間移動でもない)。
    const eased = t < 0.5 ? 2 * t * t : 1 - ((-2 * t + 2) ** 2) / 2
    const dirQ = new THREE.Quaternion().slerp(cubeSnap.dirQuat, eased)
    const upQ = new THREE.Quaternion().slerp(cubeSnap.upQuat, eased)
    const dir = cubeSnap.fromDir.clone().applyQuaternion(dirQ).normalize()
    const up = cubeSnap.fromUp.clone().applyQuaternion(upQ).normalize()
    camera.position.copy(controls.target).addScaledVector(dir, cubeSnap.distance)
    camera.up.copy(up)
    camera.lookAt(controls.target)
    if (t >= 1) cubeSnap = null
  }

  function handleCubeClick(x: number, y: number): void {
    const hit = cubeHitAt(x, y)
    if (hit === null) return
    const dir = cellDirection(hit.i, hit.j, hit.k)
    const up = upForDirection(dir)
    animateCameraTo(new THREE.Vector3(...dir), new THREE.Vector3(...up))
  }

  // ---------------------------------------------------------------- ドラッグ=自由回転(Pointer Lock)
  let cubeDownX = 0
  let cubeDownY = 0
  /** そのポインタIDを `setPointerCapture` した(解放のために控える。していなければ -1)。 */
  let cubePointerId = -1
  /** `pointerdown` はキューブの内側で起きたが、まだクリックかドラッグか未確定。 */
  let cubePointerActive = false
  /** しきい値を超えてドラッグと確定した。 */
  let cubeDragging = false
  /** Pointer Lockが実際に掛かっている(`document.pointerLockElement === renderer.domElement`)。 */
  let cubeLocked = false

  function releaseCubePointerCapture(): void {
    if (cubePointerId === -1) return
    try {
      renderer.domElement.releasePointerCapture(cubePointerId)
    } catch {
      // 既に外れていた場合も含め、失敗しても実害は無い(次のdownで取り直す)。
    }
    cubePointerId = -1
  }

  /**
   * Pointer Lockを要求する。**Promiseで失敗したら、`unadjustedMovement` が理由の可能性がある**
   * ので、オプション無しで1回だけ再試行する(タスク仕様)。どちらも失敗したときの
   * フォールバック(カーソルを出したままの通常ドラッグ)は `pointerlockerror` 側で行う ——
   * 古い実装は成功/失敗をPromiseではなくイベントだけで知らせるため、ここでは待たない。
   */
  function requestCubeLock(): void {
    let request: Promise<void> | undefined
    try {
      request = renderer.domElement.requestPointerLock({ unadjustedMovement: true })
    } catch {
      request = undefined
    }
    request?.catch(() => {
      try {
        renderer.domElement.requestPointerLock()?.catch(() => {
          /* `pointerlockerror` がフォールバックへの切替を行う */
        })
      } catch {
        /* 同上 */
      }
    })
  }

  function beginCubeDrag(): void {
    cubeDragging = true
    cubeHoverAt = null // 回している間は「明るい」予告を残さない
    if (cubeHoverKey !== '') { cubeHoverKey = ''; applyCubeMaterials() }
    requestCubeLock()
  }

  function endCubeDrag(): void {
    cubeDragging = false
    cubePointerActive = false
    releaseCubePointerCapture()
    if (document.pointerLockElement === renderer.domElement) document.exitPointerLock()
  }

  function handleCubePointerLockChange(): void {
    const locked = document.pointerLockElement === renderer.domElement
    if (locked) {
      cubeLocked = true
      return
    }
    if (cubeLocked) {
      // **ロックが外れたらドラッグ終了として扱う**(Esc・ウィンドウのフォーカス移動など。タスク仕様)。
      cubeLocked = false
      if (cubeDragging) endCubeDrag()
    }
  }
  function handleCubePointerLockError(): void {
    // **ロックが取れないことを理由に回転自体を諦めない** —— カーソルを出したまま
    // (画面端で頭打ちになる)従来どおりのドラッグへフォールバックする(タスク仕様)。
    // `movementX/movementY` は非ロック時の `pointermove` でも運ばれるので、
    // `handleCubePointerMove` 側の回転計算はロックの有無に関係なく成立する。
    cubeLocked = false
  }
  document.addEventListener('pointerlockchange', handleCubePointerLockChange)
  document.addEventListener('pointerlockerror', handleCubePointerLockError)

  /**
   * キューブのドラッグ中の `pointermove`。ロック中は `clientX/clientY` が凍結し
   * `movementX/movementY` だけが動く。フォールバック(ロック不可)でも通常の
   * `pointermove` は `movementX/movementY` を運ぶので、**回転量の計算はどちらでも同じ**
   * (タスク仕様)。回転は右ドラッグの軌道回転と同じ意味にする ——
   * `OrbitControls._handleMouseMoveRotate` と同じ式(`rotateLeft`/`rotateUp` は公開API)。
   * **上下の可動域は新たに制限しない**(既存の `OrbitControls` がそもそも制限を持たない)。
   */
  function handleCubePointerMove(event: PointerEvent): void {
    if (!cubeDragging) {
      const dx = event.offsetX - cubeDownX
      const dy = event.offsetY - cubeDownY
      if (Math.hypot(dx, dy) < CUBE_DRAG_THRESHOLD_PX) return
      beginCubeDrag()
    }
    cubeSnap = null // ドラッグが来たらアニメーションは中断してよい(タスク仕様)
    const height = renderer.domElement.clientHeight || 1
    controls.rotateLeft((2 * Math.PI * event.movementX * controls.rotateSpeed) / height)
    controls.rotateUp((2 * Math.PI * event.movementY * controls.rotateSpeed) / height)
  }

  function applyBasePlaneHover(): void {
    for (const [name, mesh] of basePlaneHoverMeshes) mesh.visible = hoverPlane === name
  }

  /**
   * 今ホバーで当てている色(**実機検査フック専用**)。
   *
   * **材質から読み戻す** —— ホバーは色相を変えず明度だけを上げるので、`highlightColors`
   * では変化が出ない(選択中の面は橙のままである)。「本当に明るくなったか」は実際に
   * 当たっている材質を見るしかない。
   */
  function hoverColorNow(): number | null {
    const pick = (object: THREE.Object3D | undefined): number | null => {
      const material = (object as { material?: { color?: THREE.Color } } | undefined)?.material
      return material?.color ? material.color.getHex() : null
    }
    const face = hoverFace
    if (face !== null) {
      return pick(
        allFaceMeshes.find(
          (mesh) =>
            mesh.userData.bodyId === face.bodyId && mesh.userData.faceIndex === face.faceIndex,
        ),
      )
    }
    const edge = hoverEdge
    if (edge !== null) {
      return pick(edgeLines.find((line) => line.userData.edgeIndex === edge.edgeIndex))
    }
    const region = hoverRegion
    if (region !== null) {
      return pick(
        sketches
          .get(region.sketchId)
          ?.regionMeshes.find((mesh) => mesh.userData.regionIndex === region.index),
      )
    }
    if (hoverPlane !== null) return pick(basePlaneHoverMeshes.get(hoverPlane))
    return null
  }

  /**
   * 面の材質は **(役割 × 選択状態 × ホバー)** をキーにした遅延生成のキャッシュから取る。
   *
   * 役割は形状最適化(第3のモード)の保持/除外/拘束で、素の面(`'none'`)を含めて4種。
   * 選択状態は3種(未選択 `'plain'` / 確定 `'user'` / AI提案 `'proposed'`)、ホバーは2種。
   * 組み合わせは最大24だが、**使った組だけ**をこの中で作る(全部を手で書き下さない) ——
   * 役割が無かった頃の6個(base/highlight/proposed × 通常/ホバー)から3倍に増える軸を、
   * 静的な定数ではなく関数1つに閉じるための形。
   *
   * **選択は役割に勝つ**(本人決定)。`state !== 'plain'` のときは役割を見ない ——
   * 保持ソリッド(緑)を選ぶとハイライト色になり、緑は一時的に見えなくなる。
   */
  type OptimizeFaceRole = 'none' | 'preserve' | 'exclude' | 'fixture'
  type FaceMaterialState = 'plain' | 'user' | 'proposed'
  const faceMaterialCache = new Map<string, THREE.MeshLambertMaterial>()

  function roleBaseColor(role: OptimizeFaceRole): number {
    if (role === 'preserve') return OPTIMIZE_PRESERVE_COLOR
    if (role === 'exclude') return OPTIMIZE_EXCLUDE_COLOR
    if (role === 'fixture') return OPTIMIZE_FIXTURE_COLOR
    return BASE_COLOR
  }

  function faceMaterialFor(
    role: OptimizeFaceRole,
    state: FaceMaterialState,
    hovered: boolean,
  ): THREE.MeshLambertMaterial {
    const effectiveRole: OptimizeFaceRole = state === 'plain' ? role : 'none'
    const key = `${effectiveRole}:${state}:${hovered ? 1 : 0}`
    const cached = faceMaterialCache.get(key)
    if (cached) return cached
    const base =
      state === 'user' ? HIGHLIGHT_COLOR : state === 'proposed' ? PROPOSED_COLOR : roleBaseColor(effectiveRole)
    const material = new THREE.MeshLambertMaterial({
      color: hovered ? lighten(base, HOVER_LIGHTEN) : base,
      side: THREE.DoubleSide,
    })
    faceMaterialCache.set(key, material)
    return material
  }
  const previewMaterial = new THREE.MeshLambertMaterial({
    color: PREVIEW_COLOR,
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.55,
    // 半透明どうしが互いを隠して真っ黒な塊にならないようにする(奥行きは書かない)
    depthWrite: false,
  })
  const edgeMaterial = new THREE.LineBasicMaterial({ color: EDGE_COLOR })

  const raycaster = new THREE.Raycaster()
  /** body_id -> そのBodyが画面に持つメッシュ。非表示・未読込のBodyはキーごと無い。 */
  const bodies = new Map<string, BodyMeshes>()
  /**
   * body_id -> サーバから届いた生の `GeometryPayload`。
   *
   * three.jsのメッシュ(`bodies`)とは別に持つ — `getGeometry()` は実機検査フックが
   * 「今どんな形が届いているか」をJSONで読むためのもので、three.jsのバッファは読めない
   * (型も漏らさない設計)。`bodies` と寿命を揃える(build/clear/pruneで同時に出し入れする)。
   */
  const rawGeometry = new Map<string, GeometryPayload>()
  /** `rawGeometry` と寿命を揃える可視判定用サンプル(`computeFocus` が2835面を毎フレーム走査しない)。 */
  const faceSamples = new Map<string, BodySamples>()
  /** `faceSamples` が変わった印。姿勢が止まっていても形が届けば `computeFocus` をやり直す。 */
  let focusGeneration = 0
  /** 全Body分の面メッシュを1本にまとめたもの(ピック用)。update() のたびに作り直す。 */
  let allFaceMeshes: THREE.Mesh[] = []
  /** プレビュー(幽霊)のメッシュ。**ピックの対象にしない**ので `bodies` とは別に持つ。 */
  const previewMeshes = new Map<string, { objects: THREE.Object3D[]; disposables: Array<{ dispose(): void }> }>()
  let pickListener: ((event: PickEvent) => void) | null = null
  let boxSelectListener: ((event: BoxSelectEvent) => void) | null = null
  let planeMoveListener: ((point: { u: number; v: number } | null) => void) | null = null

  // ---------------------------------------------------------------- ホバー強調
  //
  // **「そのままクリックしたら取れるもの」を1つだけ明るくする**(2026-08-27本人決定)。
  // 対象は面・辺・領域・基準平面で、**頂点は出さない** —— 選択に入っている頂点しか
  // マーカーが無く、「クリックすれば選択されるもの」を出せる状態にない。
  //
  // 当たり判定は `resolveHit` **1つだけ**をクリックと共有する。優先順位を2箇所に
  // 書くとプレビューが嘘をつき、この機能の存在意義が消える。
  /** ポインタの現在位置(canvasの外なら null)。**解決はフレームに1回**(`tick`)。 */
  let hoverAt: { x: number; y: number } | null = null
  /** 視点回転中(=そのポインタの動きはクリックではない)。回している間は解決しない。 */
  let orbiting = false
  let hoverFace: FaceRef | null = null
  let hoverEdge: EdgeRef | null = null
  let hoverPlane: BasePlaneName | null = null
  let hoverRegion: { sketchId: string; index: number } | null = null
  // 「変わったときだけ材質を書き換える」ための印(毎フレーム全メッシュを走査しない)。
  let hoverFaceKey = ''
  let hoverEdgeKey = ''
  let hoverRegionKey = ''
  /**
   * 直近にホバーを解決したときの「ポインタ位置+視点+`pickGeneration`」。**同じなら
   * 当たるものも同じ**なので、レイキャストごと省く(止まって眺めている間は毎フレーム
   * 2835面を撃たない)。
   */
  let hoverProbe = ''
  /**
   * **「何が指せるか」が変わるたびに増やす番号**。`hoverProbe` に混ぜてあるので、
   * ポインタも視点も動いていなくても次のフレームで必ず解決し直す。
   *
   * これが無いと、**止まったまま予告だけが古くなる**。例: 領域をホバーしたまま Esc で
   * コマンドを解除すると `pickRegions(false)` で領域は指せなくなるのに、ポインタも視点も
   * 動いていないので解決が省かれ、明るいままの領域が「クリックすれば取れる」と嘘をつく。
   * 頂点マーカーが増えたとき・プレビューで本物を隠したとき・ビューポートの寸法が
   * 変わったときも同じ(いずれも当たるものが変わる)。
   */
  let pickGeneration = 0

  /** 「何が指せるか」を変えた側が呼ぶ。 */
  function pickChanged(): void {
    pickGeneration += 1
  }

  /**
   * ホバーの印を捨てる。**描画対象を作り直した側が呼ぶ** —— 作り直したメッシュは
   * 素の材質で生まれるので、印を残すと「変わっていない」と判断して明るくし直さない。
   */
  function invalidateHover(): void {
    hoverFace = null
    hoverEdge = null
    hoverRegion = null
    hoverFaceKey = ''
    hoverEdgeKey = ''
    hoverRegionKey = ''
    hoverPlane = null
    pickChanged()
    // **材質まで戻す**。参照と印を捨てるだけだと、作り直していない対象に付けた明るい材質が
    // そのまま残る(次の解決が「なし」や別種別だと、誰も戻さないまま画面に居座る)。
    applyFaceMaterials()
    applyEdgeMaterials()
    applyRegionMaterials()
    applyBasePlaneHover()
  }
  let framed = false
  let alive = true
  let frameHandle = 0

  function size(): { width: number; height: number } {
    return {
      width: Math.max(1, container.clientWidth),
      height: Math.max(1, container.clientHeight),
    }
  }

  function applySize(): void {
    pickChanged() // 寸法が変わると同じ画素が別のものを指す(engine `viewport-resize-silently-changes-what-a-click-grabs`)
    const { width, height } = size()
    renderer.setSize(width, height, false)
    const aspect = width / height
    perspective.aspect = aspect
    perspective.updateProjectionMatrix()
    // **正投影は縦(`top`/`bottom`)を正として横を作り直す。** 透視の `aspect` と同じで、
    // 縦の見え方を寸法に依存させない(`smoke-features.mjs` がv方向を基準に置いている)。
    // **`zoom` には触らない** —— あれは OrbitControls のドリーの持ち物である。
    orthographic.right = orthographic.top * aspect
    orthographic.left = -orthographic.top * aspect
    orthographic.updateProjectionMatrix()
    // 使っていない側も同時に整えてある(切り替えた瞬間に1フレーム歪むのを防ぐ)。
  }

  function clearBody(bodyId: string): void {
    const existing = bodies.get(bodyId)
    if (!existing) return
    for (const object of existing.sceneObjects) root.remove(object)
    for (const item of existing.disposables) item.dispose()
    bodies.delete(bodyId)
    rawGeometry.delete(bodyId)
    if (faceSamples.delete(bodyId)) focusGeneration += 1
  }

  /**
   * 1面ぶんの `BufferGeometry` を作る(本物と幽霊で**同じ形の作り方**)。
   *
   * 本物(`buildBody`)と確定前の幽霊(`showPreview`)はどちらも同じ三角形群から作るので、
   * **ここがずれると「プレビューと確定結果が違う形に見える」**という、いちばん困る形の
   * 不具合になる。
   *
   * 一方で**メッシュの組み立てまでは共通化しない** —— 本物は `userData`(ピックのための
   * body_id と面の添字)と面の境界線を持ち、幽霊はどちらも持たない(指せない・線を出さない)。
   * 材質だけの違いに見えて、実際には「何のために置くか」が違う。
   */
  function faceGeometry(face: FaceGeometry): THREE.BufferGeometry {
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute(
      'position',
      new THREE.BufferAttribute(new Float32Array(face.positions), 3),
    )
    geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(face.indices), 1))
    geometry.computeVertexNormals()
    geometry.computeBoundingSphere()
    return geometry
  }

  function buildBody(bodyId: string, payload: GeometryPayload): void {
    const faceMeshes: THREE.Mesh[] = []
    const sceneObjects: THREE.Object3D[] = []
    const disposables: Array<{ dispose(): void }> = []
    for (const face of payload.faces) {
      if (face.indices.length === 0) continue // 三角形が取れなかった面(サーバが警告を出す)
      const geometry = faceGeometry(face)
      // **素の材質**で生む(役割・選択・ホバーは `applyFaceMaterials()` が
      // `rebuildPickIndex()`(→ `invalidateHover()`)経由ですぐに塗り直す)。
      const mesh = new THREE.Mesh(geometry, faceMaterialFor('none', 'plain', false))
      mesh.userData.bodyId = bodyId
      mesh.userData.faceIndex = face.index
      root.add(mesh)
      faceMeshes.push(mesh)
      sceneObjects.push(mesh)
      disposables.push(geometry)

      // 面の境界線。装飾ではなく「どこが1つの面か」を見せるためで、
      // これが無いと隣り合う同色の面を狙い分けられず、狙いにくさの計測が意味を失う。
      const edges = new THREE.EdgesGeometry(geometry, 20)
      const line = new THREE.LineSegments(edges, edgeMaterial)
      root.add(line)
      sceneObjects.push(line)
      disposables.push(edges)
    }
    bodies.set(bodyId, { faceMeshes, sceneObjects, disposables })
    rawGeometry.set(bodyId, payload)
    faceSamples.set(bodyId, sampleFaces(payload))
    focusGeneration += 1
  }

  function rebuildPickIndex(): void {
    allFaceMeshes = []
    for (const entry of bodies.values()) allFaceMeshes.push(...entry.faceMeshes)
    invalidateHover() // 作り直した面は素の材質で生まれる(`invalidateHover` のコメント)
  }

  function update(entries: BodyGeometryUpdate[]): void {
    if (entries.length === 0) return
    // 本物の形が届いた時点で、確定前の予想は用済み(古い幽霊を残さない)。
    clearPreview()
    for (const entry of entries) {
      clearBody(entry.bodyId)
      if (entry.geometry) buildBody(entry.bodyId, entry.geometry)
      // 形が変わったBodyの辺は**その場で消す**。古い添字の線を残すと、指した線と
      // 違う辺が丸まる(§7の「添字は形状変更で壊れる」の辺版)。要るなら呼び出し側が
      // 版の変化を見て取り直す(`App.svelte`)。
      if (entry.bodyId === edgeBodyId) clearEdges()
    }
    rebuildPickIndex()
    refreshContentDiagonal() // 見えているものの大きさが変わった(解像度の上下限に効く)
    updateClipPlanes() // 内容が変わった → 視錐台を引き直す(姿勢は初回のみ下で決める)

    // **`frameToContent()` が実際に合わせたときだけ機会を消費する**(2026-08-30)。
    // 空振り(箱が空)で `framed = true` にすると、以後どんなに大きな内容が届いても
    // 初回フレーミングが二度と起きない(上の関数docstring参照)。
    if (!framed && frameToContent()) {
      framed = true
    }
  }

  // ------------------------------------------------------------------ プレビュー(幽霊)
  function setBodyVisible(bodyId: string, visible: boolean): void {
    const entry = bodies.get(bodyId)
    if (!entry) return
    for (const object of entry.sceneObjects) object.visible = visible
  }

  function showPreview(entries: BodyGeometryUpdate[]): void {
    pickChanged() // 本物のBodyを隠すので、同じ画素が別のもの(または何も)を指す
    clearPreview()
    for (const entry of entries) {
      if (!entry.geometry) continue
      const objects: THREE.Object3D[] = []
      const disposables: Array<{ dispose(): void }> = []
      for (const face of entry.geometry.faces) {
        if (face.indices.length === 0) continue
        const geometry = faceGeometry(face)
        const mesh = new THREE.Mesh(geometry, previewMaterial)
        root.add(mesh)
        objects.push(mesh)
        disposables.push(geometry)
      }
      // 本物を隠してから幽霊を出す(重ねると半透明の中に古い形が透けて読めない)
      setBodyVisible(entry.bodyId, false)
      previewMeshes.set(entry.bodyId, { objects, disposables })
    }
  }

  function clearPreview(): void {
    pickChanged() // 隠していた本物が戻る(`showPreview` の対)
    if (previewMeshes.size === 0) return
    for (const [bodyId, entry] of previewMeshes) {
      for (const object of entry.objects) root.remove(object)
      for (const item of entry.disposables) item.dispose()
      setBodyVisible(bodyId, true)
    }
    previewMeshes.clear()
  }

  function prune(validBodyIds: string[]): void {
    const valid = new Set(validBodyIds)
    const stale = [...bodies.keys()].filter((bodyId) => !valid.has(bodyId))
    if (stale.length === 0) return
    clearPreview() // 消えるBodyの幽霊が取り残されないように(update と同じ理由)
    if (edgeBodyId !== null && stale.includes(edgeBodyId)) clearEdges()
    for (const bodyId of stale) clearBody(bodyId)
    rebuildPickIndex()
    refreshContentDiagonal() // 見えているものの大きさが変わった(解像度の上下限に効く)
    updateClipPlanes() // 内容が変わった → 視錐台を引き直す
  }

  /**
   * 半径 `radius` の球が画面に収まる、注視点からの距離。FOVとアスペクトの狭い方で決まる。
   *
   * **FOVを可変にした以上、収まる距離を定数で決め打ちできない**(2026-08-28)。
   * 45度・アスペクト1以上では `radius / tan(22.5°) = radius * 2.4142` になる。
   * `frameToContent` はこれを `radius = diagonal / 2` で呼ぶので `diagonal * 1.2071` となり、
   * **以前の決め打ち定数 `diagonal * 1.2` はこの式を45度で評価して丸めた値そのもの**である
   * (差は0.6%)。**したがって既定のFOVでの見え方は実質変わらない。**
   * 縦長のビューポート(aspect<1)では距離が伸びるが、これは以前が横方向を見ていなかった
   * 取りこぼしの修正にあたる(狭い方に合わせないと横がはみ出す)。
   *
   * **正投影のときも透視の画角で評価する。** 正投影では距離が見え方に効かないので、
   * ここが返すのは「透視と同じ場所にカメラを置くための距離」でしかない(収める仕事は
   * `fitHalfHeight` + `setViewHalfHeight` がする)。
   *
   * **収める量は `fitHalfHeight` が持ち、ここはそれを距離へ直すだけ**(縦横の狭い方に
   * 合わせる判断はあちらに1つだけ置く)。
   */
  function fitDistance(radius: number): number {
    return fitHalfHeight(radius) / Math.tan(THREE.MathUtils.degToRad(perspective.fov) / 2)
  }

  /**
   * 半径 `radius` の球が画面に収まる「見かけの大きさ」の半分(＝注視点の面で画面の縦に
   * 収まる世界長さの半分)。**縦横の狭い方に合わせる** —— 横長の画面では縦が、
   * 縦長の画面では横が先に詰まる。
   *
   * **`fitDistance` と対で、同じ球を収める。** 透視は距離で、正投影は視錐台の大きさで
   * 収めるので、収める量そのもの(この関数)と、それを距離へ直す換算(`fitDistance`)に
   * 分けてある。分けないと正投影側が「収める距離」という意味の無い値を通ることになる。
   */
  function fitHalfHeight(radius: number): number {
    return perspective.aspect >= 1 ? radius : radius / perspective.aspect
  }

  /**
   * 注視点の面で画面の縦に収まる世界長さの半分。**2つのカメラを同じ物差しで測るための穴**
   * で、「見かけの大きさを保つ」処理(`setFov` の切り替え)と当たり判定の太さ
   * (`worldPerPixel`)がここを通る。
   *
   * 透視は距離×tan(画角/2)、正投影は視錐台の半分の高さ÷`zoom`(ドリーは `zoom` を動かす
   * だけでカメラを動かさないので、`zoom` を割らないと見かけの大きさにならない)。
   */
  function viewHalfHeight(): number {
    if (camera === orthographic) return orthographic.top / orthographic.zoom
    const distance = camera.position.distanceTo(controls.target)
    return distance * Math.tan(THREE.MathUtils.degToRad(perspective.fov) / 2)
  }

  /**
   * 見かけの大きさ(`viewHalfHeight`)を `half` にする。**透視は注視点からの距離で、
   * 正投影は視錐台の大きさで作る** —— 正投影ではカメラを前後させても大きさは1ミリも
   * 変わらないので、距離で調節する道が無い。
   *
   * 正投影側は `zoom` を1へ戻して視錐台そのものへ焼き込む(`zoom` は OrbitControls の
   * ドリーの持ち物なので、こちらが値を持ち続けると次のドリーで踏まれる)。
   */
  function setViewHalfHeight(half: number): void {
    const safe = Math.max(half, 1e-6)
    if (camera === orthographic) {
      const { width, height } = size()
      orthographic.top = safe
      orthographic.bottom = -safe
      orthographic.right = safe * (width / height)
      orthographic.left = -safe * (width / height)
      orthographic.zoom = 1
      orthographic.updateProjectionMatrix()
      return
    }
    const distance = safe / Math.tan(THREE.MathUtils.degToRad(perspective.fov) / 2)
    const offset = camera.position.clone().sub(controls.target)
    // 注視点にぴったり重なっていると向きが無い(長さ0のベクトルは伸ばせない)。既定の向きへ逃がす。
    if (offset.lengthSq() < 1e-18) offset.copy(HOME_VIEW_OFFSET)
    camera.position.copy(controls.target).add(offset.setLength(distance))
  }

  /**
   * 使うカメラを差し替える(透視 ⇄ 正投影)。**視点は持ち越す** —— 姿勢・クリップ範囲を
   * 写し、ヘッドライトとシーンへの所属、`OrbitControls` の操作対象を全部移す。
   *
   * **`OrbitControls` は `object` の差し替えを受け付ける**が、極(`_quat`)だけは生成時の
   * `up` から焼かれたまま動かない —— これは**そのままでよい**。両方のカメラの上方向は
   * ワールド+Zで同じなので、焼かれた極も同じである(`WORLD_UP` の説明を参照)。
   */
  function activateCamera(next: THREE.PerspectiveCamera | THREE.OrthographicCamera): void {
    if (next === camera) return
    next.position.copy(camera.position)
    next.up.copy(camera.up)
    next.quaternion.copy(camera.quaternion)
    next.near = camera.near
    next.far = camera.far
    // ヘッドライトはカメラの子(視点を回しても面が真っ黒にならない)。置き去りにすると暗くなる。
    camera.remove(headlight)
    scene.remove(camera)
    next.add(headlight)
    scene.add(next)
    camera = next
    controls.object = next
    next.updateProjectionMatrix()
  }

  /**
   * near/far(視錐台の前後端)を、いまの内容とカメラ距離から引き直す。
   *
   * **`frameToContent`(姿勢)から独立させてある**(2026-08-30)。姿勢はページ読み込みで
   * 1回決めれば十分だが、near/farは「最初に届いた形の大きさ」で固定してよいものではない
   * —— 後から大きなSTEPが届けばfarを追い越して手前が切り取られ(farはズーム・オービットでは
   * 一切広がらない)、逆に小さな細部へ深く寄ればnearが大きいまま手前が切れる。**内容が変わる
   * たび**(`refreshContentDiagonal` の呼び出し元)**とカメラが意味のある幅で動くたび**
   * (`cameraMoved` 判定に相乗り)の両方で呼ぶことで、視錐台を常時追随させる。
   *
   * near は**距離側からも**導く —— コンテンツの大きさだけで決めると、大きなモデルの中の
   * 小さな面へ深く寄ったときに手前が切れる(near は「いま見ている距離」に対しても十分
   * 小さくなければならない)。far は既存の `対角 × 20` の保守性をそのまま保ちつつ、
   * 距離が伸びた場合の下限も持つ(`frameToContent` の元のガードコメントの理由と同じ)。
   *
   * `span` の下限に `gridExtent * 2` を置くのは、空の文書やグリッドしか無い場面でも
   * 視錐台がグリッド全体を収める大きさを保つため(`contentDiagonal` はボディが無いと0)。
   * **`gridExtent` は固定ではない**(2026-08-30。`applyGridScale`)ので、グリッドが
   * 広がればここの下限も一緒に広がる —— 取りこぼすと、グリッドは見えているのに
   * その外側でnear/farが詰まる、という遠い壊れ方をする。
   */
  function updateClipPlanes(): void {
    const span = Math.max(contentDiagonal, gridExtent * 2)
    const distance = camera.position.distanceTo(controls.target)
    camera.near = Math.max(Math.min(span, distance) / 1000, 0.01)
    camera.far = Math.max(span * 20, distance * 4)
    camera.updateProjectionMatrix()
  }

  /**
   * グリッド計算専用の外接直方体。**`root` + `sketchRoot` だけ**から作る —— `gridRoot`
   * (グリッド自身)を含めると「グリッドが伸びる→箱が大きくなる→グリッドが伸びる」の
   * 発散フィードバックループになる(`gridRoot` は元々 `root` に入れていない。上のコメント
   * 参照)。`refreshContentDiagonal` の箱と同じもの(そちらは対角の長さだけを取り出す)。
   */
  function contentBoxForGrid(): THREE.Box3 {
    const box = new THREE.Box3().setFromObject(root)
    box.union(new THREE.Box3().setFromObject(sketchRoot))
    return box
  }

  /**
   * **基準平面のピック範囲**の半幅を内容の大きさへ合わせる(`gridScale.ts` の `extentFor`)。
   * **格子の見た目には効かない** —— 刻みは3段固定で、格子はシェーダーが無限に描く。
   *
   * **広げるのは常に即座**(`allowShrink` に関係なく)。**縮めるのは `allowShrink` が
   * 立っているときだけ** —— `frameToContent` が実際に合わせたとき(＝`frameAll`)と、
   * 新規文書にしたとき(`resetGrid`)に限る。ふつうの編集(`refreshContentDiagonal` からの
   * 呼び出し)は常に `allowShrink: false` で、内容がしきい値付近で揺れても指せる範囲が
   * 前後に揺れることはない。
   *
   * 範囲が変わったら、ホバー板(`rebuildBasePlaneHover`)と面外の軸線の長さ
   * (`applyGridPlanes`)を組み直し、near/farの下限(`updateClipPlanes`)も引き直す。
   */
  function applyGridScale(box: THREE.Box3, allowShrink: boolean): void {
    const extent = extentFor(box)
    if (extent === gridExtent) return
    if (extent < gridExtent && !allowShrink) return
    gridExtent = extent
    rebuildBasePlaneHover(gridExtent)
    applyGridPlanes() // 面外の軸線は `gridExtent` の長さで引いている
    applyBasePlaneHover() // 組み直した新しいメッシュへ、いまのホバー状態を反映し直す
    updateClipPlanes()
  }

  /** シーンに今ある全メッシュの外接直方体にカメラを合わせる(初回だけ)。
   *
   * 複数Bodyでは初回に届く順序や本数が変わりうるので、**実際に置いたメッシュから計算する**方が
   * 頑丈(サーバのbbox形式に依存しない)。
   *
   * **姿勢(位置・向き)だけを決める。** near/farは `updateClipPlanes` の仕事(上記コメント)。
   *
   * **実際に合わせたかを boolean で返す**(2026-08-30)。箱が空(=描くものがまだ無い)なら
   * 何もせず `false` を返す —— 呼び出し側の `update()` はこれを見て初めて「一度きりの機会」
   * (`framed` フラグ)を消費する。戻り値を見ずに常に `framed = true` にすると、
   * `geometry: null` のBodyだけが届いた1通(参照ボディだけ・本体がまだ無い等)で
   * 空振りのまま機会が焼き切れ、以後どれだけ内容が届いても初回フレーミングが起きなくなる。
   */
  function frameToContent(): boolean {
    const box = new THREE.Box3().setFromObject(root)
    // **姿勢を合わせられるか(box空)に関わらず、グリッドの縮小許可は必ず1回通す**
    // (2026-08-31 QA指摘で修正)。`frameAll()`はここ(`frameToContent`)を呼ぶだけで、
    // 縮小の許可はここにしか無い —— 下の `box.isEmpty()` の早期returnより前に置かないと、
    // モデルを全部消してから`frameAll()`しても、広がったグリッドが二度と縮まなかった
    // (`contentBoxForGrid()`は`sketchRoot`も見るので、ボディだけ消してスケッチが残る
    // 場合も同じ早期returnを通る)。**姿勢(戻り値・カメラの位置/向き)の扱いは変えない**
    // —— これは「実際に合わせたか」を表す契約のままで、グリッドの縮小はそれとは独立に
    // 判定する(`applyGridScale`はその一手だけを担う)。
    applyGridScale(contentBoxForGrid(), true)
    if (box.isEmpty()) return false
    const center = new THREE.Vector3()
    box.getCenter(center)
    const diagonal = Math.max(box.getSize(new THREE.Vector3()).length(), 1e-6)
    const distance = fitDistance(diagonal / 2)
    camera.position.copy(center).addScaledVector(HOME_VIEW_OFFSET, distance)
    controls.target.copy(center)
    // **正投影は距離では収まらない。** カメラは上で透視と同じ場所へ置いた(回転の半径のため)が、
    // 収める仕事は視錐台がする。
    //
    // **`fitHalfHeight` をそのまま渡さない。** `HOME_VIEW_OFFSET` の長さが1ではないので、
    // 透視が実際に出すのは `fitHalfHeight` の 1.5362倍ぶん引いた絵である —— 素の
    // `fitHalfHeight` で視錐台を作ると、`frameAll` のたびに**正投影だけが1.54倍に寄る**
    // (2026-08-28に受入で実測。93.6px → 143.7px)。同じ係数を掛けて、どちらの投影でも
    // `frameAll` の見かけが揃うようにする。
    if (camera === orthographic) {
      setViewHalfHeight(fitHalfHeight(diagonal / 2) * HOME_VIEW_OFFSET.length())
    }
    updateClipPlanes()
    controls.update()
    return true
  }

  function setFov(deg: number): void {
    if (!Number.isFinite(deg)) return
    // **0だけが正投影**で、それ以外は 30〜90 へ丸める(あいだの1〜29という値は無い)。
    const next = deg === FOV_ORTHO ? FOV_ORTHO : Math.min(FOV_MAX, Math.max(FOV_MIN, deg))
    const wantOrtho = next === FOV_ORTHO
    const already = wantOrtho
      ? camera === orthographic
      : camera === perspective && Math.abs(next - perspective.fov) < 1e-6
    if (already) return
    // **切り替えの前に見かけの大きさを測り、切り替えた後に同じ値へ戻す。**
    // これをしないとパースの強さと一緒に拡大率まで変わり、ただの「ズーム」になってしまう
    // (正投影への切り替えでも同じ —— 視錐台の大きさを、測った値から作る)。
    const half = viewHalfHeight()
    activateCamera(wantOrtho ? orthographic : perspective)
    if (!wantOrtho) perspective.fov = next
    setViewHalfHeight(half)
    camera.far = Math.max(camera.far, camera.position.distanceTo(controls.target) * 4)
    camera.updateProjectionMatrix()
    controls.update()
  }

  function cameraPose(): {
    position: [number, number, number]
    target: [number, number, number]
    up: [number, number, number]
    near: number
    far: number
  } {
    return {
      position: [camera.position.x, camera.position.y, camera.position.z],
      target: [controls.target.x, controls.target.y, controls.target.z],
      up: [camera.up.x, camera.up.y, camera.up.z],
      near: camera.near,
      far: camera.far,
    }
  }

  function projectPoint(point: readonly [number, number, number]): { x: number; y: number } | null {
    const { width, height } = size()
    const projected = new THREE.Vector3(point[0], point[1], point[2]).project(camera)
    if (projected.z < -1 || projected.z > 1) return null
    return { x: ((projected.x + 1) / 2) * width, y: ((1 - projected.y) / 2) * height }
  }

  /** 直近に使ったハイライト色(`highlightColors`)。出していない種別は null。 */
  const highlightColor: { face: number | null; edge: number | null; vertex: number | null } = {
    face: null,
    edge: null,
    vertex: null,
  }

  /** 「持ち主+添字」の1本の鍵(面・辺・領域で共通)。 */
  function refKey(owner: string, index: number): string {
    return `${owner}\u0000${index}`
  }

  /** 今の選択(`highlight` が受け取ったもの)。**材質を決めるのは下の1箇所だけ**。 */
  let selectedFaceKeys = new Set<string>()
  let faceOrigin: 'user' | 'proposed' = 'user'
  /** ボディ単位の選択(`highlightBodies`。保持/除外を決めているあいだだけ埋まる)。 */
  let selectedBodyIds = new Set<string>()
  let bodyOrigin: 'user' | 'proposed' = 'user'

  /**
   * 形状最適化(第3のモード)の役割。**モードを抜けたら空にする**(`setOptimizeState(null)`)
   * ので、抜けているあいだは常に `'none'` になる(タスク仕様「モードを抜けているあいだは
   * 描かない」)。
   */
  let bodyRoles = new Map<string, 'preserve' | 'exclude'>()
  let fixtureFaceKeys = new Set<string>()

  /**
   * 面の材質を決める。**選択とホバーと役割の全部を見る唯一の場所**。
   *
   * 分けてはいけない —— どれも `mesh.material` を書くので、後から上書きする形にすると
   * 次のstate配信で走る `highlight()` の全走査に黙って消される。
   */
  function applyFaceMaterials(): void {
    highlightColor.face =
      selectedFaceKeys.size === 0 && selectedBodyIds.size === 0
        ? null
        : faceMaterialFor('none', selectedFaceKeys.size > 0 ? faceOrigin : bodyOrigin, false).color.getHex()
    for (const mesh of allFaceMeshes) {
      const bodyId = mesh.userData.bodyId as string
      const faceIndex = mesh.userData.faceIndex as number
      const hitAsFace = selectedFaceKeys.has(refKey(bodyId, faceIndex))
      const hitAsBody = selectedBodyIds.has(bodyId)
      const state: FaceMaterialState = hitAsFace ? faceOrigin : hitAsBody ? bodyOrigin : 'plain'
      const role: OptimizeFaceRole = fixtureFaceKeys.has(refKey(bodyId, faceIndex))
        ? 'fixture'
        : (bodyRoles.get(bodyId) ?? 'none')
      const isHovered =
        hoverFace !== null && hoverFace.bodyId === bodyId && hoverFace.faceIndex === faceIndex
      mesh.material = faceMaterialFor(role, state, isHovered)
    }
  }

  function highlight(refs: FaceRef[], origin: 'user' | 'proposed'): void {
    faceOrigin = origin
    selectedFaceKeys = new Set(refs.map((ref) => refKey(ref.bodyId, ref.faceIndex)))
    applyFaceMaterials()
  }

  /**
   * 選択中のボディを強調する(`highlight` のボディ版)。保持/除外ソリッドを決めている
   * あいだだけ非空になる —— **選択が役割色に勝つ**ので、ここに載ったボディは
   * `applyFaceMaterials()` が全面をハイライト色で塗る。
   */
  function highlightBodies(bodyIds: string[], origin: 'user' | 'proposed'): void {
    bodyOrigin = origin
    selectedBodyIds = new Set(bodyIds)
    applyFaceMaterials()
  }

  // ------------------------------------------------------------------ 形状最適化(第3のモード)の装飾
  /**
   * いま描いている装飾の**中身**(実機検査フック専用)。
   *
   * `optimizeAnnotationCount()`(子オブジェクトの総数)だけでは、拘束記号と荷重矢印を
   * 取り違えても・矢印が逆向きでも・ラベルが空文字でも数だけは合ってしまい検出できない
   * (2026-08-31 QA指摘)。ここに種別ごとの中身(拘束は対象の面、荷重は単位ベクトルと
   * ラベル文字列)を控えておき、`optimizeAnnotations()` で読めるようにする。
   */
  let optimizeAnnotationInfo: {
    fixtures: FaceRef[]
    loads: Array<{ bodyId: string; faceIndex: number; direction: [number, number, number]; label: string }>
  } = { fixtures: [], loads: [] }

  /** 拘束記号・荷重矢印+ラベルを全部消す。`setOptimizeState` が最初に呼ぶ。 */
  function clearOptimizeAnnotations(): void {
    optimizeRoot.clear()
    for (const item of optimizeDisposables) item.dispose()
    optimizeDisposables = []
    optimizeAnnotationInfo = { fixtures: [], loads: [] }
  }

  /**
   * 面の重心と平均法線を、**いま実際に描かれているメッシュ**から測る。
   *
   * サーバの配りもの(`FaceRef`)は添字だけで重心・法線を持たない。ここは装飾専用の
   * 簡易測定であって `duocad/optimize.py` の指紋(面積加重・照合用)とは別物 ——
   * 頂点の単純平均でよい(タスク仕様「あとから直すのは安い。凝りすぎない」)。
   */
  function faceCentroidNormal(
    bodyId: string,
    faceIndex: number,
  ): { center: THREE.Vector3; normal: THREE.Vector3 } | null {
    const mesh = allFaceMeshes.find(
      (candidate) => candidate.userData.bodyId === bodyId && candidate.userData.faceIndex === faceIndex,
    )
    if (!mesh) return null
    const position = mesh.geometry.getAttribute('position') as THREE.BufferAttribute
    const normalAttr = mesh.geometry.getAttribute('normal') as THREE.BufferAttribute
    if (!position || position.count === 0) return null
    const center = new THREE.Vector3()
    const normal = new THREE.Vector3()
    for (let i = 0; i < position.count; i++) {
      center.x += position.getX(i)
      center.y += position.getY(i)
      center.z += position.getZ(i)
      if (normalAttr) {
        normal.x += normalAttr.getX(i)
        normal.y += normalAttr.getY(i)
        normal.z += normalAttr.getZ(i)
      }
    }
    center.divideScalar(position.count)
    if (normal.lengthSq() < 1e-12) normal.set(0, 0, 1)
    else normal.normalize()
    return { center, normal }
  }

  /**
   * 貼った面の外接範囲を、スケッチのframe(u,v)で測る(`setExtent` へ渡す目安)。
   * 1-2-5の丸め(`snap125`。`gridScale.ts`)は基準平面のピック範囲と使い回すが、
   * **`extentFor` はそのまま使わない** —— あれの `BASE_EXTENT`(100mm)下限は
   * 「空の文書でもこれより狭くしない」という文書スケールの前提で、面のframeの原点は
   * 面の中心ではなく**面の隅(OCCTのUVパラメータ化の原点)のことが多い**。実測で
   * 40×20mmの面に対して`extentFor`が100mmを返し、面の何倍もの大きさでグリッドが
   * 描かれて背景が線で埋まる破綻を起こした(スクリーンショットで発見)。ここでは
   * 面が小さければ小さいなりの範囲にする。
   *
   * 見つからなければ `null`(=無限のまま描く。面が消えた・非表示等で測れないときに
   * 装飾が消えるより、広めに描いて残る方が安全)。**面の実際の輪郭では切らない**
   * (タスク仕様 —— トリム境界のマスクはステンシルが要り費用に見合わない)。
   */
  function faceUvExtent(bodyId: string, faceIndex: number, frame: PlaneFrame): number | null {
    const mesh = allFaceMeshes.find(
      (candidate) => candidate.userData.bodyId === bodyId && candidate.userData.faceIndex === faceIndex,
    )
    if (!mesh) return null
    const position = mesh.geometry.getAttribute('position') as THREE.BufferAttribute
    if (!position || position.count === 0) return null
    const origin = new THREE.Vector3(...(frame.origin as [number, number, number]))
    const axisU = new THREE.Vector3(...(frame.u as [number, number, number])).normalize()
    const axisV = new THREE.Vector3(...(frame.v as [number, number, number])).normalize()
    const rel = new THREE.Vector3()
    let maxU = 0
    let maxV = 0
    for (let i = 0; i < position.count; i++) {
      rel.set(position.getX(i), position.getY(i), position.getZ(i)).sub(origin)
      maxU = Math.max(maxU, Math.abs(rel.dot(axisU)))
      maxV = Math.max(maxV, Math.abs(rel.dot(axisV)))
    }
    const reach = Math.max(maxU, maxV)
    if (!(reach > 0)) return null
    return snap125(reach * 1.2)
  }

  /** 荷重の大きさのラベル(canvasテクスチャの札)。**モデルの大きさへ追随**させる
   *  (`height` は呼び出し側が `contentDiagonal` から決める)。 */
  function makeMagnitudeLabel(
    text: string,
    height: number,
  ): { sprite: THREE.Sprite; disposables: Array<{ dispose(): void }> } {
    const canvas = document.createElement('canvas')
    canvas.width = 256
    canvas.height = 96
    const ctx = canvas.getContext('2d')
    if (ctx) {
      ctx.fillStyle = 'rgba(16, 22, 30, 0.85)'
      ctx.fillRect(0, 0, canvas.width, canvas.height)
      ctx.fillStyle = '#ffc857'
      ctx.font = 'bold 48px sans-serif'
      ctx.textBaseline = 'middle'
      ctx.fillText(text, 12, canvas.height / 2)
    }
    const texture = new THREE.CanvasTexture(canvas)
    const material = new THREE.SpriteMaterial({ map: texture, depthTest: false, depthWrite: false, transparent: true })
    const sprite = new THREE.Sprite(material)
    sprite.scale.set(height * (canvas.width / canvas.height), height, 1)
    return { sprite, disposables: [texture, material] }
  }

  /**
   * 形状最適化(第3のモード)の見た目を作り直す。**`null` は全部消すだけ**
   * (モードを抜けた・そもそも入っていない)。
   *
   * `preserveBodies` / `excludeBodies` は `applyFaceMaterials()` が読む役割の表を差し替える
   * だけ(ボディの色は選択と同じ経路)。拘束・荷重は**装飾**(`optimizeRoot`)を作り直す ——
   * 拘束面は固定記号(四角錐)を法線の上に置き、荷重面は矢印(`THREE.ArrowHelper`。長さは
   * 大きさの対数スケール)+ 大きさのラベルを描く。どちらも**モデルの大きさへ追随**させる
   * (`contentDiagonal` が0なら文書が空なので何も描けない)。
   */
  function setOptimizeState(state: OptimizeVisualState | null): void {
    clearOptimizeAnnotations()
    bodyRoles = new Map()
    fixtureFaceKeys = new Set()
    if (state === null) {
      applyFaceMaterials()
      return
    }
    for (const bodyId of state.preserveBodies) bodyRoles.set(bodyId, 'preserve')
    for (const bodyId of state.excludeBodies) bodyRoles.set(bodyId, 'exclude')
    fixtureFaceKeys = new Set(state.fixtures.map((ref) => refKey(ref.bodyId, ref.faceIndex)))
    applyFaceMaterials()

    const scale = Math.max(contentDiagonal, 1e-6)
    if (scale <= 1e-6) return // 空の文書(装飾できる面が無い)

    // -------------------------------------------------- 拘束の固定記号(四角錐)
    if (state.fixtures.length > 0) {
      const markSize = scale * 0.03
      const markMaterial = new THREE.MeshLambertMaterial({ color: OPTIMIZE_FIXTURE_MARK_COLOR })
      optimizeDisposables.push(markMaterial)
      for (const ref of state.fixtures) {
        const measured = faceCentroidNormal(ref.bodyId, ref.faceIndex)
        if (!measured) continue
        // **4角錐**(`radialSegments=4`)。既定は+Y軸を高さの向きに持つので、面へ刺さる向き
        // (法線の逆)へ回してから、根元が面の少し上に来るよう法線方向へ持ち上げる。
        const geometry = new THREE.ConeGeometry(markSize * 0.6, markSize, 4)
        optimizeDisposables.push(geometry)
        const cone = new THREE.Mesh(geometry, markMaterial)
        cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), measured.normal.clone().negate())
        cone.position.copy(measured.center).addScaledVector(measured.normal, markSize * 0.6)
        optimizeRoot.add(cone)
        optimizeAnnotationInfo.fixtures.push({ bodyId: ref.bodyId, faceIndex: ref.faceIndex })
      }
    }

    // -------------------------------------------------- 荷重の矢印+ラベル
    if (state.load !== null && state.loads.length > 0) {
      const { magnitude, direction, vector } = state.load
      // **長さは大きさの対数スケール**(タスク仕様)。1N未満でも長さが0にならないよう
      // `Math.max(magnitude, 1)` で下駄を履かせる(見た目専用の式で、正確な力学ではない)。
      const length = scale * 0.12 * (1 + Math.log10(Math.max(magnitude, 1)) * 0.3)
      const labelHeight = scale * 0.05
      const labelText = `${Number(magnitude.toFixed(2))}N`
      for (const ref of state.loads) {
        const measured = faceCentroidNormal(ref.bodyId, ref.faceIndex)
        if (!measured) continue
        const dir =
          direction === 'vector' && vector !== null
            ? new THREE.Vector3(vector[0], vector[1], vector[2]).normalize()
            : direction === '-normal'
              ? measured.normal.clone().negate()
              : measured.normal.clone()
        const arrow = new THREE.ArrowHelper(dir, measured.center, length, OPTIMIZE_LOAD_COLOR, length * 0.3, length * 0.18)
        optimizeRoot.add(arrow)
        optimizeDisposables.push(arrow) // `ArrowHelper.dispose()` が線・矢頭の材質を片付ける
        const { sprite, disposables } = makeMagnitudeLabel(labelText, labelHeight)
        sprite.position.copy(measured.center).addScaledVector(dir, length * 1.15)
        optimizeRoot.add(sprite)
        optimizeDisposables.push(...disposables)
        optimizeAnnotationInfo.loads.push({
          bodyId: ref.bodyId,
          faceIndex: ref.faceIndex,
          direction: [dir.x, dir.y, dir.z],
          label: labelText,
        })
      }
    }
  }

  /**
   * 面のピック。**レイ距離も返す** —— 基準平面と面のどちらが手前かを比べるのに要る
   * (`onPointerUp`)。`pickAt` はこれの薄い覆いで、距離を要らない呼び出し側のためにある。
   */
  function faceHitAt(
    offsetX: number,
    offsetY: number,
  ): { ref: FaceRef; distance: number } | null {
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    // **隠しているメッシュは指せない**(プレビュー中の本物。three.js の Raycaster は
    // `visible` を見ないので、ここで明示的に外す)。
    const hits = raycaster.intersectObjects(
      allFaceMeshes.filter((mesh) => mesh.visible),
      false,
    )
    if (hits.length === 0) return null
    const bodyId = hits[0].object.userData.bodyId
    const faceIndex = hits[0].object.userData.faceIndex
    if (typeof bodyId !== 'string' || typeof faceIndex !== 'number') return null
    return { ref: { bodyId, faceIndex }, distance: hits[0].distance }
  }

  function pickAt(offsetX: number, offsetY: number): FaceRef | null {
    return faceHitAt(offsetX, offsetY)?.ref ?? null
  }

  // ------------------------------------------------------------------ 基準3平面(スケッチを貼る先)
  //
  // 「指せるか」の旗(`basePlanesOn`)は平面グリッドの節で宣言してある —— **どの平面を
  // 出すかがその旗で決まる**(`desiredGridPlanes`)ので、表示側と同じ場所に置く。

  function showBasePlanes(on: boolean): void {
    if (basePlanesOn === on) return
    basePlanesOn = on
    pickChanged()
    applyGridPlanes() // 出す平面(1枚 ⇄ 3枚)と色(通常 ⇄ 起動中)がこの旗で変わる
    // 指せなくなった平面の板を残さない(`basePlaneAt` が null を返すので次のフレームでも
    // 消えるが、ゲートを閉じた瞬間に消えるほうが「もう指せない」が伝わる)。
    if (!on) {
      hoverPlane = null
      applyBasePlaneHover()
    }
  }

  /**
   * 基準3平面のうち当たった1枚と、そのレイ距離(当たらなければ null)。
   *
   * **描いているグリッドは判定に使わない**(格子はシェーダーが無限に描くので、そもそも
   * 当たれるジオメトリが無い)。`planePointAt` と同じ解析交差を3枚ぶん回し、**ピック範囲**
   * の外(|u| か |v| が `gridExtent` 超)は捨て、**レイ距離が最小の1枚**を採る。
   *
   * **描画が無限になってもピック範囲は無限にしない**(2026-09-03) —— 無限にすると
   * engine `duocad-base-planes-lose-to-solids-by-depth` と
   * `viewport-resize-silently-changes-what-a-click-grabs` が同時に悪化する。
   * ホバー板(`rebuildBasePlaneHover`)も同じ `gridExtent` から作る —— ここだけずれると
   * 「板は見えているのに指せない」という遠い壊れ方をする。
   */
  function basePlaneAt(
    offsetX: number,
    offsetY: number,
  ): { name: BasePlaneName; distance: number } | null {
    if (!basePlanesOn) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    let best: { name: BasePlaneName; distance: number } | null = null
    const hit = new THREE.Vector3()
    for (const spec of BASE_PLANES) {
      const plane = new THREE.Plane(new THREE.Vector3(...spec.normal), 0)
      if (raycaster.ray.intersectPlane(plane, hit) === null) continue // 平行 / カメラの裏
      const u = hit.dot(new THREE.Vector3(...spec.u))
      const v = hit.dot(new THREE.Vector3(...spec.v))
      if (Math.abs(u) > gridExtent || Math.abs(v) > gridExtent) continue
      const distance = raycaster.ray.origin.distanceTo(hit)
      if (best === null || distance < best.distance) best = { name: spec.name, distance }
    }
    return best
  }

  function projectBasePlane(name: BasePlaneName): { x: number; y: number } | null {
    const spec = BASE_PLANES.find((candidate) => candidate.name === name)
    if (!spec || !basePlanesOn) return null
    for (const [u, v] of BASE_PLANE_PROBES) {
      const world = new THREE.Vector3(
        u * spec.u[0] + v * spec.v[0],
        u * spec.u[1] + v * spec.v[1],
        u * spec.u[2] + v * spec.v[2],
      )
      const screen = toScreen(world)
      if (screen === null) continue
      // **本当にその平面が取れる画素か**を確かめてから返す(面の `project` と同じ流儀)。
      const plane = basePlaneAt(screen.x, screen.y)
      if (plane === null || plane.name !== name) continue
      const face = faceHitAt(screen.x, screen.y)
      if (face !== null && face.distance < plane.distance) continue // 部品に隠れている
      return screen
    }
    return null
  }

  function project(bodyId: string, faceIndex: number): { x: number; y: number } | null {
    const entry = bodies.get(bodyId)
    const mesh = entry?.faceMeshes.find(
      (candidate) => candidate.userData.faceIndex === faceIndex,
    )
    if (!mesh || !mesh.visible) return null
    const position = mesh.geometry.getAttribute('position')
    const index = mesh.geometry.getIndex()
    if (!position || !index) return null
    const { width, height } = size()
    const point = new THREE.Vector3()
    const corner = new THREE.Vector3()

    const triangleCount = index.count / 3
    for (let triangle = 0; triangle < triangleCount; triangle += 1) {
      point.set(0, 0, 0)
      for (let vertex = 0; vertex < 3; vertex += 1) {
        const at = index.getX(triangle * 3 + vertex)
        corner.set(position.getX(at), position.getY(at), position.getZ(at))
        point.add(corner)
      }
      point.multiplyScalar(1 / 3)
      mesh.localToWorld(point)
      const projected = point.clone().project(camera)
      if (projected.z < -1 || projected.z > 1) continue
      const x = ((projected.x + 1) / 2) * width
      const y = ((1 - projected.y) / 2) * height
      if (x < 0 || y < 0 || x > width || y > height) continue
      if (!clickable(x, y)) continue // 浮いているパネルに覆われている(次の三角形を試す)
      const hit = pickAt(x, y)
      if (hit && hit.bodyId === bodyId && hit.faceIndex === faceIndex) return { x, y }
    }
    return null
  }

  // ------------------------------------------------------------------ D: 辺(選べる線)
  //
  // **フィレット・面取りが人間側から使えるようになる場所**。輪郭線として常時出ている
  // `EdgesGeometry` の線(`buildBody` 参照)はコアの辺の添字と対応しておらず、どれを指しても
  // フィレットの引数にならない。ここで描くのはサーバが配る
  // `kernel.tessellate_edges` の折れ線で、**1本1本がコアの添字を持っている**。
  //
  // ソリッドともスケッチとも別のグループに持つ(`update()`/`prune()` は `root` しか
  // 触らないので、押し出しで辺のグループが巻き込まれない)。
  const edgeRoot = new THREE.Group()
  scene.add(edgeRoot)

  const pickEdgeMaterial = new THREE.LineBasicMaterial({
    color: PICK_EDGE_COLOR,
    // **深度検査をしない**(奥の辺も見え、奥の辺も指せる)。three.js の線のレイキャストは
    // 面による遮蔽を見ないので、見た目だけ隠すと「見えない辺が選ばれる」になる。
    depthTest: false,
  })
  const pickEdgeSelectedMaterial = new THREE.LineBasicMaterial({
    color: HIGHLIGHT_COLOR,
    depthTest: false,
  })
  const pickEdgeProposedMaterial = new THREE.LineBasicMaterial({
    color: PROPOSED_COLOR,
    depthTest: false,
  })
  // ホバー中の辺(面と同じで明度を足すだけ)。
  const pickEdgeHoverMaterial = new THREE.LineBasicMaterial({
    color: lighten(PICK_EDGE_COLOR, HOVER_LIGHTEN),
    depthTest: false,
  })
  const pickEdgeSelectedHoverMaterial = new THREE.LineBasicMaterial({
    color: lighten(HIGHLIGHT_COLOR, HOVER_LIGHTEN),
    depthTest: false,
  })
  const pickEdgeProposedHoverMaterial = new THREE.LineBasicMaterial({
    color: lighten(PROPOSED_COLOR, HOVER_LIGHTEN),
    depthTest: false,
  })

  /** 今出している辺(**1つのBodyぶんだけ**)。空なら辺モードではない。 */
  let edgeLines: THREE.Line[] = []
  let edgeBodyId: string | null = null
  /** 今出している辺の元になった配列(**同じものを渡されたら作り直さない**ための印)。 */
  let edgeSource: EdgeGeometry[] | null = null
  const edgeDisposables: Array<{ dispose(): void }> = []

  function clearEdges(): void {
    for (const line of edgeLines) edgeRoot.remove(line)
    for (const item of edgeDisposables) item.dispose()
    edgeDisposables.length = 0
    edgeLines = []
    edgeBodyId = null
    edgeSource = null
    invalidateHover()
  }

  function showEdges(bodyId: string | null, edges: EdgeGeometry[]): void {
    // **同じものを渡されたら何もしない**。呼び出し側(`App.svelte`)は選択が変わるたびに
    // 呼び直すので、毎回作り直すと**辺を1本クリックするたびに数百本ぶんの
    // BufferGeometry を捨てて作り直す**ことになる(27部品では1Bodyで600本超)。
    if (bodyId !== null && bodyId === edgeBodyId && edges === edgeSource) return
    clearEdges()
    if (bodyId === null || edges.length === 0) return
    edgeBodyId = bodyId
    edgeSource = edges
    for (const edge of edges) {
      // 2点(=6要素)未満は線にならない。`kernel.tessellate_edges` は失敗時も端点2点を
      // 返す契約なので**ここへは届かないはず**だが、届いたときに黙って壊れた線を
      // 作らないための番人として残す。
      if (edge.points.length < 6) continue
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute(
        'position',
        new THREE.BufferAttribute(new Float32Array(edge.points), 3),
      )
      geometry.computeBoundingSphere()
      const line = new THREE.Line(geometry, pickEdgeMaterial)
      line.renderOrder = LAYER.edge // 深度を書かないぶん、描く順序で面より手前に出す
      line.userData.bodyId = bodyId
      line.userData.edgeIndex = edge.index
      edgeRoot.add(line)
      edgeLines.push(line)
      edgeDisposables.push(geometry)
    }
  }

  /** 今出している辺のうち選択に入っているもの(面の `selectedFaceKeys` と同じ役目)。 */
  let selectedEdgeIndices = new Set<number>()
  let edgeOrigin: 'user' | 'proposed' = 'user'

  /** 辺の材質を決める。**選択とホバーの両方を見る唯一の場所**(面と同じ規律)。 */
  function applyEdgeMaterials(): void {
    const selected = edgeOrigin === 'proposed' ? pickEdgeProposedMaterial : pickEdgeSelectedMaterial
    const selectedHover =
      edgeOrigin === 'proposed' ? pickEdgeProposedHoverMaterial : pickEdgeSelectedHoverMaterial
    highlightColor.edge = selectedEdgeIndices.size === 0 ? null : selected.color.getHex()
    for (const line of edgeLines) {
      const edgeIndex = line.userData.edgeIndex as number
      const isHit = selectedEdgeIndices.has(edgeIndex)
      const isHovered =
        hoverEdge !== null &&
        hoverEdge.bodyId === (line.userData.bodyId as string) &&
        hoverEdge.edgeIndex === edgeIndex
      line.material = isHit
        ? isHovered
          ? selectedHover
          : selected
        : isHovered
          ? pickEdgeHoverMaterial
          : pickEdgeMaterial
    }
  }

  function highlightEdges(refs: EdgeRef[], origin: 'user' | 'proposed'): void {
    edgeOrigin = origin
    selectedEdgeIndices = new Set(
      refs.filter((ref) => ref.bodyId === edgeBodyId).map((ref) => ref.edgeIndex),
    )
    applyEdgeMaterials()
  }

  /**
   * いまのカメラを投影の種類ごとの最小限の形へ落とす(`gridScale.ts` の純関数へ渡す用)。
   * **正投影には「距離」に相当する量が無い**ので、透視と同じ形にはできない。
   */
  function cameraProjection(): CameraProjection {
    if (camera === orthographic) {
      return { kind: 'ortho', frustumHeight: 2 * Math.max(viewHalfHeight(), 1e-6) }
    }
    return {
      kind: 'persp',
      fovDeg: perspective.fov,
      distance: camera.position.distanceTo(controls.target),
    }
  }

  /**
   * 画面1画素ぶんの世界座標の長さ(当たり判定の太さを画素で決めるため)。
   * **式は `gridScale.ts` の `mmPerPixel` 1つに寄せてある** —— グリッドの段の判定と
   * 当たり判定の太さが同じ物差しで動く(ずれると「見えている線が指せない」になる)。
   */
  function worldPerPixel(): number {
    const { height } = size()
    return Math.max(mmPerPixel(cameraProjection(), height), 1e-9)
  }

  // ------------------------------------------------------------------ 可変解像度(ズーム連動)
  //
  // **寄ったら細かく、引いたら粗く。** 曲線が多角形に見えるのは弦と弧の乖離が数画素に
  // なったときなので、目標精度は世界座標のmmではなく**画面の画素**で決める
  // (式と定数は `adaptiveResolution.ts`。同じものが `duocad/resolution.py` にもある)。

  /** カメラが止まったと見なすまでの待ち[ms]。**減衰(`dampingFactor`)が収まる時間も含む。** */
  const RESOLUTION_SETTLE_MS = 180
  /** 目標偏差がこの比だけ動いて初めて作り直す(1割程度の揺れでは測り直さない)。 */
  const RESOLUTION_CHANGE_RATIO = 1.25
  /** `worldPerPixel` が「まだ動いている」と見なす相対差。減衰の収束判定。 */
  const RESOLUTION_STILL_EPSILON = 1.0e-4
  /** カメラが「まだ動いている」と見なす移動量[画素]。減衰の収束判定(`cameraMoved`)。 */
  const CAMERA_STILL_PIXELS = 0.5
  /** 同・向きの収束判定(四元数の内積が `1 - これ` を下回ったら動いている)。 */
  const CAMERA_STILL_ANGLE = 1.0e-7
  /**
   * **いま細かく測っている面が粗い側へ落ちるまでの余裕[画素]**(`faceCulling.ts` の
   * `selectFineFaces`)。0にすると、視錐台の縁にある面が「入った/出た」を往復し、
   * そのたびに27部品ぶんの測り直しがサーバへ飛ぶ。**画素で決める**のは、
   * 縁の余裕が寄り引きで変わってはいけないため(当たり判定の太さと同じ流儀)。
   */
  const FOCUS_HYSTERESIS_PIXELS = 16

  /** 直近に採用した解像度(スケッチの折れ線と `viewResolution()` が読む)。 */
  let appliedResolution: ViewportResolution = {
    ...DEFAULT_RESOLUTION,
    worldPerPixel: 0,
    sizeHint: 0,
    focus: null,
  }
  let resolutionListener: ((resolution: ViewportResolution) => void) | null = null
  let resolutionApplied = false
  let lastWorldPerPixel = -1
  let lastSizeHint = -1
  /** 直近に「止まっている」と記録したカメラ(位置・姿勢・倍率)。**下記の閾値で比べる。** */
  const lastCameraPosition = new THREE.Vector3(NaN, NaN, NaN)
  const lastCameraQuaternion = new THREE.Quaternion(0, 0, 0, 1)
  let lastCameraZoom = -1
  /** カメラが**意味のある幅で**動くたびに増える番号(`computeFocus` の再計算の鍵)。 */
  let cameraEpoch = 0
  let steadySince = 0
  /** 直近に `computeFocus` したときの姿勢と世代(同じなら再走査しない)。 */
  let lastFocusComputeEpoch = -1
  let lastFocusComputeGeneration = -1
  /** face単位の高精度化を使うか。**切れるのは実機検査のためだけ**(既定は入り)。 */
  let faceCullingEnabled = true
  let lastAppliedFocus: Record<string, number[] | null> | null = null
  /**
   * いま見えているものの外接対角[mm]。**毎フレーム測らない** —— `Box3.setFromObject` は
   * 全メッシュを走査するので、中身が変わったとき(`update` / `prune` / `showSketches`)に
   * だけ測り直す。上下限の丸めにしか使わないので、1フレーム古くても害が無い。
   */
  let contentDiagonal = 0

  function refreshContentDiagonal(): void {
    const box = contentBoxForGrid()
    contentDiagonal = box.isEmpty() ? 0 : box.getSize(new THREE.Vector3()).length()
    // **広げるのは即座、縮めない**(`allowShrink: false`) —— ふつうの編集で内容がしきい値
    // 付近で揺れても、グリッドが縮んで点滅することはない(`applyGridScale` のdocstring)。
    applyGridScale(box, false)
  }

  /**
   * カメラが**意味のある幅で**動いたか(位置・姿勢・正投影の倍率)。
   *
   * **一致ではなく閾値で見る。** `OrbitControls` の減衰は指数で近づくので、カメラは
   * いつまでも「完全には」止まらない —— 値の一致を「止まった」の条件にすると
   * `steadySince` が毎フレーム振り出しに戻り、**解像度が二度と採り直されない**
   * (2026-08-30に実測。ホイールを回しても目標偏差が動かなくなった)。
   * 閾値は `worldPerPixel` と同じ流儀で**画面の画素**で決める —— 世界座標のmmで決めると、
   * 20mmの立方体と600mmの筐体で「止まった」の意味が変わる。
   */
  function cameraMoved(): boolean {
    if (!Number.isFinite(lastCameraPosition.x)) return true
    if (camera.position.distanceTo(lastCameraPosition) > worldPerPixel() * CAMERA_STILL_PIXELS) {
      return true
    }
    // 四元数の内積は向きの近さ(1で一致)。符号は向きに影響しないので絶対値で見る。
    if (Math.abs(camera.quaternion.dot(lastCameraQuaternion)) < 1 - CAMERA_STILL_ANGLE) return true
    return Math.abs(camera.zoom - lastCameraZoom) > Math.abs(lastCameraZoom) * RESOLUTION_STILL_EPSILON
  }

  /** 「ここで止まっている」を記録する(次のフレームの `cameraMoved` の基準)。 */
  function rememberCamera(): void {
    lastCameraPosition.copy(camera.position)
    lastCameraQuaternion.copy(camera.quaternion)
    lastCameraZoom = camera.zoom
  }

  function focusEqual(
    a: Record<string, number[] | null> | null,
    b: Record<string, number[] | null> | null,
  ): boolean {
    if (a === b) return true
    if (a === null || b === null) return false
    const keysA = Object.keys(a).sort()
    const keysB = Object.keys(b).sort()
    if (keysA.length !== keysB.length) return false
    for (let i = 0; i < keysA.length; i++) {
      if (keysA[i] !== keysB[i]) return false
      const va = a[keysA[i]]
      const vb = b[keysB[i]]
      if (va === null && vb === null) continue
      if (va === null || vb === null) return false
      if (va.length !== vb.length) return false
      for (let j = 0; j < va.length; j++) {
        if (va[j] !== vb[j]) return false
      }
    }
    return true
  }

  /** 視錐台とバックフェイスから、サーバへ送る `focus` を組み立てる(落ち着いた後だけ呼ぶ)。 */
  function computeFocus(): Record<string, number[] | null> | null {
    // 切ってあるときは**何も指定しない** = 全Body全面が細かい側(導入前と同じ)。
    if (!faceCullingEnabled || faceSamples.size === 0) return null
    const frustum = new THREE.Frustum()
    camera.updateMatrixWorld()
    frustum.setFromProjectionMatrix(
      new THREE.Matrix4().multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse),
    )
    const viewDirection = camera.getWorldDirection(new THREE.Vector3())
    const viewpoint = (camera as THREE.PerspectiveCamera).isPerspectiveCamera
      ? camera.getWorldPosition(new THREE.Vector3())
      : null
    const hysteresis = worldPerPixel() * FOCUS_HYSTERESIS_PIXELS
    const result: Record<string, number[] | null> = {}
    let anySpecified = false
    for (const [bodyId, samples] of faceSamples) {
      const selected = selectFineFaces(samples, frustum, viewpoint, viewDirection, hysteresis)
      if (selected !== null) {
        result[bodyId] = selected
        anySpecified = true
      }
    }
    return anySpecified ? result : null
  }

  /**
   * カメラが落ち着いていれば解像度を採り直す(毎フレーム `tick` から呼ばれる)。
   *
   * **門**はズームに加え**姿勢**も見る —— 寄りだけでは `worldPerPixel` が動かず、
   * 視錐台が変わっても気づけない。落ち着いたあと `computeFocus` は2835面を走査するので、
   * 姿勢と `focusGeneration` が同じなら再走査しない。
   *
   * **`moved` の分岐に `updateClipPlanes()` を相乗りさせてある**(2026-08-30)。near/farは
   * カメラ距離が変わるたびに引き直すべきだが、毎フレーム呼ぶ必要は無い —— この分岐は
   * 既存の画素基準の閾値判定(`cameraMoved()`)で門が閉じているので、オービット/パン/ドリーの
   * 減衰が画素以下に収まった時点で自然に呼ばれなくなる(動いている間は毎フレーム呼ばれるが、
   * `updateProjectionMatrix()` は毎フレームでも安い)。
   */
  function updateResolution(now: number): void {
    const perPixel = worldPerPixel()
    const moved =
      lastWorldPerPixel < 0 ||
      Math.abs(perPixel - lastWorldPerPixel) > lastWorldPerPixel * RESOLUTION_STILL_EPSILON ||
      contentDiagonal !== lastSizeHint ||
      cameraMoved()
    if (moved) {
      lastWorldPerPixel = perPixel
      lastSizeHint = contentDiagonal
      rememberCamera()
      cameraEpoch += 1
      steadySince = now
      updateClipPlanes()
      return
    }
    if (now - steadySince < RESOLUTION_SETTLE_MS) return
    // **同じカメラ・同じ形なら判定もやり直さない**(`computeFocus` は2835面を走査する)。
    if (cameraEpoch === lastFocusComputeEpoch && focusGeneration === lastFocusComputeGeneration) {
      return
    }

    const focus = computeFocus()
    lastFocusComputeEpoch = cameraEpoch
    lastFocusComputeGeneration = focusGeneration

    const target = viewResolution(perPixel, contentDiagonal)
    const ratio = target.deviation / appliedResolution.deviation
    const focusChanged = !focusEqual(focus, lastAppliedFocus)
    if (
      resolutionApplied &&
      ratio < RESOLUTION_CHANGE_RATIO &&
      ratio > 1 / RESOLUTION_CHANGE_RATIO &&
      !focusChanged
    ) {
      return
    }
    resolutionApplied = true
    lastAppliedFocus = focus
    appliedResolution = { ...target, worldPerPixel: perPixel, sizeHint: contentDiagonal, focus }
    // **スケッチはここで描き直す**(ブラウザ内で完結する経路。サーバ往復が要らない)。
    redrawSketches()
    resolutionListener?.(appliedResolution)
  }

  function edgeAt(offsetX: number, offsetY: number): EdgeRef | null {
    if (edgeLines.length === 0) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    // 線の当たり判定の太さは**世界座標**で指定する。画素で決めないと、寄れば寄るほど
    // 掴みにくくなる(20mmの立方体と2835面の筐体で同じ操作感にならない)。
    const previous = raycaster.params.Line?.threshold
    raycaster.params.Line = { threshold: worldPerPixel() * EDGE_PICK_PIXELS }
    const hits = raycaster.intersectObjects(edgeLines, false)
    if (previous !== undefined) raycaster.params.Line = { threshold: previous }
    if (hits.length === 0) return null
    const bodyId = hits[0].object.userData.bodyId
    const edgeIndex = hits[0].object.userData.edgeIndex
    if (typeof bodyId !== 'string' || typeof edgeIndex !== 'number') return null
    return { bodyId, edgeIndex }
  }

  /** 折れ線の道のりの `fraction` の位置(0=始点 1=終点)。 */
  function pointAlong(line: THREE.Line, fraction: number): THREE.Vector3 | null {
    const position = line.geometry.getAttribute('position')
    if (!position || position.count < 2) return null
    const at = fraction * (position.count - 1)
    const first = Math.min(Math.floor(at), position.count - 2)
    const t = at - first
    const a = new THREE.Vector3(position.getX(first), position.getY(first), position.getZ(first))
    const b = new THREE.Vector3(
      position.getX(first + 1),
      position.getY(first + 1),
      position.getZ(first + 1),
    )
    return a.lerp(b, t)
  }

  function projectEdge(bodyId: string, edgeIndex: number): { x: number; y: number } | null {
    if (edgeBodyId !== bodyId) return null
    const line = edgeLines.find((candidate) => candidate.userData.edgeIndex === edgeIndex)
    if (!line) return null
    // **端点は避ける**(隣の辺と共有しているので、そこを狙うと隣が取れることがある)。
    // 中ほどから順に試し、**実際にその辺へ当たる画素**だけを返す(面の `project` と同じ規律)。
    for (const fraction of [0.5, 0.35, 0.65, 0.2, 0.8, 0.1, 0.9]) {
      const point = pointAlong(line, fraction)
      if (point === null) continue
      const screen = toScreen(point)
      if (screen === null) continue
      const hit = edgeAt(screen.x, screen.y)
      if (hit && hit.bodyId === bodyId && hit.edgeIndex === edgeIndex) return screen
    }
    return null
  }

  // ------------------------------------------------------------------ 頂点(選択中のものだけ)
  //
  // **辺と違い、候補を全部出すことはしない。** 頂点を引数に取るverbが無いので
  // 「候補を見せて選ばせる」場面が無く、要るのは「AIが選んだ頂点が今どれか」を人が見て
  // 外せることだけである(`duocad/server.py` の `Session.vertices` と同じ理由)。
  //
  // **点(`THREE.Points`)で描き、大きさは画素で固定する**(`sizeAttenuation: false`)。
  // 世界座標の球にすると、描いた時のカメラ距離で大きさが決まってしまい、**寄れば巨大な玉、
  // 引けば1画素未満**になる —— 頂点マーカーは選択中の頂点を見る唯一の手段なので、
  // 27部品を引きで見ているときに消えるのは機能が無いのと同じである。
  const vertexRoot = new THREE.Group()
  scene.add(vertexRoot)

  const vertexMaterial = new THREE.PointsMaterial({
    color: HIGHLIGHT_COLOR,
    size: VERTEX_PIXELS * 2,
    sizeAttenuation: false,
    // 辺と同じく**深度検査をしない**(奥のマーカーも見え、奥のマーカーも指せる)。
    // 見た目だけ隠すと「見えない頂点が外れる」になる。
    depthTest: false,
  })
  const vertexProposedMaterial = new THREE.PointsMaterial({
    color: PROPOSED_COLOR,
    size: VERTEX_PIXELS * 2,
    sizeAttenuation: false,
    depthTest: false,
  })

  /** 今出している頂点マーカー。空なら選択に頂点が入っていない。 */
  let vertexMeshes: THREE.Points[] = []
  const vertexDisposables: Array<{ dispose(): void }> = []
  /** 直前に描いた印(**同じものを渡されたら作り直さない**ため)。 */
  let vertexKey = ''

  function clearVertices(): void {
    for (const mesh of vertexMeshes) vertexRoot.remove(mesh)
    for (const item of vertexDisposables) item.dispose()
    vertexDisposables.length = 0
    vertexMeshes = []
    vertexKey = ''
  }

  function showVertices(markers: VertexMarker[], origin: 'user' | 'proposed'): void {
    const key = `${origin}|${markers
      .map((marker) => `${marker.bodyId}|${marker.vertexIndex}|${marker.point.join(',')}`)
      .join(';')}`
    // 呼び出し側(App.svelte)は選択が変わるたびに呼び直すので、毎回作り直すと
    // ジオメトリを捨てて作り直し続けることになる(`showEdges` と同じ番人)。
    if (key === vertexKey) return
    clearVertices()
    vertexKey = key
    pickChanged() // 頂点マーカーは面より先に取られるので、増減で「指せるもの」が変わる
    const material = origin === 'proposed' ? vertexProposedMaterial : vertexMaterial
    highlightColor.vertex = markers.length === 0 ? null : material.color.getHex()
    if (markers.length === 0) return
    for (const marker of markers) {
      if (marker.point.length < 3) continue
      // **1マーカー1オブジェクト**(辺が1本1 `THREE.Line` なのと同じ) —— まとめると
      // 当たった点からコアの添字へ戻れない。数は選択中のものだけなので費用は無視できる。
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute(
        'position',
        new THREE.BufferAttribute(new Float32Array(marker.point.slice(0, 3)), 3),
      )
      geometry.computeBoundingSphere()
      const mesh = new THREE.Points(geometry, material)
      mesh.renderOrder = LAYER.region // 深度を書かないぶん、描く順序で辺より手前に出す
      mesh.userData.bodyId = marker.bodyId
      mesh.userData.vertexIndex = marker.vertexIndex
      mesh.userData.point = marker.point
      vertexRoot.add(mesh)
      vertexMeshes.push(mesh)
      vertexDisposables.push(geometry)
    }
  }

  function vertexAt(offsetX: number, offsetY: number): VertexRef | null {
    if (vertexMeshes.length === 0) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    // 点の当たり判定も**画素で決める**(辺の `edgeAt` と同じ理由 —— 寄るほど掴みにくく
    // なるのを避ける)。描いている大きさと揃えておく。
    const previous = raycaster.params.Points?.threshold
    raycaster.params.Points = { threshold: worldPerPixel() * VERTEX_PIXELS }
    const hits = raycaster.intersectObjects(vertexMeshes, false)
    if (previous !== undefined) raycaster.params.Points = { threshold: previous }
    if (hits.length === 0) return null
    const bodyId = hits[0].object.userData.bodyId
    const vertexIndex = hits[0].object.userData.vertexIndex
    if (typeof bodyId !== 'string' || typeof vertexIndex !== 'number') return null
    return { bodyId, vertexIndex }
  }

  function projectVertex(bodyId: string, vertexIndex: number): { x: number; y: number } | null {
    const mesh = vertexMeshes.find(
      (candidate) =>
        candidate.userData.bodyId === bodyId && candidate.userData.vertexIndex === vertexIndex,
    )
    if (!mesh) return null
    const point = mesh.userData.point as number[]
    const screen = toScreen(new THREE.Vector3(point[0], point[1], point[2]))
    if (screen === null) return null
    // **本当にその頂点が取れる画素か**を確かめてから返す(面の `project` と同じ流儀)。
    const hit = vertexAt(screen.x, screen.y)
    if (hit === null || hit.bodyId !== bodyId || hit.vertexIndex !== vertexIndex) return null
    return screen
  }

  // ------------------------------------------------------------------ C: スケッチ
  //
  // **描くのは線と領域だけ**で、スケッチ平面そのもの(無限に広がる面)は描かない ——
  // 面を描くと奥のソリッドが隠れ、「既存の部品の上に描く」が見えなくなる。
  //
  // ソリッドとは別のグループ(`sketchRoot`)に持つ。`update()`/`prune()` はソリッド側の
  // `root` しか触らないので、押し出しでスケッチが消えることがない。
  const sketchRoot = new THREE.Group()
  scene.add(sketchRoot)

  const sketchMaterial = new THREE.LineBasicMaterial({ color: SKETCH_COLOR })
  const sketchGhostMaterial = new THREE.LineBasicMaterial({ color: SKETCH_GHOST_COLOR })
  // 選択中の図形の線。**線の材質を作り分けるだけ**で、形の作り方は未選択と同じ
  // (`buildEntityLine` に材質を渡す形になっているのはこのため)。
  const sketchSelectedMaterial = new THREE.LineBasicMaterial({ color: SKETCH_SELECTED_COLOR })
  // 点マーカー。**深度を書かない**(`vertexMaterial` と同じ) —— 線の上に必ず出す。
  const sketchHandleMaterial = new THREE.PointsMaterial({
    color: SKETCH_HANDLE_COLOR,
    size: SKETCH_HANDLE_PIXELS * 2,
    sizeAttenuation: false,
    depthTest: false,
  })
  const sketchHandleSelectedMaterial = new THREE.PointsMaterial({
    color: SKETCH_SELECTED_COLOR,
    size: SKETCH_HANDLE_PIXELS * 2,
    sizeAttenuation: false,
    depthTest: false,
  })
  const regionMaterial = new THREE.MeshBasicMaterial({
    color: REGION_COLOR,
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.35,
    depthWrite: false,
  })
  const regionSelectedMaterial = new THREE.MeshBasicMaterial({
    color: REGION_SELECTED_COLOR,
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.55,
    depthWrite: false,
  })
  // ホバー中の領域。**3Dに領域の色を新しく作らない**(2026-08-27本人決定) ——
  // 面・辺と同じで、既にここにある色に明度を足すだけ。
  const regionHoverMaterial = new THREE.MeshBasicMaterial({
    color: lighten(REGION_COLOR, HOVER_LIGHTEN),
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.35,
    depthWrite: false,
  })
  const regionSelectedHoverMaterial = new THREE.MeshBasicMaterial({
    color: lighten(REGION_SELECTED_COLOR, HOVER_LIGHTEN),
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.55,
    depthWrite: false,
  })

  interface SketchScene {
    frame: SketchDraw['frame']
    plane: SketchDraw['plane']
    objects: THREE.Object3D[]
    disposables: Array<{ dispose(): void }>
    /** 領域のメッシュ(ピック対象)。 */
    regionMeshes: THREE.Mesh[]
    /** 図形の (u,v) の折れ線(視点合わせの中心を出すのに使う)。 */
    points: Array<[number, number]>
    /** 図形1本ごとの折れ線の点数(実機検査の `sketchPolylineCounts` が読む)。 */
    polylineCounts: number[]
    /**
     * 点のマーカー(**1点1オブジェクト**)。当たった点からコアの参照へ戻すために
     * `userData` に `entityId` と `point`(xキー)を持つ ——
     * まとめて1つの `Points` にすると戻れない(頂点マーカーと同じ理由)。
     */
    handleMeshes: THREE.Points[]
    /** 図形の折れ線(**元の図形だけ**。`userData.entityId` を持つ)。辺のピック対象。 */
    handleLines: THREE.Line[]
  }
  const sketches = new Map<string, SketchScene>()
  let ghost: { objects: THREE.Object3D[]; disposables: Array<{ dispose(): void }> } | null = null
  let activeSketchId: string | null = null
  /**
   * 通常モードで領域を指せるか(`pickRegions`)。**スケッチモード中は関係ない** ——
   * モード中は常に「そのスケッチの領域」が対象で、このゲートは見ない。
   */
  let regionPickWanted = false

  /** スケッチ平面上の (u,v) を3Dの点へ(コアの `kernel._to_pnt` と同じ規則)。 */
  function toWorld(frame: SketchDraw['frame'], u: number, v: number): THREE.Vector3 {
    return new THREE.Vector3(
      frame.origin[0] + u * frame.u[0] + v * frame.v[0],
      frame.origin[1] + u * frame.u[1] + v * frame.v[1],
      frame.origin[2] + u * frame.u[2] + v * frame.v[2],
    )
  }

  function buildEntityLine(
    frame: SketchDraw['frame'],
    entity: { kind: string; params: Record<string, number> },
    material: THREE.LineBasicMaterial,
  ): { object: THREE.Object3D; geometry: THREE.BufferGeometry; points: Array<[number, number]> } | null {
    // **今の画面の粗さで折る**(`appliedResolution`)。固定分割だった頃は、寄ると
    // 円が多角形に見え、引くと使われない点をGPUへ送っていた。
    const points = entityPoints(entity.kind, entity.params, appliedResolution)
    if (points.length < 2) return null
    const geometry = new THREE.BufferGeometry().setFromPoints(
      points.map(([u, v]) => toWorld(frame, u, v)),
    )
    return { object: new THREE.Line(geometry, material), geometry, points }
  }

  function clearSketch(sketchId: string): void {
    const existing = sketches.get(sketchId)
    if (!existing) return
    for (const object of existing.objects) sketchRoot.remove(object)
    for (const item of existing.disposables) item.dispose()
    sketches.delete(sketchId)
  }

  /**
   * 直近に受け取った描画指示。**解像度が変わったときに折り直すために控える**
   * (折れ線はここで作るので、指示そのものを持っていないと粗さだけを変えられない)。
   */
  let lastSketchDraws: SketchDraw[] = []

  /** 控えた指示で描き直す(`updateResolution` が呼ぶ)。中身が無ければ何もしない。 */
  function redrawSketches(): void {
    if (lastSketchDraws.length > 0) showSketches(lastSketchDraws)
  }

  function showSketches(entries: SketchDraw[]): void {
    lastSketchDraws = entries
    const keep = new Set(entries.map((entry) => entry.sketchId))
    for (const sketchId of [...sketches.keys()]) if (!keep.has(sketchId)) clearSketch(sketchId)
    for (const entry of entries) {
      clearSketch(entry.sketchId)
      const objects: THREE.Object3D[] = []
      const disposables: Array<{ dispose(): void }> = []
      const regionMeshes: THREE.Mesh[] = []
      const points: Array<[number, number]> = []
      const polylineCounts: number[] = []
      const handleMeshes: THREE.Points[] = []
      const handleLines: THREE.Line[] = []
      // 選ばれている図形・点の索き(`entityId` -> 選ばれた点のxキーの集合。図形全体は `null`)。
      const pickedWhole = new Set(
        entry.selectedHandles.filter((item) => item.point === null).map((item) => item.entityId),
      )
      const pickedPoints = new Set(
        entry.selectedHandles
          .filter((item) => item.point !== null)
          .map((item) => `${item.entityId}|${item.point}`),
      )

      // 領域を先に置く(線が領域の上に見えるように。奥行きは書かないので順序で決まる)
      const selected = new Set(entry.selectedRegions)
      for (const region of entry.regions) {
        if (region.indices.length === 0) continue
        const geometry = new THREE.BufferGeometry()
        geometry.setAttribute(
          'position',
          new THREE.BufferAttribute(new Float32Array(region.positions), 3),
        )
        geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(region.indices), 1))
        geometry.computeBoundingSphere()
        const mesh = new THREE.Mesh(
          geometry,
          selected.has(region.index) ? regionSelectedMaterial : regionMaterial,
        )
        mesh.userData.sketchId = entry.sketchId
        mesh.userData.regionIndex = region.index
        // ホバーとの組み合わせを後から決められるように、選択かどうかを印として残す
        // (材質から読み戻すと、ホバー色が付いた瞬間に選択が分からなくなる)。
        mesh.userData.selected = selected.has(region.index)
        sketchRoot.add(mesh)
        objects.push(mesh)
        regionMeshes.push(mesh)
        disposables.push(geometry)
      }

      for (const entity of entry.entities) {
        const built = buildEntityLine(entry.frame, entity, sketchMaterial)
        if (!built) continue
        sketchRoot.add(built.object)
        objects.push(built.object)
        disposables.push(built.geometry)
        points.push(...built.points)
        polylineCounts.push(built.points.length)
      }

      // **元の図形(`handles`)を、展開した描画の上に重ねて置く。** ここに置くのは
      // 「指せるもの」であって「見えるもの」ではない —— 反復で増えた複製は `entities`
      // 側だけに出て、指す対象にはならない(複製は導出物で `entity_id` を持たない)。
      for (const handle of entry.handles) {
        const selected = pickedWhole.has(handle.entityId)
        const built = buildEntityLine(
          entry.frame,
          handle,
          selected ? sketchSelectedMaterial : sketchMaterial,
        )
        if (built) {
          built.object.renderOrder = LAYER.sketchLine
          built.object.userData.entityId = handle.entityId
          sketchRoot.add(built.object)
          objects.push(built.object)
          disposables.push(built.geometry)
          handleLines.push(built.object as THREE.Line)
        }
        for (const point of entityHandles(handle.kind, handle.params)) {
          if (!Number.isFinite(point.u) || !Number.isFinite(point.v)) continue
          const world = toWorld(entry.frame, point.u, point.v)
          const geometry = new THREE.BufferGeometry()
          geometry.setAttribute(
            'position',
            new THREE.BufferAttribute(new Float32Array([world.x, world.y, world.z]), 3),
          )
          geometry.computeBoundingSphere()
          const picked = selected || pickedPoints.has(`${handle.entityId}|${point.key}`)
          const mesh = new THREE.Points(
            geometry,
            picked ? sketchHandleSelectedMaterial : sketchHandleMaterial,
          )
          mesh.renderOrder = LAYER.sketchPoint // 深度を書かないので、描く順序で線より手前に出す
          mesh.userData.entityId = handle.entityId
          mesh.userData.point = point.key
          sketchRoot.add(mesh)
          objects.push(mesh)
          handleMeshes.push(mesh)
          disposables.push(geometry)
        }
      }
      sketches.set(entry.sketchId, {
        frame: entry.frame,
        plane: entry.plane,
        objects,
        disposables,
        regionMeshes,
        points,
        polylineCounts,
        handleMeshes,
        handleLines,
      })
    }
    invalidateHover() // 領域のメッシュを作り直したので印を捨てる
    refreshContentDiagonal() // 見えているものの大きさが変わった(解像度の上下限に効く)
    updateClipPlanes() // 内容が変わった → 視錐台を引き直す
    refreshGridSketchFrame() // 編集中スケッチのframeがここで初めて届くことがある
  }

  /** 領域の材質を決める。**選択とホバーの両方を見る唯一の場所**(面・辺と同じ規律)。 */
  function applyRegionMaterials(): void {
    for (const [sketchId, entry] of sketches) {
      for (const mesh of entry.regionMeshes) {
        const regionIndex = mesh.userData.regionIndex as number
        const isHit = mesh.userData.selected === true
        const isHovered =
          hoverRegion !== null &&
          hoverRegion.sketchId === sketchId &&
          hoverRegion.index === regionIndex
        mesh.material = isHit
          ? isHovered
            ? regionSelectedHoverMaterial
            : regionSelectedMaterial
          : isHovered
            ? regionHoverMaterial
            : regionMaterial
      }
    }
  }

  /**
   * いま編集中のスケッチ平面のframeをグリッドへ渡す(モード外は null)。
   *
   * **`setActiveSketch` と `showSketches` の両方から呼ぶ** —— モードに入った時点では
   * そのスケッチの描画指示がまだ届いておらず、frameを知らないことがある。届いた時点で
   * 呼び直さないと、基準平面へのスケッチ中もXY床が出たままになる。
   */
  function refreshGridSketchFrame(): void {
    const target = activeSketchId === null ? undefined : sketches.get(activeSketchId)
    const frame = target?.frame ?? null
    const extent =
      frame !== null && target !== undefined && target.plane.kind === 'face'
        ? faceUvExtent(target.plane.bodyId, target.plane.faceIndex, frame)
        : null
    setGridSketchFrame(frame, extent)
  }

  function clearGhost(): void {
    if (!ghost) return
    for (const object of ghost.objects) sketchRoot.remove(object)
    for (const item of ghost.disposables) item.dispose()
    ghost = null
  }

  function showSketchGhost(
    sketchId: string,
    entity: { kind: string; params: Record<string, number> } | null,
  ): void {
    clearGhost()
    const target = sketches.get(sketchId)
    if (!entity || !target) return
    const built = buildEntityLine(target.frame, entity, sketchGhostMaterial)
    if (!built) return
    sketchRoot.add(built.object)
    ghost = { objects: [built.object], disposables: [built.geometry] }
  }

  /**
   * スケッチに入る直前の視点(抜けるときに戻す)。**モード外では null**。
   *
   * `lookAtSketch` は描いている図形の大きさに寄るので、半径4mmの円を描いて抜けると
   * **40mmの板が画面の外に残る**。視点を全体へ戻すボタンは `7363c76`(下段バーの削除)で
   * 落としてあり、人に残る手は手動のオービット/ズームしかない —— **抜けたら入る前へ戻す**。
   * 「全体が入る視点」ではなく「入る前の視点」なのは、スケッチに入る前に自分で寄せていた
   * 視点まで毎回捨てないため(本人裁定・2026-08-26)。
   *
   * `framedAtEntry` は入った時点で**もう何か描かれていたか**を覚える。**最初の板は
   * スケッチの中で生まれる**ので(`update` の初回 `frameToContent`)、そのときの「入る前」は
   * 何も無い文書の視点であり、戻すと生まれたばかりの形から離れる。その1回だけは戻さない。
   */
  let cameraBeforeSketch:
    | {
        position: THREE.Vector3
        target: THREE.Vector3
        up: THREE.Vector3
        /**
         * 入る前の見かけの大きさ(`viewHalfHeight`)。**位置だけでは戻せない** ——
         * 正投影では寄り引きが視錐台の大きさであってカメラの位置ではないので、
         * 位置を戻しても `lookAtSketch` がスケッチに合わせた倍率のまま残る。
         */
        halfHeight: number
        framedAtEntry: boolean
      }
    | null = null

  function setActiveSketch(sketchId: string | null): void {
    const wasActive = activeSketchId !== null
    if (activeSketchId !== sketchId) pickChanged() // モードの出入りで優先順位ごと変わる
    activeSketchId = sketchId
    refreshGridSketchFrame() // 出すグリッドの平面がモードの出入りで変わる
    if (sketchId !== null) {
      // **入る「とき」だけ控える**。`lookAtSketch` の側で控えないのは、あれが
      // 「正面から見る」でも呼ばれるから —— そこで控えると戻り先が寄った視点に化ける。
      // モード中に別のスケッチへ移ったときも、戻り先は最初に入る前のままにする。
      if (!wasActive) {
        cameraBeforeSketch = {
          position: camera.position.clone(),
          target: controls.target.clone(),
          up: camera.up.clone(),
          halfHeight: viewHalfHeight(),
          framedAtEntry: framed,
        }
      }
      return
    }
    clearGhost()
    // 上方向を既定へ戻す。`lookAtSketch` が平面のv軸へ倒したままだと、モードを抜けた後の
    // オービットが入る前と違う傾きになる。
    camera.up.copy(WORLD_UP)
    const previous = cameraBeforeSketch
    cameraBeforeSketch = null
    if (previous !== null && previous.framedAtEntry) {
      camera.position.copy(previous.position)
      camera.up.copy(previous.up)
      controls.target.copy(previous.target)
      // **見かけの大きさを戻すのは正投影のときだけ。** 正投影では寄り引きがカメラの位置
      // ではないので、位置を戻しても `lookAtSketch` が作った倍率が残る。透視は上の位置の
      // 復元で戻り終わっているので、ここを通しても同じ値を計算し直すだけである。
      if (camera === orthographic) setViewHalfHeight(previous.halfHeight)
      camera.updateProjectionMatrix()
    }
    controls.update()
  }

  /** スケッチ平面と視線の交点を (u,v) で返す(平面がカメラの裏なら null)。 */
  function planePointAt(offsetX: number, offsetY: number): { u: number; v: number } | null {
    if (activeSketchId === null) return null
    const target = sketches.get(activeSketchId)
    if (!target) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    const frame = target.frame
    const origin = new THREE.Vector3(...(frame.origin as [number, number, number]))
    const normal = new THREE.Vector3(...(frame.normal as [number, number, number])).normalize()
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal, origin)
    const hit = raycaster.ray.intersectPlane(plane, new THREE.Vector3())
    if (hit === null) return null
    const delta = hit.sub(origin)
    return {
      u: delta.dot(new THREE.Vector3(...(frame.u as [number, number, number]))),
      v: delta.dot(new THREE.Vector3(...(frame.v as [number, number, number]))),
    }
  }

  /**
   * その画素で当たるスケッチの図形(**アクティブなスケッチだけ**)。
   *
   * **点が先・辺が後**。点は必ず辺の上に乗っているので、辺を先に見ると端点を掴めない
   * (頂点マーカーを辺より先に見るのと同じ理由)。当たり判定の太さは画素で決める
   * (`edgeAt` / `vertexAt` と同じ —— 寄るほど掴みにくくなるのを避ける)。
   */
  function sketchEntityAt(offsetX: number, offsetY: number): SketchEntityHit | null {
    if (activeSketchId === null) return null
    const target = sketches.get(activeSketchId)
    if (!target) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)

    if (target.handleMeshes.length > 0) {
      const previous = raycaster.params.Points?.threshold
      raycaster.params.Points = { threshold: worldPerPixel() * SKETCH_HANDLE_PIXELS }
      const hits = raycaster.intersectObjects(target.handleMeshes, false)
      if (previous !== undefined) raycaster.params.Points = { threshold: previous }
      if (hits.length > 0) {
        const { entityId, point } = hits[0].object.userData
        if (typeof entityId === 'string' && typeof point === 'string') {
          return { sketchId: activeSketchId, entityId, point }
        }
      }
    }

    if (target.handleLines.length > 0) {
      const previous = raycaster.params.Line?.threshold
      raycaster.params.Line = { threshold: worldPerPixel() * EDGE_PICK_PIXELS }
      const hits = raycaster.intersectObjects(target.handleLines, false)
      if (previous !== undefined) raycaster.params.Line = { threshold: previous }
      if (hits.length > 0) {
        const { entityId } = hits[0].object.userData
        if (typeof entityId === 'string') {
          return { sketchId: activeSketchId, entityId, point: null }
        }
      }
    }
    return null
  }

  function pickRegions(on: boolean): void {
    if (regionPickWanted === on) return
    regionPickWanted = on
    pickChanged() // ゲートの開閉で「指せるもの」が変わる(止まったままでも解決し直す)
  }

  /**
   * 今クリックで当たってよい領域メッシュ。
   *
   * - **スケッチモード中**: そのスケッチの領域だけ(描いている最中に別のスケッチの
   *   領域を掴まない)。ゲートは見ない —— モードに入っている時点で領域が対象である。
   * - **通常モード**: `pickRegions(true)` の間だけ、**全スケッチの領域**。
   *   出していない間は空配列なので、面のピックはそのまま動く(辺の `edgeAt` と同じ形)。
   */
  function regionPickTargets(): THREE.Mesh[] {
    if (activeSketchId !== null) return sketches.get(activeSketchId)?.regionMeshes ?? []
    if (!regionPickWanted) return []
    const meshes: THREE.Mesh[] = []
    for (const target of sketches.values()) meshes.push(...target.regionMeshes)
    return meshes
  }

  function regionAt(offsetX: number, offsetY: number): { sketchId: string; index: number } | null {
    const targets = regionPickTargets()
    if (targets.length === 0) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    // **近い方が勝つ**(`intersectObjects` は距離順)。複数スケッチの領域が重なっても
    // 手前のものが取れる。
    const hits = raycaster.intersectObjects(targets, false)
    if (hits.length === 0) return null
    const { sketchId, regionIndex } = hits[0].object.userData
    if (typeof sketchId !== 'string' || typeof regionIndex !== 'number') return null
    return { sketchId, index: regionIndex }
  }

  function lookAtSketch(sketchId: string): void {
    const target = sketches.get(sketchId)
    if (!target) return
    const frame = target.frame
    const normal = new THREE.Vector3(...(frame.normal as [number, number, number])).normalize()
    // 中心は描いてあるものの真ん中(何も無ければ平面の原点)
    let center = toWorld(frame, 0, 0)
    let radius = 0
    if (target.points.length > 0) {
      const us = target.points.map(([u]) => u)
      const vs = target.points.map(([, v]) => v)
      const midU = (Math.min(...us) + Math.max(...us)) / 2
      const midV = (Math.min(...vs) + Math.max(...vs)) / 2
      center = toWorld(frame, midU, midV)
      radius = Math.max(Math.max(...us) - Math.min(...us), Math.max(...vs) - Math.min(...vs)) / 2
    }
    const previous = camera.position.distanceTo(controls.target)
    const distance = radius > 0 ? Math.max(fitDistance(radius * SKETCH_FIT_MARGIN), 1) : Math.max(previous, 1)
    // 上方向を平面のv軸に合わせる(そうしないと真上から見たときに回転が不定になる)
    camera.up.set(...(frame.v as [number, number, number]))
    camera.position.copy(center).add(normal.multiplyScalar(distance))
    controls.target.copy(center)
    // 正投影は距離では寄れない(`frameToContent` と同じ理由)。描いてあるものが無いときは
    // 今の見かけの大きさのまま —— 透視側が `previous` の距離を保つのと同じ扱いにする。
    if (camera === orthographic && radius > 0) setViewHalfHeight(fitHalfHeight(radius * SKETCH_FIT_MARGIN))
    camera.updateProjectionMatrix()
    controls.update()
  }

  /**
   * その画素が**本当に押せるか**(canvasの上に浮いているDOMに覆われていないか)。
   *
   * 3Dのレイ検査(`pickAt` / `faceHitAt` / `basePlaneAt`)は**canvasの中しか見ない**ので、
   * ビューポートに浮かせたパネル —— `BrowserPanel.svelte` のブラウザ —— に覆われた画素も
   * 「取れる」と答えてしまう。実際にそこを押すとクリックはパネルに吸われ、**指したはずの
   * ものが指されない**。`project*` は「その画素が本当にそれを取れる」ことを確かめてから
   * 返す約束なので、覆われた画素は候補から落とす(呼び出し側の探索が次の候補へ進む)。
   *
   * `Toast.svelte` の札が引っかからないのは、あちらが `pointer-events: none` で
   * クリックを吸わないから —— 覆っているかどうかではなく、**吸うかどうか**が判定基準であり、
   * それを直接答えるのが `elementFromPoint` である。
   */
  function clickable(x: number, y: number): boolean {
    // **キューブは同一canvasの第2パスなので `elementFromPoint` だけでは判定できない**
    // ——常にcanvasを返し続けるので、ここで明示的に矩形を除外する(タスク仕様の落とし穴1)。
    if (insideCubeRect(x, y)) return false
    const rect = container.getBoundingClientRect()
    return document.elementFromPoint(rect.left + x, rect.top + y) === renderer.domElement
  }

  function toScreen(point: THREE.Vector3): { x: number; y: number } | null {
    const { width, height } = size()
    const projected = point.clone().project(camera)
    if (projected.z < -1 || projected.z > 1) return null
    const x = ((projected.x + 1) / 2) * width
    const y = ((1 - projected.y) / 2) * height
    if (x < 0 || y < 0 || x > width || y > height) return null
    if (!clickable(x, y)) return null
    return { x, y }
  }

  function projectSketchPoint(sketchId: string, u: number, v: number): { x: number; y: number } | null {
    const target = sketches.get(sketchId)
    if (!target) return null
    return toScreen(toWorld(target.frame, u, v))
  }

  function projectRegion(sketchId: string, index: number): { x: number; y: number } | null {
    const target = sketches.get(sketchId)
    const mesh = target?.regionMeshes.find((candidate) => candidate.userData.regionIndex === index)
    if (!target || !mesh) return null
    const position = mesh.geometry.getAttribute('position')
    const triangles = mesh.geometry.getIndex()
    if (!position || !triangles) return null
    // 面の `project` と同じ流儀: 三角形の内側の点を狙い、**実際にその領域へ当たる点**を返す。
    //
    // **1つの三角形につき4点試す**(重心 + 各頂点へ寄せた3点)。重心1点だけだと、
    // スケッチモードで**点・辺のほうが手前で拾われる**画素に当たったときに候補が尽きる ——
    // ベジエの制御点は曲線から離れた所(＝領域の内側)にも立つので、細長い三角形の重心が
    // その近くに来ることが実際にある(2026-08-30、`smoke-svg` の丘の領域で踏んだ)。
    const weights: ReadonlyArray<readonly [number, number, number]> = [
      [1 / 3, 1 / 3, 1 / 3],
      [0.5, 0.25, 0.25],
      [0.25, 0.5, 0.25],
      [0.25, 0.25, 0.5],
    ]
    const point = new THREE.Vector3()
    const corner = new THREE.Vector3()
    for (let sample = 0; sample < triangles.count / 3 * weights.length; sample += 1) {
      const triangle = Math.floor(sample / weights.length)
      const weight = weights[sample % weights.length]
      point.set(0, 0, 0)
      for (let vertex = 0; vertex < 3; vertex += 1) {
        const at = triangles.getX(triangle * 3 + vertex)
        corner.set(position.getX(at), position.getY(at), position.getZ(at))
        point.addScaledVector(corner, weight[vertex])
      }
      const screen = toScreen(point.clone())
      if (screen === null) continue
      // **スケッチも一致していること**を見る —— 通常モードでは全スケッチの領域が対象に
      // なるので、添字だけ見ると別のスケッチの同じ添字を「当たった」と読む。
      const hit = regionAt(screen.x, screen.y)
      if (!hit || hit.sketchId !== sketchId || hit.index !== index) continue
      // **スケッチの図形が勝つ画素は返さない**(2026-08-30)。スケッチモード中は点・辺が
      // 領域より手前で拾われるので、`regionAt` だけで確かめると「領域に当たるが、実際に
      // クリックすると線が選ばれる」画素を返しうる —— `project*` の約束(その画素で
      // 本当にそれが取れる)が破れる。判定は実際のクリックと同じ `sketchEntityAt` で行う。
      if (sketchEntityAt(screen.x, screen.y) !== null) continue
      return screen
    }
    return null
  }

  // ------------------------------------------------------------------ ウィンドウ選択(左ドラッグ)
  //
  // **矩形に完全包含されたものだけを選ぶ**(交差判定はしない。本人明言。向きも見ない)。
  // 判定はすべて**画面座標**で行う —— 「囲った」は人が画面で見たとおりの意味であって、
  // 世界座標の箱ではない(奥行きは関係ない)。
  //
  // 何を候補にするかは**クリックのゲートと同じ**にしてある(辺は辺を出している間だけ、
  // 領域は要求している間だけ、スケッチの図形はスケッチモード中だけ)。ここだけ別の規則に
  // すると、「クリックでは指せないものがドラッグでだけ選べる」ができてしまう。
  const bandElement = document.createElement('div')
  bandElement.style.position = 'absolute'
  bandElement.style.border = '1px solid #ffb454'
  bandElement.style.background = 'rgba(255, 180, 84, 0.12)'
  // **クリックを吸わない**。吸うと `clickable()`(elementFromPoint)が canvas を見失い、
  // 射影の穴が「その画素は取れない」と答え始める。
  bandElement.style.pointerEvents = 'none'
  bandElement.style.display = 'none'
  // **大きさには影響しない**(position:absolute なのでフローの外)。親を基準にするために
  // だけ `relative` を入れる —— `.viewport` の寸法指定そのものは動かさない(styles.css の不変条件)。
  if (getComputedStyle(container).position === 'static') container.style.position = 'relative'
  container.appendChild(bandElement)

  /** 矩形を引いている最中か(引いていなければ、離したときの扱いはクリック)。 */
  let boxActive = false

  function boxRect(x: number, y: number): { left: number; top: number; right: number; bottom: number } {
    return {
      left: Math.min(downX, x),
      right: Math.max(downX, x),
      top: Math.min(downY, y),
      bottom: Math.max(downY, y),
    }
  }

  function showBand(x: number, y: number): void {
    const rect = boxRect(x, y)
    bandElement.style.display = 'block'
    bandElement.style.left = `${rect.left}px`
    bandElement.style.top = `${rect.top}px`
    bandElement.style.width = `${rect.right - rect.left}px`
    bandElement.style.height = `${rect.bottom - rect.top}px`
  }

  function hideBand(): void {
    boxActive = false
    bandElement.style.display = 'none'
  }

  const projectScratch = new THREE.Vector3()

  /** 世界座標を**コンテナ内の画素**へ(視錐台の外・カメラの裏は null)。 */
  function projectRaw(x: number, y: number, z: number): { x: number; y: number } | null {
    projectScratch.set(x, y, z).project(camera)
    if (projectScratch.z < -1 || projectScratch.z > 1) return null
    const { width, height } = size()
    return {
      x: ((projectScratch.x + 1) / 2) * width,
      y: ((1 - projectScratch.y) / 2) * height,
    }
  }

  function inside(rect: ReturnType<typeof boxRect>, point: { x: number; y: number }): boolean {
    return point.x >= rect.left && point.x <= rect.right && point.y >= rect.top && point.y <= rect.bottom
  }

  /**
   * そのジオメトリの**全頂点**が矩形の中にあるか。1つでも外れたらその時点で false。
   *
   * **早期に打ち切るのが要点**である。27部品(2835面)の全三角形を毎回最後まで見ると
   * ドラッグを離すたびに数十万点を射影することになるが、実際には矩形の外にある面が
   * ほとんどで、1点目で落ちる。
   */
  function geometryInside(geometry: THREE.BufferGeometry, matrix: THREE.Matrix4, rect: ReturnType<typeof boxRect>): boolean {
    const position = geometry.getAttribute('position')
    if (!position || position.count === 0) return false
    const world = new THREE.Vector3()
    for (let at = 0; at < position.count; at += 1) {
      world.set(position.getX(at), position.getY(at), position.getZ(at)).applyMatrix4(matrix)
      const screen = projectRaw(world.x, world.y, world.z)
      if (screen === null || !inside(rect, screen)) return false
    }
    return true
  }

  function objectInside(object: THREE.Object3D & { geometry: THREE.BufferGeometry }, rect: ReturnType<typeof boxRect>): boolean {
    object.updateWorldMatrix(true, false)
    return geometryInside(object.geometry, object.matrixWorld, rect)
  }

  function collectInBox(rect: ReturnType<typeof boxRect>): Omit<BoxSelectEvent, 'mode'> {
    const faces: FaceRef[] = []
    const edges: EdgeRef[] = []
    const vertices: VertexRef[] = []
    const regions: Array<{ sketchId: string; index: number }> = []
    const sketchEntities: SketchEntityHit[] = []

    if (activeSketchId === null) {
      // **面は常に指せる**(クリックと同じ)。隠したBodyのメッシュは `visible=false` なので外す。
      for (const [bodyId, entry] of bodies) {
        for (const mesh of entry.faceMeshes) {
          if (!mesh.visible) continue
          if (!objectInside(mesh as THREE.Mesh & { geometry: THREE.BufferGeometry }, rect)) continue
          const faceIndex = mesh.userData.faceIndex
          if (typeof faceIndex === 'number') faces.push({ bodyId, faceIndex })
        }
      }
      // 辺・頂点は「出している間だけ」(クリックの `edgeAt` / `vertexAt` と同じゲート)
      for (const line of edgeLines) {
        if (!objectInside(line as THREE.Line & { geometry: THREE.BufferGeometry }, rect)) continue
        const { bodyId, edgeIndex } = line.userData
        if (typeof bodyId === 'string' && typeof edgeIndex === 'number') edges.push({ bodyId, edgeIndex })
      }
      for (const mesh of vertexMeshes) {
        if (!objectInside(mesh as THREE.Points & { geometry: THREE.BufferGeometry }, rect)) continue
        const { bodyId, vertexIndex } = mesh.userData
        if (typeof bodyId === 'string' && typeof vertexIndex === 'number') vertices.push({ bodyId, vertexIndex })
      }
    } else {
      // **スケッチモードでは図形だけ**。図形全体が入っていればその図形を、
      // 入っていなければ矩形に入った点だけを返す(点と図形全体の二重取りを作らない)。
      const target = sketches.get(activeSketchId)
      const whole = new Set<string>()
      for (const line of target?.handleLines ?? []) {
        if (!objectInside(line as THREE.Line & { geometry: THREE.BufferGeometry }, rect)) continue
        const entityId = line.userData.entityId
        if (typeof entityId !== 'string') continue
        whole.add(entityId)
        sketchEntities.push({ sketchId: activeSketchId, entityId, point: null })
      }
      for (const mesh of target?.handleMeshes ?? []) {
        const entityId = mesh.userData.entityId
        const point = mesh.userData.point
        if (typeof entityId !== 'string' || typeof point !== 'string') continue
        if (whole.has(entityId)) continue
        if (!objectInside(mesh as THREE.Points & { geometry: THREE.BufferGeometry }, rect)) continue
        sketchEntities.push({ sketchId: activeSketchId, entityId, point })
      }
    }

    // 領域は**クリックと同じゲート**(`regionPickTargets`)。スケッチモード中はそのスケッチだけ。
    for (const mesh of regionPickTargets()) {
      if (!objectInside(mesh as THREE.Mesh & { geometry: THREE.BufferGeometry }, rect)) continue
      const { sketchId, regionIndex } = mesh.userData
      if (typeof sketchId === 'string' && typeof regionIndex === 'number') {
        regions.push({ sketchId, index: regionIndex })
      }
    }
    return { faces, edges, vertices, regions, sketchEntities }
  }

  // クリック(押した位置からほとんど動いていない)だけを選択として扱う。
  // 動いた場合は OrbitControls の視点回転であって選択ではない。
  let downAt = 0
  let downX = 0
  let downY = 0
  let downValid = false

  function onPointerDown(event: PointerEvent): void {
    // 新しいポインタ操作が来たら、進行中のキューブ視点アニメーションは中断してよい(タスク仕様)。
    cubeSnap = null
    if (event.button !== 0) return
    // **キューブの内側で始まった左ボタン押下は、キューブが引き取る**(モデルのピック・
    // 矩形選択の開始へ漏らさない。タスク仕様の落とし穴1)。まだクリックかドラッグかは
    // 未確定 —— `CUBE_DRAG_THRESHOLD_PX` を超えて動いたら `handleCubePointerMove` がドラッグへ。
    if (insideCubeRect(event.offsetX, event.offsetY)) {
      cubeDownX = event.offsetX
      cubeDownY = event.offsetY
      cubePointerActive = true
      cubeDragging = false
      cubePointerId = event.pointerId
      try {
        renderer.domElement.setPointerCapture(event.pointerId)
      } catch {
        // 対応していない環境でもフォールバック(通常ドラッグ)は成立する
      }
      return
    }
    downAt = performance.now()
    downX = event.offsetX
    downY = event.offsetY
    downValid = true
    hideBand()
  }

  function onPointerUp(event: PointerEvent): void {
    // **ボタンの種類より先に解除する**。右/中ボタンで視点を回して離した場合、ここで
    // 解除しないと次の `pointermove` が来るまでホバーが止まったままになる。
    orbiting = false
    if (event.button === 0 && (cubePointerActive || cubeDragging)) {
      const wasClick = cubePointerActive && !cubeDragging
      const x = event.offsetX
      const y = event.offsetY
      if (cubeDragging) endCubeDrag()
      else {
        cubePointerActive = false
        releaseCubePointerCapture()
      }
      if (wasClick) handleCubeClick(x, y)
      return
    }
    if (event.button !== 0 || !downValid) {
      if (event.button === 0) hideBand()
      return
    }
    downValid = false
    const moved = Math.hypot(event.offsetX - downX, event.offsetY - downY)
    const elapsed = performance.now() - downAt
    // **矩形を引いていたならウィンドウ選択**(クリックにはしない)。引いた向きは見ない。
    if (boxActive) {
      const rect = boxRect(event.offsetX, event.offsetY)
      hideBand()
      boxSelectListener?.({ ...collectInBox(rect), mode: modeOf(event) })
      return
    }
    if (moved > CLICK_MOVE_LIMIT_PX || elapsed > CLICK_TIME_LIMIT_MS) return
    pickListener?.({ ...resolveHit(event.offsetX, event.offsetY), mode: modeOf(event) })
  }

  /**
   * 修飾キーから選択の扱いを決める。**クリックもウィンドウ選択も同じ規則**
   * (2箇所に書くと片方だけ直したときに操作が食い違う)。
   *
   * **Shift も Ctrl(Mac は Meta)も同じトグル**(2026-08-30本人決定)。除外専用のキーは
   * 持たない —— 間違えたらもう一度クリック(またはドラッグ)すればよい。
   */
  function modeOf(event: { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean }): PickMode {
    return event.shiftKey || event.ctrlKey || event.metaKey ? 'toggle' : 'replace'
  }

  /**
   * その画素をクリックしたら**何が取れるか**(修飾キー以外の `PickEvent` そのもの)。
   *
   * **クリックとホバー強調はこの1つの関数だけを見る**。優先順位をクリック側と
   * ホバー側の2箇所に書くと、片方だけ直したときにプレビューが黙って嘘をつく ——
   * それはこの機能の存在意義そのものを消す。**副作用はここに置かない**
   * (`pickListener` を呼ぶのは `onPointerUp` だけ)。
   */
  function resolveHit(offsetX: number, offsetY: number): Omit<PickEvent, 'mode'> {
    const nothing: Omit<PickEvent, 'mode'> = {
      hit: null,
      edge: null,
      vertex: null,
      point: null,
      region: null,
      plane: null,
      sketchEntity: null,
    }
    // **スケッチモード中は面を拾わない**。描いている最中に奥のソリッドの面を掴むと、
    // 線を引くつもりのクリックが選択の置き換えになる。領域が手前・平面の点がその次。
    if (activeSketchId !== null) {
      const region = regionAt(offsetX, offsetY)
      const plane = planePointAt(offsetX, offsetY)
      // **図形は領域より手前**(点・辺は領域の縁に乗っているので、領域が勝つと
      // 端点を掴めない)。ただし `point`(平面上の座標)は常に一緒に返す ——
      // 描くコマンドが起動している間はそちらが優先で、判断は `App.svelte` が持つ。
      return {
        ...nothing,
        region,
        sketchEntity: sketchEntityAt(offsetX, offsetY),
        point: plane === null ? null : { sketchId: activeSketchId, ...plane },
      }
    }
    // **辺を出しているときは辺が先**。辺は面の上に重なっているので、面より後に見ると
    // 必ず面が勝つ。出していないときは `edgeAt` が即 null を返すので、面のピックは
    // そのまま動く。**この順序は変えない**(基準平面を足しても辺→面の優先順位は同じ)。
    const edge = edgeAt(offsetX, offsetY)
    if (edge !== null) return { ...nothing, edge }
    // **領域を要求しているときは領域が先**(辺と同じ「要求中のものが勝つ」)。領域は
    // スケッチ平面の上に浮いており、押し出した板の底面と重なるので、面より後に見ると
    // 視点によって面が勝ってしまう。`pickRegions(false)` の間は `regionAt` が即 null を
    // 返すので、面のピックはそのまま動く。
    const region = regionAt(offsetX, offsetY)
    if (region !== null) return { ...nothing, region }
    // **頂点マーカーは面より先**(最小の的なので、当たったならそれが狙いである)。
    // 辺・領域より後なのは、頂点が必ず辺の端点に乗っているから —— 先に見ると
    // フィレットを起動している間に辺の端が掴めなくなる。出していない間は
    // `vertexAt` が即 null を返すので、面のピックはそのまま動く。
    const vertex = vertexAt(offsetX, offsetY)
    if (vertex !== null) return { ...nothing, vertex }
    // **面と基準平面は、レイ距離が近い方**(通常のオクルージョン。辺のような優先順位は
    // 付けない —— 平面は部品と同じ空間に居るので、手前に見えているものが取れるのが自然)。
    // 基準平面は `showBasePlanes(true)` の間しか当たらない。
    //
    // **ただし同着は面が勝つ。** 基準平面に描いて押し出すと、その面は平面とぴったり重なり、
    // 2つのレイ距離が同着になる —— 素の `<` では**どちらが手前かが浮動小数の最後の桁で
    // 決まり**、見た目が同じなのに指すたび結果が変わる(2026-08-28、無関係な式の書き換えで
    // カメラが 7e-14 動いただけで面→XZ平面へ反転した)。そこで**画面で1画素にも満たない差を
    // 「手前」と見なさない** —— 見分けが付かないなら、実体のある面のほうが狙いである。
    // しきい値はシーンの寸法に依らないよう `worldPerPixel()`(注視点で1画素ぶんの世界長さ)
    // から作る。この不変は `smoke-files.mjs` の(d)が画角を振って固定している。
    const face = faceHitAt(offsetX, offsetY)
    const basePlane = basePlaneAt(offsetX, offsetY)
    if (
      basePlane !== null &&
      (face === null || basePlane.distance < face.distance - worldPerPixel())
    ) {
      return { ...nothing, plane: basePlane.name }
    }
    return { ...nothing, hit: face?.ref ?? null }
  }

  function onPointerMove(event: PointerEvent): void {
    // **キューブが引き取ったポインタはメインの経路を一切通らない**(タスク仕様の落とし穴1)。
    if (cubePointerActive || cubeDragging) {
      handleCubePointerMove(event)
      return
    }
    // ポインタがキューブの矩形の内側にあるあいだは、メインのホバー予告・矩形選択開始の
    // どちらも早期に打ち切る(まだ何も押していない=ホバーのみの場合)。
    const overCube = insideCubeRect(event.offsetX, event.offsetY)
    cubeHoverAt = overCube ? { x: event.offsetX, y: event.offsetY } : null
    if (overCube) {
      hoverAt = null
      return
    }
    // **座標を控えるだけ**。当たり判定は `tick` が1フレームに1回だけ回す
    // (pointermove は1フレームに何度も飛ぶので、ここで解決すると同じ絵のために
    // 何度もレイキャストすることになる)。
    hoverAt = { x: event.offsetX, y: event.offsetY }
    // 視点回転中は取れるものの予告を出さない(**これはクリックにならない**)。境目は
    // `onPointerUp` の moved 判定と揃える。**`downValid` ではなく `buttons` で見る** ——
    // canvasの外でボタンを離されると `pointerup` が来ず、`downValid` は立ったままになる。
    const dragged = Math.hypot(event.offsetX - downX, event.offsetY - downY)
    orbiting = event.buttons !== 0 && dragged > CLICK_MOVE_LIMIT_PX
    // **左ボタンを押したまま一定距離を越えたらウィンドウ選択**。`downValid` ではなく
    // `buttons` も見るのは、canvasの外で離されたときに帯が出たままになるのを防ぐため。
    if (downValid && (event.buttons & 1) !== 0 && dragged > BOX_SELECT_MIN_PX) {
      boxActive = true
      showBand(event.offsetX, event.offsetY)
    }
    if (planeMoveListener === null) return
    planeMoveListener(activeSketchId === null ? null : planePointAt(event.offsetX, event.offsetY))
  }

  /** ポインタがcanvasから出た。**強調を残さない**(戻ってくるまで指せるものは無い)。 */
  function onPointerLeave(): void {
    hoverAt = null
    orbiting = false
    // **引きかけの矩形は捨てる**(帯だけが残って画面に貼り付くのを防ぐ)。
    hideBand()
    downValid = false
    // キューブの操作中(クリック判定待ち・ドラッグ中)は無視する —— `setPointerCapture` で
    // 押下中は座標がどこへ行ってもこの要素が受け取り続けるので、`pointerleave` が来ても
    // 操作を打ち切らない(ロック中はそもそも座標が凍結して動かない)。
    if (!cubePointerActive && !cubeDragging) cubeHoverAt = null
  }

  renderer.domElement.addEventListener('pointerdown', onPointerDown)
  renderer.domElement.addEventListener('pointerup', onPointerUp)
  renderer.domElement.addEventListener('pointermove', onPointerMove)
  renderer.domElement.addEventListener('pointerleave', onPointerLeave)
  // ドラッグがOSに横取りされた場合(`pointerup` が来ない)。`pointerleave` と同じ後始末でよい。
  renderer.domElement.addEventListener('pointercancel', onPointerLeave)

  const observer = new ResizeObserver(() => applySize())
  observer.observe(container)
  applySize()

  /**
   * ホバー強調を1フレームぶん更新する。
   *
   * **毎フレーム解決し直す** —— 視点を回すとポインタが動かなくても当たるものが変わるので、
   * 「動いたときだけ」では足りない。材質の書き換えは**変わったときだけ**行う
   * (全メッシュ走査を毎フレームやらない)。
   */
  function updateHover(): void {
    const point = orbiting ? null : hoverAt
    const probe =
      point === null
        ? ''
        : `${pickGeneration},${point.x},${point.y},` +
          `${camera.position.toArray().join(',')},${camera.quaternion.toArray().join(',')},` +
          // **`zoom` も入れる** —— 正投影のドリーは `zoom` だけを動かしてカメラを動かさない
          // ので、位置と姿勢だけのキーでは「ホイールで寄ったのに当たるものが変わらない」になる。
          `${camera.zoom}`
    if (probe === hoverProbe) return
    hoverProbe = probe
    const next = point === null ? null : resolveHit(point.x, point.y)
    // **頂点は出さない**(2026-08-27本人決定)。頂点が取れる画素では面も辺も光らない ——
    // そこをクリックしても面は選ばれないのだから、光らせないほうが正しい。
    //
    // **スケッチの点・辺も同じ扱い**(2026-08-30)。予告の色は付けないが、
    // **そこが取れるときは領域を光らせない** —— 光らせると「クリックすれば取れるもの」の
    // 予告が嘘になり、この機能の存在意義そのものが消える(点・辺は領域より手前で拾われる)。
    const face = next?.hit ?? null
    const edge = next?.edge ?? null
    const region = next?.sketchEntity ? null : (next?.region ?? null)
    const plane = next?.plane ?? null

    const nextFaceKey = face === null ? '' : refKey(face.bodyId, face.faceIndex)
    if (nextFaceKey !== hoverFaceKey) {
      hoverFaceKey = nextFaceKey
      hoverFace = face
      applyFaceMaterials()
    }
    const nextEdgeKey = edge === null ? '' : refKey(edge.bodyId, edge.edgeIndex)
    if (nextEdgeKey !== hoverEdgeKey) {
      hoverEdgeKey = nextEdgeKey
      hoverEdge = edge
      applyEdgeMaterials()
    }
    const nextRegionKey = region === null ? '' : refKey(region.sketchId, region.index)
    if (nextRegionKey !== hoverRegionKey) {
      hoverRegionKey = nextRegionKey
      hoverRegion = region
      applyRegionMaterials()
    }
    if (plane !== hoverPlane) {
      hoverPlane = plane
      applyBasePlaneHover()
    }
  }

  /**
   * ビューキューブを同一canvasへ**第2パス**として描く。メインの描画のあと
   * `autoClear=false`→`clearDepth()`→`setViewport`/`setScissor`/`setScissorTest(true)` で
   * 描き、**終わったら必ずフルサイズのviewportと`setScissorTest(false)`に戻す**
   * (戻し忘れると次フレームの本編が隅に描かれる。タスク仕様)。
   *
   * `setViewport`/`setScissor` はCSSピクセルで渡す(three.js側が`pixelRatio`を掛ける)—— ただし
   * **y軸はWebGLネイティブの下原点**なので、`viewCubeRect()`(左上原点、CSSの慣習)から
   * `height - rect.y - rect.h` で変換する。
   */
  function renderCubePass(): void {
    updateCubeCamera()
    const rect = viewCubeRect()
    const { width, height } = size()
    const glY = height - rect.y - rect.h
    renderer.autoClear = false
    renderer.clearDepth()
    renderer.setViewport(rect.x, glY, rect.w, rect.h)
    renderer.setScissor(rect.x, glY, rect.w, rect.h)
    renderer.setScissorTest(true)
    renderer.render(cubeScene, cubeCamera)
    renderer.setScissorTest(false)
    renderer.setViewport(0, 0, width, height)
    renderer.autoClear = true
  }

  function tick(): void {
    if (!alive) return
    stepCubeSnap(performance.now())
    controls.update()
    // **描画ループで見る**(`OrbitControls` の 'change' ではない) —— 減衰つきのドリーは
    // イベントが止まった後も数十フレーム動き続けるので、「止まったか」はフレーム側でしか
    // 分からない。中身は算術数本で、当たったときだけ作り直しが走る。
    updateResolution(performance.now())
    updateHover()
    updateCubeHover()
    // グリッドは毎フレームここだけを更新する(段の不透明度とカメラ行列。ジオメトリは無い)。
    updateGridUniforms()
    renderer.render(scene, camera)
    renderCubePass()
    frameHandle = requestAnimationFrame(tick)
  }
  frameHandle = requestAnimationFrame(tick)

  return {
    update,
    getGeometry(bodyId) {
      return rawGeometry.get(bodyId) ?? null
    },
    prune,
    onPick(listener) {
      pickListener = listener
    },
    onBoxSelect(listener) {
      boxSelectListener = listener
    },
    onPlaneMove(listener) {
      planeMoveListener = listener
    },
    viewResolution() {
      return { ...appliedResolution }
    },
    setFaceCulling(enabled) {
      if (faceCullingEnabled === enabled) return
      faceCullingEnabled = enabled
      // **次のフレームで必ず採り直す**。カメラが止まったままでも判定が変わるので、
      // 直前に計算した印を捨てないと「姿勢も世代も同じ」で早期returnしてしまう。
      lastFocusComputeEpoch = -1
      lastFocusComputeGeneration = -1
    },
    sketchPolylineCounts() {
      const counts: Record<string, number[]> = {}
      for (const [sketchId, entry] of sketches) counts[sketchId] = [...entry.polylineCounts]
      return counts
    },
    onResolutionChange(listener) {
      resolutionListener = listener
    },
    highlight,
    highlightBodies,
    setOptimizeState,
    showEdges,
    showBasePlanes,
    pickRegions,
    highlightEdges,
    projectEdge,
    showVertices,
    highlightColors() {
      return { ...highlightColor }
    },
    faceColor(ref) {
      const mesh = allFaceMeshes.find(
        (candidate) => candidate.userData.bodyId === ref.bodyId && candidate.userData.faceIndex === ref.faceIndex,
      )
      const material = mesh?.material as THREE.MeshLambertMaterial | undefined
      return material?.color ? material.color.getHex() : null
    },
    optimizeAnnotationCount() {
      return optimizeRoot.children.length
    },
    optimizeAnnotations() {
      return {
        fixtures: optimizeAnnotationInfo.fixtures.map((ref) => ({ ...ref })),
        loads: optimizeAnnotationInfo.loads.map((item) => ({
          ...item,
          direction: [...item.direction] as [number, number, number],
        })),
      }
    },
    hoveredRef() {
      return {
        face: hoverFace,
        edge: hoverEdge,
        region: hoverRegion,
        plane: hoverPlane,
        color: hoverColorNow(),
      }
    },
    projectVertex,
    showPreview,
    clearPreview,
    project,
    showSketches,
    showSketchGhost,
    setActiveSketch,
    lookAtSketch,
    frameAll() {
      camera.up.copy(WORLD_UP) // スケッチの v 軸へ倒したままだと、戻したのに傾いて見える
      frameToContent()
    },
    resetGrid() {
      applyGridScale(contentBoxForGrid(), true)
    },
    gridScale() {
      return { extent: gridExtent }
    },
    gridTiers() {
      return {
        mmPerPixel: gridMmPerPixel,
        tiers: TIERS.map((step, index) => ({ step, alpha: gridTierAlphaValues[index] })),
        planes: [...gridPlanesShown],
        axis: gridAxisShown,
        sketchFaceExtent: sketchGridFrame !== null && basePlaneForFrame(sketchGridFrame) === null
          ? sketchGridExtent
          : null,
      }
    },
    setFov,
    cameraPose,
    projectPoint,
    projectSketchPoint,
    projectBasePlane,
    projectRegion,
    viewCubeRect,
    dispose() {
      alive = false
      cancelAnimationFrame(frameHandle)
      observer.disconnect()
      renderer.domElement.removeEventListener('pointerdown', onPointerDown)
      renderer.domElement.removeEventListener('pointerup', onPointerUp)
      renderer.domElement.removeEventListener('pointermove', onPointerMove)
      renderer.domElement.removeEventListener('pointerleave', onPointerLeave)
      renderer.domElement.removeEventListener('pointercancel', onPointerLeave)
      document.removeEventListener('pointerlockchange', handleCubePointerLockChange)
      document.removeEventListener('pointerlockerror', handleCubePointerLockError)
      if (document.pointerLockElement === renderer.domElement) document.exitPointerLock()
      releaseCubePointerCapture()
      // `THREE.Light` に `dispose()` は無い(GPUリソースを直接持たない) —— 幾何・材質・
      // テクスチャだけ `cubeDisposables` から片付ける。
      for (const item of cubeDisposables) item.dispose()
      cubeGroup.clear()
      cubeLabelGroup.clear()
      clearPreview()
      clearGhost()
      clearEdges()
      clearVertices()
      vertexMaterial.dispose()
      vertexProposedMaterial.dispose()
      pickEdgeMaterial.dispose()
      pickEdgeSelectedMaterial.dispose()
      pickEdgeProposedMaterial.dispose()
      pickEdgeHoverMaterial.dispose()
      pickEdgeSelectedHoverMaterial.dispose()
      pickEdgeProposedHoverMaterial.dispose()
      for (const sketchId of [...sketches.keys()]) clearSketch(sketchId)
      bandElement.remove()
      sketchMaterial.dispose()
      sketchGhostMaterial.dispose()
      sketchSelectedMaterial.dispose()
      sketchHandleMaterial.dispose()
      sketchHandleSelectedMaterial.dispose()
      regionMaterial.dispose()
      regionSelectedMaterial.dispose()
      regionHoverMaterial.dispose()
      regionSelectedHoverMaterial.dispose()
      for (const bodyId of [...bodies.keys()]) clearBody(bodyId)
      for (const item of basePlaneHoverDisposables) item.dispose()
      for (const grid of planeGrids) grid.dispose()
      gridAxisLine?.geometry.dispose()
      gridAxisMaterial.dispose()
      gridRoot.clear()
      controls.dispose()
      for (const material of faceMaterialCache.values()) material.dispose()
      faceMaterialCache.clear()
      previewMaterial.dispose()
      edgeMaterial.dispose()
      clearOptimizeAnnotations()
      renderer.dispose()
      renderer.domElement.remove()
    },
  }
}
