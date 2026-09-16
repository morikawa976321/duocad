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

/**
 * 交点探索用の幾何プリミティブ。直線区間・円・円弧の3種(`EntityKindSpec.intersectables`)。
 * 角度は度(コアの `Entity.params` と同じ単位)。
 */
export type IntersectableCurve =
  | { kind: 'segment'; a: readonly [number, number]; b: readonly [number, number] }
  | { kind: 'circle'; center: readonly [number, number]; radius: number }
  | { kind: 'arc'; center: readonly [number, number]; radius: number; startDeg: number; endDeg: number }

interface EntityKindSpec {
  /**
   * この種別を作るverb名(`add_*`)。`ghostEntity` の逆引きに使う。
   *
   * **省略できる** —— `bezier` はこれを作るverbが無い(取り込み `import_svg` だけが作り、
   * 人もAIもベジエを1本ずつは足せない)。verbが無い種別は
   * 幽霊(確定前の姿)も持たないので、`fromArgs` ではなく表ごと持たない形にしてある。
   */
  verb?: string
  /** verbの `build()` が作った引数から、コアの `Entity.params` と同じキーへ移し替える。
   *  **`verb` があるときだけ要る**(幽霊を描くのはverbで起動したコマンドだけ)。 */
  fromArgs?(args: Record<string, unknown>): Record<string, number>
  /**
   * `params` を dx/dy だけ平行移動した `params`。
   *
   * **コアの `duocad/sketch.py` の `_translate_*` と同じ規則**でなければならない ——
   * ここがずれると「プレビューと確定結果が違う場所に出る」という、いちばん困る形の
   * 不具合になる(`viewport.ts` の `faceGeometry` が本物と幽霊で共有されているのと同じ理由)。
   */
  translate(params: Record<string, number>, dx: number, dy: number): Record<string, number>
  /**
   * `params` を (cx,cy) まわりに `angleDeg` 度(反時計回り)回した姿。**種別は変わらない**
   * (コアの `duocad/sketch.py` の `EntityKindOps.rotate` と同じ)。
   *
   * `rotateCopies=false` は**向きを変えずに位置だけ**回す(中心を回して平行移動する)。
   */
  rotate(
    params: Record<string, number>,
    cx: number,
    cy: number,
    angleDeg: number,
    rotateCopies: boolean,
  ): Record<string, number>
  /** `params` を (u,v) の折れ線へ(円・円弧は分割線で近似)。コアの `Entity.params` のキーを
   *  そのまま読む(`duocad/sketch.py` の `Entity` docstring)。
   *
   *  **`resolution` は「いまの画面でどれくらい細かく折るか」**(`adaptiveResolution.ts`)。 */
  toPoints(params: Record<string, number>, resolution: ViewResolution): Array<[number, number]>
  /**
   * **動かせる点**の (xキー, yキー)。コアの `EntityKindOps.points`(`duocad/sketch.py`)と
   * 同じ並びでなければならない —— 参照が持つ `point` はこの**xキー**そのものである。
   *
   * 寸法(半径・幅高さ・角度)は入れない。点を動かしても形は変わらないが、寸法を動かすと
   * 形が変わる(寸法の編集は今回のスコープ外)。長方形は基準の角1つだけで、残り3隅は
   * `width`/`height`/`angle` からの導出値なので独立に動かせない。
   */
  pointKeys: ReadonlyArray<readonly [string, string]>
  /**
   * **`pointKeys` にない、見た目だけの角**(`viewport.ts` の `sketchCornerAt` が
   * `entityVisualCorners` 経由で読む。当たり判定・カーソルの □/× 表示の両方に効く)。
   *
   * 独立に動かせないというだけで、輪郭上の頂点であることに変わりはなく、`snap()` は
   * 隣接する辺のどちらから来ても同じ座標へ厳密に落ちる(端点へのクランプ)。それを
   * `pointKeys` と同じ精度なのに×表示にすると、長方形の基準コーナーだけが特別に見える
   * (残り3隅は `width`/`height`/`angle` の導出値というだけで、幾何的な角であることは同じ)。
   * 省略した種別(line/circle/arc/bezier)は追加の角を持たない —— 円・円弧は曲線上の
   * サンプル点を角と誤認すると線上のあちこちで□が出てしまう。
   */
  visualCorners?(params: Record<string, number>): Array<[number, number]>
  /**
   * **交点探索用の幾何プリミティブ**(直線区間・円・円弧。`sketchIntersections` が
   * 図形間の交点を探すのに使う。同じ図形の複数区間どうし(長方形の隣り合う辺)は
   * 交点探索の対象にしない —— それは自分の角で、既に `visualCorners` が持つ)。
   *
   * ベジエは持たない —— 直線・円・円弧の交差は閉じた式で解けるが、ベジエとの交点は
   * 数値的な求根が要るため今回のスコープ外。
   */
  intersectables?(params: Record<string, number>): IntersectableCurve[]
  /**
   * 与えた (u,v) をこの図形の輪郭へ落とす(スナップ)。
   *
   * **拘束ではない** —— duocadは拘束ソルバを持たない(`duocad/parameters.py` 冒頭)ので、
   * ここが返すのは「たまたま同じ座標」であり、図形を後から動かしても付いていかない。
   * `snapToSketchEntity` から**クリック(addPoint)と幽霊(hoverPoint)の両方**が呼ぶ
   * (どちらかだけ直すと、吸い付いて見える位置と確定結果がずれる)。
   *
   * `resolution` は**当たり判定(`sketchEntityAt`)が使っているのと同じ折れ線の粗さ**を
   * 渡す(呼び出し側が `viewport.viewResolution()` から読む)。射影が解析式で決まる種別は
   * 無視してよいが、bezierのように `toPoints` の近似で当たる種別はここがずれると
   * 「見えている線と落ちる場所が違う」になる。
   */
  snap(params: Record<string, number>, u: number, v: number, resolution: ViewResolution): [number, number]
}

