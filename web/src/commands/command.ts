/**
 * コマンド層 — **引数を集める共通の仕組み**。
 *
 * ```
 * 起動 → 引数収集 → プレビュー → 確定(Enter) / 中断(Esc)
 * ```
 *
 * **verbはここに載せるだけ**にして、状態機械は `CommandHost.svelte` の1つだけにする。
 * **Enterが要るかどうかも `args` から導出する** —— 規則は下の `needsEnter` に1つだけ置いてある。
 *
 * 引数収集は**まず共有選択集合を見る**。AIが `propose_selection` で辺の集合を提案 → 人が
 * 承認して verb を起動すると、その選択がそのまま verb の対象になる。空のときだけ人に問う。
 *
 * **ただし選択の中身をサーバへ送ることはしない**。対象集合(面・辺・領域)はコアが自分の確定
 * 選択から取るので、`build` が作るのは数値・選択肢・パスだけである。ここで選択を読むのは
 * 「何を待っているか」を画面に出し、Enterを許すかを決めるためだけ。
 *
 * ここ(`command.ts`)は**Svelteに依存しない純粋な定義と道具**だけを持つ。
 * 状態機械・DOM・通信は `CommandHost.svelte` の側にある。
 */
import type {
  Point2,
  SelectionRef,
  SelectionState,
  ShapeKind,
  ShapeRef,
  SketchEntityRef,
} from '../session'

/** 選択の種別。コアの `SELECTABLE_KINDS` と同じ顔ぶれ(`tests/test_optimize_setup.py` が照合)。 */
export type SelectableKind = ShapeKind | 'body' | 'region' | 'sketch_entity'

/** 種別ごとの数え方(「辺 4本」「面 2面」)。件数表示を1箇所に集める。
 *
 * **`Record<SelectableKind, string>` なので、種別を足したらここも埋まる** ——
 * 埋め忘れると件数が `undefined` になり、型検査も実行時例外も出ないまま画面に出る。 */
const KIND_COUNTERS: Record<SelectableKind, string> = {
  face: '面',
  edge: '本',
  vertex: '点',
  body: '個',
  region: '個',
  sketch_entity: '個',
}

// --------------------------------------------------------------------------------------
// 参照(ShapeRef / RegionRef)の道具 — コアの `duocad/selection.py` と同じ判定を持つ
// --------------------------------------------------------------------------------------
/**
 * 参照の種別を返す。**RegionRef は `kind` キーを持たない**ので `sketch` の有無で見分ける
 * (§3.5のデータモデル。コアの `selection.ref_kind` と同じ規則)。
 */
export function refKind(ref: SelectionRef): SelectableKind {
  if (!('sketch' in ref)) return (ref as ShapeRef).kind
  // **どちらも `sketch` を持つ**ので `entity` の有無で見分ける(コアの `ref_kind` と同じ)。
  return 'entity' in ref ? 'sketch_entity' : 'region'
}

/**
 * 参照の**同一性の鍵**(コアの `selection.ref_key` と同じ規則)。等しい鍵 ⇔ 同じ実体。
 *
 * **等価判定を書き下ろす場所はここ1つだけ**にしてある(`sameRef` はこの一致で定義される)。
 * 鍵と同値判定を別々に書くと「Set が落とす重複と `sameRef` が言う重複がずれる」が
 * 静かに入る —— コア側の `ref_key` と同じ理由。`Set`/`Map` の引き当てに使うので**文字列**。
 *
 * | 種別 | 鍵 |
 * |---|---|
 * | region | `region:<sketch>:<index>` |
 * | sketch_entity | `sketch_entity:<sketch>:<entity>:<point>` |
 * | body | `body:<body>` —— **添字を見ない**(Bodyは他のBodyの部分形状ではない) |
 * | face / edge / vertex | `<kind>:<body>:<index>` |
 *
 * `provenance` は鍵に入れない(コアの `same_ref` が見ていない)。
 * **点は `p` を付けて書く** —— 素で並べると `point: null`(図形全体)と、仮に空文字の点名が
 * 来たときが同じ鍵になる。第1要素が種別なので、種別が違えば必ず別の鍵になる。
 */
