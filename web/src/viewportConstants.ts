/** ビューポートのモジュール定数(色・層番号・画角・当たり判定の画素数)と純粋ヘルパー。 */
import * as THREE from 'three'

import type { BodyMesh } from './bodyMesh'
import type { BasePlaneName } from './session'

/**
 * ワールドの上方向 —— X=右 / Y=奥 / Z=上の右手系(カーネル OCCT/build123d も Z-up で、
 * `_BASE_PLANE_AXES` の "XY" の法線も `(0,0,1)`)。
 * `(0,0,1)` をこのファイルの他の場所へ直書きしない: up軸の値が散ると片方が取り残される
 * (`camera.up.set(0, 1, 0)` の直書きが残り、スケッチに1回入って出るだけで Y-up へ戻っていた)。
 */
export const WORLD_UP = new THREE.Vector3(0, 0, 1)

/**
 * three.js の既定の上方向そのものを、モジュールの読み込み時に差し替える。
 * `camera.up` を後から書き換える形では効かない箇所が2つあるので、カメラ・OrbitControls・ライトを
 * 1つも `new` する前にここで確定させる(three 0.185.1 の実物で確認):
 *
 * - `OrbitControls` はコンストラクタで `_quat` に生成時の `object.up` を焼き込み、
 *   `update()` で再計算しない。Y のままだと、見た目の上方向は正しいのに
 *   水平ドラッグの回転軸だけが Y軸まわりのまま静かに残る。
 * - `HemisphereLight` はコンストラクタで `position` に `DEFAULT_UP` を複写し、
 *   その position が空(sky)の向きになる。Y のままだと天地が横倒しになる。
 */
THREE.Object3D.DEFAULT_UP.copy(WORLD_UP)

export const BASE_COLOR = 0x9aa6b2
export const HIGHLIGHT_COLOR = 0xffb454
/** スケッチの線・確定前の線・領域。ソリッドの色と混ざらない色にする。 */
export const SKETCH_COLOR = 0xe8eef6
export const SKETCH_GHOST_COLOR = 0x7ee081
/** 確定すると拒まれる形の幽霊(`envelope_regions` の分裂)。選択の橙と色相で離す。 */
export const SKETCH_GHOST_WARN_COLOR = 0xff5c5c
/** スケッチの点マーカー(未選択)。 */
export const SKETCH_HANDLE_COLOR = 0x9fb3c8
/** 選択中のスケッチの点・辺。選択の色は1つ(面・辺・領域と同じ `HIGHLIGHT_COLOR`)。 */
export const SKETCH_SELECTED_COLOR = HIGHLIGHT_COLOR
export const REGION_COLOR = 0x4d7ea8
export const REGION_SELECTED_COLOR = 0xffb454
/** 確定前の形。半透明で「まだ無いもの」に見せる。 */
export const PREVIEW_COLOR = 0x7ee081
/**
 * 形状最適化(第3のモード)の役割色。選択が勝つ: 選ばれているあいだは
 * `HIGHLIGHT_COLOR` になり、役割色は一時的に見えなくなる(`applyFaceMaterials` が実体)。
 */
export const OPTIMIZE_PRESERVE_COLOR = 0x3f9b58
export const OPTIMIZE_EXCLUDE_COLOR = 0xc75448
/** 拘束面(青系に塗る)。 */
export const OPTIMIZE_FIXTURE_COLOR = 0x3f7fc2
/** 拘束の固定記号(四角錐)と、荷重の矢印・ラベルの色。 */
export const OPTIMIZE_FIXTURE_MARK_COLOR = 0x1c3550
export const OPTIMIZE_LOAD_COLOR = 0xffc857
/** 測定(issue #10)の線・端点・札。 */
export const MEASURE_COLOR = 0xff6ad5
export const MEASURE_TEXT_COLOR = '#ff8ae0'
/** 干渉チェック(`interferenceLayer.ts`)。重なりの立体は赤、接触(共有面・辺・点)は橙。 */
export const INTERFERENCE_OVERLAP_COLOR = 0xff3b30
export const INTERFERENCE_CONTACT_COLOR = 0xffa21f
/**
 * 開発用のデバッグ表示の色(`?debug=space` / `?debug=anchors` / `?voxel=1`)。
 * `shoot-topopt.mjs` と同じ見え方を保つので、勝手に動かさないこと。
 */
