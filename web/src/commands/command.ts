/**
 * コマンド層 — 引数を集める共通の仕組み(起動 → 引数収集 → プレビュー → 確定(Enter) / 中断(Esc))。
 * verbはここに載せるだけで、状態機械は `CommandHost.svelte` の1つだけ。Enterが要るかも `args` から導出する(`needsEnter`)。
 *
 * 引数収集はまず共有選択集合を見る(AIが `set_selection` で選んだものがそのまま対象になる)。ただし選択の中身は
 * サーバへ送らない: 対象集合(面・辺・領域)はコアが自分の選択から取るので、`build` が作るのは数値・選択肢・パスだけ。
 * このファイルはSvelteに依存しない純粋な定義と道具だけを持つ。
 */
import type {
  BodyRef,
  Point2,
  RegionRef,
  SelectionRef,
  SelectionState,
  ShapeKind,
  ShapeRef,
  SketchEntityRef,
} from '../session'
import type { COMMAND_CATEGORY, CommandArguments, CommandName } from '../generated/protocol'

/** 選択の種別。コアの `SELECTABLE_KINDS` と同じ顔ぶれ(`tests/test_optimize_setup.py` が照合)。 */
export type SelectableKind = ShapeKind | 'body' | 'region' | 'sketch_entity'

/** 種別ごとの数え方(「辺 4本」「面 2面」)。 */
const KIND_COUNTERS: Record<SelectableKind, string> = {
  face: '面',
  edge: '本',
  vertex: '点',
  body: '個',
  region: '個',
  sketch_entity: '個',
}

/** 直接ピック(`PickArgSpec`)で人に出す「どこを触るか」の1行。 */
const PICK_TARGET_HINTS: Record<PickTarget, string> = {
  edge: '3Dビューの辺',
  vertex: '3Dビューの頂点',
  face: '3Dビューの平らな面',
  sketchPoint: 'スケッチの点(線の端点・円/弧の中心など)',
  sketchEntity: 'スケッチの線分',
}

// 参照(ShapeRef / RegionRef)の道具 — コアの `duocad/selection.py` と同じ判定を持つ
/** 参照の種別。RegionRef は `kind` キーを持たないので `sketch` の有無で見分ける(コアの `selection.ref_kind` と同じ規則)。 */
export function refKind(ref: SelectionRef): SelectableKind {
  if (!('sketch' in ref)) return (ref as ShapeRef).kind
  // どちらも `sketch` を持つので `entity` の有無で見分ける(コアの `ref_kind` と同じ)。
  return 'entity' in ref ? 'sketch_entity' : 'region'
}

/**
/**
 * 参照の同一性の鍵(コアの `selection.ref_key` と同じ規則)。等しい鍵 ⇔ 同じ実体。`Set`/`Map` の引き当てに使うので文字列。
 * 等価判定を書き下ろすのはここ1つだけ(`sameRef` はこの一致で定義される)。
 *
 * | 種別 | 鍵 |
 * |---|---|
 * | region | `region:<sketch>:<index>` |
 * | sketch_entity | `sketch_entity:<sketch>:<entity>:<point>` |
 * | body | `body:<body>` —— **添字を見ない**(Bodyは他のBodyの部分形状ではない) |
 * | face / edge / vertex | `<kind>:<body>:<index>` |
 *
 * `provenance` は鍵に入れない(コアの `same_ref` が見ていない)。点は `p` を付けて書く(素で並べると `point: null` と空文字の点名が同じ鍵になる)。
 */
export function refKey(ref: SelectionRef): string {
  const kind = refKind(ref)
  if (kind === 'sketch_entity') {
    const item = ref as SketchEntityRef
    const point = item.point ?? null
    return `sketch_entity:${item.sketch}:${item.entity}:${point === null ? '' : `p${point}`}`
  }
  if (kind === 'region') {
    const item = ref as { sketch: string; index: number }
    return `region:${item.sketch}:${item.index}`
  }
  const item = ref as ShapeRef
  if (kind === 'body') return `body:${item.body}`
  return `${item.kind}:${item.body}:${item.index}`
}

