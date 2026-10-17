/**
 * フィーチャのコマンド定義。`duocad/server.py` の `FEATURE_COMMANDS` に開いた口へ引数の集め方を書くだけ(コアには何も足していない)。
 *
 * 対象集合(領域・辺)はサーバへ送らない。サーバ側の `Session` が自分の選択集合から取る(`selection.require_*`)。
 * 選択引数の定義(`kind: 'selection'`)が残るのは、人がまだ選んでいないことを画面に出し、辺のピッキングを起動するため(`CommandHost` の `onWants`)。
 * スケッチの図形(反復の対象)はコアの選択集合に入らないので、スケッチパネルで選び `entities` 引数として渡る。
 *
 * 反復にはサーバのプレビューを出さない: 展開規則(全周は `360/count` の刻み角、など)はコアが持っており、ブラウザへ二重実装すると画面の予想と実際が食い違う。
 */
import { pickedRefOf, positive, type ArgValues, type CommandDef, type Point2 } from './command'
import { angleArg, buildMove, copyOptions, isRotateMode, modeOptions, offsetArg } from './moveMode'
import type { SelectionRef } from '../session'

/** 1以上の整数だけを通す(個数。コアの `parameters.is_positive_int` と同じ判定)。 */
function count(label: string, least: number): (value: number) => string | null {
  return (value) => {
    if (!Number.isInteger(value)) return `${label}は整数`
    return value >= least ? null : `${label}は${least}以上`
  }
}

/** 打った値が数値でありさえすればよい引数(移動量は0も負も正当)。 */
function finite(label: string): (value: number) => string | null {
  return (value) => (Number.isFinite(value) ? null : `${label}は数値`)
}

/** 選択引数が空なら理由を返す。値は送らず、「まだ選んでいない」を画面に出すためだけ。 */
function missingSelection(values: ArgValues, name: string, label: string): string | null {
  return ((values[name] ?? []) as SelectionRef[]).length === 0 ? `${label}が選ばれていない` : null
}

/**
 * 押し出し(verb `extrude`)。スケッチ編集中は閉領域、通常モードはソリッドの面を選ぶ2つの定義を作る(サーバは選択の種別で分岐し、混在は断る)。
 * 種別(join/cut/new)は人に選ばせる: 既定の join に落ちると「切り取るつもりで足した」が黙って通る。
 * 面の距離は外向きが正(削るには負)。領域の距離は0だけが無効。
 */
function makeExtrude(kind: 'region' | 'face'): CommandDef {
  const isFace = kind === 'face'
  const selectionName = isFace ? 'faces' : 'regions'
  const selectionLabel = isFace ? '押し出す面' : '押し出す領域'
  return {
    id: 'extrude',
    label: '押し出す',
    buttonId: 'extrude',
    preview: 'server',
    args: [
      { kind: 'selection', name: selectionName, label: selectionLabel, shapeKind: kind, min: 1, max: null },
      {
        kind: 'number',
        name: 'distance',
        label: isFace ? '距離(正=面の外向き)' : '距離',
        unit: 'mm',
        inputId: 'distance',
        reject: (value) => (value === 0 ? (isFace ? '距離が0(正=面の外向き / 負=削る)' : '距離が0(形が変わらない)') : null),
      },
      {
        kind: 'choice',
        name: 'mode',
        label: '種別',
        options: [
          { value: 'join', label: '結合', buttonId: 'extrude-mode-join' },
          { value: 'cut', label: '切り取り', buttonId: 'extrude-mode-cut' },
          { value: 'new', label: '新規ボディ', buttonId: 'extrude-mode-new' },
        ],
      },
    ],
    build(values) {
      const empty = missingSelection(values, selectionName, isFace ? '面' : '領域')
      const distance = values.distance
      const mode = values.mode
      if (empty !== null) return { ok: false, reason: empty }
      if (typeof distance !== 'number') return { ok: false, reason: '距離が数値でない' }
      if (typeof mode !== 'string') return { ok: false, reason: '種別が選ばれていない' }
      return { ok: true, args: { distance, mode } }
    },
  }
}