export const DEBUG_SPACE_COLOR = 0x39445e
export const DEBUG_RHO_COLOR = 0xbfc7d5
export const DEBUG_ANCHOR_FIXED_COLOR = 0x2ee6a8
export const DEBUG_ANCHOR_LOAD_COLOR = 0xff5c2e
/** 面の境界線。 */
export const EDGE_COLOR = 0x3f4a56
/** 選べる辺。面の境界線(`EDGE_COLOR`)と見分けがつく明るさにする。 */
export const PICK_EDGE_COLOR = 0xbfd4ea
/**
 * 辺の当たり判定の太さ[CSS px]。画面上の距離で判定する(`screenPick.ts` / `viewport.ts` の
 * `pickLinesAt`)ので、ズーム・画角・投影の切替で掴みやすさが変わらない。
 * `SKETCH_HANDLE_PIXELS`・`VERTEX_PIXELS` と同じ物差しの上にあり、大小関係がそのまま優先順位になる。
 */
export const EDGE_PICK_PIXELS = 6
/**
 * スケッチの点マーカー・角・交点・中点(共通)の当たり半径[CSS px]。
 * 点・角・交点は辺より優先度が高い(`sketchEntityAt` が点→角/交点→中点→辺の順で試す)ので、
 * 辺の当たり半径より大きくても辺の掴みやすさは壊れない。
 */
export const SKETCH_HANDLE_PIXELS = 7
/** スケッチ線の太さ[CSS px]。 */
export const SKETCH_LINE_WIDTH_PX = 1.5
/** スケッチ点入力待ちの十字(未スナップ)。 */
export const SKETCH_CURSOR_CROSS_COLOR = '#ffc848'
/** スケッチ点入力待ちのスナップ強調(線・点・格子)。 */
export const SKETCH_CURSOR_SNAP_COLOR = '#5fd97a'
/** ウィンドウ選択と見なすドラッグの最小距離[画素]。`CLICK_MOVE_LIMIT_PX` と同じ値にして、クリックでもドラッグでもない画素を作らない。 */
export const BOX_SELECT_MIN_PX = 6
/** 頂点マーカーの半径[CSS px]。`EDGE_PICK_PIXELS` より細くする(太いと、辺を狙うクリックが端点のマーカーに吸われる)。 */
export const VERTEX_PIXELS = 4
/**
 * 同じ細かさのソリッドの的とスケッチの的(辺とスケッチ線、頂点とスケッチの点)が「重なって見える」とみなす画面距離の差
 * [CSS px]。差がこれ以内ならソリッドが勝ち、離れていれば近い方が勝つ(`screenPick.ts` の `rankByOverlap`)。
 * 押し出した元のスケッチの線は押し出した辺とぴったり重なる(差 0)ので、線の太さ程度あれば足りる。
 */
export const PICK_OVERLAP_PIXELS = 2
/**
 * 矩形選択で線(辺・スケッチの線)が見えているかを見る間隔[CSS px]。当たり半径(`EDGE_PICK_PIXELS`)と同じ:
 * それより短く見えている部分は、クリックでもほぼ狙えない。
 */
export const BOX_SEEN_STEP_PX = 6
export const BACKGROUND_COLOR = 0x161a1f
/**
 * 平面グリッドの格子線。刻みは 1/10/100mm の3段固定(`gridScale.ts` の `TIERS`)で、
 * 内容の大きさでも視点でも変わらない(変わるのは段ごとの不透明度だけ)。描くのは `planeGrid.ts` の
 * 無限グリッド。`root` には入れない(初回フレーミングがグリッドの箱に引っ張られるのを避ける)。
 */
export const GRID_COLOR = 0x3a4450
/** スケッチのコマンドを起動している間のグリッド。 */
export const GRID_ARMED_COLOR = 0x7a90ab

/**
 * 基準3平面の解析的な定義(描いているグリッドとは別物)。ピック範囲は ±`gridExtent`(可変)。
 * 3枚とも原点を通るので `Plane` の `constant` は 0 でよい。
 *
 * u/v はコアの `_BASE_PLANE_AXES`(`duocad/kernel.py`)と同じ取り方: スケッチのframeが
 * どの基準平面のものかは、この表との一致で判定する(`basePlaneForFrame`)。法線の符号だけは
 * コアと違う(XZがコアでは -Y)が、解析交差にも一致判定にも効かない。
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
 * u も v も 0 にしない: どちらかが0の点は2枚に同時に乗り(XY平面上の (60, 0) は XZ 平面の上でもある)、
 * レイ距離が同じ2枚のどちらが取れるかが並び順で決まってしまう。
 * 座標は固定でよい: 最大85で、`gridExtent` は `BASE_EXTENT`(=100)を下限に広がる方向にしか動かない。
 */