/** 2つの参照が同じ実体を指すか(コアの `same_ref` と同じ)。 */
export function sameRef(left: SelectionRef, right: SelectionRef): boolean {
  return refKey(left) === refKey(right)
}

/** 面の参照を作る。添字は1始まり。 */
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

/** 辺の参照(添字は1始まり。`describe_edges` / `fillet` と同じ体系)。 */
export function edgeRef(bodyId: string, edgeIndex: number): ShapeRef {
  return { body: bodyId, kind: 'edge', index: edgeIndex }
}

/** 頂点の参照(添字は1始まり)。 */
export function vertexRef(bodyId: string, vertexIndex: number): ShapeRef {
  return { body: bodyId, kind: 'vertex', index: vertexIndex }
}

/** 領域の参照(`kind` を持たない。`sketch` と `index` で見分ける)。 */
export function regionRef(sketchId: string, index: number): RegionRef {
  return { sketch: sketchId, index }
}

/** ボディの参照。添字は無く、コアと同じく `index: null` を明示する(`BodyRef` の docstring)。 */
export function bodyRef(bodyId: string): BodyRef {
  return { body: bodyId, kind: 'body', index: null }
}

/** 選択から種別ごとに取り出す。判定は `refKind` に一本化する(`'sketch' in item` だと領域と図形の両方が拾われ、選んだ点が領域の添字として読まれる)。 */
export function refsOfKind(items: readonly SelectionRef[], kind: ShapeKind): ShapeRef[]
export function refsOfKind(items: readonly SelectionRef[], kind: 'body'): BodyRef[]
export function refsOfKind(items: readonly SelectionRef[], kind: 'region'): RegionRef[]
export function refsOfKind(items: readonly SelectionRef[], kind: 'sketch_entity'): SketchEntityRef[]
export function refsOfKind(items: readonly SelectionRef[], kind: SelectableKind): SelectionRef[] {
  return items.filter((item) => refKind(item) === kind)
}

// 引数の仕様
/** どの種別の引数も持つ共通部分。`when` を種別ごとに書くと、後から足した種別が黙って「常に必須」の側に残るので1箇所に置く。 */
interface ArgSpecCommon {
  name: string
  label: string
  /**
   * その引数が、いま集まっている値の下で要るか(省略 = 常に要る)。`move_body` / `move_sketch_entities` の `mode` のように、
   * 選んだ値によって後続の引数が入れ替わるコマンド用。偽の引数は `argSatisfied` が揃っているとみなし、`CommandHost` は入力欄を出さない。
   * verbを分けずここで分ける(本人判断。`id` を割ると verb 名とコマンドIDの一致(`catalog.Command.name` の契約)が崩れる)。
   *
   * `values` は確定値ではなく「いま集まっている値」。肯定形(`values.mode === 'rotate'`)で書くこと
   * (`!== 'translate'` だと、モードを選ぶ前から回転側の引数を要求し始める)。
   */
  when?: (values: ArgValues) => boolean
}

/** 選択から取る引数。まず共有選択集合を見て、足りないときだけ人に問う。`max` が `null` なら何本でも。`max` があるときは先頭から `max` 個だけ使い、超えた分は引数に入らないことを画面に出す。 */
export interface SelectionArgSpec extends ArgSpecCommon {
  kind: 'selection'
  shapeKind: SelectableKind
  /**
   * 食える種別が複数あるときの全体(省略すると `[shapeKind]`)。`move_body` だけが使い、面でも辺でも頂点でもボディを名指しできる
   * (コアの `selection.require_bodies` が3種のどれからでも逆引きする)。`shapeKind` 1つに固定すると、辺だけ選んだときに
   * サーバは通すのに画面だけが「面が選ばれていない」と言う。
   */
  kinds?: SelectableKind[]
  /**
   * 頂点そのものを対象にする引数(測定)。起動中は頂点を指せる種類に入れる(`App.svelte` の `wantedVertices` → `pickKinds`)。
   * `kinds` に `'vertex'` があれば同じく指せる(`move_body` / `mirror_body` の頂点はボディを名指しする手の1つ)。
   */
  pickVertices?: true
  min: number
  max: number | null
}

