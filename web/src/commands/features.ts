/**
 * フィーチャのコマンド定義 — **AIからしか使えないverbが無くなる場所**。
 *
 * 領域押し出し・フィレット・面取り・矩形反復・円形反復の5つはコアにあり、ここは
 * `duocad/server.py` の `FEATURE_COMMANDS` に開いた口へ引数の集め方を書くだけである
 * (コアには何も足していない)。状態機械・数値入力・プレビュー・中断は
 * `CommandHost.svelte` の1つを使い回す。
 *
 * **対象集合はサーバへ送らない**。領域・辺は**コアの確定選択そのもの**で、ここが送るのは
 * 数値と選択肢だけ。どの辺を丸めるかはサーバ側の `Session` が自分の選択集合から取る
 * (`selection.require_*`)。選択引数の定義(`kind: 'selection'`)は残るが、**送るためでは
 * なく、人がまだ選んでいないことを画面に出し、辺のピッキングを起動するため**である
 * (`CommandHost` の `onWants`)。
 *
 * **図形(反復の対象)だけは選択集合から取れない**。コアの選択集合に入るのは面・辺・頂点・
 * 領域の4種で、スケッチの図形は入らない。スケッチパネルで選び、`entities` 引数として渡る。
 *
 * **反復にはプレビューを出さない**。押し出し・フィレット・面取りはサーバに投機実行させるが、
 * 反復の展開規則(全周は `360/count` の刻み角、など)はコアが
 * 持っており、幽霊のためにブラウザへ二重実装すると「画面の予想と実際が違う」という最悪の
 * 食い違いを自作することになる。
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

/**
 * 選択引数が1つでも空なら理由を返す(埋まっていれば null)。
 *
 * **値そのものは送らない。** 対象集合(面・辺・領域)はコアが自分の確定選択から取るので、
 * ここで見るのは「人がまだ選んでいない」を画面へ出すためだけである。
 */
/** 打った値が数値でありさえすればよい引数(移動量は0も負も正当)。 */
function finite(label: string): (value: number) => string | null {
  return (value) => (Number.isFinite(value) ? null : `${label}は数値`)
}

function missingSelection(values: ArgValues, name: string, label: string): string | null {
  return ((values[name] ?? []) as SelectionRef[]).length === 0 ? `${label}が選ばれていない` : null
}

/**
 * 押し出し。**スケッチ編集中は閉領域、通常モードはソリッドの面**を選ぶ1本のverb(`extrude`)。
 * どちらもサーバは確定選択の種別で分岐する(領域と面の混在は断られる)ので、UI は
 * 選ぶ対象の違いだけを持つ2つの定義を作る。`join` で足し `cut` で引き、`new` で独立した
 * ボディにする。**種別を人に選ばせる**のは、既定の `join` に落ちると「切り取るつもりで
 * 足した」が黙って通り、しかも形が増えるので失敗に見えないため。
 *
 * **`new`(新規ボディ)を明示的に選べることが要る**のは、`join` が「行き先が無ければ作る」
 * という**落ち方**でしか新しいボディを作れなかったから —— 既存のボディがいる文書では
 * 2つ目の板が作れず、黙って融合していた。
 *
 * 面の距離は**面の外向きが正**なので、面から削る(`cut`)には負の距離を打つ。
 * 領域の距離は0だけが無効(符号は領域の向きに対する押し出し方向)。
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
 * フィレット。**辺を複数選べることが前提**(複数選択と辺のピッキングの両方が要る)。
 *
 * 半径は1つで全部の辺に効く(コアは辺ごとのリストも取るが、UIから辺ごとに違う半径を
 * 打たせる場面はまだ無い。要るようになったら選択パネル側に付ける)。
 *
 * **失敗の扱いは骨格の外にある**: OCCTのフィレットは例外を投げずに `IsDone()=False` で
 * 失敗し、しかも半径に対して単調でない(§7。r=10.0 失敗 / r=10.5 成功)。コアが例外へ
 * 変換し、サーバが理由を返し、`App.svelte` が**選択を保持したまま打った値を戻す**。
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

/** 面取り。フィレットと同じ集め方(まっすぐな辺は平面・円い辺は円錐になる)。 */
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