/** スケッチモードの押し出し(閉領域を選ぶ)。 */
export const EXTRUDE_REGIONS: CommandDef = makeExtrude('region')

/** 通常モードの押し出し(ソリッドの面を選ぶ。同一平面・同一ボディなら何枚でも)。 */
export const EXTRUDE_FACES: CommandDef = makeExtrude('face')

/**
 * 回転体(verb `revolve`)。スケッチ編集中に閉領域を選び、軸をスケッチの x / y 軸か同じスケッチの線分から選ぶ。
 * 線分は共有選択に入れずピックで指す(`MIRROR_SKETCH_ENTITIES` の軸と同じ。選択に入れると領域の選択と混ざる)。
 * 角度は既定 360 で、負なら逆向き。両側(`symmetric`)は画面からは送らない(コアの既定 false)。
 */
export const REVOLVE: CommandDef<'revolve'> = {
  id: 'revolve',
  label: '回転体',
  buttonId: 'revolve',
  preview: 'server',
  args: [
    { kind: 'selection', name: 'regions', label: '回す領域', shapeKind: 'region', min: 1, max: null },
    {
      kind: 'choice',
      name: 'axis',
      label: '回転軸',
      options: [
        { value: 'x', label: 'x軸', buttonId: 'revolve-axis-x' },
        { value: 'y', label: 'y軸', buttonId: 'revolve-axis-y' },
        // この値はサーバへ行かない(`axis` は x / y のみ)。`axis_line` を出す印で、配信に `axis_line` があれば欄へ戻る。
        { value: 'line', label: '線分を選ぶ', buttonId: 'revolve-axis-line', sends: null, impliedBy: 'axis_line' },
      ],
    },
    {
      kind: 'pick',
      name: 'axis_line',
      label: '回転軸の線分',
      target: 'sketchEntity',
      buttonId: 'revolve-axis-line-pick',
      when: (values) => values.axis === 'line',
    },
    {
      kind: 'number',
      name: 'angle',
      label: '角度(負=逆向き)',
      unit: '°',
      inputId: 'revolve-angle',
      default: 360,
      reject: (value) => (value !== 0 && Math.abs(value) <= 360 ? null : '角度は0より大きく360以下(負は逆向き)'),
    },
    {
      kind: 'choice',
      name: 'mode',
      label: '種別',
      options: [
        { value: 'join', label: '結合', buttonId: 'revolve-mode-join' },
        { value: 'cut', label: '切り取り', buttonId: 'revolve-mode-cut' },
        { value: 'new', label: '新規ボディ', buttonId: 'revolve-mode-new' },
      ],
    },
  ],
  build(values) {
    const empty = missingSelection(values, 'regions', '領域')
    if (empty !== null) return { ok: false, reason: empty }
    const axisKind = values.axis
    if (typeof axisKind !== 'string') return { ok: false, reason: '回転軸が選ばれていない' }
    const angle = values.angle
    const mode = values.mode
    if (typeof angle !== 'number') return { ok: false, reason: '角度が数値でない' }
    if (typeof mode !== 'string') return { ok: false, reason: '種別が選ばれていない' }
    if (axisKind === 'line') {
      // 参照のまま送る(鏡映の軸と同じ)。線分であることの検査はコアに任せる。
      const line = pickedRefOf(values.axis_line)
      if (line === null) return { ok: false, reason: '回転軸の線分が指されていない' }
      return { ok: true, args: { axis_line: line, angle, mode } }
    }
    return { ok: true, args: { axis: axisKind, angle, mode } }
  },
}

/** 始点のずらし(`shifts`)の欄の文字列を整数の並びへ。空白かカンマ区切り。読めなければ null。 */
export function parseShifts(text: string): number[] | null {
  const parts = text.split(/[\s,]+/).filter((part) => part !== '')
  const values = parts.map(Number)
  return values.every(Number.isInteger) ? values : null
}