export interface NumberArgSpec extends ArgSpecCommon {
  kind: 'number'
  unit: string
  /** 入力欄の `id`(実機検査がここを叩く)。 */
  inputId: string
  /**
   * 空欄なら集めた点から導く寸法(スケッチの長さ・幅・高さ・半径)。`default` と違って値を作らず、`build` が点から計算する。
   * この印が付いた数値は how に数えない(`isHowInput`)ので、引数を完成させる最後のクリックでそのまま適用される。
   *
   * 層をまたぐ契約: `CommandHost` は空欄のプレースホルダに「いまマウスが指している値」を出すとき、まず `name` で
   * `build` の結果を引く。引けない寸法は `live` が作る。
   */
  fromPoints?: true
  /**
   * 空欄の薄字に出す値を `build` の結果から作る(`add_line` の長さ・角度のように、名前で引けない寸法だけが持つ)。表示だけに使い、サーバへ行かない。
   */
  live?(args: Record<string, unknown>): number | null
  /** 使えない値なら理由を返す(使えるなら null)。 */
  reject(value: number): string | null
  /**
   * 空欄のまま確定してよい引数だけが持つ既定値(opt-in)。半径・距離・個数など「空欄=まだ決めていない」べき引数に付けると、
   * 0が有効値でないところへ黙って0が埋まって壊れる。`move_body` の dx/dy/dz のように「空欄=その軸は動かさない」が成立する引数だけに付けること。
   */
  default?: number
}

/** スケッチ平面をクリックして集める点。選択引数と別物: 点はその場の1クリックで集合に残らない(始点が選択集合に溜まると次のコマンドが拾う)。 */
export interface PointArgSpec extends ArgSpecCommon {
  kind: 'point'
}

/**
 * 2つ以上から1つ選ぶ引数。既定値を持たない(`join` 既定だと「切り取るつもりで足した」が黙って通る。サーバ側も `mode` を必須にしてある)。
 * `sends` は共有の作業空間(`state.command.args`)へ出すサーバ側の値(省略なら `value` そのまま。`build` と同じ読み替えをここにも
 * 書く: `copy` の 'copy' → true、荷重の 'down' → [0,0,-1])。サーバの値にならない選択肢(鏡映面の「面を選ぶ」)は `null`。
 * 配信された値を欄へ戻すときも同じ対応で引く(`CommandHost.svelte` の `syncFromShared`)。
 * `impliedBy` は `sends: null` の選択肢だけが持つ、配信からその選択肢を戻す手がかり: 配信にこの名前の引数があれば、
 * 自分の引数が配信に無くてもこの選択肢を選び直す(再読み込みで回転体の「線分を選ぶ」が戻らず `axis_line` が隠れたままになるのを防ぐ)。
 */
export interface ChoiceArgSpec extends ArgSpecCommon {
  kind: 'choice'
  options: Array<{ value: string; label: string; buttonId: string; sends?: unknown; impliedBy?: string }>
}

/** 選択肢がサーバへ出す値(`sends` が無ければ `value`)。`null` は「出さない」。 */
export function choiceSends(option: ChoiceArgSpec['options'][number]): unknown {
  return option.sends === undefined ? option.value : option.sends
}

/**
 * 配信された引数(`remote`)から、選択肢の欄の値を決める(`CommandHost.svelte` の `syncFromShared`)。
 * `previous` はこの画面がサーバにあると信じている引数(`lastPublished`)、`current` は欄のいまの値。
 * 戻りは `{ value }`(`undefined` は「選んでいない」)か、配信の値に対応する選択肢が無いときの `{ unmapped: true }`(欄は触らない)。
 *
 * `impliedBy` で選択肢を戻すのは、**その引数か自分の引数を配信が変えたときだけ**: 人が x 軸へ切り替えてまだ送っていない間に
 * AI が角度だけ直すと、配信には古い `axis_line` が残ったまま届く。それを手がかりに「線分」へ戻すと、人の切り替えが消えて
 * 古い線分の軸で確定される(sol レビューで再現)。
 */
