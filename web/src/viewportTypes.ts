/** `App.svelte` へ見せるデータ型と `Viewport` インタフェース(three.js の型は出さない)。 */
import * as THREE from 'three'

import { type ViewResolution } from './adaptiveResolution'
import { type UvBounds } from './gridScale'
import type { BasePlaneName, EdgeGeometry, OptimizeDebugGrid } from './session'

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

/**
 * 荷重のかかる面1枚と、**その面の**大きさ[N]・向き。契約は `duocad/optimize.py` の
 * `state_payload`(`loads` と `load_specs` を `(body, index)` で突き合わせたもの)。
 *
 * **大きさ・向きは面ごとに違ってよい** —— 面Aへ50Nの鉛直下向き、面Bへ20Nの法線方向、が
 * 同じ配列に並ぶ。1本の値を全ての面へ使い回してはならない。
 */
export interface OptimizeLoadVisual extends FaceRef {
  magnitude: number
  direction: 'normal' | '-normal' | 'vector'
  vector: [number, number, number] | null
}

/**
 * **開発用のデバッグ表示**(`web/src/debugFlags.ts` のURLパラメータでだけ出る)。
 *
 * 契約の正は `duocad/optimize_run.py` の `_DebugSink`。**製品の既定の画面には出ない** ——
 * `setOptimizeDebug(null)`(既定)のあいだ、ここのオブジェクトは1つも生まれない。
 *
 * 座標は**ワールドmm**。要素 (i,j,k) の中心は `origin + spacing*(i+0.5, j+0.5, k+0.5)`、
 * その角の節点 (i,j,k) は `origin + spacing*(i,j,k)`(セル中心の規約。
 * `duocad/topopt/mesh.py:169`)。**サーバ側で足し引きしていない**ので、ここで足す。
 */
export type { OptimizeDebugGrid }

/** 立方体で描く要素の集合。`elements` はC順の通し添字 `i*(ny*nz) + j*nz + k`。 */
export interface OptimizeDebugVoxels {
  grid: OptimizeDebugGrid
  elements: number[]
  /** 本当の数(間引く前)。`shown` と違えば `stride` で間引かれている。 */
  total: number
  shown: number
  stride: number
}

/**
 * 拘束/荷重に割り当たった格子節点。`points` はワールドmmの平坦な [x,y,z,...]。
 *
 * **`grid` を必ず渡すこと。** 球の半径は要素サイズから決めるので、これが無いと
 * `?debug=anchors` を単独で付けたとき(塊も密度場も来ない)に別の基準で描くことになり、
 * **同じ節点が題材によって違う大きさで出る**。
 */
export interface OptimizeDebugAnchors {
  grid: OptimizeDebugGrid
  /** 拘束側 / 荷重側の節点を**側ごとに1つへまとめた**点群(面の別は色に出ないため)。 */
  fixed: { faces: number[]; count: number; points: number[] }
  load: { faces: number[]; count: number; points: number[] }
}

/**
 * `setOptimizeDebug` が受け取る**まるごとの状態**(差分ではない)。
 *
 * 呼ぶたびに全部作り直す —— 種類ごとに部分更新する口にすると、「塊だけ古い」状態が
 * 生まれ、切り分けの道具としての信用が消える。
 */
export interface OptimizeDebugState {
  /** 塊(設計空間)。`?debug=space`。 */
  space: OptimizeDebugVoxels | null
  /** 平滑化する前の密度場。`?voxel=1`。 */
  rho: OptimizeDebugVoxels | null
  /** 面アンカーの節点。`?debug=anchors`。 */
  anchors: OptimizeDebugAnchors | null
  /**
   * ボクセルの代わりに隠すボディ(`?voxel=1` の結果ボディ)。**表示だけ**の話で、
   * 文書の可視状態には触れない(サーバへは何も送らない)。
   */
  hiddenBodyId: string | null
}