export const BASE_PLANE_PROBES: ReadonlyArray<readonly [number, number]> = [
  [30, 30], [-30, 30], [30, -30], [-30, -30],
  [60, 60], [-60, 60], [60, -60], [-60, -60],
  [15, 45], [-45, 15], [45, -15], [-15, -45],
  [85, 85], [-85, -85], [10, 10], [-10, -10],
]
/** 軸の色(X赤 / Y緑 / Z青)。 */
export const AXIS_X_COLOR = 0xc45c5c
export const AXIS_Y_COLOR = 0x5cad6e
export const AXIS_Z_COLOR = 0x5a8ec4

/**
 * 基準平面ごとの「面内の2軸」と「面外の軸」。面内の軸は平面グリッドのシェーダーが描き、
 * 面外の軸だけ線ジオメトリで描く(`planeGrid.ts` は平面の中しか知らないため)。
 * 3平面を同時に出すときに同じ世界軸を二重に描かない割り振りは `desiredGridPlanes` が持つ。
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
 * 常設の原点ギズモ(`originGizmo.ts`)の寸法[mm]。正の方向だけを描く。
 * 軸線・平面とも原点から `GAP` ぶん離れた位置から始まる。
 */
export const ORIGIN_GIZMO_AXIS_LENGTH_MM = 40
export const ORIGIN_GIZMO_AXIS_GAP_MM = 4
export const ORIGIN_GIZMO_PLANE_REACH_MM = 32
export const ORIGIN_GIZMO_PLANE_GAP_MM = 8
export const ORIGIN_GIZMO_POINT_RADIUS_MM = 1.4
export const ORIGIN_GIZMO_PLANE_OPACITY = 0.12
/** 原点の点・3平面の色(中立なグレー)。 */
export const ORIGIN_GIZMO_NEUTRAL_COLOR = 0xc4c9d0

// ------------------------------------------------------------------ 断面表示(`sectionView.ts`)
/** 切り口(フタ)の色。 */
export const SECTION_CAP_COLOR = 0x6b7682
/** 平面の枠線の色(原点ギズモと同じ中立グレー)。 */
export const SECTION_FRAME_COLOR = ORIGIN_GIZMO_NEUTRAL_COLOR
/** ハンドル(矢印)の色。ホバー中・ドラッグ中は `HOVER_LIGHTEN` だけ明るくする。 */
export const SECTION_HANDLE_COLOR = 0xaeb8c4
/** 枠と切り口が内容の外接箱からはみ出す量(平面内の長い方の辺に対する比)。 */
export const SECTION_FRAME_MARGIN_RATIO = 0.08
/** 同・最小のはみ出し[mm]。 */
export const SECTION_FRAME_MARGIN_MIN_MM = 2
/**
 * ハンドル(矢印)の画面上の寸法[px]。`worldPerPixel` を掛けて毎フレーム世界寸法へ直す。
 * `pickRadius` は当たり判定だけの筒の半径。
 */
export const SECTION_HANDLE = {
  shaftLength: 70,
  shaftRadius: 2,
  coneLength: 18,
  coneRadius: 7,
  pickRadius: 10,
} as const

/**
 * 描く順序の層番号。生の数値をコード中に書かない。
 * 負の値は装飾(B群。深度検査あり・深度書き込み無し)、0以上はA群
 * (ピック対象は `depthTest: false` なので、順序がそのまま前後関係になる)。
 */