export function syncedChoice(
  spec: ChoiceArgSpec,
  remote: Record<string, unknown>,
  previous: Record<string, unknown>,
  current: string | undefined,
): { value: string | undefined } | { unmapped: true } {
  const value = remote[spec.name]
  if (value !== undefined) {
    // サーバの値から選択肢を逆引きする(`copy: true` → 'copy'、`direction: [0,0,-1]` → 'down')。
    const option = spec.options.find((candidate) => JSON.stringify(choiceSends(candidate)) === JSON.stringify(value))
    return option === undefined ? { unmapped: true } : { value: option.value }
  }
  const changed = (name: string) => JSON.stringify(remote[name]) !== JSON.stringify(previous[name])
  const implied = spec.options.find(
    (candidate) =>
      candidate.impliedBy !== undefined &&
      remote[candidate.impliedBy] !== undefined &&
      (changed(candidate.impliedBy) || changed(spec.name)),
  )
  if (implied !== undefined) return { value: implied.value }
  // サーバの値にならない選択肢(`sends: null`)は、サーバに無いことが「選んでいない」を意味しない。
  const option = spec.options.find((candidate) => candidate.value === current)
  return option !== undefined && choiceSends(option) === null ? { value: current } : { value: undefined }
}

/** スケッチの図形を選ぶ引数(反復のverb)。値は共有選択集合のうち編集中スケッチの図形のID(`App.svelte` の `selectedEntities`)で、`CommandHost` へは `entities` プロパティで渡る。 */
export interface EntitiesArgSpec extends ArgSpecCommon {
  kind: 'entities'
  min: number
}

/** 打ち込む文字列の引数(今はファイルのパスだけ)。パスの正しさはサーバにしか判定できず、ここで検査するとパス解釈が2箇所になる。 */
export interface TextArgSpec extends ArgSpecCommon {
  kind: 'text'
  /** 入力欄の `id`(実機検査がここを叩く)。 */
  inputId: string
  placeholder: string
  /** 使えない値なら理由を返す(使えるなら null)。空文字の拒否くらいにとどめる。 */
  reject(value: string): string | null
  /** 空欄のままでよい引数(既定はコア側にある)。空欄なら `build` は引数ごと送らない。 */
  optional?: boolean
  /**
   * サーバの作業空間へ出す形(省略時は打った文字列のまま)。サーバ側の型が文字列でない引数(`loft` の `shifts` は整数の配列)は
   * ここで読み替え、読めない打ちかけの値は `undefined` を返して出さない(型の合わない値を出すと `set_command_args` が拒む)。
   * 配信から欄へ戻すときは `String(値)` なので、配列はカンマ区切りの文字列に戻る。
   */
  toShared?(value: string): unknown
  /**
   * 確定・起動し直しの後も欄の値を残すか(既定は残す: ファイルのパスは同じ場所へ保存し直すのが普通の使い方)。
   * 選んだ対象と対になる値(ロフトの `shifts` は断面ごと)は `false`: 残すと次の起動で断面の数と合わず確定できない。
   */
  remember?: boolean
}

/**
 * スケッチを貼る先を1つ指す引数(`sketch_create` だけが取る)。値は基準平面の名前か、面は選択にあることを表す `'face'`。
 * 面を選択引数にしないのは、「基準平面か面か」のどちらかであって選択集合から導出できないため(選択が空でも基準平面なら揃う)。
 * `build` が送るのは基準平面のときの `{base_plane}` だけで、面は送らない(サーバがコアの選択から取る)。
 */
export interface PlaneArgSpec extends ArgSpecCommon {
  kind: 'plane'
}

/** `PlaneArgSpec` が取りうる値。基準平面3つ(`session.ts` の `BasePlaneName` と同じ綴り)と、貼る先の面はコアの選択にあることを表す `'face'`。 */
export type PlaneArgValue = 'XY' | 'XZ' | 'YZ' | 'face'