/**
 * 円弧を折れ線へ。`start` / `end` は**度**(コアの `Entity.params` がそう持っている)、
 * 分割数の計算は**ラジアン**で行う(`adaptiveResolution.ts` の単位に合わせる)。
 *
 * **掃引の規約がコアと違う**: ここは `end - start` の符号どおりに描くが、`duocad/kernel.py` は
 * `(end_deg - start_deg) % 360` で**必ず反時計回り**に掃く(`sketch.add_arc` は角度を正規化せず
 * そのまま格納する)。`end < start` を送ると幽霊と確定結果が別の弧になるので、角度を作る側
 * (`commands/sketch.ts` の `arcFromPoints`)は常に `start_deg <= end_deg` を出すこと。
 * 時計回りの弧は始点と終点を入れ替えれば同じ弧として表せる。
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

/**
 * 点 (x,y) を中心 (cx,cy) まわりに `angleDeg` 度だけ反時計回りに回す。
 * コアの `duocad/sketch.py` の `_rotate_point` と同じ式。
 */
function rotatePoint(
  x: number,
  y: number,
  cx: number,
  cy: number,
  angleDeg: number,
): [number, number] {
  const rad = (angleDeg * Math.PI) / 180
  const cos = Math.cos(rad)
  const sin = Math.sin(rad)
  const dx = x - cx
  const dy = y - cy
  return [cx + dx * cos - dy * sin, cy + dx * sin + dy * cos]
}

/** 点 (u,v) を線分 (u0,v0)-(u1,v1) へ射影し、両端でクランプする。 */
function projectToSegment(
  u: number,
  v: number,
  u0: number,
  v0: number,
  u1: number,
  v1: number,
): [number, number] {
  const dx = u1 - u0
  const dy = v1 - v0
  const lengthSq = dx * dx + dy * dy
  if (lengthSq === 0) return [u0, v0]
  const t = Math.min(1, Math.max(0, ((u - u0) * dx + (v - v0) * dy) / lengthSq))
  return [u0 + t * dx, v0 + t * dy]
}

/** 点 (u,v) に最も近い線分を折れ線から選び、そこへ射影する(bezierのスナップ近似)。 */
function projectToPolyline(u: number, v: number, points: Array<[number, number]>): [number, number] {
  let best: [number, number] = points[0]
  let bestDistSq = Infinity
  for (let index = 0; index < points.length - 1; index += 1) {
    const [u0, v0] = points[index]
    const [u1, v1] = points[index + 1]
    const candidate = projectToSegment(u, v, u0, v0, u1, v1)
    const distSq = (candidate[0] - u) ** 2 + (candidate[1] - v) ** 2
    if (distSq < bestDistSq) {
      bestDistSq = distSq
      best = candidate
    }
  }
  return best
}

/**
 * 角度(度)を弧 `[startDeg, endDeg]`(反時計回り。`endDeg <= startDeg` は0度またぎ)へクランプする。
 * 弧の外なら**円周上で近いほうの端**へ寄せる(`arcPoints`/`toPoints` と同じ0度またぎの扱い)。
 *
 * **`%` で正規化する(反復加算のwhileループにしない)** —— SVG取り込みなどで
 * `start_deg`/`end_deg` が異常値になった場合、whileループ版は停止しない
 * (`angleWithinArc` のdocstring参照。同じ理由でこちらも直した)。
 */
function clampAngleToArc(angle: number, startDeg: number, endDeg: number): number {
  if (!Number.isFinite(startDeg) || !Number.isFinite(endDeg)) return startDeg
  const span = endDeg <= startDeg ? endDeg + 360 - startDeg : endDeg - startDeg
  let offset = (angle - startDeg) % 360
  if (offset < 0) offset += 360
  if (offset <= span) return startDeg + offset
  const distToEnd = offset - span
  const distToStartWrap = 360 - offset
  return distToEnd <= distToStartWrap ? startDeg + span : startDeg
}

/** ベジエの制御点(**始点・制御点1・制御点2・終点の順**。コアの `BEZIER_KEYS` と同じ)。 */
const BEZIER_KEYS = ['x0', 'y0', 'cx1', 'cy1', 'cx2', 'cy2', 'x1', 'y1'] as const

function bezierControlPoints(params: Record<string, number>): Array<[number, number]> {
  return [
    [params.x0, params.y0],
    [params.cx1, params.cy1],
    [params.cx2, params.cy2],
    [params.x1, params.y1],
  ]
}

function bezierParams(points: Array<[number, number]>): Record<string, number> {
  const flat = points.flat()
  return Object.fromEntries(BEZIER_KEYS.map((key, index) => [key, flat[index]]))
}

// 平行移動は種別ごとに**表の外**へ出してある —— `rotate(rotateCopies=false)` が
// 「中心を回した差分だけ平行移動する」形で同じ関数を呼ぶので、表の中から `this` 越しに
// 引くことになる(`this` の型が付かない)。コアの `_translate_*` と1対1。
function translateLine(params: Record<string, number>, dx: number, dy: number): Record<string, number> {
  return { x0: params.x0 + dx, y0: params.y0 + dy, x1: params.x1 + dx, y1: params.y1 + dy }
}

function translateAtCenter(
  params: Record<string, number>,
  xKey: string,
  yKey: string,
  dx: number,
  dy: number,
): Record<string, number> {
  // **他のキーはそのまま持ち越す**(半径・角度は平行移動で変わらない。コアの
  // `_translate_circle` / `_translate_arc` / `_translate_rectangle` が `dict(params)` を
  // 元にしているのと同じ)。
  return { ...params, [xKey]: params[xKey] + dx, [yKey]: params[yKey] + dy }
}

