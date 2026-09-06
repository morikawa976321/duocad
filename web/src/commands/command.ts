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

/** 直接ピック(`PickArgSpec`)で人に出す「どこを触るか」の1行。**問い方を1箇所に集める。** */
const PICK_TARGET_HINTS: Record<PickTarget, string> = {
  edge: '3Dビューの辺',
  vertex: '3Dビューの頂点',
  sketchPoint: 'スケッチの点(線の端点・円/弧の中心など)',
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
 * どの種別の引数も持つ共通部分。**`when` を1箇所に置くためにある。**
 *
 * `when` を種別ごとに書き足す形にすると、書いた種別だけがモード依存になり、
 * 後から足した種別が黙って「常に必須」の側に残る —— `argSatisfied` は
 * `ArgSpec` 全体に対して1つの規則で答える必要がある。
 */
interface ArgSpecCommon {
  name: string
  label: string
  /**
   * **その引数が、いま集まっている値の下で要るか**(省略 = 常に要る)。
   *
   * `move_body` / `move_sketch_entities` の `mode` のように、**選んだ値によって後続の
   * 引数が入れ替わる**コマンドのためにある(並進なら dx/dy/dz、回転なら角度と軸)。
   * 偽を返した引数は `argSatisfied` が「対象外なので揃っている」とみなし、
   * `CommandHost` は入力欄自体を出さない。
   *
   * **verbを分けるのではなくここで分ける**(本人判断) —— `id` を割ると verb 名と
   * コマンドIDの一致(`catalog.Command.name` の契約)が崩れ、送信経路の分岐が増える。
   *
   * **`values` は「いま集まっている値」であって確定値ではない。** まだ打っていない
   * 引数は入っていないので、`values.mode === 'rotate'` のような**肯定形**で書くこと ——
   * `!== 'translate'` と書くと、モードを選ぶ前から回転側の引数を要求し始める。
   */
  when?: (values: ArgValues) => boolean
}

/**
 * 選択から取る引数。**まず共有選択集合を見て、足りないときだけ人に問う**。
 *
 * `max` が `null` なら何本でも(フィレットの辺集合)。`max` があるときは
 * 選択の先頭から `max` 個だけを使う(余分を勝手に捨てるのではなく、`max` を超えた分は
 * 引数に入らないことを画面に出す)。
 */
export interface SelectionArgSpec extends ArgSpecCommon {
  kind: 'selection'
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
export interface NumberArgSpec extends ArgSpecCommon {
  kind: 'number'
  unit: string
  /** 入力欄の `id`(実機検査がここを叩く)。 */
  inputId: string
  placeholder: string
  /**
   * **空欄なら集めた点から導く寸法**(スケッチの長さ・幅・高さ・半径)。
   *
   * `default` の兄弟だが別物である —— `default` は固定値で空欄を埋め、こちらは値を作らず
   * **`build` が集まった点から計算する**。この印が付いた数値は how に数えないので
   * (`isHowInput`)、引数を完成させる最後のクリックでそのまま適用される。
   *
   * **層をまたぐ契約**: `name` は `build` が出す引数のキーと同じでなければならない ——
   * `CommandHost` が空欄のプレースホルダに「いまマウスが指している値」を出すとき、
   * その名前で `build` の結果を引く。
   */
  fromPoints?: true
  /** 使えない値なら理由を返す(使えるなら null)。 */
  reject(value: number): string | null
  /**
   * 空欄のまま確定してよい引数だけが持つ既定値。**opt-in**(個別のverbが明示的に
   * 付けるフィールドで、汎用の既定値ではない) —— 半径・距離・個数など「空欄=
   * まだ決めていない」べき引数に付けると、0が有効値でないところへ黙って0が埋まって
   * 壊れる(`move_body`のdx/dy/dz・`move_sketch_entities`のdx/dyのように「空欄=その軸は
   * 動かさない」が成立する引数だけに付けること)。
   */
  default?: number
}

/**
 * スケッチ平面をクリックして集める点。
 *
 * **選択引数と別物にしてある**。選択は「コアが持つ共有集合から取る」ものだが、点は
 * その場の1クリックであり、集合に残らない(線を引くための始点が選択集合に溜まると、
 * 次のコマンドが拾ってしまう)。
 */
export interface PointArgSpec extends ArgSpecCommon {
  kind: 'point'
}

/**
 * 2つ以上から1つ選ぶ引数。今のところ押し出しの**結合/切り取り**だけ。
 *
 * **既定値を持たない**。既定を `join` にすると「切り取るつもりで足した」が黙って通り、
 * しかも形が増えるので失敗に見えない —— 数値引数に既定を入れないのと同じ理由で、
 * ここも人が1回選ぶまで引数が揃わない扱いにする(サーバ側も `mode` を必須にしてある)。
 */
export interface ChoiceArgSpec extends ArgSpecCommon {
  kind: 'choice'
  options: Array<{ value: string; label: string; buttonId: string }>
}

/**
 * スケッチの図形を選ぶ引数(反復のverbが取る)。
 *
 * **選択引数と別物にしてある**。共有選択集合に入れられるのは面・辺・頂点・領域の4種で、
 * **スケッチの図形はそこに入らない**(コアの `SELECTABLE_KINDS`)。値はスケッチパネルで
 * 選んだ図形のIDで、`CommandHost` へは `entities` プロパティで渡る。
 */
export interface EntitiesArgSpec extends ArgSpecCommon {
  kind: 'entities'
  min: number
}

/**
 * 打ち込む文字列の引数(今のところファイルのパスだけ)。
 *
 * **数値引数と分けてある**。パスは打った文字そのものが値であり、正しさの判定はサーバ
 * (実在するか・書けるか)にしか無い —— ここで拡張子やドライブ文字を検査し始めると、
 * **サーバと2箇所で別々のパス解釈を持つ**ことになる。
 */
export interface TextArgSpec extends ArgSpecCommon {
  kind: 'text'
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
export interface PlaneArgSpec extends ArgSpecCommon {
  kind: 'plane'
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

/**
 * `PickArgSpec` が指させるもの。**3Dの辺・3Dの頂点・スケッチ図形の1点**の3つ。
 *
 * ピックイベント(`viewportTypes.PickEvent`)が既に運んでいる `edge` / `vertex` /
 * `sketchEntity` にそのまま対応する —— 新しい当たり判定を作らずに済むのがこの3つを
 * 選んだ理由で、増やすときは**ピックイベントに既にある種別**から選ぶこと。
 */
export type PickTarget = 'edge' | 'vertex' | 'sketchPoint'

/**
 * **図形を1つ直接ピックして集める引数**(共有選択集合を経由しない)。
 *
 * `SelectionArgSpec` と別物にしてある理由は、**同じ集合に入れると意味が混ざる**から
 * である —— `move_body` の回転軸に使う辺を共有選択へ入れると、コアの
 * `selection.require_bodies` がその辺のボディまで「動かす対象」に数えてしまう
 * (コア側が `commit_selection` を介さない参照で軸を受けているのはこのため)。
 * `PointArgSpec`(スケッチ平面の自由クリック)とも別物で、こちらは**既にある実体**を指す。
 *
 * 値は参照1つ(`SelectionRef`)。種別ごとの形は:
 *
 * | `target` | 値 | 拾う元 |
 * |---|---|---|
 * | `edge` | `{body, kind:'edge', index}` | `PickEvent.edge`(3Dビュー) |
 * | `vertex` | `{body, kind:'vertex', index}` | `PickEvent.vertex`(3Dビュー) |
 * | `sketchPoint` | `{sketch, entity, point}` | `PickEvent.sketchEntity`(スケッチ平面) |
 *
 * **値は参照のまま送る**(座標へ潰さない) —— コアは辺の直線・頂点・図形の点を
 * B-repや `params` から厳密に解決できるが、ブラウザが持っているのは配られた折れ線と
 * 数値だけである。参照で送れば解決はコアの1箇所にとどまり、まっすぐでない辺のような
 * 拒否もコアの既存の規則がそのまま効く。**幽霊(確定前のプレビュー)に要る数値は
 * `App.svelte` が別に解決する** —— そちらは近似ではなく、配られた折れ線・頂点座標・
 * `params` という**コアが出した数値そのもの**から作る。
 */
export interface PickArgSpec extends ArgSpecCommon {
  kind: 'pick'
  /** 何を1つ指させるか。 */
  target: PickTarget
  /** 「指す」ボタンの `id`(実機検査がここを叩く)。指した中身は `<id>-value` に出る。 */
  buttonId: string
  /**
   * **省略してよい引数**(既定はコア側にある)。`true` なら空のままでも引数は揃う。
   *
   * 空欄に既定値を置く `NumberArgSpec.default` と違い、**ここは値を作らない** ——
   * 回転中心の既定(対象の重心 / 代表点の平均)はコアにしか無い規則で、画面側へ写すと
   * 規則が2箇所になり、片方だけ変わったときに幽霊と確定結果が別の場所に出る。
   * **省略されたら引数ごと送らない**のが正しい(`build` がそう書くこと)。
   *
   * 省略できる引数は `firstMissingArg` が指さないので、**人がそこを指したいと言う手**が
   * 別に要る —— `CommandHost` の「指す」ボタン(`armedPick`)がそれである。
   */
  optional?: boolean
}

export type ArgSpec =
  | SelectionArgSpec
  | NumberArgSpec
  | PointArgSpec
  | ChoiceArgSpec
  | EntitiesArgSpec
  | TextArgSpec
  | PlaneArgSpec
  | PickArgSpec

/** スケッチ平面上の点 [u, v](mm)。**定義は `session.ts`**(コアと同じ形)。ここは通すだけ。 */
export type { Point2 }

/**
 * 集まった引数の値。選択は参照の配列、数値は数、点は [u,v]、選択肢は文字列、図形はID列、
 * スケッチを貼る先は文字列(`PlaneArgValue`)、直接ピックは**参照1つ**。
 */
export type ArgValues = Record<
  string,
  SelectionRef[] | SelectionRef | number | Point2 | string | string[]
>

/** 直接ピックの値として読めるなら返す(読めなければ null)。 */
export function pickedRefOf(value: unknown): SelectionRef | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  const ref = value as Record<string, unknown>
  if (typeof ref.sketch === 'string') return typeof ref.entity === 'string' ? (value as SelectionRef) : null
  return typeof ref.body === 'string' && typeof ref.index === 'number' ? (value as SelectionRef) : null
}

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
 * **ファイル操作(保存/開く/STEP入出力等)の多くはこの分類を経由しない**(`FilesPanel.svelte`
 * が `session.runVerb` を直接呼ぶ。`App.svelte` の `runFileOp`/`changesDocument` 参照)。
 * ただし `file` は現役の分類である —— `commands/files.ts` の `IMPORT_STEP` がこの分類を持ち、
 * `App.svelte` の送信経路(`CATEGORY_OF`)もこれを見て `CommandHost` の確定完了を判定する。
 *
 * **`ui` 以外はコアの宣言(`duocad/catalog.py`)の `category` と同じ値**である。
 */
export type CommandCategory = 'sketch' | 'feature' | 'file' | 'ui'

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
 * 後ろに繋いで1つの並びにする(`file` 分類の `CommandDef` は `commands/files.ts` にある ——
 * そちらのモジュールdocstring参照)。スケッチモードの一覧は `commands/sketch.ts`。
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
export function overflowNote(
  command: CommandDef,
  selection: SelectionState | null,
  values: ArgValues,
): string {
  for (const spec of command.args) {
    if (spec.kind !== 'selection' || spec.max === null) continue
    if (!argApplies(spec, values)) continue
    const kinds = argKinds(spec)
    const total = (selection?.items ?? []).filter((item) => kinds.includes(refKind(item))).length
    if (total > spec.max) {
      const counter = KIND_COUNTERS[spec.shapeKind]
      return `${spec.label}は${spec.max}${counter}まで(いま${total}${counter}。Ctrl+クリックで外す)`
    }
  }
  return ''
}

/**
 * **その引数は、いま集まっている値の下で要るか**(`when` が無ければ常に要る)。
 *
 * 判定を1箇所に置いてある —— `argSatisfied` / `overflowNote` / 入力欄の描画が
 * それぞれ `spec.when?.(values)` を書くと、`when` を持たない種別を足したときに
 * どれか1つだけが `undefined` を偽と読む、という食い違いが静かに入る。
 */
export function argApplies(spec: ArgSpec, values: ArgValues): boolean {
  return spec.when === undefined || spec.when(values)
}

/** その引数が揃っているか。**対象外(`when` が偽)の引数は揃っているとみなす。** */
export function argSatisfied(spec: ArgSpec, values: ArgValues): boolean {
  // **対象外は「揃っている」側へ倒す**。`firstMissingArg` は最初の未充足を返すので、
  // 対象外を未充足のままにすると、モードを選んだ瞬間に**使わない側の引数**で
  // 収集が止まる(並進を選んだのに角度を聞かれる)。
  if (!argApplies(spec, values)) return true
  if (spec.kind === 'pick') {
    if (spec.optional === true) return true
    return pickedRefOf(values[spec.name]) !== null
  }
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
  // 点から導ける寸法は**空欄のままで揃っている**(値は `build` が点から作る)。
  if (value === undefined && spec.fromPoints === true) return true
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
 *
 * **`fromPoints` の数値だけは how に数えない** —— 空欄のままでも点から値が決まるので、
 * 「打ち終わったか」を人が言う必要がない(打ちたい人はEnterで言える)。
 */
function isHowInput(spec: ArgSpec): boolean {
  if (spec.kind === 'number') return spec.fromPoints !== true
  return spec.kind === 'choice' || spec.kind === 'text'
}

/**
 * **見えている幽霊をそのまま確定してよいコマンドか**(`fromPoints` の寸法を持つか)。
 *
 * 持っているコマンドは「点をあと1つ待っている」状態でもマウスの位置で形が決まっている
 * ので、そこでのEnterは**画面に出ている形**を確定する。持たないコマンド
 * (`sketch_create`)は幽霊が無いので、今までどおり引数が揃うまで確定できない。
 */
export function commitsGhost(command: CommandDef): boolean {
  return command.args.some((spec) => spec.kind === 'number' && spec.fromPoints === true)
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
 * ここへ落ちるのは **スケッチの描く4verb**(`add_line`/`add_circle`/`add_rectangle`/
 * `add_arc`。寸法欄は持つが `fromPoints` なので how に数えない)と
 * **`sketch_create`**(貼る先の平面1つ)である。
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
  if (spec.kind === 'pick') {
    return `${spec.label}をクリック(${PICK_TARGET_HINTS[spec.target]})`
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

/**
 * 指した参照を人に見せる1行(まだ指していなければ空文字)。
 *
 * **`refKey` を出さない** —— 鍵は `Set`/`Map` の引き当てのための文字列で、人が読む形では
 * ない(`sketch_entity:<uuid>:<uuid>:px1` になる)。ここは種別と番号だけを短く出す。
 */
export function pickedLabel(value: unknown): string {
  const ref = pickedRefOf(value)
  if (ref === null) return ''
  if (refKind(ref) === 'sketch_entity') {
    const item = ref as SketchEntityRef
    return item.point === null ? '図形の最初の点' : `点 ${item.point}`
  }
  const item = ref as ShapeRef
  return `${item.kind === 'edge' ? '辺' : '頂点'} ${item.index}`
}
