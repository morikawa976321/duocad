/** ビューポートのモジュール定数(色・層番号・画角・当たり判定の画素数)と純粋ヘルパー。 */
import * as THREE from 'three'

import type { BasePlaneName } from './session'

/**
 * ワールドの上方向 —— **X=右 / Y=奥 / Z=上の右手系**(Fusion 360・Blender・スライサと
 * 同じ、CADの標準)。カーネル(OCCT/build123d)は元から Z-up で、`_BASE_PLANE_AXES` の
 * "XY" の法線も `(0,0,1)` である。
 *
 * **`(0,0,1)` をこのファイルの他の場所へ直書きしないこと。** up軸の値が2か所以上に散ると
 * 次の改修で必ず片方が取り残される —— 直前まで `camera.up.set(0, 1, 0)` の直書きが2か所
 * あり、**スケッチに1回入って出るだけで Y-up へ戻っていた**。
 */
export const WORLD_UP = new THREE.Vector3(0, 0, 1)

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

export const BASE_COLOR = 0x9aa6b2
export const HIGHLIGHT_COLOR = 0xffb454
/** スケッチの線・確定前の線・領域。ソリッドの色と混ざらない色にする。 */
export const SKETCH_COLOR = 0xe8eef6
export const SKETCH_GHOST_COLOR = 0x7ee081
/** 確定すると拒まれる形の幽霊(`envelope_regions` の分裂)。選択の橙と色相で離す。 */
export const SKETCH_GHOST_WARN_COLOR = 0xff5c5c
//: スケッチの点マーカー(未選択)。線(`SKETCH_COLOR`)より暗くして線を主役に保つ。
export const SKETCH_HANDLE_COLOR = 0x9fb3c8
//: 選択中のスケッチの点・辺。**選択の色は1つ**(面・辺・領域と同じ `HIGHLIGHT_COLOR`)。
export const SKETCH_SELECTED_COLOR = HIGHLIGHT_COLOR
export const REGION_COLOR = 0x4d7ea8
export const REGION_SELECTED_COLOR = 0xffb454
/** 確定前の形。半透明で「まだ無いもの」に見せる。 */
export const PREVIEW_COLOR = 0x7ee081
/**
 * 形状最適化(第3のモード)の役割色。**選択が勝つ**(本人決定) —— 選ばれているあいだは
 * `HIGHLIGHT_COLOR` になり、役割色は一時的に見えなくなる
 * (`applyFaceMaterials` が実体。役割は `state !== 'plain'` のとき無視する)。
 */
export const OPTIMIZE_PRESERVE_COLOR = 0x3f9b58
export const OPTIMIZE_EXCLUDE_COLOR = 0xc75448
/** 拘束面(青系に塗る)。 */
export const OPTIMIZE_FIXTURE_COLOR = 0x3f7fc2
/** 拘束の固定記号(四角錐)と、荷重の矢印・ラベルの色。面の塗りとは別に見分ける。 */
export const OPTIMIZE_FIXTURE_MARK_COLOR = 0x1c3550
export const OPTIMIZE_LOAD_COLOR = 0xffc857
/** 測定(issue #10)の線・端点・札。選択(橙)・幽霊(緑)・荷重(黄)のどれとも
    見分けがつく色にしてある(同時に画面に出うる)。 */
export const MEASURE_COLOR = 0xff6ad5
export const MEASURE_TEXT_COLOR = '#ff8ae0'
/**
 * **開発用のデバッグ表示の色**(`?debug=space` / `?debug=anchors` / `?voxel=1`)。
 * `shoot-topopt.mjs` と同じ見え方を保つので、勝手に動かさないこと。
 */
export const DEBUG_SPACE_COLOR = 0x39445e
export const DEBUG_RHO_COLOR = 0xbfc7d5
export const DEBUG_ANCHOR_FIXED_COLOR = 0x2ee6a8
export const DEBUG_ANCHOR_LOAD_COLOR = 0xff5c2e
/** 面の境界線。背景(`BACKGROUND_COLOR`)に埋もれないよう`GRID_COLOR`と同程度の明るさに
    してある。`PICK_EDGE_COLOR`とは見分けがつく明るさを保つ(下記コメント)。 */