/**
 * ソリッドブーリアン。ターゲットは面1枚を直接ピック(`PickArgSpec`)、ツールは確定選択
 * (複数)。`mode` で union/cut/intersect を切り替える(`extrude` の mode と同じ構造)。
 *
 * **ターゲットのピックはツール選択より前** —— `args` の並びが `firstMissingArg` の順序。
 * ターゲットを確定選択に入れると `require_bodies` がツール側とロールが混ざる。
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

/**
 * 矩形反復。`nx`/`ny` は**元を含めた個数**(Fusion360と同じ流儀・コアの契約)。
 *
 * `sketch_id` はここでは付けない —— どのスケッチかは「今どのスケッチモードに居るか」で
 * あって、コマンドが集める引数ではない(`App.svelte` が送信時に足す)。
 */
export const PATTERN_RECTANGULAR: CommandDef<'pattern_rectangular'> = {
  id: 'pattern_rectangular',
  label: '矩形反復',
  buttonId: 'pattern-rectangular',
  // **幽霊はブラウザが描く**。変わるのはスケッチの図形でソリッドではないので、
  // サーバに投機実行させても「形が変わったBody」は1つも返らない(`duocad/catalog.py` の note)。
  // 増える複製の座標は `entityKinds.ts` の表がコアと同じ規則で出せる。
  preview: 'local',
  args: [
    { kind: 'entities', name: 'entities', label: '反復する図形', min: 1 },
    {
      kind: 'number',
      name: 'dx',
      label: 'X間隔',
      unit: 'mm',
      inputId: 'pattern-dx',
      reject: () => null, // 0 は「その方向には並べない」で正当(dx=0 の1列は普通に使う)
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
    // 2個以上並べるのに間隔が両方0なら、複製は元と完全に重なる。**黙って通さない**
    // (成功したのに何も増えて見えない = 原因の分からない不具合になる)。
    if (dx === 0 && dy === 0 && nx * ny > 1) {
      return { ok: false, reason: '間隔が両方0(複製が元と重なる)' }
    }
    if (nx === 1 && ny === 1) return { ok: false, reason: '個数が1×1(反復にならない)' }
    return { ok: true, args: { entities, dx, dy, nx, ny } }
  },
}

/**
 * 円形反復。**全周(360度)は `360/count`、全周未満は `total_angle/(count-1)`** の
 * 場合分けをコアが持っている(全周を `count-1` で割ると最後の複製が最初と重なる)。
 * UIはここを再実装せず、角度と個数をそのまま渡す —— 「全周8個で隣り合う角度が45度」に
 * なることが、その場合分けが効いていることの目視確認になる。
 *
 * 回転中心はスケッチ平面上のクリック(点引数)で取る。**中心と図形の中心が一致すると
 * コアが ValueError**(全複製が元と重なる)なので、その場合は理由が画面に出る。
 */
export const PATTERN_CIRCULAR: CommandDef<'pattern_circular'> = {
  id: 'pattern_circular',
  label: '円形反復',
  buttonId: 'pattern-circular',
  // 矩形反復と同じローカル方式。**回転中心を置くクリックの前から幽霊が出る**
  // (`CommandHost` が待っている点をポインタの位置で仮に埋めるので、中心を動かすと
  //  複製の輪が付いてくる)。
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
 * ボディの平行移動 / 回転 / 複製。**面でも辺でも頂点でもボディを指せる** ——
 * コアの `selection.require_bodies` が `ShapeRef.body` から逆引きするので、
 * この引数は3種を全部食う(`kinds`)。複数のボディにまたがる選択なら全部が同じ量だけ動く。
 *
 * **複製するかどうかは人に選ばせる**(既定を置かない) —— 押し出しの `mode` と同じ理由で、
 * 「複製するつもりで動かした」は元に戻すまで気づきにくい。**並進か回転かも同じ**。
 *
 * **回転軸は別チャンネルで指す**(`kind: 'pick'`)。共有選択集合へ入れると、コアの
 * `require_bodies` が軸に使った辺のボディまで「動かす対象」に数える —— 軸に使った部品まで
 * 一緒に回る、という形で出る。中心は省略できて、既定はコアが決める(対象の合成重心)。
 *
 * **軸のピックは角度より前に置いてある。** `args` の並びがそのまま `firstMissingArg` の
 * 順序、つまり「次に何を問うか」なので、回転を選んだ瞬間に辺が指せる ——
 * 逆順にすると、数値を打ってから3Dを触りに戻る動線になる。
 */
export const MOVE_BODY: CommandDef<'move_body'> = {
  id: 'move_body',
  label: '動かす',
  buttonId: 'move-body',
  // **ローカル方式**。形は変わらず位置だけが変わるので、届いているメッシュを
  // dx/dy/dz だけずらして半透明で重ねれば足りる(OCCでの再計算が要らない唯一のverb)。
  // **元の実体は隠さない** —— 移動前と移動後が並んで見えることがこのプレビューの中身である。
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
    // 移動量は非有限だけ拒否する(0も負も正当。スケッチ側は拒否しない)。
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
      // 省略時の中心は**コアが決める**(`axis_edge` だけなら辺が乗る直線そのもの)。
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
        // **軸は参照のまま送る。** 辺が乗る直線を厳密に出せるのはコアだけで(ブラウザが
        // 持っているのは配られた折れ線)、まっすぐでない辺の拒否もコア側の規則が効く。
        const axis = pickedRefOf(given.axis_edge)
        if (axis === null) return { ok: false, reason: '回転軸にする辺が指されていない' }
        const center = pickedRefOf(given.center_vertex)
        // **指していない中心は引数ごと送らない**(既定=対象の合成重心はコアが決める)。
        return {
          ok: true,
          args: { mode: 'rotate', angle, axis_edge: axis, ...(center === null ? {} : { center_vertex: center }) },
        }
      },
    })
  },
}