export function refKey(ref: SelectionRef): string {
  const kind = refKind(ref)
  if (kind === 'sketch_entity') {
    const item = ref as SketchEntityRef
    // **点と辺は別物**(同じ図形の (x0,y0) と「図形全体」を混同しない)
    const point = item.point ?? null
    return `sketch_entity:${item.sketch}:${item.entity}:${point === null ? '' : `p${point}`}`
  }
  if (kind === 'region') {
    const item = ref as { sketch: string; index: number }
    return `region:${item.sketch}:${item.index}`
  }
  const item = ref as ShapeRef
  // **Bodyそのものは添字を持たない**。`index` を鍵に混ぜると、`index` が付いた参照と
  // 付いていない参照が別物になる(コアの `ref_key` と同じ扱い)。
  if (kind === 'body') return `body:${item.body}`
  return `${item.kind}:${item.body}:${item.index}`
}

/** 2つの参照が同じ実体を指すか(`provenance` は見ない。コアの `same_ref` と同じ)。 */
export function sameRef(left: SelectionRef, right: SelectionRef): boolean {
  return refKey(left) === refKey(right)
}

/** 面の参照を作る(ビューポートのピック結果から)。添字は1始まり。 */
export function faceRef(bodyId: string, faceIndex: number): ShapeRef {
  return { body: bodyId, kind: 'face', index: faceIndex }
}

/** スケッチの図形(または点)の参照を作る。`point` が null なら図形全体(= 辺)。 */
export function sketchEntityRef(
  sketchId: string,
  entityId: string,
  point: string | null,
): SketchEntityRef {
  return { sketch: sketchId, entity: entityId, point }
}

// --------------------------------------------------------------------------------------
// 引数の仕様
// --------------------------------------------------------------------------------------
/**
 * 選択から取る引数。**まず共有選択集合を見て、足りないときだけ人に問う**。
 *
 * `max` が `null` なら何本でも(フィレットの辺集合)。`max` があるときは
 * 選択の先頭から `max` 個だけを使う(余分を勝手に捨てるのではなく、`max` を超えた分は
 * 引数に入らないことを画面に出す)。
 */
export interface SelectionArgSpec {
  kind: 'selection'
  name: string
  label: string
  /** 主たる種別(件数の数え方と問いの文言はこれで決まる)。 */
  shapeKind: SelectableKind
  /**
   * **食える種別が複数あるとき**の全体(省略すると `[shapeKind]`)。
   *
   * `move_body` がこれを使う唯一の例で、面でも辺でも頂点でもボディを名指しできる
   * (コアの `selection.require_bodies` が3種のどれからでも逆引きする)。ここを
   * `shapeKind` 1つに固定したままだと、辺だけ選んで move_body を起動したときに
   * **サーバは通すのに画面だけが「面が選ばれていない」と言う**食い違いが出る。
   */
  kinds?: SelectableKind[]
  min: number
  max: number | null
}

/** 打ち込む数値の引数(距離・半径・個数)。 */
export interface NumberArgSpec {
  kind: 'number'
  name: string
  label: string
  unit: string
  /** 入力欄の `id`(実機検査がここを叩く)。 */
  inputId: string
  placeholder: string
  /** 使えない値なら理由を返す(使えるなら null)。 */
  reject(value: number): string | null
}

/**
 * スケッチ平面をクリックして集める点。
 *
 * **選択引数と別物にしてある**。選択は「コアが持つ共有集合から取る」ものだが、点は
 * その場の1クリックであり、集合に残らない(線を引くための始点が選択集合に溜まると、
 * 次のコマンドが拾ってしまう)。
 */
export interface PointArgSpec {
  kind: 'point'
  name: string
  label: string
}

/**
 * 2つ以上から1つ選ぶ引数。今のところ押し出しの**結合/切り取り**だけ。
 *
 * **既定値を持たない**。既定を `join` にすると「切り取るつもりで足した」が黙って通り、
 * しかも形が増えるので失敗に見えない —— 数値引数に既定を入れないのと同じ理由で、
 * ここも人が1回選ぶまで引数が揃わない扱いにする(サーバ側も `mode` を必須にしてある)。
 */