export function isPlaneArgValue(value: unknown): value is PlaneArgValue {
  return value === 'XY' || value === 'XZ' || value === 'YZ' || value === 'face'
}

/**
 * `PickArgSpec` が指させるもの: 3Dの辺・頂点・面と、スケッチの点・図形1つ。ピックイベント(`viewportTypes.PickEvent`)が既に
 * 運んでいる `edge` / `vertex` / `hit` / `sketchEntity` に対応する。増やすときはピックイベントに既にある種別から選ぶこと。
 *
 * `sketchPoint` と `sketchEntity` は同じ当たり判定の読み方違い: 前者は当たった点(`point` が文字列)をそのまま使い、
 * 後者は図形そのものを使う(端点の近くを押しても `point` を落として図形1つに畳む。鏡映軸に要るのは線分であって端点ではない)。
 */
export type PickTarget = 'edge' | 'vertex' | 'face' | 'sketchPoint' | 'sketchEntity'

/**
 * 図形を1つ直接ピックして集める引数(共有選択集合を経由しない)。
 *
 * `SelectionArgSpec` と別物なのは、同じ集合に入れると意味が混ざるため —— `move_body` の回転軸に使う辺を共有選択へ入れると、
 * コアの `selection.require_bodies` がその辺のボディまで「動かす対象」に数えてしまう。`PointArgSpec`(自由クリック)とも
 * 別物で、こちらは既にある実体を指す。
 *
 * 値は参照1つ(`SelectionRef`)。種別ごとの形は:
 *
 * | `target` | 値 | 拾う元 |
 * |---|---|---|
 * | `edge` | `{body, kind:'edge', index}` | `PickEvent.edge`(3Dビュー) |
 * | `vertex` | `{body, kind:'vertex', index}` | `PickEvent.vertex`(3Dビュー) |
 * | `face` | `{body, kind:'face', index}` | `PickEvent.hit`(3Dビュー) |
 * | `sketchPoint` | `{sketch, entity, point}` | `PickEvent.sketchEntity`(スケッチ平面) |
 * | `sketchEntity` | `{sketch, entity, point: null}` | `PickEvent.sketchEntity`(スケッチ平面) |
 *
 * 修飾キー(Shift / Ctrl)付きのクリックはここへ来ない(待っている間も対象を選び直せるよう、`App.svelte` が共有選択の編集へ通す)。
 *
 * 値は参照のまま送る(座標へ潰さない): 解決をコアの1箇所にとどめ、まっすぐでない辺のような拒否もコアの規則に任せる。
 * 幽霊(確定前のプレビュー)に要る数値は、`App.svelte` が配られた折れ線・頂点座標・`params` というコアが出した数値から別に解決する。
 */