function translateBezier(params: Record<string, number>, dx: number, dy: number): Record<string, number> {
  return bezierParams(
    bezierControlPoints(params).map(([px, py]) => [px + dx, py + dy] as [number, number]),
  )
}

/**
 * 長方形の傾き(度・反時計)。**キーが無ければ 0** —— `angle` を持たない時代に保存された
 * 操作ログを再生した図形は `params` にこのキーを持たない(コアの `kernel.rectangle_angle`)。
 */
function rectangleAngle(params: Record<string, number>): number {
  return params.angle ?? 0
}

/**
 * 長方形の4隅を、基準の角 `(x, y)` から反時計回りに返す。
 *
 * **コアの `duocad/kernel.py` の `rectangle_corners` と同じ規則でなければならない** ——
 * ここがずれると、幽霊(確定前の姿)と確定した形が別の場所に出る。
 */
function rectangleCorners(params: Record<string, number>): Array<[number, number]> {
  const { x, y, width, height } = params
  const radians = (rectangleAngle(params) * Math.PI) / 180
  const cosA = Math.cos(radians)
  const sinA = Math.sin(radians)
  const offsets: Array<[number, number]> = [
    [0, 0],
    [width, 0],
    [width, height],
    [0, height],
  ]
  return offsets.map(([ox, oy]) => [x + ox * cosA - oy * sinA, y + ox * sinA + oy * cosA])
}