export const EDGE_COLOR = 0x3f4a56
/** 選べる辺。面の境界線(`EDGE_COLOR`)と**見分けがつく明るさ**にする。 */
export const PICK_EDGE_COLOR = 0xbfd4ea
/**
 * 辺の当たり判定の太さ[CSS px]。細すぎると掴めず、太すぎると隣の辺が取れる。
 *
 * **画面上の距離で判定する**(`screenPick.ts` / `viewport.ts` の `pickLinesAt`)ので、
 * ズーム・画角・投影の切替で掴みやすさが変わらない。この3つの定数は同じ物差しの上にあり、
 * 大小関係がそのまま優先順位になる(点 < 辺、頂点 < 辺)。
 */
export const EDGE_PICK_PIXELS = 6
//: スケッチの点マーカー・角・交点・中点(共通)の当たり半径[CSS px]。
//: 点(と角・交点)は辺より優先度が高い(`sketchEntityAt`が点→角/交点→中点→辺の順で試す)ので、
//: 辺の当たり半径(EDGE_PICK_PIXELS)より大きくても辺の掴みやすさが壊れるわけではない。
//: 2026-09-11に本人希望で5→7へ拡大(元は「辺より小さく」だった)。
export const SKETCH_HANDLE_PIXELS = 7
/** スケッチ線の太さ[CSS px]。`planeGrid` の最太段(半幅1.1px)より少し太く、格子と重なっても見える程度。 */
export const SKETCH_LINE_WIDTH_PX = 1.5
/** スケッチ点入力待ちの十字(未スナップ)。暗背景で視認できる黄。 */
export const SKETCH_CURSOR_CROSS_COLOR = '#ffc848'
/** スケッチ点入力待ちのスナップ強調(線・点・格子)。幽霊線と同系の緑。 */
export const SKETCH_CURSOR_SNAP_COLOR = '#5fd97a'
//: ウィンドウ選択と見なすドラッグの最小距離[画素]。**クリックの上限と同じ値**にして、
//: 「クリックでもドラッグでもない画素」が生まれないようにする(下の `CLICK_MOVE_LIMIT_PX`)。
export const BOX_SELECT_MIN_PX = 6
/** 頂点マーカーの半径[CSS px]。辺の当たり判定(`EDGE_PICK_PIXELS`)より細くする ——
    太いと、辺を狙うクリックが端点のマーカーに吸われる(頂点は必ず辺の端に乗っている)。 */
export const VERTEX_PIXELS = 4
export const BACKGROUND_COLOR = 0x161a1f
/**
 * 平面グリッドの格子線。単位は mm。**刻みは 1/10/100mm の3段固定**(`gridScale.ts` の
 * `TIERS`)で、内容の大きさでも視点でも変わらない —— 変わるのは段ごとの不透明度だけで、
 * 線の太さがそのまま寸法の意味になる。描くのは `planeGrid.ts` の
 * 無限グリッドで、範囲という概念を持たない。`root` には入れない(初回フレーミングが
 * グリッドの箱に引っ張られるのを避ける)。
 */
export const GRID_COLOR = 0x3a4450
/**
 * スケッチのコマンドを起動している間のグリッド。**「今これが指せる」が分かればよく、
 * 見た目は作り込まない**(当たり判定の感触と見た目は後日ユーザーが対話で詰める)。
 */
export const GRID_ARMED_COLOR = 0x7a90ab

/**
 * 基準3平面の解析的な定義(**描いているグリッドとは別物**)。ピック範囲は ±`gridExtent`
 * (可変)。3枚とも原点を通るので three.js の `Plane` の `constant` は 0 でよい。
 *
 * **u/v はコアの `_BASE_PLANE_AXES`(`duocad/kernel.py`)と同じ取り方**にしてある ——
 * スケッチのframeがどの基準平面のものかは、この表との一致で判定する
 * (`basePlaneForFrame`)。法線の符号だけはコアと違う(XZがコアでは -Y)が、
 * 解析交差にも一致判定にも符号は効かないので揃えていない。
 */