export interface ChoiceArgSpec {
  kind: 'choice'
  name: string
  label: string
  options: Array<{ value: string; label: string; buttonId: string }>
}

/**
 * スケッチの図形を選ぶ引数(反復のverbが取る)。
 *
 * **選択引数と別物にしてある**。共有選択集合に入れられるのは面・辺・頂点・領域の4種で、
 * **スケッチの図形はそこに入らない**(コアの `SELECTABLE_KINDS`)。値はスケッチパネルで
 * 選んだ図形のIDで、`CommandHost` へは `entities` プロパティで渡る。
 */
export interface EntitiesArgSpec {
  kind: 'entities'
  name: string
  label: string
  min: number
}

/**
 * 打ち込む文字列の引数(今のところファイルのパスだけ)。
 *
 * **数値引数と分けてある**。パスは打った文字そのものが値であり、正しさの判定はサーバ
 * (実在するか・書けるか)にしか無い —— ここで拡張子やドライブ文字を検査し始めると、
 * **サーバと2箇所で別々のパス解釈を持つ**ことになる。
 */
export interface TextArgSpec {
  kind: 'text'
  name: string
  label: string
  /** 入力欄の `id`(実機検査がここを叩く)。 */
  inputId: string
  placeholder: string
  /** 使えない値なら理由を返す(使えるなら null)。空文字の拒否くらいにとどめる。 */
  reject(value: string): string | null
}

/**
 * **スケッチを貼る先**を1つ指す引数(`sketch_create` だけが取る)。
 *
 * 値は2状態しかない: 基準平面の名前(`'XY' | 'XZ' | 'YZ'`)か、**面は選択にある**を表す
 * `'face'` である。面の側を選択引数にしないのは、この引数が「基準平面か面か」という
 * *どちらか*であって、選択集合から導出できないため —— 選択が空でも基準平面なら揃う。
 *
 * `build` が送るのは基準平面のときの `{base_plane}` だけで、**面は送らない**
 * (`extrude_face` と同じ流儀。面はサーバがコアの確定選択から取る)。
 */
export interface PlaneArgSpec {
  kind: 'plane'
  name: string
  label: string
}

/**
 * `PlaneArgSpec` が取りうる値。基準平面3つ(`session.ts` の `BasePlaneName` と同じ綴り)と、
 * 「貼る先の面はコアの確定選択にある」を表す `'face'`。
 */
export type PlaneArgValue = 'XY' | 'XZ' | 'YZ' | 'face'

/** `PlaneArgSpec` に入れてよい値か。 */
export function isPlaneArgValue(value: unknown): value is PlaneArgValue {
  return value === 'XY' || value === 'XZ' || value === 'YZ' || value === 'face'
}

export type ArgSpec =
  | SelectionArgSpec
  | NumberArgSpec
  | PointArgSpec
  | ChoiceArgSpec
  | EntitiesArgSpec
  | TextArgSpec
  | PlaneArgSpec

/** スケッチ平面上の点 [u, v](mm)。**定義は `session.ts`**(コアと同じ形)。ここは通すだけ。 */
export type { Point2 }

/**
 * 集まった引数の値。選択は参照の配列、数値は数、点は [u,v]、選択肢は文字列、図形はID列、
 * スケッチを貼る先は文字列(`PlaneArgValue`)。
 */
export type ArgValues = Record<string, SelectionRef[] | number | Point2 | string | string[]>

/** `build` の結果。作れないときは理由を返す(黙って何も送らない、をしない)。 */
export type BuildResult =
  | { ok: true; args: Record<string, unknown> }
  | { ok: false; reason: string }