/**
 * ボディの鏡映複製。**常に複製を作る**(move_bodyと違い、動かす/複製の選択肢は無い)。
 *
 * 鏡映面の指し方は4つ: 基準平面(XY/XZ/YZ)か、**既存の平らな面を1つクリックする**
 * (`plane_face`)。面は**共有選択集合を経由しない別チャンネル**(`PickArgSpec`)で受ける ——
 * 同じ集合に載せると `require_bodies` が鏡に使った面のボディまで対象に数え、基準にした
 * 部品まで一緒に複製される。
 *
 * **面のピックが開くのは「面を選ぶ」を押した後だけ**である(`when`)。3択のどれかで
 * 済む人の画面には面のピックが現れず、選択のクリックが吸われることもない。
 */
export const MIRROR_BODY: CommandDef<'mirror_body'> = {
  id: 'mirror_body',
  label: '鏡映複製',
  buttonId: 'mirror-body',
  // **幽霊は無い**(`move_body` は持つ)。鏡映面を選んだ時点で複製の位置は一意に決まるので、
  // 確定してからCtrl+Zで戻す運用にしてある。
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
        // **この値はサーバへ行かない**(`base_plane` は XY/XZ/YZ しか受け付けない)。
        // 下の `plane_face` を出すための印で、`build` が読み替える。
        { value: 'face', label: '面を選ぶ', buttonId: 'mirror-body-face' },
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
      // **参照のまま送る**(`move_body` の軸と同じ規律)。面が乗る平面を厳密に出せるのは
      // コアだけで、平らでない面の拒否もコアの既存の規則がそのまま効く。
      const face = pickedRefOf(values.plane_face)
      if (face === null) return { ok: false, reason: '鏡映面にする面が指されていない' }
      return { ok: true, args: { plane_face: face } }
    }
    return { ok: true, args: { base_plane: basePlane } }
  },
}