/**
 * ロフト(verb `loft`)。通常モードで、別々のスケッチの領域を**つなぐ順に**選ぶ(共有選択は追加順を保ち、コアはその順で使う)。
 * スケッチ編集中に出さないのは、編集中は開いているスケッチの領域しか指せないため(`viewport.regionPickTargets`)。
 * 通常モードで領域を要求するのはこのコマンドだけで、起動中だけ全スケッチの領域が当たる(`App.svelte` の `wantsRegions`)。
 * 始点・終点の点(`start_point` / `end_point`)は画面からは送らない: スケッチの点のハンドルは編集中のスケッチにしか無い。
 * 始点のずらし(`shifts`)は断面の数と同じ個数の整数をカンマ区切りで打つ(空欄はずらさない)。
 */
export const LOFT: CommandDef<'loft'> = {
  id: 'loft',
  label: 'ロフト',
  buttonId: 'loft',
  preview: 'server',
  args: [
    { kind: 'selection', name: 'regions', label: 'つなぐ断面(順に)', shapeKind: 'region', min: 2, max: null },
    {
      kind: 'choice',
      name: 'mode',
      label: '種別',
      options: [
        { value: 'join', label: '結合', buttonId: 'loft-mode-join' },
        { value: 'cut', label: '切り取り', buttonId: 'loft-mode-cut' },
        { value: 'new', label: '新規ボディ', buttonId: 'loft-mode-new' },
      ],
    },
    {
      kind: 'text',
      name: 'shifts',
      label: '始点のずらし(断面ごと)',
      inputId: 'loft-shifts',
      placeholder: '0, 1, 0',
      optional: true,
      reject: (value) => (parseShifts(value) === null ? '始点のずらしは整数をカンマ区切りで' : null),
    },
  ],
  build(values) {
    const regions = (values.regions ?? []) as SelectionRef[]
    if (regions.length < 2) return { ok: false, reason: '断面を2つ以上選ぶ(つなぐ順に)' }
    const mode = values.mode
    if (typeof mode !== 'string') return { ok: false, reason: '種別が選ばれていない' }
    const text = typeof values.shifts === 'string' ? values.shifts.trim() : ''
    if (text === '') return { ok: true, args: { mode } }
    const shifts = parseShifts(text)
    if (shifts === null) return { ok: false, reason: '始点のずらしは整数をカンマ区切りで' }
    if (shifts.length !== regions.length) {
      return { ok: false, reason: `始点のずらしは断面の数(${regions.length})だけ並べる` }
    }
    return { ok: true, args: { mode, shifts } }
  },
}

/**
 * フィレット。半径は1つで全辺に効く。
 * OCCTのフィレットは例外を投げず `IsDone()=False` で失敗し、半径に対して単調でない(§7。r=10.0 失敗 / r=10.5 成功)。
 * コアが例外へ変換し、`App.svelte` が選択を保持したまま打った値を戻す。
 */
export const FILLET: CommandDef<'fillet'> = {
  id: 'fillet',
  label: '丸める',
  buttonId: 'fillet',
  preview: 'server',
  args: [
    { kind: 'selection', name: 'edges', label: '丸める辺', shapeKind: 'edge', min: 1, max: null },
    {
      kind: 'number',
      name: 'radius',
      label: '半径',
      unit: 'mm',
      inputId: 'fillet-radius',
      reject: positive('半径'),
    },
  ],
  build(values) {
    const empty = missingSelection(values, 'edges', '辺')
    const radius = values.radius
    if (empty !== null) return { ok: false, reason: empty }
    if (typeof radius !== 'number') return { ok: false, reason: '半径が数値でない' }
    return { ok: true, args: { radius } }
  },
}