/**
 * コマンドの分類。**送信経路の分岐はこれ1つで決まる。**
 *
 * - `sketch`  … スケッチのverb(`sketch_id` を足して送る。スケッチモードでなければ送らない)
 * - `feature` … ソリッドのverb(失敗しても打った値を戻して打ち直せるようにする)
 * - `ui`      … サーバへ何も送らない道具(スケッチモードの「選ぶ」だけ)
 *
 * **ファイル操作(保存/開く/STEP入出力等)はこの分類を経由しない**(2026-08-27に
 * `FilesPanel.svelte` が `session.runVerb` を直接呼ぶ形へ移設した。`App.svelte` の
 * `runFileOp`/`changesDocument` 参照)。かつて存在した `file` 分類は、この移設で
 * `CommandDef` 側の使用が0件になったため2026-09-03に削除した。
 *
 * **`ui` 以外はコアの宣言(`duocad/catalog.py`)の `category` と同じ値**である。
 */
export type CommandCategory = 'sketch' | 'feature' | 'ui'

export interface CommandDef {
  /** verb名。**プレビューと確定で同じものをサーバへ送る**(食い違うと嘘のプレビューになる)。 */
  id: string
  /** どの経路で送るか(`CommandCategory`)。**verbを足したら必ず書く。** */
  category: CommandCategory
  label: string
  /** ツールバーのボタンの `id`(実機検査がここを叩く)。 */
  buttonId: string
  /**
   * ツールバーにボタンを出さない(2026-09-03)。verb自体は生きたまま
   * (`id`/`category`/`build` は通常どおり機能する)、`CommandHost` の描画だけ外す ——
   * 対象は `select_in_sketch` のみ:直接クリックでの選択と重複するボタンを人間向けUIから
   * 落とす一方、verbとしては(AI等の別経路のために)残す。
   */
  hidden?: boolean
  args: ArgSpec[]
  /**
   * 確定前の姿の出し方:
   *
   * - `server` … サーバの `PREVIEWABLE_VERBS` に載っている(投機実行して形を測らせる)
   * - `local`  … ブラウザだけで描ける(スケッチの図形。**サーバへ投げない**ので
   *   「Escで文書が変わらない」が構造的に成立する)
   * - `none`   … 出さない
   */
  preview: 'server' | 'local' | 'none'
  /** 集まった引数から、サーバへ渡すverbの引数を作る。 */
  build(values: ArgValues): BuildResult
}

// --------------------------------------------------------------------------------------
// コマンドの定義
// --------------------------------------------------------------------------------------
/** 面の押し出し。 */
export const EXTRUDE_FACE: CommandDef = {
  id: 'extrude_face',
  category: 'feature',
  label: '押し出す',
  buttonId: 'extrude',
  preview: 'server',
  args: [
    { kind: 'selection', name: 'face', label: '押し出す面', shapeKind: 'face', min: 1, max: 1 },
    {
      kind: 'number',
      name: 'distance',
      label: '距離',
      unit: 'mm',
      inputId: 'distance',
      placeholder: 'mm',
      // 既定値を入れないのと同じ理由で0を弾く: 打たずにEnterした結果
      // 意図しない距離で確定するのを防ぐ。正=足す / 負=削る。
      reject: (value) => (value === 0 ? '距離が0(正=足す / 負=削る)' : null),
    },
  ],
  build(values) {
    // **面そのものは送らない。** 押す面とそのBodyはサーバがコアの確定選択から取る
    // (`duocad/selection.py` の `require_confirmed`)。ここで見るのは、人がまだ
    // 選んでいないことを画面へ出すためだけ。
    const faces = (values.face ?? []) as SelectionRef[]
    const distance = values.distance
    if (faces.length === 0) return { ok: false, reason: '面が選ばれていない' }
    if (typeof distance !== 'number' || !Number.isFinite(distance)) {
      return { ok: false, reason: '距離が数値でない' }
    }
    return { ok: true, args: { distance } }
  },
}

/**
 * ソリッドを触るモードの一覧のうち、**このファイルが定義するぶん**(押し出し1つ)。
 * `App.svelte` が `SOLID_FEATURE_COMMANDS`(features.ts)と `SKETCH_CREATE`(sketch.ts)を
 * 後ろに繋いで1つの並びにする(`file` 分類の `CommandDef` は2026-08-28からもう無い ——
 * `commands/files.ts` のモジュールdocstring)。スケッチモードの一覧は `commands/sketch.ts`。
 */
