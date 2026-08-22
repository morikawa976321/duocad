/**
 * コマンド層 — **引数を集める共通の仕組み**。
 *
 * ## なぜ1つにまとめるのか
 *
 * v1のverbは19個ある。verbごとにUIを作り込むと、19回「選択を読む・数値を打たせる・
 * プレビューする・Escで中断する」を書き直すことになり、そのうち何本かは必ず
 * 「選択を先に見ない」「Escで文書が変わる」のどちらかを取りこぼす。
 * **verbはここに載せるだけ**にして、状態機械は `CommandHost.svelte` の1つだけにする。
 *
 * ```
 * 起動 → 引数収集 → プレビュー → 確定(Enter) / 中断(Esc)
 * ```
 *
 * **Enterが要るかどうかも `args` から導出する**(2026-08-22)。対象が1つに定まり、
 * 数値も選択肢も無いコマンド(線分・編集用に複製)は、引数を完成させるクリックで
 * そのまま適用される —— 規則は下の `needsEnter` に1つだけ置いてある。
 *
 * ## 引数収集は、まず共有選択集合を見る
 *
 * **これがAI→人間の受け渡しが成立する仕組み**(実装計画書§8b)。AIが
 * `propose_selection` で辺の集合を提案 → 人が承認して verb を起動すると、
 * その選択がそのまま verb の対象になる。空のときだけ人に問う。
 *
 * **ただし選択の中身をサーバへ送ることはしない**(2026-08-20)。対象集合(面・辺・領域)は
 * コアが自分の確定選択から取るので、`build` が作るのは数値・選択肢・パスだけである。
 * ここで選択を読むのは「何を待っているか」を画面に出し、Enterを許すかを決めるためだけ。
 *
 * ここ(`command.ts`)は**Svelteに依存しない純粋な定義と道具**だけを持つ。
 * 状態機械・DOM・通信は `CommandHost.svelte` の側にある。
 */
import type { Point2, SelectionRef, SelectionState, ShapeKind, ShapeRef } from '../session'

/** 選択の種別。コアの `SELECTABLE_KINDS` と同じ4つ。 */
export type SelectableKind = ShapeKind | 'region'

/** 画面に出す種別の名前(件数表示・引数の問いかけに使う)。 */
export const KIND_LABELS: Record<SelectableKind, string> = {
  face: '面',
  edge: '辺',
  vertex: '頂点',
  region: '領域',
}

/** 種別ごとの数え方(「辺 4本」「面 2面」)。件数表示を1箇所に集める。 */
const KIND_COUNTERS: Record<SelectableKind, string> = {
  face: '面',
  edge: '本',
  vertex: '点',
  region: '個',
}

// --------------------------------------------------------------------------------------
// 参照(ShapeRef / RegionRef)の道具 — コアの `duocad/selection.py` と同じ判定を持つ
// --------------------------------------------------------------------------------------
/**
 * 参照の種別を返す。**RegionRef は `kind` キーを持たない**ので `sketch` の有無で見分ける
 * (§3.5のデータモデル。コアの `selection.ref_kind` と同じ規則)。
 */
export function refKind(ref: SelectionRef): SelectableKind {
  return 'sketch' in ref ? 'region' : (ref as ShapeRef).kind
}

/** 2つの参照が同じ実体を指すか(`provenance` は見ない。コアの `same_ref` と同じ)。 */
export function sameRef(left: SelectionRef, right: SelectionRef): boolean {
  const kind = refKind(left)
  if (kind !== refKind(right)) return false
  if (kind === 'region') {
    const a = left as { sketch: string; index: number }
    const b = right as { sketch: string; index: number }
    return a.sketch === b.sketch && a.index === b.index
  }
  const a = left as ShapeRef
  const b = right as ShapeRef
  return a.body === b.body && a.kind === b.kind && a.index === b.index
}

/** 一覧の `{#each}` キー・DOMの `data-` 属性に使う一意な文字列。 */
export function refKey(ref: SelectionRef): string {
  return 'sketch' in ref
    ? `region:${ref.sketch}:${ref.index}`
    : `${(ref as ShapeRef).kind}:${(ref as ShapeRef).body}:${ref.index}`
}