export const BASE_PLANES: ReadonlyArray<{
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
export const BASE_PLANE_PROBES: ReadonlyArray<readonly [number, number]> = [
  [30, 30], [-30, 30], [30, -30], [-30, -30],
  [60, 60], [-60, 60], [60, -60], [-60, -60],
  [15, 45], [-45, 15], [45, -15], [-15, -45],
  [85, 85], [-85, -85], [10, 10], [-10, -10],
]
/** 軸は色で見分ける(X赤 / Y緑 / Z青)。グリッド本体より少し明るく、選択色とは被らない。 */
export const AXIS_X_COLOR = 0xc45c5c
export const AXIS_Y_COLOR = 0x5cad6e
export const AXIS_Z_COLOR = 0x5a8ec4

/**
 * 基準平面ごとの「面内の2軸」と「面外の軸」。**面内の軸は平面グリッドのシェーダーが描き、
 * 面外の軸だけ線ジオメトリで描く**(`planeGrid.ts` は平面の中しか知らないため)。
 *
 * 3平面を同時に出すとき(スケッチの対象待ち)は、同じ世界軸を2枚が二重に描かないよう
 * `desiredGridPlanes` が面内の軸を割り振る —— この表はあくまで「その平面の u/v は
 * どの軸か」を持つだけである。
 */
export const BASE_PLANE_AXES: Record<
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
 * 常設の原点ギズモ(`originGizmo.ts`)の寸法[mm]。**正の方向だけ**を描く(本人指定
 * 2026-09-13) —— 軸線・平面とも原点からではなく、それぞれ `GAP` ぶん離れた位置から
 * 始まる(原点の点・軸線・平面が互いに重ならないための余白)。`PLANE_REACH` を
 * `AXIS_LENGTH` より短くしてあるのは、軸の先端が平面の角より外へ突き出て見えるようにする
 * ため(重なった装飾どうしの前後関係を目で追えるようにする)。
 */
export const ORIGIN_GIZMO_AXIS_LENGTH_MM = 40
export const ORIGIN_GIZMO_AXIS_GAP_MM = 4
export const ORIGIN_GIZMO_PLANE_REACH_MM = 32
export const ORIGIN_GIZMO_PLANE_GAP_MM = 8
export const ORIGIN_GIZMO_POINT_RADIUS_MM = 1.4
export const ORIGIN_GIZMO_PLANE_OPACITY = 0.12
/** 原点の点・3平面の色(軸のような意味を持たないので中立なグレー)。 */
export const ORIGIN_GIZMO_NEUTRAL_COLOR = 0xc4c9d0

// ------------------------------------------------------------------ 断面表示(`sectionView.ts`)
/** 切り口(フタ)の色。ボディの基本色より暗くして「切った面」と読めるようにする。 */
export const SECTION_CAP_COLOR = 0x6b7682
/** 平面の枠線の色(原点ギズモと同じ中立グレー)。 */
export const SECTION_FRAME_COLOR = ORIGIN_GIZMO_NEUTRAL_COLOR
/** ハンドル(矢印)の色。ホバー中・ドラッグ中は `HOVER_LIGHTEN` だけ明るくする。 */
export const SECTION_HANDLE_COLOR = 0xaeb8c4
/** 枠と切り口が内容の外接箱からはみ出す量(平面内の長い方の辺に対する比)。 */
export const SECTION_FRAME_MARGIN_RATIO = 0.08
/** 同・最小のはみ出し[mm](薄い内容でも枠が縁に張り付かない)。 */
export const SECTION_FRAME_MARGIN_MIN_MM = 2
/**
 * ハンドル(矢印)の画面上の寸法[px]。`worldPerPixel` を掛けて毎フレーム世界寸法へ直すので、
 * ズームしても見かけの大きさは変わらない。`pickRadius` は当たり判定だけの筒の半径。
 */
export const SECTION_HANDLE = {
  shaftLength: 70,
  shaftRadius: 2,
  coneLength: 18,
  coneRadius: 7,
  pickRadius: 10,
} as const

/**
 * **描く順序の層番号(1箇所にまとめる)。** 生の数値をコード中に書かない
 * (併記事項)。
 *
 * 負の値は装飾(B群。深度検査あり・深度書き込み無し)、0以上は既存の階段
 * (A群＝ピック対象は `depthTest: false` なので、順序がそのまま前後関係になる)。
 */
export const LAYER = {
  planeGrid: -3,
  gridAxisLine: -2,
  basePlaneHover: -1,
  /** 常設の原点ギズモ(`originGizmo.ts`)。`gridRoot` には入れない1個のGroupなので層は1つでよい。 */
  originGizmo: -4,
  /** 断面の平面の枠(`sectionView.ts`)。指せない装飾なのでB群。 */
  sectionFrame: -5,
  solid: 0,
  optimize: 1,
  /** 開発用のデバッグ表示(既定では何も描かない)。注記より手前、辺より奥。 */
  debug: 2,
  /**
   * 断面(`sectionView.ts`)。ステンシルの偶奇を書く判定メッシュ → 切り口の板(C群と同じく
   * 深度検査あり・書き込みあり)の順で、板がソリッドの後に描かれることが要る。
   */
  sectionStencil: 3,
  sectionCap: 4,
  edge: 10,
  region: 11,
  sketchLine: 12,
  sketchPoint: 13,
  /** 測定の線と札(`measureAnnotation.ts`)。深度を書かないので、描く順序で全部の手前に出す。 */
  measure: 14,
  /**
   * 断面のハンドル(矢印)。ポインタを最優先で引き取るので、描くのも最前 —— 測定の札より手前に
   * 置くのは「見えるもの＝指せるもの」のため(札に隠れたハンドルは掴めない)。
   */
  sectionHandle: 15,
} as const

/**
 * **B群の深度バイアスの単位量**(**その点の奥行きでの画面の高さ**に対する比。視線方向へ
 * 「層番号 × これ × 画面の高さ[mm]」だけカメラ側へ寄せた深度専用コピーを射影し直す。
 * 色・UV・ピック判定に使うジオメトリ自体は動かさない)。
 *
 * **クリップ空間の z を直接ずらす方式(`gl_Position.z -= bias * w`)は選ばない** ——
 * その一定バイアスが表す世界空間の距離は、パースの非線形写像により視線距離の2乗に
 * 比例して暴れる。near=0.1・far=10000の実スケールでは距離1000mmで約500mm相当まで
 * 膨らみ、手前にあるべきソリッドをグリッドが追い越して見える。正投影は元々この写像が
 * 線形なので暴れず、症状は非正投影のときにだけ出る。
 *
 * **世界空間の一定量(mm)にもしない** —— 寄って見ると一定量が画面上で何十画素にもなり、
 * 床に立つソリッドの手前の縁から、奥にあって隠れるはずの格子線が「寄せた量」の分だけ
 * はみ出して見える。画面の高さに比例させれば、はみ出しはどの距離でも
 * 「層番号 × これ × 画面の画素数」の半分以下に収まる。
 *
 * 0.001(画面の高さの1/1000 ≒ 1画素)の見積もり: 深度の分解能(24bit)は注視点距離 D で
 * 約 D²/(near·2²⁴)、near=D/1000(`viewport.ts` の近接面)なら 6e-5·D。画角30度の画面の
 * 高さの1/1000は 5.4e-4·D で約9倍の余裕があり、共平面の面には確実に勝つ。
 * 1層あたりの世界空間の量は、画角30度・奥行き1000mmで約0.5mm。
 */
export const DEPTH_BIAS_UNIT = 0.001

/**
 * B群の層番号(大きいほどカメラ側)。**`sketchFaceGrid` は別枠** ——
 * 任意面のスケッチ平面は貼り付けた面(A群のソリッド。深度書き込みあり)と完全に同一平面で
 * 争うので、床・基準平面グリッド(`planeGrid`。通常は開いた空間に浮いていて争う相手がいない)
 * と同じバイアス経路を使いつつ、層番号だけ分けて併記事項の規約(層は名前付き定数表)に従う。
 */
export const DEPTH_BIAS_LAYER = {
  planeGrid: 1,
  gridAxisLine: 2,
  basePlaneHover: 3,
  sketchFaceGrid: 4,
  /** 常設の原点ギズモ。既定表示のXY床(`planeGrid`)と同じ平面に乗ることがあるので、確実に手前へ。 */
  originGizmo: 5,
  /** 断面の枠。原点を通る断面では床のグリッド・原点ギズモの平面と同じ平面に乗る。 */
  sectionFrame: 6,
} as const

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
export const FOV_DEFAULT = 30

/**
 * 完全正投影を表す画角。**「とても小さい画角」ではない**
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
export const SKETCH_FIT_MARGIN = 1.243

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
export const HOME_VIEW_OFFSET = new THREE.Vector3(1, -1, 0.6)

/** クリックとドラッグ(視点回転)の境目。これ以上動いたら選択ではなく回転とみなす。 */
export const CLICK_MOVE_LIMIT_PX = 6
export const CLICK_TIME_LIMIT_MS = 500

/**
 * ホバー強調で上げる明度(HSLのL)。**3色目は足さない** ——
 * 色相が「選択されているか」(橙)、明度が「今カーソルの下にあるか」を
 * 表す2軸にする。未選択にも選択済みにも同じ幅で掛けるので、規則は
 * 「カーソルの下にあるものは明るい」の1行で済む。
 */
export const HOVER_LIGHTEN = 0.16
/** ホバー中の基準平面に重ねる板の濃さ。グリッドの線が透けて見える程度に留める。 */
export const BASE_PLANE_HOVER_OPACITY = 0.14

/** 色相・彩度はそのままに明度だけ上げる(`HOVER_LIGHTEN` の実体)。 */
export function lighten(hex: number, amount: number): number {
  const color = new THREE.Color(hex)
  const hsl = { h: 0, s: 0, l: 0 }
  color.getHSL(hsl)
  color.setHSL(hsl.h, hsl.s, Math.min(1, hsl.l + amount))
  return color.getHex()
}

/** 1個のBodyが画面に持つメッシュ(面ごとの三角形メッシュ+境界線)。 */
export interface BodyMeshes {
  /** ピック判定の対象(面の三角形メッシュだけ)。 */
  faceMeshes: THREE.Mesh[]
  /** `root` に足した全オブジェクト(面メッシュ+境界線)。clearBodyで**これを**取り除く
   * (面メッシュだけを外すと境界線(LineSegments)がシーンに残り続け、dispose済みの
   * ジオメトリを参照するゴースト線としてメモリリークする)。 */
  sceneObjects: THREE.Object3D[]
  disposables: Array<{ dispose(): void }>
}

/**
 * B群(装飾。ピック対象でない)の深度バイアスを材質へ仕込む —— 視点空間(`mvPosition`。
 * 射影より前)で `層番号 × DEPTH_BIAS_UNIT × その奥行きでの画面の高さ` だけカメラ側へ
 * 寄せてから射影し直す(理由は `DEPTH_BIAS_UNIT` のdocを参照)。画面の高さは
 * 透視なら `2·奥行き / projectionMatrix[1][1]`、正投影なら `2 / projectionMatrix[1][1]`。
 *
 * **透視では視点の z 軸ではなく、カメラへ向かう視線そのものに沿って寄せる** —— z だけ
 * 減らすと画面中心から外れた頂点は外側へ写り(倍率 d/(d−bias))、寄って見るほど線が
 * シェーダーで描くグリッドの軸から横へずれる。視線沿いなら画面上の位置は変わらない
 * (`planeGrid.ts` の `hit - viewDir * uDepthBias` と同じ寄せ方)。正投影は視線が z 軸と
 * 平行なので z を減らすのと同じ。
 * **`transformed`(ローカル座標)自体は動かさない**ので、描画のズレがレイ検査
 * (`basePlaneAt` の解析交差・面のピック)へ漏れない。
 *
 * `customProgramCacheKey` を層ごとに変える —— 変えないと、同じ材質種で層の違う
 * プログラムが1つに共有されて**後から作った方のバイアスが効かない**。
 *
 * `#include <project_vertex>` が無い材質へ掛けたら例外にする(黙って素通しにしない)。
 */
export function withDepthBias<T extends THREE.Material>(material: T, layer: number): T {
  const bias = (layer * DEPTH_BIAS_UNIT).toFixed(8)
  material.onBeforeCompile = (shader) => {
    const marker = '#include <project_vertex>'
    if (!shader.vertexShader.includes(marker)) {
      throw new Error(`深度バイアスを掛けられない材質(project_vertexが無い): ${material.type}`)
    }
    shader.vertexShader = shader.vertexShader.replace(
      marker,
      `${marker}
	float depthBiasMm = ${bias} * 2.0 / projectionMatrix[1][1];
	if (isPerspectiveMatrix(projectionMatrix)) {
		depthBiasMm *= -mvPosition.z;
		mvPosition.xyz *= max(1.0 - depthBiasMm / length(mvPosition.xyz), 0.0);
	} else {
		mvPosition.z += depthBiasMm;
	}
	gl_Position = projectionMatrix * mvPosition;`,
    )
  }
  material.customProgramCacheKey = () => `duocad-depth-bias-${bias}`
  return material
}