/**
 * 削除。**通常モードでもスケッチモードでも同じ1つ**である。
 *
 * `CommandDef` を2つに割らないのは、コアの verb が1つだから ——
 * `selection.require_deletable` がボディ由来(面/辺/頂点/ボディ)と図形の**両方**を1回で
 * 畳むので、UI側で分けると「どちらのボタンを押したか」でサーバの挙動が変わるように見える
 * (実際には変わらない)。`EXTRUDE_REGIONS`/`EXTRUDE_FACES` が同じ id で両方の一覧に居るのと同じ形で、
 * この1つを `SOLID_FEATURE_COMMANDS` と `commands/sketch.ts` の一覧の両方へ入れる。
 *
 * `kinds` に **`region` を入れていない**のは、コアが領域を削除できないからである
 * (`duocad/verbs/delete.py`)。**逆に、コアが食える種別は全部入れてある** —— ここを
 * 絞ると「サーバは通すのに画面だけが対象を数え落とす」食い違いになり、しかも消えるのは
 * 数え落としたほうのボディなので、**画面が「1個」と言いながら2個消える**。
 *
 * 領域は**対象の数には数えない**が、確定選択に混ざっていても削除は止まらない ——
 * コアの `require_deletable` が領域だけ黙って無視し、残りを消す。
 * 領域**だけ**を選んでいるとき(=`target` が0件のまま)は、従来どおりコアが拒否する。
 *
 * **howが無い**(引数は対象だけ)。`needsEnter` は `max: null` の側で真になるので、
 * 選び終わりは人がEnter(またはボタンの押し直し)で言う。
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
      // 件数の数え方は「個」。面でも図形でも消える単位はボディ1つ / 図形1つである。
      shapeKind: 'body',
      kinds: ['body', 'face', 'edge', 'vertex', 'sketch_entity'],
      min: 1,
      max: null,
    },
  ],
  build(values) {
    const empty = missingSelection(values, 'target', '削除するボディ・図形')
    if (empty !== null) return { ok: false, reason: empty }
    // **対象そのものは送らない**(サーバがコアの確定選択から取る)。引数は1つも無い。
    return { ok: true, args: {} }
  },
}

/**
 * 通常モードのフィーチャ一覧。**反復はスケッチの図形を対象にするのでここには入らない**
 * (下の `SKETCH_FEATURE_COMMANDS`)。
 */
export const SOLID_FEATURE_COMMANDS: CommandDef[] = [
  EXTRUDE_FACES,
  FILLET,
  CHAMFER,
  BOOLEAN_BODY,
  MOVE_BODY,
  MIRROR_BODY,
  DELETE_SELECTION,
]

/**
 * スケッチモードで足すぶん。**押し出しは領域版**(通常モードは面版。同じ id `extrude`・
 * 同じボタン `#extrude` で、選ぶ対象だけが違う)。
 */
export const SKETCH_FEATURE_COMMANDS: CommandDef[] = [
  EXTRUDE_REGIONS,
  PATTERN_RECTANGULAR,
  PATTERN_CIRCULAR,
  // **削除は通常モードと同じ1つ**(`DELETE_SELECTION` のdocstring)。スケッチモードでは
  // 3Dで点・辺をクリックした `sketch_entity` が選択に入るので、同じボタンが図形を消す。
  DELETE_SELECTION,
]

/**
 * そのverbは**スケッチの図形を対象にするか**(`sketch_id` を足して送る必要があるか)。
 *
 * `category` では分けられない —— 反復は「フィーチャだが対象がスケッチの図形」という
 * 位置にいる(コアでも `entities` を引数で受ける唯一のverbで、図形は選択集合に入らない)。
 */
export function needsSketchId(verb: string): boolean {
  return verb === PATTERN_RECTANGULAR.id || verb === PATTERN_CIRCULAR.id
}