export const LAYER = {
  planeGrid: -3,
  gridAxisLine: -2,
  basePlaneHover: -1,
  /** 常設の原点ギズモ(`originGizmo.ts`)。 */
  originGizmo: -4,
  /** 断面の平面の枠(`sectionView.ts`)。 */
  sectionFrame: -5,
  solid: 0,
  optimize: 1,
  /** 開発用のデバッグ表示。注記より手前、辺より奥。 */
  debug: 2,
  /** 断面(`sectionView.ts`)。ステンシルの偶奇を書く判定メッシュ → 切り口の板の順で、板がソリッドの後に描かれることが要る。 */
  sectionStencil: 3,
  sectionCap: 4,
  edge: 10,
  region: 11,
  sketchLine: 12,
  sketchPoint: 13,
  /** 測定の線と札(`measureAnnotation.ts`)。深度を書かないので、描く順序で全部の手前に出す。 */
  measure: 14,
  /** 断面のハンドル(矢印)。測定の札より手前に置く(見えるもの＝指せるもの。札に隠れたハンドルは掴めない)。 */
  sectionHandle: 15,
} as const

/**
 * B群の深度バイアスの単位量(その点の奥行きでの画面の高さに対する比)。視線方向へ
 * 「層番号 × これ × 画面の高さ[mm]」だけカメラ側へ寄せた深度専用コピーを射影し直す。
 * 色・UV・ピック判定に使うジオメトリ自体は動かさない。
 *
 * クリップ空間の z を直接ずらす方式(`gl_Position.z -= bias * w`)は採らない: 一定バイアスが
 * 表す世界空間の距離は、パースの非線形写像により視線距離の2乗に比例して暴れる
 * (near=0.1・far=10000 で距離1000mmのとき約500mm相当。手前のソリッドをグリッドが追い越す)。
 * 正投影は写像が線形なので暴れず、症状は非正投影のときだけ出る。
 * 世界空間の一定量(mm)も採らない: 寄ると画面上で何十画素にもなり、床に立つソリッドの手前の縁から
 * 奥の隠れるはずの格子線がはみ出して見える。
 *
 * 0.001(画面の高さの1/1000 ≒ 1画素)の見積もり: 深度の分解能(24bit)は注視点距離 D で
 * 約 D²/(near·2²⁴)、near=D/1000(`viewport.ts` の近接面)なら 6e-5·D。画角30度の画面の
 * 高さの1/1000は 5.4e-4·D で約9倍の余裕があり、共平面の面には確実に勝つ。
 * 1層あたりの世界空間の量は、画角30度・奥行き1000mmで約0.5mm。
 */
export const DEPTH_BIAS_UNIT = 0.001

/**
 * B群の層番号(大きいほどカメラ側)。`sketchFaceGrid` は別枠: 任意面のスケッチ平面は
 * 貼り付けた面(A群のソリッド。深度書き込みあり)と完全に同一平面で争うので、
 * 床・基準平面グリッドと同じバイアス経路を使いつつ層番号だけ分ける。
 */
export const DEPTH_BIAS_LAYER = {
  planeGrid: 1,
  gridAxisLine: 2,
  basePlaneHover: 3,
  sketchFaceGrid: 4,
  /** 常設の原点ギズモ。既定表示のXY床(`planeGrid`)と同じ平面に乗ることがある。 */
  originGizmo: 5,
  /** 断面の枠。原点を通る断面では床のグリッド・原点ギズモの平面と同じ平面に乗る。 */
  sectionFrame: 6,
  /**
   * 編集していないスケッチの線・領域(立体の裏に回ったら隠れる。docs/DESIGN_DECISIONS.md「見えるものはすべてさわれる」)。
   * 面に貼ったスケッチ・押し出した元のスケッチは面と厳密に同一平面で、寄せないと面と奥行きを争う。
   * **遮蔽の許し(`occlusion.ts` の `slack`)より小さく保つ**: 寄せた量より薄い板の裏の線は板の手前に描かれるので、
   * 許しが寄せた量より狭いと「見えているのに指せない」になる(`slack` はこの量 + 1画素を下限に持つ)。
   */
  sketch: 1,
} as const

/**
 * カメラの画角[度]の下限・上限・既定。UIとlocalStorage(`viewSettings.ts`)もここを import する。
 *
 * 90を超えると端の歪みが強すぎて形の判断に使えない。下限が30なのは、そこから下に欲しいのが
 * 「弱いパース」ではなく正投影そのものだから: 画角を絞って正投影を真似ると距離補正でカメラが
 * 際限なく遠ざかり(30度で既に注視点距離が3.7倍)、深度の精度を捨てるわりに平行投影にはならない
 * (手前と奥の見かけの比は画角ではなく距離で決まる)。1〜29は選べる値に無く、
 * 0(`FOV_ORTHO`)は近似ではなく本物の `OrthographicCamera` へ切り替える。
 */