export const COMMANDS: CommandDef[] = [EXTRUDE_FACE]

export function findCommand(id: string, list: CommandDef[] = COMMANDS): CommandDef {
  const found = list.find((command) => command.id === id)
  if (!found) throw new Error(`unknown command: ${id}`)
  return found
}

// --------------------------------------------------------------------------------------
// 引数収集の判定(状態機械が使う純粋な計算)
// --------------------------------------------------------------------------------------
/**
 * 選択集合から、この引数が食える参照を取り出す。
 *
 * **種別で絞る**(面のコマンドに辺を渡さない)。`max` があれば先頭からその数だけ。
 * 順序は選択の追加順のまま(コアが順序を保つ契約。フィレットの辺と半径の対応が
 * ずれないため)。
 */
export function argKinds(spec: SelectionArgSpec): SelectableKind[] {
  return spec.kinds ?? [spec.shapeKind]
}

export function itemsForArg(spec: SelectionArgSpec, selection: SelectionState | null): SelectionRef[] {
  const kinds = argKinds(spec)
  const items = (selection?.items ?? []).filter((item) => kinds.includes(refKind(item)))
  return spec.max === null ? items : items.slice(0, spec.max)
}

/**
 * `max` を超えて選ばれている引数があれば理由を返す(無ければ空文字)。
 *
 * **黙って先頭だけ使わない**し、**送りもしない**。ここで先頭を選んで送ると、UIだけが
 * 「どれを押すか」を勝手に決めることになる(コア側の `require_confirmed(exactly_one=True)` は
 * 2枚以上を拒否する)。`CommandHost` はこれが空でない間 Enter を受け付けない。
 */
export function overflowNote(command: CommandDef, selection: SelectionState | null): string {
  for (const spec of command.args) {
    if (spec.kind !== 'selection' || spec.max === null) continue
    const kinds = argKinds(spec)
    const total = (selection?.items ?? []).filter((item) => kinds.includes(refKind(item))).length
    if (total > spec.max) {
      const counter = KIND_COUNTERS[spec.shapeKind]
      return `${spec.label}は${spec.max}${counter}まで(いま${total}${counter}。Ctrl+クリックで外す)`
    }
  }
  return ''
}

/** その引数が揃っているか。 */
export function argSatisfied(spec: ArgSpec, values: ArgValues): boolean {
  if (spec.kind === 'selection') {
    return ((values[spec.name] ?? []) as SelectionRef[]).length >= spec.min
  }
  if (spec.kind === 'point') {
    const point = values[spec.name]
    return Array.isArray(point) && point.length === 2 && point.every(Number.isFinite)
  }
  if (spec.kind === 'choice') {
    const value = values[spec.name]
    return typeof value === 'string' && spec.options.some((option) => option.value === value)
  }
  if (spec.kind === 'entities') {
    const ids = values[spec.name]
    return Array.isArray(ids) && ids.length >= spec.min && ids.every((id) => typeof id === 'string')
  }
  if (spec.kind === 'text') {
    const text = values[spec.name]
    return typeof text === 'string' && spec.reject(text) === null
  }
  if (spec.kind === 'plane') {
    return isPlaneArgValue(values[spec.name])
  }
  const value = values[spec.name]
  return typeof value === 'number' && Number.isFinite(value) && spec.reject(value) === null
}

/** まだ揃っていない最初の引数(全部揃っていれば null)。 */
export function firstMissingArg(command: CommandDef, values: ArgValues): ArgSpec | null {
  return command.args.find((spec) => !argSatisfied(spec, values)) ?? null
}

/**
 * その引数は**対象(target)を複数取りうる**か —— `needsEnter` の条件(1)。
 *
 * 選択引数は上限が1でなければ複数取りうる(`max === null` は何本でも)。図形引数
 * (`entities`)は上限そのものが無い。**いつ選び終わったかは機械には分からない**ので、
 * 終わりを人が言う必要がある。
 */
function takesManyTargets(spec: ArgSpec): boolean {
  if (spec.kind === 'selection') return spec.max !== 1
  return spec.kind === 'entities'
}