/** 面取り。フィレットと同じ集め方。 */
export const CHAMFER: CommandDef<'chamfer'> = {
  id: 'chamfer',
  label: '面取り',
  buttonId: 'chamfer',
  preview: 'server',
  args: [
    { kind: 'selection', name: 'edges', label: '面取りする辺', shapeKind: 'edge', min: 1, max: null },
    {
      kind: 'number',
      name: 'distance',
      label: '長さ',
      unit: 'mm',
      inputId: 'chamfer-distance',
      reject: positive('長さ'),
    },
  ],
  build(values) {
    const empty = missingSelection(values, 'edges', '辺')
    const distance = values.distance
    if (empty !== null) return { ok: false, reason: empty }
    if (typeof distance !== 'number') return { ok: false, reason: '長さが数値でない' }
    return { ok: true, args: { distance } }
  },
}

/** シェル(中空化)。選んだ面を抜き、外形から内側へ同じ厚さの壁を残す。面を選ばない閉じた中空は無い(コアも断る)。 */
export const SHELL_BODY: CommandDef<'shell_body'> = {
  id: 'shell_body',
  label: 'シェル',
  buttonId: 'shell-body',
  preview: 'server',
  args: [
    { kind: 'selection', name: 'faces', label: '抜く面', shapeKind: 'face', min: 1, max: null },
    {
      kind: 'number',
      name: 'thickness',
      label: '厚さ',
      unit: 'mm',
      inputId: 'shell-thickness',
      reject: positive('厚さ'),
    },
  ],
  build(values) {
    const empty = missingSelection(values, 'faces', '面')
    const thickness = values.thickness
    if (empty !== null) return { ok: false, reason: empty }
    if (typeof thickness !== 'number') return { ok: false, reason: '厚さが数値でない' }
    return { ok: true, args: { thickness } }
  },
}

/**
 * ソリッドブーリアン。ターゲットは面1枚を直接ピック(`PickArgSpec`)、ツールは選択(複数)。
 * ターゲットのピックをツール選択より前に置く(`args` の並びが `firstMissingArg` の順序)。ターゲットを選択に入れると `require_bodies` でツールとロールが混ざる。
 */
export const BOOLEAN_BODY: CommandDef<'boolean_body'> = {
  id: 'boolean_body',
  label: 'ブーリアン',
  buttonId: 'boolean-body',
  preview: 'server',
  args: [
    {
      kind: 'pick',
      name: 'target_ref',
      label: 'ターゲット(面1枚)',
      target: 'face',
      buttonId: 'boolean-body-target',
    },
    {
      kind: 'selection',
      name: 'tools',
      label: 'ツールボディ(面/辺/頂点で指す)',
      shapeKind: 'face',
      kinds: ['face', 'edge', 'vertex'],
      min: 1,
      max: null,
    },
    {
      kind: 'choice',
      name: 'mode',
      label: '種別',
      options: [
        { value: 'union', label: '結合', buttonId: 'boolean-body-union' },
        { value: 'cut', label: '切り取り', buttonId: 'boolean-body-cut' },
        { value: 'intersect', label: '交差', buttonId: 'boolean-body-intersect' },
      ],
    },
  ],
  build(values) {
    const target = pickedRefOf(values.target_ref)
    if (target === null) return { ok: false, reason: 'ターゲットボディが指されていない' }
    const empty = missingSelection(values, 'tools', 'ツールボディ')
    const mode = values.mode
    if (empty !== null) return { ok: false, reason: empty }
    if (typeof mode !== 'string') return { ok: false, reason: '種別が選ばれていない' }
    return { ok: true, args: { target_ref: target, mode } }
  },
}