export const FOV_MIN = 30
export const FOV_MAX = 90
export const FOV_DEFAULT = 30

/**
 * 完全正投影を表す画角。「とても小さい画角」ではなく、`PerspectiveCamera` の代わりに
 * `OrthographicCamera` が使われる。
 */
export const FOV_ORTHO = 0

/** 選べる画角か(0ちょうど、または30〜90の2択)。UI(`SettingsDialog.svelte`)と `viewSettings.ts` が呼ぶ。 */
export function isSelectableFov(value: number): boolean {
  return Number.isFinite(value) && (value === FOV_ORTHO || (value >= FOV_MIN && value <= FOV_MAX))
}

/** `lookAtSketch` が図形の外接半径に掛ける余白(45度で従来の `radius * 3` と一致: `1.243/tan(22.5°) ≈ 3.00`)。 */
export const SKETCH_FIT_MARGIN = 1.243

/**
 * 既定の視点の向き(front-top-right)。距離に掛けて注視点へ足すベクトルで、長さは1ではなく
 * 1.5362(= `hypot(1, 1, 0.6)`)。初期視点と `frameToContent` が使う。
 * `frameToContent` は `fitDistance` の 1.5362倍の位置にカメラを置くので、収まるぎりぎりより
 * 一回り引いた絵になる。この余白は既定の見え方そのものなので直さない(直すと全画角で見かけが1.54倍になる)。
 * 正投影は視錐台で大きさを作るため、この係数を掛けないと透視と揃わない。
 * Y が負なのは Y=奥 のため。Fusion / SolidWorks の既定アイソメと同じ front(−Y) / top(+Z) / right(+X) の側から見る。
 */
export const HOME_VIEW_OFFSET = new THREE.Vector3(1, -1, 0.6)

/** クリックとドラッグ(視点回転)の境目。これ以上動いたら選択ではなく回転とみなす。 */
export const CLICK_MOVE_LIMIT_PX = 6
export const CLICK_TIME_LIMIT_MS = 500

/**
 * ホバー強調で上げる明度(HSLのL)。3色目は足さない: 色相=選択されているか(橙)、
 * 明度=今カーソルの下にあるか、の2軸にする。
 */
export const HOVER_LIGHTEN = 0.16
/** ホバー中の基準平面に重ねる板の濃さ。 */
export const BASE_PLANE_HOVER_OPACITY = 0.14

/** 色相・彩度はそのままに明度だけ上げる(`HOVER_LIGHTEN` の実体)。 */
export function lighten(hex: number, amount: number): number {
  const color = new THREE.Color(hex)
  const hsl = { h: 0, s: 0, l: 0 }
  color.getHSL(hsl)
  color.setHSL(hsl.h, hsl.s, Math.min(1, hsl.l + amount))
  return color.getHex()
}

/** 1個のBodyが画面に持つもの(全面をまとめた三角形メッシュ1つ+境界線1つ。`bodyMesh.ts`)。 */
export interface BodyMeshes {
  body: BodyMesh
  /** `root` に足した全オブジェクト(面メッシュ+境界線)。clearBody はこれを取り除く
   * (面メッシュだけを外すと境界線がシーンに残り、dispose済みジオメトリを参照するゴースト線になる)。 */
  sceneObjects: THREE.Object3D[]
}

/**
 * B群(装飾。ピック対象でない)の深度バイアスを材質へ仕込む。視点空間(`mvPosition`。射影より前)で
 * `層番号 × DEPTH_BIAS_UNIT × その奥行きでの画面の高さ` だけカメラ側へ寄せてから射影し直す。
 * 画面の高さは透視なら `2·奥行き / projectionMatrix[1][1]`、正投影なら `2 / projectionMatrix[1][1]`。
 *
 * 透視では視点の z 軸ではなく、カメラへ向かう視線に沿って寄せる: z だけ減らすと画面中心から
 * 外れた頂点は外側へ写り、寄って見るほど線がシェーダーで描くグリッドの軸から横へずれる
 * (`planeGrid.ts` の `hit - viewDir * uDepthBias` と同じ寄せ方)。
 * `transformed`(ローカル座標)は動かさないので、描画のズレがレイ検査へ漏れない。
 *
 * `customProgramCacheKey` を層ごとに変える: 変えないと、層の違うプログラムが1つに共有されて
 * 後から作った方のバイアスが効かない。
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
