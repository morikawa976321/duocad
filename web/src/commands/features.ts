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
 * 反復の展開規則(全周は `360/count`、回転した長方形は4本の線分へ落とす、など)はコアが
 * 持っており、幽霊のためにブラウザへ二重実装すると「画面の予想と実際が違う」という最悪の
 * 食い違いを自作することになる。
 */
import { pickedRefOf, type ArgValues, type CommandDef, type Point2 } from './command'
import { MOVE_MODE_OPTIONS, isRotateMode, isTranslateMode, rejectAngle } from './moveMode'
import type { SelectionRef } from '../session'

/** 0以下を弾く(寸法として使えない)。**既定値を入れない**のがここの流儀。 */
function positive(label: string): (value: number) => string | null {
  return (value) => (value > 0 ? null : `${label}は0より大きい値`)
}

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
 * 領域の押し出し。**手だけで板に穴を開けられるようになる1本**。
 *
 * `extrude_face`(面を押す)とは別のverbである。こちらはスケッチの領域を取り、
 * `join` で足し `cut` で引き、`new` で独立したボディにする。**種別を人に選ばせる**のは、
 * 既定の `join` に落ちると「切り取るつもりで足した」が黙って通り、しかも形が増えるので
 * 失敗に見えないため。
 *
 * **`new`(新規ボディ)を明示的に選べることが要る**のは、`join` が「行き先が無ければ作る」
 * という**落ち方**でしか新しいボディを作れなかったから —— 既存のボディがいる文書では
 * 2つ目の板が作れず、黙って融合していた。
 */