/** `setOptimizeState` が受け取る、形状最適化(第3のモード)の見た目の元。 */
export interface OptimizeVisualState {
  preserveBodies: string[]
  excludeBodies: string[]
  fixtures: FaceRef[]
  loads: OptimizeLoadVisual[]
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

/**
 * ボディの幽霊の**回転**(`move_body` の `mode="rotate"`)。
 *
 * **コアが解決した後の数値と同じ形**である(`duocad/verbs/move_body.py` の
 * `_rotation_args` が `axis_point` / `axis_dir` の数値へ潰し、操作ログにもその数値だけが
 * 載る)。辺・頂点への参照はここへ持ち込まない —— 幽霊は確定前なのでサーバから解決済みの
 * 数値を貰えず、**呼び出し側(`App.svelte`)が数値で渡せるときだけ幽霊を出す**契約にしてある
 * (既定の中心=対象の重心はブラウザ側で正確に出せないので、そのときは幽霊を描かない)。
 */
export interface BodyGhostRotation {
  /** 軸が通る点 [x, y, z](mm・世界座標)。 */
  point: [number, number, number]
  /** 軸の向き [x, y, z](正規化前でよい。長さ0は幽霊を描かない)。 */
  dir: [number, number, number]
  /** 回転角(度)。**軸の向きに対する右ねじが正**(コアの `move_body` と同じ符号)。 */
  angleDeg: number
}

/**
 * ボディ1つぶんの**ローカル幽霊**(確定前の姿を、届いているメッシュから置く指示)。
 *
 * 平行移動(`offset`)と回転(`rotate`)を持つ。**コア側では排他**(`mode` で切り替える)
 * なので、回転のときの `offset` は `[0,0,0]` である —— ここで両方を許しているのは、
 * 変換の合成を1本の行列にまとめる `bodyGhostMatrix` の側で場合分けを持たないためだけで、
 * 「並進しながら回す `move_body`」という仕様があるわけではない。
 */
export interface BodyGhost {
  bodyId: string
  /** 平行移動 [dx, dy, dz](mm・世界座標)。 */
  offset: [number, number, number]
  /** 軸まわりの回転。省略・null なら平行移動だけ。 */
  rotate?: BodyGhostRotation | null
}

/** `BodyGhostRotation` として使える値か(有限な数値・長さ0でない軸)。 */
export function usableRotation(rotate: BodyGhostRotation | null | undefined): rotate is BodyGhostRotation {
  if (!rotate) return false
  const numbers = [...rotate.point, ...rotate.dir, rotate.angleDeg]
  if (!numbers.every((value) => typeof value === 'number' && Number.isFinite(value))) return false
  return rotate.dir.some((value) => value !== 0)
}

/**
 * 幽霊のグループに掛ける変換。**「軸が通る点まわりの回転」を平行移動で挟んで作る**
 * (`T(p) · R(axis, θ) · T(-p)`)—— three.js の `makeRotationAxis` は原点まわりなので、
 * 軸の位置はこの挟み込みでしか入らない。コア(`gp_Trsf.SetRotation(gp_Ax1(点, 向き), θ)`)と
 * 同じ変換である。
 *
 * **`export` してあるのは検算のため**(`ghostInfo` が返す行列と同じものを外から組める)。
 */
export function bodyGhostMatrix(entry: BodyGhost): THREE.Matrix4 {
  const matrix = new THREE.Matrix4().makeTranslation(
    entry.offset[0],
    entry.offset[1],
    entry.offset[2],
  )
  const rotate = entry.rotate
  if (usableRotation(rotate)) {
    const axis = new THREE.Vector3(rotate.dir[0], rotate.dir[1], rotate.dir[2]).normalize()
    const point = new THREE.Vector3(rotate.point[0], rotate.point[1], rotate.point[2])
    matrix
      .multiply(new THREE.Matrix4().makeTranslation(point.x, point.y, point.z))
      .multiply(new THREE.Matrix4().makeRotationAxis(axis, (rotate.angleDeg * Math.PI) / 180))
      .multiply(new THREE.Matrix4().makeTranslation(-point.x, -point.y, -point.z))
  }
  return matrix
}

/** 1Bodyぶんの形状の差し替え。`geometry: null` は非表示・削除(メッシュを取り除く)。 */
export interface BodyGeometryUpdate {
  bodyId: string
  geometry: GeometryPayload | null
}

/**
 * クリックが選択集合に対して何をするか。修飾キーから決まる。
 *
 * **`add` / `remove` ではなく `toggle` の1つにしてある**(本人明言:
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
   * 当たった頂点。**マーカーを出しているときだけ** null 以外になる(`showVertices`)。
   * 既定では辺より後・面より先に見る —— 頂点は必ず辺の端点に乗っているので辺より先に
   * 見ると、フィレットを起動している間に辺の端が掴めなくなる。逆に面より後だと、
   * 最小の的である頂点マーカーがその下の面に取られて二度と外せない。
   *
   * **`preferVertices(true)` の間だけこの順序が入れ替わる**(頂点が辺より先)。
   * 頂点そのものを指させるコマンドは、既定の順序では角の頂点を一生取れないため。
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
  /**
   * **見た目だけの角・直線どうしの交点に当たったときだけ**、その厳密な座標。
   *
   * これが在るときは `entityKinds.ts` の `snapToSketchEntity`(この図形自身の輪郭への
   * 射影)を呼ばずに**そのままスナップ結果として使うこと** —— 交点は2つの図形にまたがり、
   * `entityId` は片方でしかないので、その図形自身へ射影しても交点には戻らない
   * (連続的な最近傍点になるだけで、めったに交点そのものと一致しない)。角
   * (`entityVisualCorners`)は今のところ自分自身への射影と一致するが、同じ経路に
   * 揃えておくことで交点と角を呼び出し側で区別しなくてよくなる。
   */
  snapAt?: readonly [number, number]
  /**
   * `snapAt` が**線分の中点**であることの印(角・交点とは別の優先度階層 —— 点・角/交点の
   * 次、辺より上。`viewport.ts` の `sketchMidpointAt` だけが立てる)。
   *
   * `snapDirection` とセットで使う: カーソル強調(`sketchCursorOverlay.ts`)がその線分と
   * 直交する短いティックを描くには、線分の向き(画面上の角度)が要る。
   */
  midpoint?: boolean
  /** `midpoint` が真のときだけ意味を持つ、その線分の向き(単位ベクトル・(u,v)空間)。 */
  snapDirection?: readonly [number, number]
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
 * スケッチを貼った平面。**実ジオメトリの面(`face`)か、それ以外か**(`face` 側は `FaceRef`
 * と同じ形にして、面メッシュの索きをそのまま使えるようにする)。グリッドの表示平面決定
 * (`desiredGridPlanes`)と範囲の見積もり(`faceUvExtent`)が読む。
 *
 * **`session.ts` の `PlaneRef` とは語彙が違う** —— 向こうは `base`/`face`/`offset` の3種だが、
 * ここは `face` かどうかの2値しか持たない。`offset` 平面は基準が面でも、そこから離れた
 * 位置には実ジオメトリの境界が無い(`faceUvBoxOnly` を当てられない)ので `base` 側へ畳む
 * (`App.svelte` の `sketchDraws` が変換する)。
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
   *
   * **確定前の姿の扱いは2種類で違う**: サーバ投機実行のプレビュー(`showPreview`)は
   * 落ちるが、ローカル幽霊(`showBodyGhosts`)は**届いた形で組み直して出し直す** ——
   * あちらはサーバにしか作れない別の形、こちらは「いまの本物を変換した姿」で、
   * 届いた生メッシュから同じ指示で作り直せる。呼び直す側(`CommandHost` の
   * `previewedKey`)は引数が変わらない限り再発火しないので、ここで出し直さないと
   * 幽霊は消えたまま戻らない。
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
   *
   * 第2引数は**同じ画素**の `sketchEntityAt` の結果(クリックの `PickEvent.sketchEntity`
   * と同じ関数)。呼び出し側(App.svelte)はこれを使って点を図形へスナップしてから
   * 幽霊に渡す — クリック側の `addPoint` も同じスナップ関数を通るので、両者はずれない。
   */
  onPlaneMove(
    listener: (point: { u: number; v: number } | null, sketchEntity: SketchEntityHit | null) => void,
  ): void
  /** スケッチの点入力待ちのカーソル強調を出すか(`CommandHost` の `onWantsPoint` から)。 */
  setSketchCursorWanted(wanted: boolean): void
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
  /**
   * **開発用のデバッグ表示**(塊・平滑化前のボクセル・面アンカーの節点)。
   * `null`(既定)なら何も描かない —— **フラグを付けない利用者の画面は1画素も変わらない。**
   *
   * 描くものは装飾で、**指せない**(`allFaceMeshes` は変わらない)。面アンカーの球は
   * **深度検査を切って最前面**に描く(形状の裏側の節点も見えないと、
   * 「見えているつもりで見えていない」になる)。
   */
  setOptimizeDebug(state: OptimizeDebugState | null): void
  /**
   * デバッグ表示のいまの数え(実機検査用。何も描いていなければ全部0)。
   *
   * `anchorsDepthTest` を出しているのは**罠を機械で見張るため** —— 節点は深度検査を
   * 切って最前面に描く決まりで、これを落とすと「見えているつもりで見えていない」になる。
   * 目視では気づけないので `false` であることを実機検査が確かめる。
   */
  optimizeDebugCounts(): {
    space: number
    rho: number
    fixed: number
    load: number
    spaceStride: number
    rhoStride: number
    anchorsDepthTest: boolean | null
    hiddenBodyId: string | null
  }
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
  // ------------------------------------------------------------------ 常設の原点ギズモ(issue #8)
  /**
   * 原点の点・X/Y/Z軸・XY/XZ/YZ平面(7項目)を**まとめて**出し入れする(本人指定
   * 「7つまとめて」)。当たり判定は持たない純粋な装飾で、`showBasePlanes` とは無関係
   * (あちらはスケッチの対象待ちだけの一時表示・こちらは常設)。既定は表示(`true`)。
   */
  setOriginVisible(visible: boolean): void
  /**
   * **頂点を辺より先に見る**(`false` で既定の順序へ戻す)。
   *
   * 既定は辺が先である —— 頂点は必ず辺の端点に乗っているので、先に見るとフィレットを
   * 起動している間に辺の端が掴めなくなる(`PickEvent.vertex` のdocstring)。だが
   * **頂点そのものを1つ指させたいコマンド**(`move_body` の回転中心)では、その既定の
   * ままだと角の頂点が必ず辺に取られて**一生指せない**。辺と同じ「要求しているものが
   * 勝つ」規律で、待っている間だけ順序を入れ替える。
   */
  preferVertices(on: boolean): void
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
   * 検出できない。そこまで見るには `optimizeAnnotations()` を使うこと。
   */
  optimizeAnnotationCount(): number
  /**
   * 形状最適化(第3のモード)の装飾の**中身**。**実機検査フック専用**。
   *
   * `fixtures` は固定記号を置いた面の参照、`loads` は矢印ごとの単位ベクトルとラベル文字列
   * (`"250N"` のような表示テキストそのもの)。**種別を区別して数える・向きとラベルを
   * 突き合わせる**ところまで実機検査で確かめられるようにするための穴
   * (`optimizeAnnotationCount()` の粗さを補う)。
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
    /** 当たったスケッチの図形(点または辺)。スケッチモード中だけ null 以外。 */
    sketchEntity: SketchEntityHit | null
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
  /**
   * プレビューを消して本物のBodyを出し直す。**ボディ側の幽霊は2種類とも消える**
   * (サーバ投機実行の `showPreview` と、ブラウザだけで置く `showBodyGhosts`)——
   * どちらも「確定前の姿」であり、消える場面(Esc・本物の到着・Bodyの消滅)が同じ。
   * **本物の到着(`update`)だけは、消した後にローカル幽霊を組み直す**(`update` の項)。
   */
  clearPreview(): void
  /**
   * **既にあるボディを変換した姿**を、ブラウザだけで半透明に重ねる(`move_body`)。
   *
   * `showPreview` との違いは3つ:
   *
   * 1. **サーバへ何も投げない** —— 位置しか変わらないので、OCCでの再計算が要らない。
   *    すでに届いているメッシュ(`getGeometry` と同じ生データ)をそのまま使う。
   * 2. **本物を隠さない** —— 元の位置と移動先の両方が見えることが、このプレビューの
   *    存在理由そのものである(`duocad/catalog.py` の `move_body` の note)。
   * 3. **1ボディ = 1 `THREE.Group`** で、変換はグループに掛ける。ボディの中の面を
   *    個別に動かすverbは無いので、変換の単位はボディでよい。
   *
   * 幽霊は**ピックの対象にしない**(まだ存在しない位置は指せない)。まだ形が届いていない
   * ボディ(非表示など)は黙って落ちる。空配列で消える。
   */
  showBodyGhosts(entries: BodyGhost[]): void
  /**
   * いま出ている幽霊の中身。**実機検査が「値を変えたら幽霊が追随したか」を数値で見る穴**
   * (`getGeometry` と同じ流儀 —— three.jsのバッファは外から読めない)。
   *
   * `matrix` は**そのグループに実際に掛かっている行列**(three.js の列優先16要素)。
   * 指示(`offset` / `rotate`)だけでは「行列を組む式が正しいか」を外から検算できない
   * ので、掛かった結果そのものを出す。
   */
  ghostInfo(): {
    sketch: Array<{ kind: string; params: Record<string, number> }>
    bodies: Array<BodyGhost & { matrix: number[] }>
  }
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
   * 確定前の図形(スケッチの幽霊)を描く。空配列で消える。
   * **個々の図形は`buildEntityLine`が失敗しても他は描く**(表に無い種別・
   * 折れ線化できない図形が1つ混ざっていても、残りの幽霊まで消さない)。
   *
   * **サーバへ投げない**。図形の形はブラウザ側で分かっており、コアで投機実行すると
   * 文書に一度は入る(戻すとはいえ)。Escで中断しても文書が変わらないことを、
   * 「そもそも送らない」で成立させる。
   */
  showSketchGhosts(sketchId: string, entities: Array<{ kind: string; params: Record<string, number> }>): void
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
   * **`lookAtSketch` の戻り道ではない**。抜けたときの復帰は
   * `setActiveSketch(null)` が「入る前の視点」へ戻す形で担う。こちらは
   * 「今あるもの全体」という別の視点で、実機検査が図形を画面内へ収めるために呼ぶ。
   */
  frameAll(): void
  /**
   * 新規文書にしたときに呼ぶ。**カメラは動かさない**(新規は空の文書で、合わせる内容が
   * 無いため`frameAll`を呼ぶ理由が無い)。基準グリッドの縮小だけを許可する
   * (`frameAll`と同じヒステリシスの「許可」を、姿勢を動かさずに与える)。
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
   * サーバは面ごとの精度指定を送る前の「文書全体で1つの精度」と同じ振る舞いに戻る。
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
   * `near`/`far` も返す。こちらも実機検査の穴で、文書の状態ではないので
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
   * 格子はシェーダーで無限に描く。
   *
   * **実機検査フック専用** —— 「指せる範囲が大きなモデルに合わせて広がった」
   * ことは見た目からは何mmかまで分からないので、値そのものを機械で見られるようにする。
   * 文書の状態ではないので、コアへは何も飛ばさない(`cameraPose` と同じ方針)。
   */
  gridScale(): { extent: number }
  /**
   * グリッドの3段(1/10/100mm)の今の見え方。**実機検査フック専用** —— 刻みは固定なので、
   * 代わりに「いま各段がどれだけ濃いか」と「どの平面を出しているか」を出す。
   *
   * `planes` は表示中の平面の名前で、基準平面(`XY`/`XZ`/`YZ`)か、任意面へのスケッチ中は
   * `sketch-face`(名前は `desiredGridPlanes` が付ける)。
   * `axis` は**面外に線として描いている軸**(無ければ null)。
   * `sketchFaceExtent` は任意面グリッドの枠の半径[mm](`faceUvBoxOnly`。測れなかった・
   * 任意面でないときは null)。`planeBounds` は `planes` と同じ並びの枠(中心と半径。
   * カーソル追従で広がった後の値も含む)。
   * `depthBiasLayers` は `planes` と同じ並びの深度バイアス層番号(`DEPTH_BIAS_LAYER`)。
   * 任意面のグリッドが基準平面と別枠(`sketchFaceGrid`)を使っているかを機械で見るための穴
   * (frameが基準平面と数値的に一致する実ジオメトリの面でも、正しく `sketchFaceGrid` に
   * なっていることをここで検査する)。
   */
  gridTiers(): {
    mmPerPixel: number
    tiers: Array<{ step: number; alpha: number }>
    planes: string[]
    axis: 'X' | 'Y' | 'Z' | null
    sketchFaceExtent: number | null
    depthBiasLayers: number[]
    planeBounds: Array<UvBounds | null>
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
   * 矩形も動くので、
   * 検査が決め打ちの座標を使わずここから読めるようにする。
   */
  viewCubeRect(): { x: number; y: number; w: number; h: number }
  dispose(): void
}