const ENTITY_KIND_TABLE: Record<EntityKind, EntityKindSpec> = {
  line: {
    pointKeys: [['x0', 'y0'], ['x1', 'y1']],
    intersectables: (params) => [
      { kind: 'segment', a: [params.x0, params.y0], b: [params.x1, params.y1] },
    ],
    verb: 'add_line',
    translate: translateLine,
    rotate(params, cx, cy, angleDeg, rotateCopies) {
      if (rotateCopies) {
        const [x0, y0] = rotatePoint(params.x0, params.y0, cx, cy, angleDeg)
        const [x1, y1] = rotatePoint(params.x1, params.y1, cx, cy, angleDeg)
        return { x0, y0, x1, y1 }
      }
      const mx = (params.x0 + params.x1) / 2
      const my = (params.y0 + params.y1) / 2
      const [nmx, nmy] = rotatePoint(mx, my, cx, cy, angleDeg)
      return translateLine(params, nmx - mx, nmy - my)
    },
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
    snap(params, u, v) {
      return projectToSegment(u, v, params.x0, params.y0, params.x1, params.y1)
    },
  },
  circle: {
    pointKeys: [['cx', 'cy']],
    intersectables: (params) => [{ kind: 'circle', center: [params.cx, params.cy], radius: params.radius }],
    verb: 'add_circle',
    translate: (params, dx, dy) => translateAtCenter(params, 'cx', 'cy', dx, dy),
    rotate(params, cx, cy, angleDeg) {
      // **円は `rotateCopies` を見ない**(向きの無い図形。コアの `_rotate_circle` と同じ)。
      const [nx, ny] = rotatePoint(params.cx, params.cy, cx, cy, angleDeg)
      return { cx: nx, cy: ny, radius: params.radius }
    },
    fromArgs(args) {
      const center = args.center as [number, number]
      return { cx: center[0], cy: center[1], radius: args.radius as number }
    },
    toPoints(params, resolution) {
      // 反時計回り・0度をまたいでよい(`add_arc` の契約と揃える。円は0〜360固定)。
      return arcPoints(params.cx, params.cy, params.radius, 0, 360, resolution)
    },
    snap(params, u, v) {
      const dx = u - params.cx
      const dy = v - params.cy
      const dist = Math.hypot(dx, dy)
      if (dist === 0) return [params.cx + params.radius, params.cy]
      const scale = params.radius / dist
      return [params.cx + dx * scale, params.cy + dy * scale]
    },
  },
  rectangle: {
    pointKeys: [['x', 'y']],
    visualCorners: rectangleCorners,
    intersectables: (params) => {
      const corners = rectangleCorners(params)
      return [0, 1, 2, 3].map(
        (index): IntersectableCurve => ({ kind: 'segment', a: corners[index], b: corners[(index + 1) % 4] }),
      )
    },
    verb: 'add_rectangle',
    translate: (params, dx, dy) => translateAtCenter(params, 'x', 'y', dx, dy),
    rotate(params, cx, cy, angleDeg, rotateCopies) {
      if (rotateCopies) {
        const [nx, ny] = rotatePoint(params.x, params.y, cx, cy, angleDeg)
        return { ...params, x: nx, y: ny, angle: rectangleAngle(params) + angleDeg }
      }
      const corners = rectangleCorners(params)
      const ecx = (corners[0][0] + corners[2][0]) / 2
      const ecy = (corners[0][1] + corners[2][1]) / 2
      const [ncx, ncy] = rotatePoint(ecx, ecy, cx, cy, angleDeg)
      return translateAtCenter(params, 'x', 'y', ncx - ecx, ncy - ecy)
    },
    fromArgs(args) {
      const corner = args.corner as [number, number]
      return {
        x: corner[0],
        y: corner[1],
        width: args.width as number,
        height: args.height as number,
        angle: (args.angle as number | undefined) ?? 0,
      }
    },
    toPoints(params) {
      const corners = rectangleCorners(params)
      return [...corners, corners[0]]
    },
    snap(params, u, v) {
      const corners = rectangleCorners(params)
      let best: [number, number] = corners[0]
      let bestDistSq = Infinity
      for (let index = 0; index < 4; index += 1) {
        const [u0, v0] = corners[index]
        const [u1, v1] = corners[(index + 1) % 4]
        const candidate = projectToSegment(u, v, u0, v0, u1, v1)
        const distSq = (candidate[0] - u) ** 2 + (candidate[1] - v) ** 2
        if (distSq < bestDistSq) {
          bestDistSq = distSq
          best = candidate
        }
      }
      return best
    },
  },
  arc: {
    pointKeys: [['cx', 'cy']],
    intersectables: (params) => [
      {
        kind: 'arc',
        center: [params.cx, params.cy],
        radius: params.radius,
        startDeg: params.start_deg,
        endDeg: params.end_deg,
      },
    ],
    verb: 'add_arc',
    translate: (params, dx, dy) => translateAtCenter(params, 'cx', 'cy', dx, dy),
    rotate(params, cx, cy, angleDeg, rotateCopies) {
      const [nx, ny] = rotatePoint(params.cx, params.cy, cx, cy, angleDeg)
      // **向きを回すときだけ開始/終了角も回す**(コアの `_rotate_arc`)。
      const start = rotateCopies ? params.start_deg + angleDeg : params.start_deg
      const end = rotateCopies ? params.end_deg + angleDeg : params.end_deg
      return { cx: nx, cy: ny, radius: params.radius, start_deg: start, end_deg: end }
    },
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
    snap(params, u, v) {
      const dx = u - params.cx
      const dy = v - params.cy
      const angle = (Math.atan2(dy, dx) * 180) / Math.PI
      const clamped = clampAngleToArc(angle, params.start_deg, params.end_deg)
      const rad = (clamped * Math.PI) / 180
      return [params.cx + params.radius * Math.cos(rad), params.cy + params.radius * Math.sin(rad)]
    },
  },
  bezier: {
    pointKeys: [['x0', 'y0'], ['cx1', 'cy1'], ['cx2', 'cy2'], ['x1', 'y1']],
    // **`verb` を持たない唯一の種別**(上の `EntityKindSpec.verb` のコメント)。
    // ただし平行移動と回転は要る —— 取り込んだSVGの曲線も反復・移動の対象になる。
    translate: translateBezier,
    rotate(params, cx, cy, angleDeg, rotateCopies) {
      // **ベジエはアフィン不変**なので、制御点を回すだけで曲線が厳密に回る
      // (コアの `_rotate_bezier`)。
      if (rotateCopies) {
        return bezierParams(
          bezierControlPoints(params).map(([px, py]) => rotatePoint(px, py, cx, cy, angleDeg)),
        )
      }
      const points = bezierControlPoints(params)
      const mx = points.reduce((sum, point) => sum + point[0], 0) / 4
      const my = points.reduce((sum, point) => sum + point[1], 0) / 4
      const [nmx, nmy] = rotatePoint(mx, my, cx, cy, angleDeg)
      return translateBezier(params, nmx - mx, nmy - my)
    },
    toPoints(params, resolution) {
      return bezierPoints(
        params.x0, params.y0,
        params.cx1, params.cy1,
        params.cx2, params.cy2,
        params.x1, params.y1,
        resolution,
      )
    },
    snap(params, u, v, resolution) {
      // 折れ線の最近傍の線分へ射影する(当たり判定 `sketchEntityAt` と同じ近似)。
      // **当たり判定の `handleLines` と同じ `resolution` で折る** —— ここが
      // `DEFAULT_RESOLUTION` 固定だと、既定ズーム以外で「当たった見えている線」と
      // 「落ちる座標」がずれる(当たり判定は常に `appliedResolution` で描いている)。
      return projectToPolyline(
        u,
        v,
        bezierPoints(
          params.x0, params.y0,
          params.cx1, params.cy1,
          params.cx2, params.cy2,
          params.x1, params.y1,
          resolution,
        ),
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
 * その図形の**動かせる点**を `{key, u, v}` で返す(`move_sketch_entities` の選択の的)。
 *
 * `key` はコアの参照が持つ `point` の値そのもの。表に無い種別は例外(`entityPoints` と同じ)。
 */
export function entityHandles(
  kind: string,
  params: Record<string, number>,
): Array<{ key: string; u: number; v: number }> {
  return entityKindSpec(kind).pointKeys.map(([xKey, yKey]) => ({
    key: xKey,
    u: params[xKey],
    v: params[yKey],
  }))
}

/**
 * その図形の点(`pointKeys`)は互いに独立して動くか(2個以上あるか)。
 *
 * 1個しかない種別(rectangle/circle/arc)は「その点だけ動かす」と「図形全体を動かす」が
 * `move_sketch_entities` の中で完全に同じ処理になる(`duocad/verbs/move_sketch.py` の
 * `_moved_params`: `point=None` は `EntityKindOps.translate` を呼び、`point='x'` 等は
 * その1点の座標を直接動かすだけで、rectangleならどちらも結局 `x,y` を足すだけの同じ式 ——
 * `duocad/sketch.py` の `_translate_rectangle` 参照)。つまりこれらの種別では点は
 * 「ドラッグして掴む場所」としては辺のどこを掴むのとも変わらず、常時表示すると
 * 「なぜこの1点だけ特別に見えるか」という見た目の非対称だけが残る(本人指摘・2026-09-11)。
 * `viewport.ts` はこれを「常時表示するか、ホバー/選択時だけ見せるか」の判定に使う ——
 * ピック可能かどうか(`pickPointsAt` が読む座標そのもの)は変えない。
 */
export function entityHasIndependentPoints(kind: string): boolean {
  return entityKindSpec(kind).pointKeys.length > 1
}

/**
 * その図形の「見た目だけの角」(`EntityKindSpec.visualCorners`)を列挙する。
 *
 * `entityHandles` とは別 —— こちらは `move_sketch_entities` の的にならない座標の並びで、
 * `viewport.ts` の当たり判定(`sketchEntityAt`)が本物の点と同じ優先度で拾うために読む。
 * 持たない種別は空配列(`entityHandles` と違い例外にしない —— line/circle/arc/bezierの
 * ように「追加の角が無い」のは正常系で、呼び出し側が毎回 kind 分岐する必要が無いように
 * するため)。
 */
export function entityVisualCorners(kind: string, params: Record<string, number>): Array<[number, number]> {
  return entityKindSpec(kind).visualCorners?.(params) ?? []
}

/**
 * その図形の交点探索用プリミティブ(`EntityKindSpec.intersectables`)を列挙する。
 * 持たない種別(ベジエ)は空配列 —— `entityVisualCorners` と同じ理由で例外にしない。
 */
export function entityIntersectables(kind: string, params: Record<string, number>): IntersectableCurve[] {
  return entityKindSpec(kind).intersectables?.(params) ?? []
}

/**
 * その図形の**直線区間の中点**(`viewport.ts` の当たり判定がカーソル強調(垂直な短い
 * ティック)に使う)。円・円弧・ベジエは対象外(本人指定「線分の中点」) ——
 * `intersectables` のうち `kind:'segment'` だけを見れば自然にそうなる。
 */
export function entityMidpoints(
  kind: string,
  params: Record<string, number>,
): Array<{ u: number; v: number; direction: readonly [number, number] }> {
  const out: Array<{ u: number; v: number; direction: readonly [number, number] }> = []
  for (const curve of entityIntersectables(kind, params)) {
    if (curve.kind !== 'segment') continue
    const dx = curve.b[0] - curve.a[0]
    const dy = curve.b[1] - curve.a[1]
    const length = Math.hypot(dx, dy)
    if (length === 0) continue
    out.push({ u: (curve.a[0] + curve.b[0]) / 2, v: (curve.a[1] + curve.b[1]) / 2, direction: [dx / length, dy / length] })
  }
  return out
}

/**
 * 2線分の交点。**端点を含む区間内**でだけ交わる場合に返す(延長線上の交点は含めない)。
 * 平行(分母0)は交点なし —— 重なる場合も含めて拾わない(重なりは「交点」ではなく
 * 「一致」で、意味が違う)。
 */
function segmentSegmentIntersection(
  a0: readonly [number, number],
  a1: readonly [number, number],
  b0: readonly [number, number],
  b1: readonly [number, number],
): Array<[number, number]> {
  const dax = a1[0] - a0[0]
  const day = a1[1] - a0[1]
  const dbx = b1[0] - b0[0]
  const dby = b1[1] - b0[1]
  const denom = dax * dby - day * dbx
  if (denom === 0) return []
  const dx = b0[0] - a0[0]
  const dy = b0[1] - a0[1]
  const t = (dx * dby - dy * dbx) / denom
  const s = (dx * day - dy * dax) / denom
  if (t < 0 || t > 1 || s < 0 || s > 1) return []
  return [[a0[0] + t * dax, a0[1] + t * day]]
}

/** 線分と円の交点(0〜2個)。線分の区間内(端点含む)だけを返す。 */
function segmentCircleIntersections(
  a0: readonly [number, number],
  a1: readonly [number, number],
  center: readonly [number, number],
  radius: number,
): Array<[number, number]> {
  const dx = a1[0] - a0[0]
  const dy = a1[1] - a0[1]
  const fx = a0[0] - center[0]
  const fy = a0[1] - center[1]
  const a = dx * dx + dy * dy
  if (a === 0) return []
  const b = 2 * (fx * dx + fy * dy)
  const c = fx * fx + fy * fy - radius * radius
  const discriminant = b * b - 4 * a * c
  if (discriminant < 0) return []
  const sqrtDiscriminant = Math.sqrt(discriminant)
  const out: Array<[number, number]> = []
  for (const t of [(-b - sqrtDiscriminant) / (2 * a), (-b + sqrtDiscriminant) / (2 * a)]) {
    if (t < 0 || t > 1) continue
    out.push([a0[0] + t * dx, a0[1] + t * dy])
  }
  return out
}

/** 円と円の交点(0〜2個)。同心円(距離0)・互いに届かない/内包する場合は交点なし。 */
function circleCircleIntersections(
  c1: readonly [number, number],
  r1: number,
  c2: readonly [number, number],
  r2: number,
): Array<[number, number]> {
  const dx = c2[0] - c1[0]
  const dy = c2[1] - c1[1]
  const d = Math.hypot(dx, dy)
  if (d === 0 || d > r1 + r2 || d < Math.abs(r1 - r2)) return []
  const a = (d * d + r1 * r1 - r2 * r2) / (2 * d)
  const heightSq = r1 * r1 - a * a
  const height = heightSq > 0 ? Math.sqrt(heightSq) : 0
  const mx = c1[0] + (a * dx) / d
  const my = c1[1] + (a * dy) / d
  if (height === 0) return [[mx, my]]
  const ox = -(dy / d) * height
  const oy = (dx / d) * height
  return [[mx + ox, my + oy], [mx - ox, my - oy]]
}

/**
 * 点が中心から見て弧 `[startDeg,endDeg]`(反時計回り。0度またぎ可)の範囲内にあるか。
 *
 * **`%` で正規化する(反復加算のwhileループにしない)** —— `clampAngleToArc` の
 * whileループ版は、SVG取り込みなどで `start_deg`/`end_deg` が異常値(非常に大きい値・
 * `Infinity`)になった場合に停止しない(実測: `smoke-svg.mjs` がここでハングした)。
 * `%` なら入力がどんな大きさでも定数時間で終わり、`Infinity`/`NaN` は比較が全部false
 * になって自然に「範囲外」に落ちる。
 */
function angleWithinArc(point: readonly [number, number], center: readonly [number, number], startDeg: number, endDeg: number): boolean {
  if (!Number.isFinite(startDeg) || !Number.isFinite(endDeg)) return false
  const angle = (Math.atan2(point[1] - center[1], point[0] - center[0]) * 180) / Math.PI
  const span = endDeg <= startDeg ? endDeg + 360 - startDeg : endDeg - startDeg
  let offset = (angle - startDeg) % 360
  if (offset < 0) offset += 360
  return offset <= span
}

type ArcCurve = Extract<IntersectableCurve, { kind: 'arc' }>

/** 2つの幾何プリミティブ(直線区間・円・円弧)の交点。どちらが先でも同じ結果になる。 */
function curveIntersections(a: IntersectableCurve, b: IntersectableCurve): Array<[number, number]> {
  if (a.kind === 'segment' && b.kind === 'segment') return segmentSegmentIntersection(a.a, a.b, b.a, b.b)
  if (a.kind === 'segment' && b.kind === 'circle') return segmentCircleIntersections(a.a, a.b, b.center, b.radius)
  if (a.kind === 'circle' && b.kind === 'segment') return segmentCircleIntersections(b.a, b.b, a.center, a.radius)
  if (a.kind === 'circle' && b.kind === 'circle') return circleCircleIntersections(a.center, a.radius, b.center, b.radius)
  const arcFilter = (arc: ArcCurve) => (point: [number, number]) => angleWithinArc(point, arc.center, arc.startDeg, arc.endDeg)
  if (a.kind === 'segment' && b.kind === 'arc') return segmentCircleIntersections(a.a, a.b, b.center, b.radius).filter(arcFilter(b))
  if (a.kind === 'arc' && b.kind === 'segment') return segmentCircleIntersections(b.a, b.b, a.center, a.radius).filter(arcFilter(a))
  if (a.kind === 'circle' && b.kind === 'arc') return circleCircleIntersections(a.center, a.radius, b.center, b.radius).filter(arcFilter(b))
  if (a.kind === 'arc' && b.kind === 'circle') return circleCircleIntersections(a.center, a.radius, b.center, b.radius).filter(arcFilter(a))
  if (a.kind !== 'arc' || b.kind !== 'arc') return [] // ここへ来るのは常に arc×arc のはず
  return circleCircleIntersections(a.center, a.radius, b.center, b.radius)
    .filter(arcFilter(a))
    .filter(arcFilter(b))
}

/**
 * スケッチ内の**異なる図形どうし**の交点を全部列挙する(直線区間・円・円弧の任意の
 * 組み合わせ。`viewport.ts` の `sketchCornerAt` が本物の点・`visualCorners` と
 * 同じ優先度で拾うために読む)。
 *
 * 同じ図形どうしのペアは見ない —— 長方形自身の隣り合う辺が作る交点(=自分の角)は
 * 既に `visualCorners` が持つ。ベジエは `intersectables` が空なので自然に対象外になる
 * (閉じた式で解けない。本人指定は「線分・円・円弧の交点」)。
 */
export function sketchIntersections(
  handles: ReadonlyArray<{ entityId: string; kind: string; params: Record<string, number> }>,
): Array<{ u: number; v: number; entityIds: readonly [string, string] }> {
  const points: Array<{ u: number; v: number; entityIds: readonly [string, string] }> = []
  for (let i = 0; i < handles.length; i += 1) {
    const curvesA = entityIntersectables(handles[i].kind, handles[i].params)
    if (curvesA.length === 0) continue
    for (let j = i + 1; j < handles.length; j += 1) {
      const curvesB = entityIntersectables(handles[j].kind, handles[j].params)
      if (curvesB.length === 0) continue
      for (const curveA of curvesA) {
        for (const curveB of curvesB) {
          for (const hit of curveIntersections(curveA, curveB)) {
            points.push({ u: hit[0], v: hit[1], entityIds: [handles[i].entityId, handles[j].entityId] })
          }
        }
      }
    }
  }
  return points
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
 * 与えた (u,v) をこの図形の輪郭へ落とす(`EntityKindSpec.snap` 参照)。表に無い種別は例外。
 *
 * `resolution` を省くと**当たり判定と揃わない既定の粗さ**になる(`entityPoints` と同じ
 * 注意)。呼ぶのは `snapToSketchEntity` 経由で、そちらが `viewport.viewResolution()` から
 * 今の値を渡す。
 */
export function snapToEntity(
  kind: string,
  params: Record<string, number>,
  u: number,
  v: number,
  resolution: ViewResolution = DEFAULT_RESOLUTION,
): [number, number] {
  return entityKindSpec(kind).snap(params, u, v, resolution)
}

/**
 * スケッチの当たり判定(`SketchEntityHit`)が拾った図形へ (u,v) を落とす。
 *
 * **クリック(`addPoint`)と幽霊(`hoverPoint`)は必ずこの関数を通すこと**(App.svelte
 * 20260906のタスク不変条件) —— 片方だけ直すと、吸い付いて見える位置と確定結果がずれる。
 * 点マーカーに当たっているとき(`point !== null`)は射影せず、`entityHandles` が持つ
 * その点の (u,v) をそのまま返す。
 *
 * `resolution` は**当たり判定(`sketchEntityAt`)が使っている折れ線の粗さ**
 * (`viewport.viewResolution()`)をそのまま渡すこと —— bezierのように `toPoints` の
 * 近似で当たる種別は、ここがずれると「当たった線」と「落ちる座標」が別になる。
 */
export function snapToSketchEntity(
  entity: { kind: string; params: Record<string, number> },
  point: string | null,
  u: number,
  v: number,
  resolution: ViewResolution = DEFAULT_RESOLUTION,
): [number, number] {
  if (point !== null) {
    const handle = entityHandles(entity.kind, entity.params).find((item) => item.key === point)
    if (handle) return [handle.u, handle.v]
  }
  return snapToEntity(entity.kind, entity.params, u, v, resolution)
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

/**
 * 反復・移動の**上限**(幽霊の枚数)。
 *
 * プレビューは打っている途中の値で毎キーストローク組み直す —— `nx`/`ny` に4桁を打った
 * 瞬間に数十万本の折れ線を作ると、確定する前にブラウザが固まる。上限を超えたら
 * **1本も描かない**(途中まで描くと「間隔は合っているのに個数が足りない」に見え、
 * 打ち間違いなのか不具合なのか人には区別できない)。
 *
 * **コア側に同じ上限は無い**。ここは画面が固まらないためだけの値で、確定結果は
 * いつもどおり打った個数どおりに作られる —— **上限を超えた個数でも Enter は通り、
 * 幽霊を見ないまま大量の複製ができる**。それでよいとした理由は、上限を verb 側へ
 * 持ち込むと「画面が重い」というブラウザ側の都合が文書の作れる形を決めてしまうためで、
 * ここは表示の都合として閉じてある。
 */
export const GHOST_COPY_LIMIT = 2000

/**
 * 円形反復の**刻み角**。全周は `total/count`、全周未満は `total/(count-1)`。
 *
 * **コアの `duocad/region.py` の `_expanded_entities` と同じ場合分け**である ——
 * 全周を `count-1` で割ると最後の複製が最初と重なる(`pattern_circular` のdocstring)。
 * 360の判定が厳密等価なのも**コアに合わせている**(こちらだけ許容差を持たせると、
 * 359.9999 で幽霊と確定結果が別の刻みになる)。
 */
function circularStep(count: number, totalAngle: number): number {
  if (count <= 1) return 0
  return totalAngle === 360 ? 360 / count : totalAngle / (count - 1)
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/**
 * 平行移動した `params`。**元の値の上へ重ねて全鍵を揃える** ——
 * `translate` は種別ごとに「変える鍵だけ」を返すものと全部返すものが混在する
 * (コアの `duocad/verbs/move_sketch.py` の `_moved_params` と同じ理由・同じ形)。
 */
function translated(
  kind: string,
  params: Record<string, number>,
  dx: number,
  dy: number,
): Record<string, number> {
  return { ...params, ...entityKindSpec(kind).translate(params, dx, dy) }
}

/**
 * その種別の `point`(参照が持つ**xキー**)に対応する (xキー, yキー)。
 * 表に無い点の名前は null(コアの `entity_point_pair` は ValueError だが、こちらは
 * 幽霊なので描かないだけにする —— 打っている最中の状態で例外を投げない)。
 */
function entityPointPair(kind: string, point: string): readonly [string, string] | null {
  return entityKindSpec(kind).pointKeys.find(([xKey]) => xKey === point) ?? null
}

/** 2つの `params` が**完全に同じ**か(コアの `values == current` と同じ厳密比較)。 */
function sameParams(left: Record<string, number>, right: Record<string, number>): boolean {
  const keys = Object.keys(left)
  if (keys.length !== Object.keys(right).length) return false
  return keys.every((key) => Object.is(left[key], right[key]))
}

/**
 * `move_sketch_entities` の **`mode="rotate"`** の幽霊
 * (`duocad/verbs/move_sketch.py` の `_rotation_args` / `_rotated_params` / `_copy` の写し)。
 *
 * **中心は数値の `center` [u, v] を渡されたときだけ描く。** コアは省略時に「対象の代表点の
 * 平均」(`_center_of_targets`)を使うが、その規則をここへ写すと**規則が2箇所に増える** ——
 * 種別ごとの代表点(線分は中点・円/弧は中心・長方形は中央・ベジエは制御点の重心)が片方
 * だけ変わった瞬間、幽霊と確定結果が別の場所に出る。**描かないほうが安全**である。
 */
function rotatedTargets(
  args: Record<string, unknown>,
  sources: GhostSource[],
  wholeEntity: boolean,
): Array<{ kind: EntityKind; params: Record<string, number> }> {
  const center = args.center as unknown
  if (!Array.isArray(center) || center.length !== 2) return []
  const cx = finiteNumber(center[0])
  const cy = finiteNumber(center[1])
  const angleDeg = finiteNumber(args.angle)
  if (cx === null || cy === null || angleDeg === null) return []
  // **360の倍数はコアが ValueError にする**(0度と同じで、複製なら元と重なる)。
  if (angleDeg % 360 === 0) return []
  const out: Array<{ kind: EntityKind; params: Record<string, number> }> = []
  for (const source of sources) {
    const points = source.points ?? [null]
    // **複製は常に図形まるごと**。動かす側も、図形全体(`null`)が選ばれていれば
    // まるごと(「辺が勝つ」。コアの `_normalize_targets`)。
    if (wholeEntity || points.includes(null)) {
      // **`rotateCopies` は `true`** —— 対象そのものを回すので、「向きを変えず位置だけ回す」
      // (円形反復の複製のための挙動)ではない。
      const rotated = entityKindSpec(source.kind).rotate(source.params, cx, cy, angleDeg, true)
      const params = { ...source.params, ...rotated }
      // 複製が元と完全に重なるのもコアは ValueError(円をその中心まわりに回した場合)。
      if (wholeEntity && sameParams(params, source.params)) return []
      out.push({ kind: source.kind as EntityKind, params })
      continue
    }
    // 選ばれた点だけを回す(種別は変わらない)。同じ図形の複数の点は**積み上げる**
    // (コアの `_apply` と同じ。平行移動側と同じ理由)。
    let params = { ...source.params }
    for (const point of points) {
      const pair = entityPointPair(source.kind, point as string)
      if (pair === null) continue
      const [x, y] = rotatePoint(params[pair[0]], params[pair[1]], cx, cy, angleDeg)
      params = { ...params, [pair[0]]: x, [pair[1]]: y }
    }
    out.push({ kind: source.kind as EntityKind, params })
  }
  return out
}

/**
 * `ghostCopies` が受け取る「変換の元になる図形」。
 *
 * `points` は **`move_sketch_entities` だけ**が使う —— この verb の対象は「図形1つ全体」
 * (参照の `point` が `null` = 辺)と「図形の1点」の2種類あり、**どちらかで結果が変わる**
 * (全体なら形を変えずに動き、1点なら線分が伸びる)。反復には点の概念が無いので省略できる。
 */
export interface GhostSource {
  kind: string
  params: Record<string, number>
  /** 選ばれている点のxキー。`null` は図形1つ全体。省略は「全体」1つと同じ。 */
  points?: Array<string | null>
}

/**
 * **既にある図形**の、確定前の姿(幽霊)。
 *
 * `ghostEntity` との違いは対象である —— あちらは「これから打つ点」から図形を組み立てる
 * (`add_*`)。こちらは**既にスケッチに居る図形**(`sources`)を反復・平行移動・回転した姿を返す
 * (`pattern_rectangular` / `pattern_circular` / `move_sketch_entities`)。
 * どちらでもないverbは空配列(スケッチの図形を触らないverbは幽霊を持たない)。
 *
 * `sources` は**解決済みの数値**を持つ図形(`SketchState.entities` の `kind`/`params`)。
 * 呼び出し側(`App.svelte`)がどれを対象にするかを決める —— 反復は引数の `entities`、
 * 移動は共有選択集合、という違いがあり、その違いはここに持ち込まない。
 * 移動だけは**どの点が選ばれているか**(`GhostSource.points`)も要る(そちらのdocstring)。
 *
 * **反復は「増える分」だけを返す**(格子の (0,0) / 円形の i=0 を出さない)。そこは元の図形が
 * 既に本物の色で描かれている場所で、幽霊を重ねても新しい情報が1つも増えないうえ、
 * 同じ線が2本重なって Z ファイティングになる。
 */
export function ghostCopies(
  verb: string,
  args: Record<string, unknown>,
  sources: GhostSource[],
): Array<{ kind: EntityKind; params: Record<string, number> }> {
  if (sources.length === 0) return []
  if (verb === 'move_sketch_entities') {
    // **複製は図形まるごと**である(点だけの複製はできない。コアの `_copy`)。
    const wholeEntity = args.copy === true
    if (args.mode === 'rotate') return rotatedTargets(args, sources, wholeEntity)
    const dx = finiteNumber(args.dx)
    const dy = finiteNumber(args.dy)
    if (dx === null || dy === null) return []
    return sources.map((source) => {
      const points = source.points ?? [null]
      // **同じ図形に「辺」と「点」が同時に選ばれていたら辺(全体)が勝つ**
      // (コアの `_normalize_targets`。両方を素直に当てるとその点だけ2回動く)。
      if (wholeEntity || points.includes(null)) {
        return {
          kind: source.kind as EntityKind,
          params: translated(source.kind, source.params, dx, dy),
        }
      }
      // 点だけを動かす —— **形が変わる**(線分が伸びる)ので、図形ごと平行移動しては
      // ならない。同じ図形の複数の点は**積み上げてから**1つの姿にする(コアの `_apply`)。
      let params = { ...source.params }
      for (const point of points) {
        const pair = entityPointPair(source.kind, point as string)
        if (pair === null) continue
        params = { ...params, [pair[0]]: params[pair[0]] + dx, [pair[1]]: params[pair[1]] + dy }
      }
      return { kind: source.kind as EntityKind, params }
    })
  }
  if (verb === 'pattern_rectangular') {
    const dx = finiteNumber(args.dx)
    const dy = finiteNumber(args.dy)
    const nx = finiteNumber(args.nx)
    const ny = finiteNumber(args.ny)
    if (dx === null || dy === null || nx === null || ny === null) return []
    if (nx < 1 || ny < 1 || !Number.isInteger(nx) || !Number.isInteger(ny)) return []
    if (sources.length * (nx * ny - 1) > GHOST_COPY_LIMIT) return []
    const out: Array<{ kind: EntityKind; params: Record<string, number> }> = []
    for (const source of sources) {
      for (let j = 0; j < ny; j += 1) {
        for (let i = 0; i < nx; i += 1) {
          if (i === 0 && j === 0) continue // 元の図形の位置(docstring)
          out.push({
            kind: source.kind as EntityKind,
            params: translated(source.kind, source.params, i * dx, j * dy),
          })
        }
      }
    }
    return out
  }
  if (verb === 'pattern_circular') {
    const center = args.center as unknown
    if (!Array.isArray(center) || center.length !== 2) return []
    const cx = finiteNumber(center[0])
    const cy = finiteNumber(center[1])
    const count = finiteNumber(args.count)
    const totalAngle = finiteNumber(args.total_angle)
    if (cx === null || cy === null || count === null || totalAngle === null) return []
    if (count < 1 || !Number.isInteger(count)) return []
    if (sources.length * (count - 1) > GHOST_COPY_LIMIT) return []
    // **`rotate_copies` は引数に無い** —— UIは送らず、コアの既定(`True`)が効く
    // (`duocad/verbs/pattern.py` の `pattern_circular`)。ここも同じ既定を使う。
    const step = circularStep(count, totalAngle)
    const out: Array<{ kind: EntityKind; params: Record<string, number> }> = []
    for (const source of sources) {
      const spec = entityKindSpec(source.kind)
      for (let i = 1; i < count; i += 1) {
        out.push({
          kind: source.kind as EntityKind,
          params: spec.rotate(source.params, cx, cy, i * step, true),
        })
      }
    }
    return out
  }
  return []
}