export const EXTRUDE_REGIONS: CommandDef = {
  id: 'extrude',
  category: 'feature',
  label: '領域を押し出す',
  buttonId: 'extrude-regions',
  preview: 'server',
  args: [
    {
      kind: 'selection',
      name: 'regions',
      label: '押し出す領域',
      shapeKind: 'region',
      min: 1,
      max: null,
    },
    {
      kind: 'number',
      name: 'distance',
      label: '距離',
      unit: 'mm',
      inputId: 'regions-distance',
      placeholder: 'mm',
      reject: (value) => (value === 0 ? '距離が0(形が変わらない)' : null),
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
    const empty = missingSelection(values, 'regions', '領域')
    const distance = values.distance
    const mode = values.mode
    if (empty !== null) return { ok: false, reason: empty }
    if (typeof distance !== 'number') return { ok: false, reason: '距離が数値でない' }
    if (typeof mode !== 'string') return { ok: false, reason: '種別が選ばれていない' }
    return { ok: true, args: { distance, mode } }
  },
}

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
export const FILLET: CommandDef = {
  id: 'fillet',
  category: 'feature',
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
      placeholder: 'mm',
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
export const CHAMFER: CommandDef = {
  id: 'chamfer',
  category: 'feature',
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
      placeholder: 'mm',
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
 * 矩形反復。`nx`/`ny` は**元を含めた個数**(Fusion360と同じ流儀・コアの契約)。
 *
 * `sketch_id` はここでは付けない —— どのスケッチかは「今どのスケッチモードに居るか」で
 * あって、コマンドが集める引数ではない(`App.svelte` が送信時に足す)。
 */
export const PATTERN_RECTANGULAR: CommandDef = {
  id: 'pattern_rectangular',
  category: 'feature',
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
      placeholder: 'mm',
      reject: () => null, // 0 は「その方向には並べない」で正当(dx=0 の1列は普通に使う)
    },
    {
      kind: 'number',
      name: 'dy',
      label: 'Y間隔',
      unit: 'mm',
      inputId: 'pattern-dy',
      placeholder: 'mm',
      reject: () => null,
    },
    {
      kind: 'number',
      name: 'nx',
      label: 'X個数',
      unit: '個',
      inputId: 'pattern-nx',
      placeholder: '個',
      reject: count('X個数', 1),
    },
    {
      kind: 'number',
      name: 'ny',
      label: 'Y個数',
      unit: '個',
      inputId: 'pattern-ny',
      placeholder: '個',
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
export const PATTERN_CIRCULAR: CommandDef = {
  id: 'pattern_circular',
  category: 'feature',
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
      placeholder: '個',
      reject: count('個数', 2),
    },
    {
      kind: 'number',
      name: 'total_angle',
      label: '全角度',
      unit: '度',
      inputId: 'pattern-angle',
      placeholder: '度',
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
 * ソリッドを触るモードで足すぶん(押し出し・丸める・面取り)。
 *
 * `App.svelte` が `COMMANDS`(`extrude_face`)の後ろへ繋ぐ。**反復はスケッチの
 * 図形を対象にするのでここには入らない**(下の `SKETCH_FEATURE_COMMANDS`)。
 */
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
export const MOVE_BODY: CommandDef = {
  id: 'move_body',
  category: 'feature',
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
    {
      kind: 'choice',
      name: 'copy',
      label: '複製',
      options: [
        { value: 'move', label: '動かす', buttonId: 'move-body-move' },
        { value: 'copy', label: '複製する', buttonId: 'move-body-copy' },
      ],
    },
    {
      kind: 'choice',
      name: 'mode',
      label: '種別',
      options: MOVE_MODE_OPTIONS.map((option) => ({
        ...option,
        buttonId: `move-body-${option.value}`,
      })),
    },
    {
      kind: 'number',
      name: 'dx',
      label: 'X',
      unit: 'mm',
      inputId: 'move-body-dx',
      placeholder: 'mm',
      reject: finite('X'),
      // 空欄はこの軸を動かさない(=0)扱い。3軸とも空欄(=全部0)は`build`が別途弾く。
      default: 0,
      when: isTranslateMode,
    },
    {
      kind: 'number',
      name: 'dy',
      label: 'Y',
      unit: 'mm',
      inputId: 'move-body-dy',
      placeholder: 'mm',
      reject: finite('Y'),
      default: 0,
      when: isTranslateMode,
    },
    {
      kind: 'number',
      name: 'dz',
      label: 'Z',
      unit: 'mm',
      inputId: 'move-body-dz',
      placeholder: 'mm',
      reject: finite('Z'),
      default: 0,
      when: isTranslateMode,
    },
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
    {
      kind: 'number',
      name: 'angle',
      label: '角度',
      unit: '度',
      inputId: 'move-body-angle',
      placeholder: '度',
      // **既定値を置かない**(dx/dy/dz と違い「空欄=回さない」が成立しない ——
      // 0度はコアが断る値であって、無指定の代わりにはならない)。
      reject: rejectAngle,
      when: isRotateMode,
    },
  ],
  build(values) {
    const empty = missingSelection(values, 'target', 'ボディ(面/辺/頂点)')
    if (empty !== null) return { ok: false, reason: empty }
    const copy = values.copy === 'copy'
    if (values.mode === 'rotate') {
      const angle = values.angle
      if (typeof angle !== 'number') return { ok: false, reason: '角度が数値でない' }
      const reason = rejectAngle(angle)
      if (reason !== null) return { ok: false, reason }
      // **軸は参照のまま送る。** 辺が乗る直線を厳密に出せるのはコアだけで(ブラウザが
      // 持っているのは配られた折れ線)、まっすぐでない辺の拒否もコア側の規則が効く。
      const axis = pickedRefOf(values.axis_edge)
      if (axis === null) return { ok: false, reason: '回転軸にする辺が指されていない' }
      const center = pickedRefOf(values.center_vertex)
      // **指していない中心は引数ごと送らない**(既定=対象の合成重心はコアが決める)。
      return {
        ok: true,
        args: {
          mode: 'rotate',
          angle,
          axis_edge: axis,
          ...(center === null ? {} : { center_vertex: center }),
          copy,
        },
      }
    }
    const dx = values.dx
    const dy = values.dy
    const dz = values.dz
    if (typeof dx !== 'number' || typeof dy !== 'number' || typeof dz !== 'number') {
      return { ok: false, reason: '移動量が数値でない' }
    }
    // **0,0,0 はコアも弾く**(移動なら何も起きず、複製なら元と完全に重なる)。
    // ここで先に断るのは、理由を人の近くで出すため。
    if (dx === 0 && dy === 0 && dz === 0) return { ok: false, reason: '移動量が全部0' }
    // **`mode` を明示的に載せる** —— コア側の既定も `translate` だが、モードを人に
    // 選ばせた結果を送らないと、画面の「並進」ボタンが何も意味しないことになる。
    return { ok: true, args: { mode: 'translate', dx, dy, dz, copy } }
  },
}

/**
 * 削除。**通常モードでもスケッチモードでも同じ1つ**である。
 *
 * `CommandDef` を2つに割らないのは、コアの verb が1つだから ——
 * `selection.require_deletable` がボディ由来(面/辺/頂点/ボディ)と図形の**両方**を1回で
 * 畳むので、UI側で分けると「どちらのボタンを押したか」でサーバの挙動が変わるように見える
 * (実際には変わらない)。`EXTRUDE_REGIONS` が既に両方の一覧に居るのと同じ形で、
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
export const DELETE_SELECTION: CommandDef = {
  id: 'delete',
  category: 'feature',
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

export const SOLID_FEATURE_COMMANDS: CommandDef[] = [
  EXTRUDE_REGIONS,
  FILLET,
  CHAMFER,
  MOVE_BODY,
  DELETE_SELECTION,
]

/**
 * スケッチモードで足すぶん。**領域押し出しは両方に居る** —— 描いた直後にそのまま
 * 押し出せることと、モードを抜けてから領域を選んで押し出せることの両方が要る。
 *
 * **通常モードの側で領域を指す道は3Dクリックだけである** —— このコマンドを起動している
 * 間だけ全スケッチの領域が3Dで指せる(`App.svelte` の `wantsRegions` →
 * `viewport.pickRegions`。辺と同じ規律)。
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