/** 面の参照を作る(ビューポートのピック結果から)。添字は1始まり。 */
export function faceRef(bodyId: string, faceIndex: number): ShapeRef {
  return { body: bodyId, kind: 'face', index: faceIndex }
}

/** 種別ごとの件数を「辺 4本 / 面 1面」の形にまとめる(選択パネルの要約)。 */
export function describeSelection(items: SelectionRef[]): string {
  const counts = new Map<SelectableKind, number>()
  for (const item of items) {
    const kind = refKind(item)
    counts.set(kind, (counts.get(kind) ?? 0) + 1)
  }
  if (counts.size === 0) return 'なし'
  return [...counts]
    .map(([kind, count]) => `${KIND_LABELS[kind]} ${count}${KIND_COUNTERS[kind]}`)
    .join(' / ')
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
  shapeKind: SelectableKind
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
 * **スケッチの図形はそこに入らない**(コアの `SELECTABLE_KINDS`)。MCP側も反復だけは
 * 図形IDを引数で受けており(`mcp_server.pattern_rectangular` の
 * 「スケッチの図形は選択集合の項目ではないので引数で受ける」)、ここはその人間側の対。
 * 値はスケッチパネルで選んだ図形のIDで、`CommandHost` へは `entities` プロパティで渡る。
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
 * **数値引数と分けてある**。数値は `Number.parseFloat` を通して弾く仕組みが要るが、
 * パスは打った文字そのものが値であり、正しさの判定はサーバ(実在するか・書けるか)に
 * しか無い —— ここで拡張子やドライブ文字を検査し始めると、**サーバと2箇所で
 * 別々のパス解釈を持つ**ことになる(相対パスの基準はサーバの作業ディレクトリ)。
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

export type ArgSpec =
  | SelectionArgSpec
  | NumberArgSpec
  | PointArgSpec
  | ChoiceArgSpec
  | EntitiesArgSpec
  | TextArgSpec

/** スケッチ平面上の点 [u, v](mm)。**定義は `session.ts`**(コアと同じ形)。ここは通すだけ。 */
export type { Point2 }

/** 集まった引数の値。選択は参照の配列、数値は数、点は [u,v]、選択肢は文字列、図形はID列。 */
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
 * - `file`    … ファイル操作(応答が状態配信で返るとは限らない。`changesDocument` 参照)
 * - `ui`      … サーバへ何も送らない道具(スケッチモードの「選ぶ」だけ)
 *
 * **`ui` 以外はコアの宣言(`duocad/catalog.py`)の `category` と同じ値**であり、
 * `tests/test_ui_command_wiring.py` が一致を機械で見る。以前はこの分岐が
 * `isSketchVerb` / `isFeatureVerb` / `isFileVerb` の3つの判定関数と
 * 「どれにも当たらなければ `extrude_face`」という順序依存の暗黙のフォールバックだった ——
 * verbを足して登録を忘れると、**そのverbが黙って `extrude_face` として送られていた**。
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
 * `App.svelte` が `SOLID_FEATURE_COMMANDS`(features.ts)と `FILE_COMMANDS`(files.ts)を
 * 後ろに繋いで1つの並びにする。スケッチモードの一覧は `commands/sketch.ts`
 * (モードごとに一覧が違うだけで、状態機械は同じ1つ)。
 */
export const COMMANDS: CommandDef[] = [EXTRUDE_FACE]

/**
 * 起動していないときに構えているコマンド。
 *
 * **明示的な「起動」の1手を挟まない**のは、4手(面を選ぶ→数値を打つ→Enter)が
 * 本人の速度ベンチマークそのものであり、ここに1手足すと退行になるため。
 * 別のコマンドを起動すると入れ替わり、Escでここへ戻る。
 */
export const DEFAULT_COMMAND_ID = EXTRUDE_FACE.id

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
export function itemsForArg(spec: SelectionArgSpec, selection: SelectionState | null): SelectionRef[] {
  const items = (selection?.items ?? []).filter((item) => refKind(item) === spec.shapeKind)
  return spec.max === null ? items : items.slice(0, spec.max)
}

/**
 * `max` を超えて選ばれている引数があれば理由を返す(無ければ空文字)。
 *
 * **黙って先頭だけ使わない**し、**送りもしない**。押し出しは面1つのverbで、
 * コア側(`selection.require_confirmed(exactly_one=True)`)が2枚以上を拒否する ——
 * ここで先頭を選んで送ると、UIだけが「どれを押すか」を勝手に決めることになる。
 * `CommandHost` はこれが空でない間 Enter を受け付けない(減らしてから確定する)。
 */
export function overflowNote(command: CommandDef, selection: SelectionState | null): string {
  for (const spec of command.args) {
    if (spec.kind !== 'selection' || spec.max === null) continue
    const total = (selection?.items ?? []).filter((item) => refKind(item) === spec.shapeKind).length
    if (total > spec.max) {
      const counter = KIND_COUNTERS[spec.shapeKind]
      return `${spec.label}は${spec.max}${counter}まで(いま${total}${counter}。Ctrl+クリックで減らす)`
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
 * per-commandのフラグを置かず `args` から**導出する**。狙いは、後から足すverbが
 * 何も書かなくても同じ規則に従うことで、規則の在り処を1箇所に保つことである
 * (フラグにすると、書き忘れたverbだけが黙って別の挙動になる)。
 *
 * ## 判定基準(2026-08-22本人決定。engine `2026-08-22-duocad-ui-interaction-principles`)
 *
 * 次の**どちらかに当たれば Enter が要る**:
 *
 * 1. **対象を複数取りうる**(`takesManyTargets`) …「まだ足すのか、これで終わりか」を
 *    機械が決められない。
 * 2. **howの入力がある**(`isHowInput`) … 打ち終わり・選び終わりを機械が決められない。
 *
 * **どちらでもない = 対象が1つに定まり、howが無い**ものだけが即適用になる。最後の1手
 * (点を置く / 1つ選ぶ)で引数が揃った時点で、それ以上人に聞くことが残っていないからである。
 * 現行の17個のうちここへ落ちるのは **`add_line`(点2つ)と `make_editable_copy`(面1つ)**
 * だけで、その表は `tests/test_ui_command_wiring.py` が機械で固定している。
 *
 * ## 列挙していない種別は「即適用の側」に落ちる(意図した既定)
 *
 * 点(`point`)がそうであるように、**対象を1つだけ指す新しい種別**(例: 平面を1つ選ぶ)を
 * 足したときは、ここを直さなくても即適用になる。逆に、**新しい how を足すときは
 * `isHowInput` へ足すこと** —— 忘れると、打っている途中の値で適用される。
 *
 * 引数が1つも無いコマンド(`select_in_sketch`)もここでは `false` になるが、即適用は
 * **引数を完成させるクリックでしか発火しない**(`CommandHost.svelte`)ので、完成させる
 * 引数が無いコマンドは何をしても発火しない。
 *
 * **Enterが要らない = Enterが効かない、ではない。** 起動より前から対象が揃っていた場合
 * (AIが選んだ面のまま起動した等)は即適用の発火条件を満たさないので、Enter(または
 * ボタンの押し直し)が唯一の適用手段として残る。
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
    return `${spec.label}を${need}${KIND_COUNTERS[spec.shapeKind]}選ぶ(クリック / Shift+クリックで追加)`
  }
  if (spec.kind === 'point') {
    return `${spec.label}をクリック(スケッチ平面の上)`
  }
  if (spec.kind === 'choice') {
    return `${spec.label}を選ぶ(${spec.options.map((option) => option.label).join(' / ')})`
  }
  if (spec.kind === 'entities') {
    const have = ((values[spec.name] ?? []) as string[]).length
    return `${spec.label}を${spec.min - have}個選ぶ(スケッチパネルの図形一覧)`
  }
  if (spec.kind === 'text') {
    const text = values[spec.name]
    if (typeof text === 'string' && text !== '') return spec.reject(text) ?? ''
    return `${spec.label}を打つ(${spec.placeholder})`
  }
  const value = values[spec.name]
  if (typeof value === 'number' && Number.isFinite(value)) {
    return spec.reject(value) ?? ''
  }
  return `${spec.label}を打つ(${spec.unit})`
}