/** 矩形反復。`nx`/`ny` は元を含めた個数(コアの契約)。`sketch_id` は `App.svelte` が送信時に足す。 */
export const PATTERN_RECTANGULAR: CommandDef<'pattern_rectangular'> = {
  id: 'pattern_rectangular',
  label: '矩形反復',
  buttonId: 'pattern-rectangular',
  // 変わるのはスケッチの図形でソリッドではないので、サーバの投機実行は形が変わったBodyを返さない(`duocad/catalog.py` の note)。複製の座標は `entityKinds.ts` の表が出す。
  preview: 'local',
  args: [
    { kind: 'entities', name: 'entities', label: '反復する図形', min: 1 },
    {
      kind: 'number',
      name: 'dx',
      label: 'X間隔',
      unit: 'mm',
      inputId: 'pattern-dx',
      reject: () => null, // 0 は「その方向には並べない」で正当
    },
    {
      kind: 'number',
      name: 'dy',
      label: 'Y間隔',
      unit: 'mm',
      inputId: 'pattern-dy',
      reject: () => null,
    },
    {
      kind: 'number',
      name: 'nx',
      label: 'X個数',
      unit: '個',
      inputId: 'pattern-nx',
      reject: count('X個数', 1),
    },
    {
      kind: 'number',
      name: 'ny',
      label: 'Y個数',
      unit: '個',
      inputId: 'pattern-ny',
      reject: count('Y個数', 1),
    },
  ],
  build(values) {
    const entities = (values.entities ?? []) as string[]
    const dx = values.dx
    const dy = values.dy
    const nx = values.nx
    const ny = values.ny
    if (entities.length === 0) return { ok: false, reason: '図形が選ばれていない' }
    if (
      typeof dx !== 'number' ||
      typeof dy !== 'number' ||
      typeof nx !== 'number' ||
      typeof ny !== 'number'
    ) {
      return { ok: false, reason: '間隔と個数が数値でない' }
    }
    if (dx === 0 && dy === 0 && nx * ny > 1) {
      return { ok: false, reason: '間隔が両方0(複製が元と重なる)' }
    }
    if (nx === 1 && ny === 1) return { ok: false, reason: '個数が1×1(反復にならない)' }
    return { ok: true, args: { entities, dx, dy, nx, ny } }
  },
}

/**
 * 円形反復。全周(360度)は `360/count`、全周未満は `total_angle/(count-1)` の場合分けをコアが持つ
 * (全周を `count-1` で割ると最後の複製が最初と重なる)。UIは角度と個数をそのまま渡す。
 * 回転中心はスケッチ平面上のクリック(点引数)。中心と図形の中心が一致するとコアが ValueError。
 */
export const PATTERN_CIRCULAR: CommandDef<'pattern_circular'> = {
  id: 'pattern_circular',
  label: '円形反復',
  buttonId: 'pattern-circular',
  // 回転中心を置くクリックの前から幽霊が出る(`CommandHost` が待っている点をポインタ位置で仮に埋める)。
  preview: 'local',
  args: [
    { kind: 'entities', name: 'entities', label: '反復する図形', min: 1 },
    { kind: 'point', name: 'center', label: '回転中心' },
    {
      kind: 'number',
      name: 'count',
      label: '個数',
      unit: '個',
      inputId: 'pattern-count',
      reject: count('個数', 2),
    },
    {
      kind: 'number',
      name: 'total_angle',
      label: '全角度',
      unit: '度',
      inputId: 'pattern-angle',
      reject: (value) =>
        value > 0 && value <= 360 ? null : '全角度は 0より大きく360以下',
    },
  ],
  build(values) {
    const entities = (values.entities ?? []) as string[]
    const center = values.center as Point2 | undefined
    const total = values.count
    const angle = values.total_angle
    if (entities.length === 0) return { ok: false, reason: '図形が選ばれていない' }
    if (!center) return { ok: false, reason: '回転中心が要る' }
    if (typeof total !== 'number' || typeof angle !== 'number') {
      return { ok: false, reason: '個数と角度が数値でない' }
    }
    return { ok: true, args: { entities, center, count: total, total_angle: angle } }
  },
}

/**
 * ボディの平行移動 / 回転 / 複製。面・辺・頂点のどれでもボディを指せる(`selection.require_bodies` が `ShapeRef.body` から逆引きする)。
 * 複製するか・並進か回転かは既定を置かず人に選ばせる(押し出しの `mode` と同じ理由)。
 *
 * 回転軸は共有選択集合に入れず別チャンネル(`kind: 'pick'`)で指す: 入れると `require_bodies` が軸にした辺のボディまで動かす対象に数える。
 * 中心は省略可で、既定はコアが決める(対象の合成重心)。軸のピックは角度より前に置く(`args` の並びが `firstMissingArg` の順序)。
 */