export interface PickArgSpec extends ArgSpecCommon {
  kind: 'pick'
  target: PickTarget
  /** 「指す」ボタンの `id`(実機検査がここを叩く)。指した中身は `<id>-value` に出る。 */
  buttonId: string
  /**
   * 省略してよい引数(既定はコア側にある)。`true` なら空のままでも引数は揃い、省略されたら引数ごと送らない(`build` がそう書くこと)。
   * 値を作らないのは、回転中心の既定(対象の重心 / 代表点の平均)がコアにしか無い規則で、画面側へ写すと幽霊と確定結果が別の場所に出うるため。
   * 省略できる引数は `firstMissingArg` が指さないので、人が指したいときは `CommandHost` の「指す」ボタン(`armedPick`)を使う。
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

export type ArgValues = Record<
  string,
  SelectionRef[] | SelectionRef | number | Point2 | string | string[]
>

export function pickedRefOf(value: unknown): SelectionRef | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  const ref = value as Record<string, unknown>
  if (typeof ref.sketch === 'string') return typeof ref.entity === 'string' ? (value as SelectionRef) : null
  return typeof ref.body === 'string' && typeof ref.index === 'number' ? (value as SelectionRef) : null
}

export type BuildResult<Args = Record<string, unknown>> =
  | { ok: true; args: Args }
  | { ok: false; reason: string }

/**
 * コマンドの分類。送信経路の分岐はこれ1つで決まる。
 *
 * - `sketch`  … スケッチのverb(`sketch_id` を足して送る。スケッチモードでなければ送らない)
 * - `feature` … ソリッドのverb(失敗しても打った値を戻して打ち直せるようにする)
 * - `query`   … 読み取り専用(文書もログも変えない。応答は状態配信ではなく専用の返事 —— 測定。`App.svelte` の `handleMeasure`)
 * - `ui`      … サーバへ何も送らない道具(スケッチモードの「選ぶ」だけ)
 *
 * ファイル操作の多くはこの分類を経由しない(`documentFiles.svelte.ts` が `session.sendCommand` へ直接送る)が、`file` は現役:
 * `commands/files.ts` の `IMPORT_STEP` が持ち、`App.svelte` の送信経路(`CATEGORY_OF`)が確定完了の判定に見る。
 * `ui` 以外はコアの宣言(`duocad/catalog.py`)の `category` そのもので、生成物(`generated/protocol.ts` の `COMMAND_CATEGORY`)から
 * `categoryOf`(`category.ts`)が引く。
 */
export type CommandCategory = 'sketch' | 'feature' | 'file' | 'query' | 'ui'

export type HostCommandName = {
  [N in CommandName]: (typeof COMMAND_CATEGORY)[N] extends CommandCategory ? N : never
}[CommandName]

/** その verb の `build` が作る引数(コアの宣言からの生成物)。`sketch_id` は入らない(`App.svelte` が送信時に足す)。 */
export type BuildArgs<N extends CommandName> = Omit<CommandArguments[N], 'sketch_id'>

type UiCommandId = 'select_in_sketch'

interface CommandDefBase {
  label: string
  /** ツールバーのボタンの `id`(実機検査がここを叩く)。 */
  buttonId: string
  /**
   * ツールバーにボタンを出さない(verbとしては生きたまま。AI等の別経路用)。対象は `select_in_sketch` のみ
   * (直接クリックでの選択と重複するため)。
   */
  hidden?: boolean
  args: ArgSpec[]
  /**
   * 確定前の姿の出し方: `server` … サーバの `PREVIEWABLE_VERBS` に載っている(投機実行して形を測らせる) /
   * `local` … ブラウザだけで描ける(サーバへ投げないので「Escで文書が変わらない」が構造的に成立する) / `none` … 出さない
   */
  preview: 'server' | 'local' | 'none'
}

/** コマンドの定義。`CommandDef<'fillet'>` のように verb 名を付けて書く(型検査が `id` と `build` の引数をコアの宣言と照合する)。付けないのは一覧(`CommandDef[]`)とサーバへ送らない道具だけ。 */
export type CommandDef<N extends HostCommandName = HostCommandName> =
  | (CommandDefBase & {
      /** verb名。プレビューと確定で同じものをサーバへ送る(食い違うと嘘のプレビューになる)。 */
      id: N
      build(values: ArgValues): BuildResult<BuildArgs<N>>
    })
  | (CommandDefBase & { id: UiCommandId; build(values: ArgValues): BuildResult })

/** `build` を verb を問わない形で呼ぶ。`CommandHost.svelte` は引数を名前で運ぶだけなので、verb ごとの型はここで落とす。 */
export function buildArgs(command: CommandDef, values: ArgValues): BuildResult {
  return command.build(values) as BuildResult
}

// 引数の検査と組み立て(`commands/*.ts` が共有する)
/** 寸法として使えない値(0以下)を弾く。既定値は入れない。 */
export function positive(label: string): (value: number) => string | null {
  return (value) => (value > 0 ? null : `${label}は0より大きい値`)
}

/** 空のパスだけ弾く。それ以上の判定はサーバに任せる(パス解釈をサーバの1箇所に置く)。 */
export function pathLike(label: string): (value: string) => string | null {
  return (value) => (value.trim() === '' ? `${label}を打つ` : null)
}

export function buildPathArgs(values: ArgValues): BuildResult<{ path: string }> {
  const path = values.path
  if (typeof path !== 'string' || path.trim() === '') return { ok: false, reason: 'パスが空' }
  return { ok: true, args: { path: path.trim() } }
}

// コマンドの定義
export function findCommand(id: string, list: CommandDef[]): CommandDef {
  const found = list.find((command) => command.id === id)
  if (!found) throw new Error(`unknown command: ${id}`)
  return found
}

// 引数収集の判定(状態機械が使う純粋な計算)
/** 選択集合から、この引数が食える参照を取り出す。種別で絞り、`max` があれば先頭からその数だけ。順序は選択の追加順のまま(コアが順序を保つ契約。フィレットの辺と半径の対応がずれないため)。 */
export function argKinds(spec: SelectionArgSpec): SelectableKind[] {
  return spec.kinds ?? [spec.shapeKind]
}

/** 件数の数え方。複数の種別を食う引数は「つ」(面/辺/頂点が混ざる)。 */
function counterFor(spec: SelectionArgSpec): string {
  return argKinds(spec).length > 1 ? 'つ' : KIND_COUNTERS[spec.shapeKind]
}

export function itemsForArg(spec: SelectionArgSpec, selection: SelectionState | null): SelectionRef[] {
  const kinds = argKinds(spec)
  const items = (selection?.items ?? []).filter((item) => kinds.includes(refKind(item)))
  return spec.max === null ? items : items.slice(0, spec.max)
}

/** `max` を超えて選ばれている引数があれば理由を返す(無ければ空文字)。黙って先頭だけ使わず、送りもしない(コア側の `require_selected(exactly_one=True)` は2枚以上を拒否する)。`CommandHost` は空でない間 Enter を受け付けない。 */
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
      const counter = counterFor(spec)
      return `${spec.label}は${spec.max}${counter}まで(いま${total}${counter}。Ctrl+クリックで外す)`
    }
  }
  return ''
}