/**
 * その引数は**how(どうするか)の入力**か —— `needsEnter` の条件(2)。
 *
 * 数値・選択肢・文字列。打ち終わり / 選び終わりは、対象と同じく機械には分からない。
 * **新しい入力の種別を足すときは、必ずここへ足すこと**(下の既定の説明を読むこと)。
 */
function isHowInput(spec: ArgSpec): boolean {
  return spec.kind === 'number' || spec.kind === 'choice' || spec.kind === 'text'
}

/**
 * **Enterを押して確定する必要があるコマンドか**(要らないものは「即適用」になる)。
 *
 * per-commandのフラグを置かず `args` から**導出する**。狙いは、後から足すverbが何も書かなくても
 * 同じ規則に従うことである(フラグにすると、書き忘れたverbだけが黙って別の挙動になる)。
 *
 * 次の**どちらかに当たれば Enter が要る**:
 *
 * 1. **対象を複数取りうる**(`takesManyTargets`) …「まだ足すのか、これで終わりか」を
 *    機械が決められない。
 * 2. **howの入力がある**(`isHowInput`) … 打ち終わり・選び終わりを機械が決められない。
 *
 * **どちらでもない = 対象が1つに定まり、howが無い**ものだけが即適用になる。現行で
 * ここへ落ちるのは **`add_line`(点2つ)・`sketch_create`(貼る先の平面1つ)**。
 *
 * **列挙していない種別は「即適用の側」に落ちる(意図した既定)。** 対象を1つだけ指す新しい
 * 種別はここを直さなくても即適用になる。逆に、**新しい how を足すときは `isHowInput` へ
 * 足すこと** —— 忘れると、打っている途中の値で適用される。
 *
 * 引数が1つも無いコマンド(`select_in_sketch`)もここでは `false` になるが、即適用は
 * **引数を完成させるクリックでしか発火しない**(`CommandHost.svelte`)ので、完成させる引数が
 * 無いコマンドは何をしても発火しない。
 *
 * **Enterが要らない = Enterが効かない、ではない。** 起動より前から対象が揃っていた場合
 * (AIが選んだ面のまま起動した等)は即適用の発火条件を満たさないので、Enter(またはボタンの
 * 押し直し)が唯一の適用手段として残る。
 */
export function needsEnter(command: CommandDef): boolean {
  return command.args.some((spec) => takesManyTargets(spec) || isHowInput(spec))
}

/**
 * その引数について、人に何を問うかの1行。
 *
 * 数値引数は「打った値が使えない」場合にその理由を出す(空欄のときは何を打つかを出す)。
 */
export function promptFor(spec: ArgSpec, values: ArgValues): string {
  if (spec.kind === 'selection') {
    const have = ((values[spec.name] ?? []) as SelectionRef[]).length
    const need = spec.min - have
    return `${spec.label}を${need}${KIND_COUNTERS[spec.shapeKind]}選ぶ(クリック / Shift・Ctrl+クリックでトグル / 左ドラッグで囲む)`
  }
  if (spec.kind === 'point') {
    return `${spec.label}をクリック(スケッチ平面の上)`
  }
  if (spec.kind === 'choice') {
    return `${spec.label}を選ぶ(${spec.options.map((option) => option.label).join(' / ')})`
  }
  if (spec.kind === 'entities') {
    const have = ((values[spec.name] ?? []) as string[]).length
    return `${spec.label}を${spec.min - have}個選ぶ(スケッチの図形を3Dでクリック)`
  }
  if (spec.kind === 'text') {
    const text = values[spec.name]
    if (typeof text === 'string' && text !== '') return spec.reject(text) ?? ''
    return `${spec.label}を打つ(${spec.placeholder})`
  }
  if (spec.kind === 'plane') {
    return `${spec.label}をクリック(基準平面のグリッド XY/XZ/YZ か、ソリッドの面)`
  }
  const value = values[spec.name]
  if (typeof value === 'number' && Number.isFinite(value)) {
    return spec.reject(value) ?? ''
  }
  return `${spec.label}を打つ(${spec.unit})`
}