export const MOVE_BODY: CommandDef<'move_body'> = {
  id: 'move_body',
  label: '動かす',
  buttonId: 'move-body',
  // 形は変わらず位置だけが変わるので、届いているメッシュを dx/dy/dz だけずらして半透明で重ねる。元の実体は隠さない(移動前後を並べて見せる)。
  preview: 'local',
  args: [
    {
      kind: 'selection',
      name: 'target',
      label: '動かすボディ(面/辺/頂点で指す)',
      shapeKind: 'face',
      kinds: ['face', 'edge', 'vertex'],
      min: 1,
      max: null,
    },
    { kind: 'choice', name: 'copy', label: '複製', options: copyOptions('move-body') },
    { kind: 'choice', name: 'mode', label: '種別', options: modeOptions('move-body') },
    { kind: 'number', ...offsetArg('dx', 'move-body', finite('X')) },
    { kind: 'number', ...offsetArg('dy', 'move-body', finite('Y')) },
    { kind: 'number', ...offsetArg('dz', 'move-body', finite('Z')) },
    {
      kind: 'pick',
      name: 'axis_edge',
      label: '回転軸にする辺',
      target: 'edge',
      buttonId: 'move-body-axis',
      when: isRotateMode,
    },
    {
      kind: 'pick',
      name: 'center_vertex',
      label: '回転中心の頂点',
      target: 'vertex',
      buttonId: 'move-body-center',
      // 省略時の中心はコアが決める(`axis_edge` だけなら辺が乗る直線そのもの)。
      optional: true,
      when: isRotateMode,
    },
    { kind: 'number', ...angleArg('move-body') },
  ],
  build(values) {
    return buildMove(values, {
      missingTarget: (given) => missingSelection(given, 'target', 'ボディ(面/辺/頂点)'),
      axes: ['dx', 'dy', 'dz'],
      zeroReason: '移動量が全部0',
      rotateArgs: (angle, given) => {
        // 軸は参照のまま送る: 辺が乗る直線を厳密に出せるのはコアだけ(ブラウザの持つのは折れ線)。
        const axis = pickedRefOf(given.axis_edge)
        if (axis === null) return { ok: false, reason: '回転軸にする辺が指されていない' }
        const center = pickedRefOf(given.center_vertex)
        // 指していない中心は引数ごと送らない(既定の合成重心はコアが決める)。
        return {
          ok: true,
          args: { mode: 'rotate', angle, axis_edge: axis, ...(center === null ? {} : { center_vertex: center }) },
        }
      },
    })
  },
}

/**
 * ボディの鏡映複製。常に複製を作る。鏡映面は基準平面(XY/XZ/YZ)か、既存の平らな面1つのクリック(`plane_face`)。
 * 面は共有選択集合を経由しない別チャンネル(`PickArgSpec`)で受ける: 同じ集合に載せると `require_bodies` が鏡に使った面のボディまで複製する。
 * 面のピックは「面を選ぶ」を押した後だけ開く(`when`)ので、3択で済む人の選択クリックは吸われない。
 */