/** その引数は、いま集まっている値の下で要るか(`when` が無ければ常に要る)。判定をここ1箇所に置く(各所が `spec.when?.(values)` を書くと、`when` を持たない種別を足したときに1つだけ `undefined` を偽と読む食い違いが入る)。 */
export function argApplies(spec: ArgSpec, values: ArgValues): boolean {
  return spec.when === undefined || spec.when(values)
}

/** その引数が揃っているか。対象外(`when` が偽)の引数は揃っているとみなす。 */
export function argSatisfied(spec: ArgSpec, values: ArgValues): boolean {
  // 対象外は「揃っている」側へ倒す。未充足のままだと、モードを選んだ瞬間に使わない側の引数(並進を選んだのに角度)で収集が止まる。
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
    if (spec.optional === true && (text === undefined || text === '')) return true
    return typeof text === 'string' && spec.reject(text) === null
  }
  if (spec.kind === 'plane') {
    return isPlaneArgValue(values[spec.name])
  }
  const value = values[spec.name]
  // 点から導ける寸法は空欄のままで揃っている(値は `build` が点から作る)。
  if (value === undefined && spec.fromPoints === true) return true
  return typeof value === 'number' && Number.isFinite(value) && spec.reject(value) === null
}

export function firstMissingArg(command: CommandDef, values: ArgValues): ArgSpec | null {
  return command.args.find((spec) => !argSatisfied(spec, values)) ?? null
}

/** その引数は対象(target)を複数取りうるか —— `needsEnter` の条件(1)。選択引数は上限が無い(`max === null`)か上限が下限より大きければ複数取りうる。下限＝上限(測定の「ちょうど2つ」)は当たらない(何個で終わりか機械に分かる)。図形引数(`entities`)は上限が無く、選び終わりは機械に分からない。 */
function takesManyTargets(spec: ArgSpec): boolean {
  if (spec.kind === 'selection') return spec.max === null || spec.max > spec.min
  return spec.kind === 'entities'
}

