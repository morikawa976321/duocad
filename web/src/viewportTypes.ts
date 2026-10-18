/** `App.svelte` へ見せるデータ型と `Viewport` インタフェース(three.js の型は出さない)。 */
import * as THREE from 'three'

import { type ViewResolution } from './adaptiveResolution'
import type { BasePlaneName, EdgeGeometry, OptimizeDebugGrid } from './session'

/** kernel.tessellate() が返す1面ぶん(サーバのJSONそのまま)。 */
export interface FaceGeometry {
  index: number
  kind: string
  area: number
  centroid: number[]
  normal: number[]
  positions: Float32Array | number[]
  indices: Uint32Array | number[]
  /** この面を細かく測ったか粗く測ったか(サーバが付与。古いサーバは無い)。 */
  detail?: 'fine' | 'coarse'
  /** 三角形の通し番号(サーバの `view._FaceTessellations`)。次の配信で同じ面が省かれたとき、これで引き当てる(`wire.ts`)。 */
  serial?: number
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
 * 「いまの画面はどれくらい細かく描くべきか」。カメラから導く値であって設定ではない。
 *
 * `worldPerPixel` と `sizeHint` が事実で、`deviation` / `angularDeflection` は
 * `adaptiveResolution.ts` の式で導いた目標精度。サーバへ送るのは事実の方だけ
 * (`set_view_resolution`)で、同じ式が `duocad/resolution.py` にもある。
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
 * 大きさ・向きは面ごとに違ってよい(1本の値を全ての面へ使い回してはならない)。
 */
export interface OptimizeLoadVisual extends FaceRef {
  magnitude: number
  direction: 'normal' | '-normal' | 'vector'
  vector: [number, number, number] | null
}

/**
 * 開発用のデバッグ表示(`web/src/debugFlags.ts` のURLパラメータでだけ出る)。
 * 契約の正は `duocad/optimize_run.py` の `_DebugSink`。座標はワールドmm。
 * セル中心の規約の変換点は `optimizeDebug.ts` の `buildDebugVoxels` だけ(サーバ側は素の添字を送る)。
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
 * `grid` を必ず渡すこと: 球の半径は要素サイズから決めるので、無いと
 * `?debug=anchors` 単独のとき同じ節点が題材によって違う大きさで出る。
 */
export interface OptimizeDebugAnchors {
  grid: OptimizeDebugGrid
  /** 拘束側 / 荷重側の節点を**側ごとに1つへまとめた**点群(面の別は色に出ないため)。 */
  fixed: { faces: number[]; count: number; points: number[] }
  load: { faces: number[]; count: number; points: number[] }
}

/**
 * `setOptimizeDebug` が受け取る**まるごとの状態**(差分ではない)。
 * 部分更新の口にすると「塊だけ古い」状態が生まれるので、呼ぶたびに全部作り直す。
 */
export interface OptimizeDebugState {
  /** 塊(設計空間)。`?debug=space`。 */
  space: OptimizeDebugVoxels | null
  /** 平滑化する前の密度場。`?voxel=1`。 */
  rho: OptimizeDebugVoxels | null
  /** 面アンカーの節点。`?debug=anchors`。 */
  anchors: OptimizeDebugAnchors | null
  /** ボクセルの代わりに隠すボディ(`?voxel=1` の結果ボディ)。表示だけで、文書の可視状態には触れない。 */
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

/** 1ボディぶんの辺(`EdgesPayload` の中身。`setBodyEdges`)。 */
export interface BodyEdgesPayload {
  revision: number
  /** 折れ線の弦の偏差[mm](遮蔽の判定がこの分の奥行きの差を許す)。 */
  deviation: number
  edges: EdgeGeometry[]
}

/** 1ボディぶんの頂点(`VerticesPayload` の中身。`setBodyVertices`)。座標は世界座標 [x, y, z]。 */
export interface BodyVerticesPayload {
  revision: number
  vertices: Array<{ index: number; point: number[] }>
}

/**
 * 画面に出す測定1件(issue #10)—— 測った2点(世界座標)と札の文字。
 * `from` / `to` はコアの `measure` の `between.from` / `between.to` そのもの(垂線の足を含む)。
 */
export interface MeasurementVisual {
  from: number[]
  to: number[]
  label: string
}

/**
 * ボディの幽霊の**回転**(`move_body` の `mode="rotate"`)。
 *
 * コアが解決した後の数値と同じ形(`duocad/verbs/move_body.py` の `_rotation_args`)。
 * 辺・頂点への参照は持ち込まない: 幽霊は確定前でサーバから解決済みの数値を貰えないので、
 * 呼び出し側(`App.svelte`)が数値で渡せるときだけ幽霊を出す契約
 * (既定の中心=対象の重心はブラウザ側で正確に出せないので、そのときは描かない)。
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
 * コア側では平行移動と回転は排他(`mode`)で、回転のときの `offset` は `[0,0,0]`。
 * 両方を許しているのは `bodyGhostMatrix` に場合分けを持たせないためだけで、
 * 「並進しながら回す」仕様があるわけではない。
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
 * 幽霊のグループに掛ける変換 `T(p) · R(axis, θ) · T(-p)`。three.js の `makeRotationAxis` は
 * 原点まわりなので、軸の位置は平行移動の挟み込みでしか入らない。コア
 * (`gp_Trsf.SetRotation(gp_Ax1(点, 向き), θ)`)と同じ変換。
 * `export` してあるのは検算のため(`tests/eval_ghosts.mjs` が掛かる行列を外から組んで突き合わせる)。
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
 * クリックが選択集合に対して何をするか。Shift も Ctrl(Mac は Meta)も `toggle`、修飾なしは `replace`。
 * `add` / `remove` に分けないのは本人明言:
 * 「間違えたらもう一回クリックすればいいので、除外専用の機能は実質無意味」。
 */
export type PickMode = 'replace' | 'toggle'

/**
 * スケッチモードの外で指せるものの種類(`Viewport.setPickKinds`)。コマンドを起動していなければ全部、起動中は
 * そのコマンドが受け付けるものだけ(docs/DESIGN_DECISIONS.md「見えるものはすべてさわれる」)。
 * スケッチモード中は見ない(編集中のスケッチの中身だけを指す)。基準平面は `showBasePlanes` が決める。
 */
export type PickKind = 'face' | 'edge' | 'vertex' | 'region' | 'sketchPoint' | 'sketchLine'

/**
 * 1つのクリックで取れるもの。通常モードの優先は細かさが先(点 → 線 → 面)、同じ細かさならソリッドが先
 * (重なって見えるとき。`PICK_OVERLAP_PIXELS`)。見えないもの(非表示・立体の裏)は取れない(`occlusion.ts`)。
 * 当たった1つだけが非null(スケッチモードの `sketchEntity`・`region`・`point` は一緒に返り、優先は `App.svelte` が決める)。
 */
export interface PickEvent {
  /** 当たった面(Body+添字)。外したら null。スケッチモード中は常に null。 */
  hit: FaceRef | null
  /** 当たった辺。 */
  edge: EdgeRef | null
  /** 当たった頂点。 */
  vertex: VertexRef | null
  /** Shift / Ctrl(Mac は Meta)=トグル / 修飾なし=置き換え。 */
  mode: PickMode
  /**
   * 当たったスケッチの図形(点または辺)。スケッチモード中は編集中のスケッチだけ、通常モードでは見えている全スケッチ。
   * `point` が文字列ならその点(コアの参照が持つ `point` と同じxキー)、`null` なら図形全体(= 辺)。
   * 点は辺より先に見る(点は必ず辺の上に乗っていて、後だと端点を掴めない)。
   */
  sketchEntity: SketchEntityHit | null
  /** スケッチモード中の、クリックした点のスケッチ平面上の座標。平面がカメラの裏なら null。 */
  point: { sketchId: string; u: number; v: number } | null
  /**
   * 当たった領域(スケッチ+添字)。スケッチモード中はそのスケッチの領域、通常モードでは見えている全スケッチの領域。
   * 通常モードで領域と面が重なる画素は面が勝つ(同じ細かさならソリッドが先)。
   */
  region: { sketchId: string; index: number } | null
  /**
   * 当たった基準平面(XY/XZ/YZ)。`showBasePlanes(true)` の間だけ非null。
   * 平面は面の無い画素でだけ返る(手前か奥かは比べない)ので、非nullなら `hit` は null。
   */
  plane: BasePlaneName | null
}

/** スケッチの図形(点または辺)への当たり。`point` が null なら図形全体。 */
export interface SketchEntityHit {
  sketchId: string
  entityId: string
  point: string | null
  /**
   * 見た目だけの角・直線どうしの交点に当たったときだけ、その厳密な座標。
   * 在るときは `entityKinds.ts` の `snapToSketchEntity` を呼ばずそのままスナップ結果として使うこと
   * (交点は2つの図形にまたがり、片方の図形へ射影しても交点には戻らない)。
   */
  snapAt?: readonly [number, number]
  /**
   * `snapAt` が線分の中点であることの印(優先度は点・角/交点の次、辺より上。
   * `viewport.ts` の `sketchMidpointAt` だけが立てる)。`snapDirection` とセットで使う。
   */
  midpoint?: boolean
  /** `midpoint` が真のときだけ意味を持つ、その線分の向き(単位ベクトル・(u,v)空間)。 */
  snapDirection?: readonly [number, number]
}

/**
 * 左ドラッグのウィンドウ選択の結果(矩形に完全包含され、一部でも見えているものだけ)。
 * 何も囲まなかったドラッグも空配列で呼ぶ(修飾なしなら選択を空にする)。
 */
export interface BoxSelectEvent {
  faces: FaceRef[]
  edges: EdgeRef[]
  vertices: VertexRef[]
  regions: Array<{ sketchId: string; index: number }>
  sketchEntities: SketchEntityHit[]
  mode: PickMode
  /** 引いた矩形(辺・頂点がまだ届いていなかったとき、届いてから `Viewport.collectInBox` で集め直す)。 */
  rect: BoxRect
  /** 矩形に掛かるのに辺・頂点がまだ届いていないボディ(空なら集め終わっている)。 */
  pendingBodies: string[]
}

/** コンテナ内の画素の矩形(左上原点)。 */
export interface BoxRect {
  left: number
  top: number
  right: number
  bottom: number
}

/**
 * スケッチを貼った平面。実ジオメトリの面(`face`)か、それ以外か。
 * `session.ts` の `PlaneRef`(`base`/`face`/`offset`)とは語彙が違う: `offset` 平面は
 * 離れた位置に実ジオメトリの境界が無い(`faceUvBoxOnly` を当てられない)ので
 * `base` 側へ畳む(`App.svelte` の `sketchDraws` が変換する)。
 */
export type SketchPlaneRef = { kind: 'base' } | ({ kind: 'face' } & FaceRef)

/** 1枚のスケッチの描画指示(three.js の型は出さない)。 */
export interface SketchDraw {
  sketchId: string
  /** コアのスケッチの版(`SketchState.revision`)。同じ版なら作り直さない(選択の色だけ塗り直す)。 */
  revision: number
  /** `p = origin + u*uAxis + v*vAxis`(コアが配る枠。session.ts の `SketchFrame`)。 */
  frame: { origin: number[]; u: number[]; v: number[]; normal: number[] }
  /** 貼った平面(基準面か、既存ソリッドの面か)。 */
  plane: SketchPlaneRef
  /** 編集中のスケッチか(編集中は線・点を立体に透かして描き、作図の吸着を出す。それ以外は立体の裏に回ると隠れる)。 */
  editing: boolean
  /**
   * 旧形式の反復(描くたびに複製を作る。今の verb は作らない)を持つか。持つときだけ `entities` を描く:
   * 複製は `entities` にしか無い。持たなければ `entities` と `handles` は同じ形なので、`handles` だけ描く。
   */
  livePatterns: boolean
  /** 反復を展開した描画対象。`params` はコアの `Entity.params` と同じキー。 */
  entities: Array<{ kind: string; params: Record<string, number> }>
  regions: Array<{ index: number; positions: Float32Array | number[]; indices: Uint32Array | number[] }>
  /** 選択中の領域の添字(強調して描く)。 */
  selectedRegions: number[]
  /**
   * 文書の図形(`entity_id` つき)。点・辺のピックと点の表示に使う。旧形式の反復の複製は `entities`(= コアの `drawn`)に
   * しか無く `entity_id` を持たないので指せない。
   */
  handles: Array<{ entityId: string; kind: string; params: Record<string, number> }>
  /** 選択中の図形(強調して描く)。`point` が null なら図形全体。 */
  selectedHandles: Array<{ entityId: string; point: string | null }>
}

/**
 * 断面表示(issue #6)の状態。App が持ち、viewport は写す(文書の状態でも画面設定でもない。
 * 起動時と文書の差し替えで OFF)。`position` は平面の法線軸上の座標[mm](XY なら Z)。
 */
export interface SectionState {
  on: boolean
  plane: BasePlaneName
  position: number
  inverted: boolean
}

export interface Viewport {
  /**
   * Bodyの形状をまとめて反映する。初回(まだ何も描いていない状態)だけ、反映後の
   * シーン全体にカメラを合わせる。以後は視点を保ったまま差分だけ描き直す。
   *
   * 確定前の姿の扱いは2種類で違う: サーバ投機実行のプレビュー(`showPreview`)は落ちるが、
   * ローカル幽霊(`showBodyGhosts`)は届いた形で組み直して出し直す。呼び直す側
   * (`CommandHost` の `previewedKey`)は引数が変わらない限り再発火しないので、
   * ここで出し直さないと幽霊は消えたまま戻らない。`keepPreview` は形の変わらない(粗さだけの)再配信で、
   * サーバ投機実行の幽霊も残す(本物は隠し直す)。
   */
  update(bodies: BodyGeometryUpdate[], options?: { keepPreview?: boolean }): void
  /** 直近に `update()` で渡した生の `GeometryPayload`(無ければ null)。実機検査フック専用。 */
  getGeometry(bodyId: string): GeometryPayload | null
  /**
   * `validBodyIds` に無いBodyの描画を取り除く。`duocad/server.py` の `Session.delta` の契約
   * (知っているのに `bodies` にも `unchanged` にも現れないIDは消えた)に対応する側。
   * 呼び出し側は契約どおり毎回呼ぶ。
   */
  prune(validBodyIds: string[]): void
  /** 面のクリック(当たり・外れとも)を受け取る。 */
  onPick(listener: (event: PickEvent) => void): void
  /**
   * スケッチモード中、ポインタがスケッチ平面のどこを指しているか。モード外・平面の裏では null。
   * 第2引数は同じ画素の `sketchEntityAt` の結果(クリック側の `addPoint` も同じスナップ関数を通る)。
   */
  onPlaneMove(
    listener: (point: { u: number; v: number } | null, sketchEntity: SketchEntityHit | null) => void,
  ): void
  /** スケッチの点入力待ちのカーソル強調を出すか(`CommandHost` の `onWantsPoint` から)。 */
  setSketchCursorWanted(wanted: boolean): void
  /**
   * 選択中の面を強調する(空配列で解除)。選択の色は人が選んでもAIが選んでも1つ(橙)。
   * 領域は3Dでは色分けしない(スケッチ側で強調する)。
   */
  highlight(refs: FaceRef[]): void
  /**
   * 選択中のボディを強調する(空配列で解除)。形状最適化で保持/除外ソリッドを決めているあいだだけ非空。
   * 選択は役割色(`setOptimizeState` の緑/赤)に勝つ。
   */
  highlightBodies(bodyIds: string[]): void
  /**
   * 形状最適化の見た目(保持/除外ソリッドの色、拘束面の色分けと固定記号、荷重の矢印+ラベル)。
   * `null` はモードを抜けたとき(ソリッドの色も素へ戻す)。記号・矢印は指せない装飾。
   */
  setOptimizeState(state: OptimizeVisualState | null): void
  /**
   * 開発用のデバッグ表示(塊・平滑化前のボクセル・面アンカーの節点)。
   * `null`(既定)なら何も描かない。指せない装飾。面アンカーの球は深度検査を切って最前面に描く
   * (形状の裏側の節点も見えないと「見えているつもりで見えていない」になる)。
   */
  setOptimizeDebug(state: OptimizeDebugState | null): void
  // ------------------------------------------------------------------ 何を指せるか
  /** スケッチモードの外で指せる種類(`PickKind`)。既定は全部。 */
  setPickKinds(kinds: readonly PickKind[]): void
  // ------------------------------------------------------------------ D: 辺・頂点
  /**
   * そのボディの辺の折れ線(null で捨てる)。届いたボディの辺だけが指せる。描くのはホバー中・選択中の辺だけで、
   * 見た目の辺はボディの境界線が受け持つ。呼び手は「今の形の辺」だけを渡す(古い添字は別の辺を指す)。
   */
  setBodyEdges(bodyId: string, edges: BodyEdgesPayload | null): void
  /** そのボディの頂点の座標(null で捨てる)。描くのはホバー中・選択中の頂点だけ。 */
  setBodyVertices(bodyId: string, vertices: BodyVerticesPayload | null): void
  /**
   * 辺・頂点がまだ無いボディがカーソルの近く(または矩形選択の中)にある(取り寄せの合図。重複して呼ばれうる)。
   * 辺・頂点を指せる間だけ呼ぶ。
   */
  onPickDataWanted(listener: (bodyIds: string[]) => void): void
  // ------------------------------------------------------------------ 基準3平面(スケッチを貼る先)
  /**
   * 基準3平面(XY/XZ/YZ)を指せるようにする(`false` で指せなくなる)。
   * 辺と同じ規律で、その対象を要求しているコマンドを起動している間だけ指せる
   * (常時だと面を狙うクリックが、原点を跨いで±100mmに広がる手前のグリッドに取られる)。
   *
   * 当たり判定は `raycaster.ray.intersectPlane()` を3枚ぶん回す解析交差。描画中のグリッド
   * (`gridRoot`)は判定に使わない(3平面の線が1つの `BufferGeometry` に混在し、
   * 当たった線から平面を復元できない)。
   */
  showBasePlanes(on: boolean): void
  // ------------------------------------------------------------------ 常設の原点ギズモ(issue #8)
  /**
   * 原点の点・X/Y/Z軸・XY/XZ/YZ平面(7項目)をまとめて出し入れする。
   * 当たり判定を持たない装飾。既定は表示。
   */
  setOriginVisible(visible: boolean): void
  /**
   * 断面表示(issue #6)。状態は App が持ち、ここは写すだけ。ON のあいだボディの面・境界線・
   * 選べる辺・頂点マーカー・幽霊は平面で切られ、切られた側は指せない(`resolveHit` と
   * `collectInBox` が同じ平面で判定する)。スケッチ・領域・グリッド・原点ギズモ・注記は切らない。
   */
  setSection(state: SectionState): void
  /** ハンドルのドラッグで位置が動いた(App が状態を更新し `setSection` で書き戻す)。 */
  onSectionDrag(listener: (position: number) => void): void
  /** 表示中のボディ(幽霊を含む)の外接箱の中心。空なら null。 */
  contentCenter(): [number, number, number] | null
  /**
   * 辺のホバーと `edgeChain` を、辺1本ではなく連なり(`setEdgeChains`)の単位にする(既定 `false`)。
   * フィレット・面取りの起動中だけ立てる(測定・回転軸は辺を1本ずつ指す)。
   */
  groupEdgeChains(on: boolean): void
  /**
   * その辺と同じ連なりの辺の添字(`groupEdgeChains(false)` の間はその辺だけ)。
   * 連なりの単位で扱っているのに、その辺の連なりがまだ届いていなければ `null`。
   */
  edgeChain(ref: EdgeRef): number[] | null
  /** 届いた連なり(各要素が1つの連なりの添字の全部)を、そのボディの辺に覚えさせる。 */
  setEdgeChains(bodyId: string, chains: number[][]): void
  /** 連なりの単位で扱っている間に、連なりの分からない辺へホバーが乗った(取り寄せの合図。重複して呼ばれうる)。 */
  onEdgeChainWanted(listener: (ref: EdgeRef) => void): void
  /** 選択中の辺を強調する(面の `highlight` と同じ規律)。裏に回っても透かして描く。 */
  highlightEdges(refs: EdgeRef[]): void
  /** 選択中の頂点を点(画面上で一定の大きさ)で出す。色は面・辺と同じ橙。裏に回っても透かして描く。 */
  highlightVertices(refs: VertexRef[]): void
  /** 矩形を今のカメラで集め直す(`BoxSelectEvent.pendingBodies` の辺・頂点が届いた後)。 */
  collectInBox(rect: BoxRect): Omit<BoxSelectEvent, 'mode'>
  // ------------------------------------------------------------------ 測定(issue #10)
  /**
   * 測った2点を結ぶ線・両端の点・数値の札を出す(`null` で消す)。指せない装飾。
   * 点から面へ下ろした垂線の足は面の裏やソリッドの内側に落ちうるので、深度検査をしない。
   */
  showMeasurement(visual: MeasurementVisual | null): void
  /** スケッチを作り直した回数の累計(実機検査・物差し用。選択だけが変わった配信では増えない)。 */
  sketchRebuilds(): number
  /** 直近の本描画の描画呼び出しの数(`renderer.info.render.calls`。ビューキューブを除く)。物差し用。 */
  drawCalls(): number
  /** 本描画を行った累計回数(静止中に描いていないかを見る物差し)。 */
  renderCount(): number
  /** 今ホバーで明るくしているもの(実機検査フック専用)。ホバーは色を足さず明度だけを上げる。 */
  hoveredRef(): {
    face: FaceRef | null
    edge: EdgeRef | null
    vertex: VertexRef | null
    region: { sketchId: string; index: number } | null
    plane: BasePlaneName | null
    /** 当たったスケッチの図形(点または辺)。 */
    sketchEntity: SketchEntityHit | null
    /** そのものに今実際に当たっている色(16進)。何も指していなければ null。 */
    color: number | null
  }
  /**
   * 頂点の画面座標(無理なら null)。実機検査が本物のクリックで頂点を選ぶための穴で、返す前にクリックと同じ判定
   * (`resolveHit`)でその頂点が取れることを確かめる。そのボディの頂点が届いていなければ null。
   */
  projectVertex(bodyId: string, vertexIndex: number): { x: number; y: number } | null
  /**
   * 辺の上の点の画面座標(無理なら null)。実機検査が本物のクリックで辺を選ぶための穴で、返す前にクリックと同じ判定
   * (`resolveHit`)でこの辺が取れることを確かめる。そのボディの辺が届いていなければ null。
   */
  projectEdge(bodyId: string, edgeIndex: number): { x: number; y: number } | null
  /**
   * 確定前の形(プレビュー)を重ねる。本物のBodyは隠し、幽霊を代わりに描く。
   * 幽霊はピックの対象にしない。本物の状態が届いたら(`update`)プレビューは無効になり自動的に消える。
   */
  showPreview(bodies: BodyGeometryUpdate[]): void
  /**
   * プレビューを消して本物のBodyを出し直す。ボディ側の幽霊(`showPreview` と `showBodyGhosts`)も消える。
   * 本物の到着(`update`)だけは、消した後にローカル幽霊を組み直す。
   */
  clearPreview(): void
  /**
   * 既にあるボディを変換した姿を、ブラウザだけで半透明に重ねる(`move_body`)。
   * `showPreview` との違い:
   * 1. サーバへ何も投げない。届いているメッシュ(`getGeometry` と同じ生データ)をそのまま使う。
   * 2. 本物を隠さない。元の位置と移動先の両方が見えることが存在理由(`duocad/catalog.py` の `move_body` の note)。
   * 3. 1ボディ = 1 `THREE.Group` で、変換はグループに掛ける。
   *
   * ピックの対象にしない。まだ形が届いていないボディ(非表示など)は黙って落ちる。空配列で消える。
   */
  showBodyGhosts(entries: BodyGhost[]): void
  /**
   * 面の表面上の点の画面座標(コンテナ内のCSSピクセル)。無理なら null。
   * 自動テスト(`scripts/smoke.mjs`)が本物のクリックで面を選ぶための穴。
   * 重心は円環や円筒では面の外にあるので三角形の上の点を返し、
   * その画素からのレイが実際にこの面へ当たることを確かめてから返す。
   * 他のBodyに隠れている面は、そのBodyを隠さない限り null になる。
   */
  project(bodyId: string, faceIndex: number): { x: number; y: number } | null
  // ------------------------------------------------------------------ C: スケッチ
  /**
   * スケッチを描き直す(渡さなかったスケッチの描画は消える)。図形は線、領域は面で描く。
   */
  showSketches(sketches: SketchDraw[]): void
  /**
   * 確定前の図形(スケッチの幽霊)を描く。空配列で消える。
   * 渡してよいのは、ブラウザで分かるもの(ローカル方式)か、サーバが文書を変えずに計算したもの
   * (`envelope_regions`)だけ。投機実行の結果は渡さない(文書に一度は入るので、
   * Escで中断しても文書が変わらないことが巻き戻し頼みになる)。
   * `tone: 'warn'` は確定すると拒まれる形(分裂など)。
   */
  showSketchGhosts(
    sketchId: string,
    entities: Array<{ kind: string; params: Record<string, number> }>,
    tone?: 'normal' | 'warn',
  ): void
  /**
   * スケッチモードの対象を決める。`null` でモードを抜ける。
   * モード中のクリックは面ではなく「領域」または「平面上の点」になる
   * (`PickEvent.point` / `PickEvent.region`)。奥のソリッドの面を掴むと、線を引くつもりの
   * クリックが選択の置き換えになるため。
   */
  setActiveSketch(sketchId: string | null): void
  /** スケッチ平面を正面から見る視点へ移す。 */
  lookAtSketch(sketchId: string): void
  /**
   * 今あるもの全体が入る視点へ戻す。抜けたときの復帰は `setActiveSketch(null)` が
   * 「入る前の視点」へ戻す。実機検査が図形を画面内へ収めるために呼ぶ。
   */
  frameAll(): void
  /** 新規文書にしたときに呼ぶ。カメラは動かさず、基準グリッドの縮小だけを許可する。 */
  resetGrid(): void
  /** 左ドラッグのウィンドウ選択が終わったときに呼ばれる。ドラッグの向きは見ない(本人明言)。 */
  onBoxSelect(listener: (event: BoxSelectEvent) => void): void
  /**
   * カメラの画角[度]を変える。`FOV_ORTHO`(0) は完全正投影で、そのとき裏では
   * `OrthographicCamera` へ丸ごと差し替わる。0以外は `FOV_MIN`〜`FOV_MAX` に丸め、NaN・∞は無視する。
   * 画面の設定であって文書の状態ではない(コアにも操作ログにも行かず、undoの対象にもならない)。
   * 切り替えの前後で、注視点の面で画面の縦に収まる世界長さを保つ
   * (しないと画角の変更がただの「ズーム」になり、パースの強さを比べられない)。
   */
  setFov(deg: number): void
  // ------------------------------------------------------------------ 可変解像度(ズーム連動)
  /** 今の画面が要求している解像度。 */
  viewResolution(): ViewportResolution
  /**
   * face単位の高精度化(視錐台+バックフェイスカリング)を切る/入れる(既定は入り)。
   * 実機検査のための穴(`web/scripts/smoke-face-culling.mjs`)。切ると `focus` を送らなくなり、
   * サーバは文書全体で1つの精度の振る舞いに戻る。
   */
  setFaceCulling(enabled: boolean): void
  /**
   * 解像度が意味のある幅で変わったときに呼ばれる(ズーム・画角変更・寸法変更)。
   * ホイール中ずっと呼ぶと27部品ぶんの再テセレーションがサーバで連続して走るので、
   * カメラが止まってから(減衰が収まるまで待つ)、偏差が一定比以上動いたときだけ1回通す。
   * スケッチの折れ線(ブラウザ内で完結)は、同じ瞬間にここが描き直す。
   */
  onResolutionChange(listener: (resolution: ViewportResolution) => void): void
  /** スケッチの図形1本ごとの折れ線の点数(`sketch_id` -> 図形順の点数)。実機検査フック専用。 */
  sketchPolylineCounts(): Record<string, number[]>
  /**
   * 今のカメラの姿勢(ワールド座標)。実機検査専用。
   * 確かめたいのは、`up` がワールド+Zであること、および水平ドラッグでオービットさせても
   * `position.z - target.z` が変わらないこと(回転の極が+Z)。後者は `OrbitControls` が
   * 生成時の up を `_quat` へ焼き込む仕様のせいで静かに壊れる。
   */
  cameraPose(): {
    position: [number, number, number]
    target: [number, number, number]
    up: [number, number, number]
    near: number
    far: number
  }
  /**
   * ワールド座標の点が画面のどこに写るか(ビューポート左上を原点とする画素)。
   * カメラの前後のクリップ範囲の外なら null。
   * `project*` と違い遮蔽は見ず、投影そのものを問う(「+Zが画面で上か」は遮蔽と無関係に決まる)。
   */
  projectPoint(point: readonly [number, number, number]): { x: number; y: number } | null
  /** スケッチ平面上の (u,v) の画面座標。実機検査用。 */
  projectSketchPoint(sketchId: string, u: number, v: number): { x: number; y: number } | null
  /**
   * 基準平面の上の点の画面座標(無理なら null)。実機検査用。
   * 返す前に、その画素からのレイが実際にその平面へ当たり、かつ手前に面が無いことを確かめる。
   * `showBasePlanes(true)` の間しか返らない。
   */
  projectBasePlane(name: BasePlaneName): { x: number; y: number } | null
  /**
   * 領域の画面座標(実機検査用)。今その領域が指せる状態でなければ null
   * (返す前にクリックと同じ判定 `resolveHit` でその領域が取れることを確かめる)。
   */
  projectRegion(sketchId: string, index: number): { x: number; y: number } | null
  /**
   * 見えている立体だけを撮ったファイル一覧のサムネイル(PNGのbase64。`thumbnail.ts`)。
   * 見えている立体が無ければ空文字。
   */
  captureThumbnail(): string
  dispose(): void
}