export const MIRROR_BODY: CommandDef<'mirror_body'> = {
  id: 'mirror_body',
  label: '鏡映複製',
  buttonId: 'mirror-body',
  // 幽霊は出さない。確定してからCtrl+Zで戻す運用。
  preview: 'none',
  args: [
    {
      kind: 'selection',
      name: 'target',
      label: '鏡映するボディ(面/辺/頂点で指す)',
      shapeKind: 'face',
      kinds: ['face', 'edge', 'vertex'],
      min: 1,
      max: null,
    },
    {
      kind: 'choice',
      name: 'base_plane',
      label: '鏡映面',
      options: [
        { value: 'XY', label: 'XY平面', buttonId: 'mirror-body-xy' },
        { value: 'XZ', label: 'XZ平面', buttonId: 'mirror-body-xz' },
        { value: 'YZ', label: 'YZ平面', buttonId: 'mirror-body-yz' },
        // この値はサーバへ行かない(`base_plane` は XY/XZ/YZ のみ)。`plane_face` を出す印で、`build` が読み替える。
        { value: 'face', label: '面を選ぶ', buttonId: 'mirror-body-face', sends: null, impliedBy: 'plane_face' },
      ],
    },
    {
      kind: 'pick',
      name: 'plane_face',
      label: '鏡映面にする面',
      target: 'face',
      buttonId: 'mirror-body-plane-face',
      when: (values) => values.base_plane === 'face',
    },
  ],
  build(values) {
    const empty = missingSelection(values, 'target', 'ボディ(面/辺/頂点)')
    if (empty !== null) return { ok: false, reason: empty }
    const basePlane = values.base_plane
    if (typeof basePlane !== 'string') return { ok: false, reason: '鏡映面が選ばれていない' }
    if (basePlane === 'face') {
      // 参照のまま送る(`move_body` の軸と同じ)。
      const face = pickedRefOf(values.plane_face)
      if (face === null) return { ok: false, reason: '鏡映面にする面が指されていない' }
      return { ok: true, args: { plane_face: face } }
    }
    return { ok: true, args: { base_plane: basePlane } }
  },
}

/**
 * 削除。通常モードでもスケッチモードでも同じ1つ(コアの verb が1つで、`selection.require_deletable` がボディ由来と図形の両方を1回で畳むため)。
 * `SOLID_FEATURE_COMMANDS` と `commands/sketch.ts` の一覧の両方へ入れる。
 *
 * `kinds` に `region` を入れないのはコアが領域を削除できないから(`duocad/verbs/delete.py`)。
 * コアが食える種別は全部入れる: 絞ると画面が数え落としたボディまで消え、「1個」と言いながら2個消える。
 * 領域は対象の数に数えず、混ざっていても削除は止まらない(`require_deletable` が領域だけ黙って無視する)。領域だけを選んだときはコアが拒否する。
 * 引数は対象だけで、選び終わりは人がEnter(またはボタンの押し直し)で言う(`needsEnter` が `max: null` で真)。
 */
export const DELETE_SELECTION: CommandDef<'delete'> = {
  id: 'delete',
  label: '削除する',
  buttonId: 'delete',
  preview: 'none',
  args: [
    {
      kind: 'selection',
      name: 'target',
      label: '削除するボディ・図形',
      shapeKind: 'body',
      kinds: ['body', 'face', 'edge', 'vertex', 'sketch_entity'],
      min: 1,
      max: null,
    },
  ],
  build(values) {
    const empty = missingSelection(values, 'target', '削除するボディ・図形')
    if (empty !== null) return { ok: false, reason: empty }
    return { ok: true, args: {} }
  },
}

/** 通常モードのフィーチャ一覧。反復はスケッチの図形が対象なのでここには入らない。 */
export const SOLID_FEATURE_COMMANDS: CommandDef[] = [
  EXTRUDE_FACES,
  FILLET,
  CHAMFER,
  SHELL_BODY,
  BOOLEAN_BODY,
  MOVE_BODY,
  MIRROR_BODY,
  LOFT,
  DELETE_SELECTION,
]

/** スケッチモードで足すぶん。押し出しは領域版(同じ id `extrude`・ボタン `#extrude`)。 */
export const SKETCH_FEATURE_COMMANDS: CommandDef[] = [
  EXTRUDE_REGIONS,
  REVOLVE,
  PATTERN_RECTANGULAR,
  PATTERN_CIRCULAR,
  DELETE_SELECTION,
]

/** そのverbがスケッチの図形を対象にするか(`sketch_id` を足して送る必要があるか)。`category` では分けられない: 反復は `entities` を引数で受ける唯一のフィーチャ。 */
export function needsSketchId(verb: string): boolean {
  return verb === PATTERN_RECTANGULAR.id || verb === PATTERN_CIRCULAR.id
}