/** その引数は how(どうするか)の入力か —— `needsEnter` の条件(2)。数値・選択肢・文字列。新しい入力の種別を足すときは必ずここへ足すこと。`fromPoints` の数値だけは how に数えない(空欄でも点から値が決まる。打ちたい人はEnterで言える)。 */
function isHowInput(spec: ArgSpec): boolean {
  if (spec.kind === 'number') return spec.fromPoints !== true
  return spec.kind === 'choice' || spec.kind === 'text'
}

/** 見えている幽霊をそのまま確定してよいコマンドか(`fromPoints` の寸法を持つか)。持つコマンドは点をあと1つ待つ状態でもマウス位置で形が決まっているので、そこでのEnterは画面に出ている形を確定する。持たないコマンド(`sketch_create`)は幽霊が無く、引数が揃うまで確定できない。 */
export function commitsGhost(command: CommandDef): boolean {
  return command.args.some((spec) => spec.kind === 'number' && spec.fromPoints === true)
}

/**
 * Enterを押して確定する必要があるコマンドか(要らないものは「即適用」)。
 * per-commandのフラグを置かず `args` から導出する(後から足すverbが何も書かなくても同じ規則に従い、書き忘れで黙って別の挙動にならない)。
 *
 * 次のどちらかに当たれば Enter が要る: (1) 対象を複数取りうる(`takesManyTargets`) (2) howの入力がある(`isHowInput`)。
 * どちらでもないのは、スケッチの描く5verb(`add_line`/`add_circle`/`add_ellipse`/`add_rectangle`/`add_arc`。寸法欄は `fromPoints` なので
 * how に数えない)と `sketch_create` だけ。列挙していない種別は即適用の側に落ちる(意図した既定)ので、新しい how を足すときは
 * `isHowInput` へ足すこと(忘れると、打っている途中の値で適用される)。
 *
 * 引数が1つも無いコマンド(`select_in_sketch`)は `false` だが、即適用は引数を完成させるクリックでしか発火しない
 * (`CommandHost.svelte`)ので何をしても発火しない。Enterが要らない = Enterが効かない、ではない: 起動より前から対象が
 * 揃っていた場合(AIが選んだ面のまま起動した等)は即適用の発火条件を満たさず、Enter(またはボタンの押し直し)が唯一の適用手段。
 * 読み取り専用(`category: 'query'`)だけは例外で、起動時に揃っていればその場で走る(`CommandHost.start()`)。
 */
export function needsEnter(command: CommandDef): boolean {
  return command.args.some((spec) => takesManyTargets(spec) || isHowInput(spec))
}

/** その引数について、人に何を問うかの1行。数値引数は打った値が使えない場合にその理由を出す(空欄のときは何を打つか)。 */
export function promptFor(spec: ArgSpec, values: ArgValues): string {
  if (spec.kind === 'selection') {
    const have = ((values[spec.name] ?? []) as SelectionRef[]).length
    const need = spec.min - have
    return `${spec.label}を${need}${counterFor(spec)}選ぶ(クリック / Shift・Ctrl+クリックでトグル / 左ドラッグで囲む)`
  }
  if (spec.kind === 'point') {
    return `${spec.label}をクリック(スケッチ平面の上)`
  }
  if (spec.kind === 'pick') {
    return `${spec.label}をクリック(${PICK_TARGET_HINTS[spec.target]} / Shift・Ctrl+クリックは対象の選び直し)`
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

/** 指した参照を人に見せる1行(まだ指していなければ空文字)。`refKey` や図形のIDは出さず、種別と番号だけ短く出す。`sketchPoint` は図形の最初の点、`sketchEntity` は図形そのものを指すので、何を指したかは `target` を見ないと決まらない。 */
export function pickedLabel(value: unknown, target?: PickTarget): string {
  const ref = pickedRefOf(value)
  if (ref === null) return ''
  if (refKind(ref) === 'sketch_entity') {
    if (target === 'sketchEntity') return '図形1つ'
    const item = ref as SketchEntityRef
    return item.point === null ? '図形の最初の点' : `点 ${item.point}`
  }
  const item = ref as ShapeRef
  const kind = item.kind === 'edge' ? '辺' : item.kind === 'face' ? '面' : '頂点'
  return `${kind} ${item.index}`
}
