<script lang="ts">
  /**
   * コマンドの状態機械(起動 → 引数収集 → プレビュー → 確定(Enter) / 中断(Esc))。verbごとに作り直さない1つだけの漏斗。
   *
   * ## Enterが要らないコマンドがある
   *
   * 要否は `needsEnter(command)` が `args` から導出する(規則は `command.ts`)。ここが持つ発火条件は1つ:
   * 起動した後の1手で引数が揃ったときにだけ `commit()` する。
   * - 点で揃うもの(線分)… `addPoint` の中で同期に判定する。
   * - 基準平面で揃うもの(スケッチ作成)… `setBasePlane` の中で同期に判定する。
   * - 選択で揃うもの(編集用に複製 / 面に貼るスケッチ)… 面のクリックはWS往復の後に届くので、この画面が出した選択変更が起動後に届いた回数(`selfSelections`)を見る。
   *   起動時の値を控え(`armedSelections`)、増えたときだけ発火する。起動前から面が選ばれていても、AIが選択を書き換えても発火しない。
   * これで「起動しただけで文書が変わる」が構造的に起きない。
   *
   * ## 既定は「無起動」
   *
   * `commandId` は `string | null` で、`null` が「どのコマンドも起動していない」。`null` の間は引数収集・プレビュー・Enter確定を素通りさせ、`#command-abort` もDOMに描かない。
   * スケッチモードの既定「選ぶ」(`select_in_sketch`)も同じ待機状態で、中断は出さない。
   * `onWants([])` / `onWantsPlane(false)` を送るので辺・領域・基準平面のピックは止まるが、面・頂点は常に指せる。
   *
   * 初期値は `defaultCommandId ?? null`。通常モードは `undefined`(無起動)、スケッチモードは `DEFAULT_SKETCH_COMMAND_ID`(描くコマンドが常に点を待つと領域をクリックで選べなくなるため無起動にしない)。
   * Escも、確定が実際に成功したときも、この初期値へ戻る(`abort()` / `commandSucceeded()` 共有の `returnToIdle()`)。
   * 成功の確認を待つのが要点: `commit()` 直後に降りると、失敗したときに打ち直す入力欄まで消える(`commit()` のコメント参照)。同じverbをもう一度使うにはツールバーのボタンを押し直す。
   *
   * 「入った時点で起動済み」は `startCommandId` が別に持つ。戻り先(`defaultCommandId`)は代入しかせず即適用の待ち受けを構えないので、混ぜるとスケッチモードで貼る先の面をクリックしても何も起きない。
   *
   * ## 引数収集と選択
   *
   * 選択引数の値はコアの選択集合から導出し、ここには持たない。空のときだけ「面を1つ選ぶ」と問う。AIが選んだ集合はそのまま対象になる。
   * 選択の中身はWSへ送らない(サーバがコアの選択から取る)。選択の指紋はプレビューの重複抑止だけに使う。
   * 引数収集中にAIが選択を書き換えても止めない(選択に出所の区別は無い)。
   *
   * ## プレビューは投機的実行
   *
   * 引数が揃ったら `preview` を投げる(サーバがverbを試して形だけ返し、文書は元へ戻す)。押し出しは実測で数百ms〜1秒かかるので、打鍵ごとには投げず、打ち終わり(`previewController.ts`)を待って1回だけ投げる。
   * Enterはプレビューを待たない(プレビューは確認用で、確定の前提ではない)。
   */
  import { onDestroy, onMount, untrack } from 'svelte'
  import { previewInputKey, type PreviewController, type PreviewInput } from '../previewController'
  import { createSharedArgsController, publishedArgs } from '../sharedArgsController'
  import {
    acceptsSurface,
    argApplies,
    argKinds,
    argSatisfied,
    buildArgs,
    commitsGhost,
    findCommand,
    firstMissingArg,
    itemsForArg,
    needsEnter,
    overflowNote,
    pickedLabel,
    promptFor,
    refKey,
    refKind,
    type ArgSpec,
    type ArgValues,
    type ChoiceArgSpec,
    type CommandDef,
    type BuiltCommandRequest,
    type FaceKindOf,
    type NumberArgSpec,
    type PickArgSpec,
    type PickTarget,
    type Point2,
    type SelectableKind,
    type SelectionArgSpec,
    type TextArgSpec,
  } from './command'
  import { categoryOf } from './category'
  import { grainOf, type SelectionGrain } from './grain'
  import { evaluateExpression, firstUnreadableExpression, isPlainNumber } from '../expression'
  import { isImeKey, isTextEntry, ownsEnterKey, ownsEscapeKey } from '../keys'
  import type { CommandState, SelectionRef, SelectionState } from '../session'
  import type { PickShapes } from '../viewportTypes'
  import { COMMAND_CATEGORY } from '../generated/protocol'

  let {
    selection,
    busy,
    shared = null,
    selfSelections = 0,
    commands,
    defaultCommandId,
    startCommandId,
    hover = null,
    entities = [],
    onStart = () => {},
    onEnd = () => {},
    onBegin = () => {},
    onSetArgs = () => {},
    onEndShared = () => {},
    onRun,
    preview,
    onNote,
    onWants = () => {},
    onWantsPlane = () => {},
    onWantsPoint = () => {},
    onWantsPick = () => {},
    onWantsVertices = () => {},
    onWantsShapes = () => {},
    onWantsGrain = () => {},
    onDeselect = () => {},
    faceKindOf = () => null,
  }: {
    /** コアの選択集合(state.selection)。 */
    selection: SelectionState | null
    /** 直前の操作の応答待ち。待っている間は確定を受け付けない。 */
    busy: boolean
    /**
     * 配信された起動中のコマンド(`state.command`。issue #153)。スケッチ・フィーチャの verb はここが正で、このコンポーネントは
     * 起動中の verb も引数も自分で持たない: ツールバーのクリックは `onBegin`、入力欄は `onSetArgs`、Esc は `onEndShared` で
     * サーバへ頼み、配信で `shared` が変わってから欄を描く。AI が起動・入力したものも同じ経路で届く。
     */
    shared?: CommandState | null
    /**
     * この画面のクリックで選択が変わった回数(`App.svelte` が数える)。Enterの要らないコマンドの即適用は、これが起動後に増えたときにだけ発火する。
     * 選択の中身を見て発火させるとAIの `modify_selection` でも走る。数以外の意味を持たせない(誰が変えたかの判断は `App.svelte`)。
     * 数えるのは共有選択集合の変更だけ。外から非同期に届く別の引数で揃うコマンドを足すときは、その引数にも同じ形の数を用意する。
     */
    selfSelections?: number
    /** このモードで使えるコマンド(`App.svelte` が渡す)。同じ id が複数のモードに居ても、`findCommand` はこの一覧の中だけで引く。 */
    commands: CommandDef[]
    /** 起動していない状態を表すには渡さない(`undefined`)。渡さなければ `null`(無起動)になる。 */
    defaultCommandId?: string
    /**
     * 最初から起動しておくコマンド(`start()` を1回だけ通す)。`defaultCommandId`(Esc・確定成功後の戻り先)とは別物。モードに入った時点で人のクリックを待たせたい側(スケッチモードの貼る先待ち)が使う。
     * 戻り先にはしない: Escで降りたら無起動になり、基準平面も面も指せなくなる(「起動 → Esc のあとのクリックでは発火しない」。`smoke-files.mjs` の(c))。
     */
    startCommandId?: string
    /** スケッチ平面上でポインタが今指している点。幽霊にだけ使う。 */
    hover?: Point2 | null
    /** スケッチパネルで選ばれている図形のID。反復のverbだけが取り、共有選択集合には入らない(`command.ts` の `EntitiesArgSpec`)。 */
    entities?: string[]
    /** 起動(`start()`)の瞬間に呼ばれる。形状最適化の4つのverbが、既存の割り当てをプリセレクトして見せるために使う(`App.svelte`)。 */
    onStart?: (verb: string) => void
    /** 起動中のコマンドが終わった(Esc・中断ボタン・確定成功)ときに呼ばれる。無起動・既定「選ぶ」のEscでは呼ばない。アプリ層が選択を全解除するための知らせ。 */
    onEnd?: (verb: string) => void
    /** 構える対象の verb を起動する(サーバの `begin_command`)。 */
    onBegin?: (verb: string, args: Record<string, unknown>) => void
    /** 起動中の verb の引数を併合する(サーバの `set_command_args`。`null` の値はそのキーを外す)。 */
    onSetArgs?: (args: Record<string, unknown>) => void
    /** 起動中の verb を畳む(サーバの `end_command`)。 */
    onEndShared?: () => void
    onRun: (...request: BuiltCommandRequest) => void
    preview: PreviewController
    onNote: (message: string) => void
    /** 起動中のコマンドが選択から取る種別。辺を要求したときだけアプリ層が辺の折れ線を取りに行く(常時出すと27部品ぶんの辺が要り、面のピックも辺に取られて押し出しが遅くなる)。 */
    onWants?: (kinds: SelectableKind[]) => void
    /** 起動中のコマンドが基準平面を要求しているか。アプリ層が `viewport.showBasePlanes()` を切り替える(常時指せると面を狙うクリックが手前のグリッドに取られる)。 */
    onWantsPlane?: (wants: boolean) => void
    /** スケッチ平面上の点を待っているか。 */
    onWantsPoint?: (wants: boolean) => void
    /** 起動中のコマンドが図形の直接ピックを待っているか(待っていなければ `null`)。頂点マーカーは選択に入ったものしか出ないので、中心に使う頂点を指せるようにするにはこの知らせが要る。 */
    onWantsPick?: (target: PickTarget | null, datum: boolean) => void
    /** 起動中のコマンドが頂点そのものを対象にするか(`SelectionArgSpec.pickVertices`。測定)。アプリ層が対象ボディの頂点を候補として出し、辺より先に取る。 */
    onWantsVertices?: (wants: boolean) => void
    /** 起動中のコマンドが受ける面・辺の形(`SelectionArgSpec.shapes`)。無起動・形を問わないコマンドでは両方 null。 */
    onWantsShapes?: (shapes: PickShapes) => void
    /** 起動中のコマンドが指した部分ではなく持ち主(ボディ・図形)ごとに効くか(`grain.ts`)。無起動では `NO_GRAIN`。 */
    onWantsGrain?: (grain: SelectionGrain) => void
    /** 共有選択からこれらの参照を外す(`PickArgSpec.excludesFrom`)。 */
    onDeselect?: (refs: SelectionRef[]) => void
    /** 面の種類を引く(`SelectionArgSpec.surfaceKinds`)。 */
    faceKindOf?: FaceKindOf
  } = $props()

  /** Delete・Backspace が走らせる verb(`DELETE_SELECTION`)。 */
  const DELETE_VERB = 'delete'

  /** 構える対象(サーバの作業空間に載る verb)か。スケッチとフィーチャの verb(`duocad/workspace.py` の `stageable` と同じ線)。 */
  function isStageable(verb: string): boolean {
    const category = (COMMAND_CATEGORY as Record<string, string>)[verb]
    return category === 'sketch' || category === 'feature'
  }

  // 初期値を1回だけ捕まえるのが正しい: モードが変わるときは `App.svelte` が `{#key}` でこのコンポーネントごと作り直す(集めかけの引数を持ち越さない)。
  // svelte-ignore state_referenced_locally
  const idleCommandId = defaultCommandId ?? null
  /** 構える対象でないコマンド(測定・ファイル操作・スケッチの「選ぶ」)の起動。構える対象はサーバの `shared` が正で、ここには持たない。 */
  let localCommandId = $state<string | null>(idleCommandId)
  /** 配信された起動中の verb のうち、このモードの一覧にあるもの(別のモードの verb は、モードが追随してから出る)。 */
  const sharedId = $derived<string | null>(
    shared !== null && isStageable(shared.verb) && commands.some((candidate) => candidate.id === shared.verb)
      ? shared.verb
      : null,
  )
  const commandId = $derived<string | null>(sharedId ?? localCommandId)
  const command = $derived<CommandDef | null>(commandId === null ? null : findCommand(commandId, commands))

  /** verb が実際に走っているか。無起動と、スケッチの既定「選ぶ」(`command !== null` だが待機状態)は除く。 */
  const commandActive = $derived(commandId !== null && commandId !== idleCommandId)

  /** 数値引数の生の入力(引数名 -> 打った文字列)。確定・中断のたびに空へ戻す。 */
  let numberText = $state<Record<string, string>>({})

  /** クリックで集めた点(引数名 -> [u,v])。選択集合には入れない(線の始点が溜まると次のコマンドが拾う)。 */
  let points = $state<Record<string, Point2>>({})

  /** 選んだ選択肢(引数名 -> 値)。既定は入れない(`ChoiceArgSpec` のdocstring)。 */
  let choices = $state<Record<string, string>>({})

  /** 指したスケッチ平面(引数名 -> `'XY' | 'XZ' | 'YZ' | 'face'`)。点と同じく選択集合には入れない(基準平面は選択の種別でなく、面は参照が入るだけで「面に貼る」意思ではない)。 */
  let planes = $state<Record<string, string>>({})

  /** 打ち込んだ文字列(引数名 -> 値)。確定しても消さない(下の `reset` 参照): 同じ場所への保存し直しが普通の使い方。`remember: false` の欄だけ消す(`resetInputs`)。 */
  let textValues = $state<Record<string, string>>({})

  /** 直接ピックで指した参照(引数名 -> 参照1つ)。共有選択集合には入れない(`PickArgSpec` のdocstring)。 */
  let picks = $state<Record<string, SelectionRef>>({})

  /** 人が「いまここを指す」と言った省略可能なピック引数の名前(なければ `null`)。必須のピックは `firstMissingArg` が指すが、省略できるもの(回転中心)は常に「揃っている」側なので、指したいと言う手が要る。 */
  let armedPick = $state<string | null>(null)

  /** 集まった引数(選択は導出・数値は打った文字列から・点はクリックから)。 */
  const values = $derived.by<ArgValues>(() => {
    const collected: ArgValues = {}
    for (const spec of command?.args ?? []) {
      if (spec.kind === 'selection') {
        collected[spec.name] = itemsForArg(spec, selection, faceKindOf)
      } else if (spec.kind === 'point') {
        const point = points[spec.name]
        if (point) collected[spec.name] = point
      } else if (spec.kind === 'choice') {
        const chosen = choices[spec.name]
        if (chosen) collected[spec.name] = chosen
      } else if (spec.kind === 'entities') {
        collected[spec.name] = entities
      } else if (spec.kind === 'text') {
        collected[spec.name] = textValues[spec.name] ?? ''
      } else if (spec.kind === 'plane') {
        const chosen = planes[spec.name]
        if (chosen) collected[spec.name] = chosen
      } else if (spec.kind === 'pick') {
        const ref = picks[spec.name]
        if (ref) collected[spec.name] = ref
      } else {
        const raw = rawNumber(spec)
        // 空欄で `default` を持つ引数は既定値扱い(`NumberArgSpec.default`)。
        if (raw === '') {
          if (spec.default !== undefined) collected[spec.name] = spec.default
          continue
        }
        const evaluated = evaluateExpression(raw)
        if (evaluated.ok) collected[spec.name] = evaluated.value
      }
    }
    return collected
  })

  /** `command` が `null`(無起動)の間は待っている引数も無い。 */
  const missing = $derived(command === null ? null : firstMissingArg(command, values))

  /** 選ばれすぎ(`max` 超え)の理由。空でない間は確定させない。 */
  const overflow = $derived(command === null ? '' : overflowNote(command, selection, values, faceKindOf))

  /** 無起動では確定できない(`command === null` を明示的に含める)。 */
  const ready = $derived(command !== null && missing === null && overflow === '')
  const built = $derived(command !== null && ready ? buildArgs(command, values) : null)

  /** 幽霊に使う引数。待っている点をポインタの位置で仮に埋める(線分の2点目前のゴムひも)。表示専用で、確定に使うのは `built`。 */
  const ghostBuilt = $derived.by(() => {
    if (built?.ok) return built
    if (command === null || missing?.kind !== 'point' || hover === null) return null
    const provisional = { ...values, [missing.name]: hover }
    if (firstMissingArg(command, provisional) !== null) return null
    const result = buildArgs(command, provisional)
    return result.ok ? result : null
  })

  /** 確定に使う引数。揃っていれば `built`、点を待っていても幽霊が出ているコマンド(`commitsGhost`)なら画面に見えている形(`ghostBuilt`)。始点だけ置いたEnterでゴムひもの線を確定するのは意図した挙動。 */
  const commitSource = $derived(
    built ?? (command !== null && commitsGhost(command) ? ghostBuilt : null),
  )

  /** いま要る引数だけ(`when` が偽のものを落とす)。入力欄・ボタンの描画はこれを通す。 */
  const liveArgs = $derived((command?.args ?? []).filter((spec: ArgSpec) => argApplies(spec, values)))

  const numberArgs = $derived(liveArgs.filter((spec) => spec.kind === 'number') as NumberArgSpec[])
  const choiceArgs = $derived(liveArgs.filter((spec) => spec.kind === 'choice') as ChoiceArgSpec[])
  const textArgs = $derived(liveArgs.filter((spec) => spec.kind === 'text') as TextArgSpec[])
  const pickArgs = $derived(liveArgs.filter((spec) => spec.kind === 'pick') as PickArgSpec[])
  /** 引数行を overlay へ出すか(in-flow の高さを変えない。`styles.css` の `.toolbar-overlay`)。 */
  const overlayOpen = $derived(
    choiceArgs.length > 0 ||
      pickArgs.length > 0 ||
      textArgs.length > 0 ||
      commandActive,
  )

  /** 打ってあるのに読めない式の理由(無ければ null)。読めない式は `values` に載らないので、生の文字列を見て打ち間違いと未入力を区別する。 */
  const expressionError = $derived(
    firstUnreadableExpression(numberArgs.map((spec) => ({ label: spec.label, raw: rawNumber(spec) }))),
  )

  /** Enter・ボタン押し直し・最後のクリックで確定できるか(`ready` か、幽霊が出ているか。読めない式が残っていれば確定しない)。 */
  const canCommit = $derived(commitSource !== null && overflow === '' && expressionError === null)

  /** 式を打った欄の評価結果(素の数値だけなら空)。 */
  const expressionEcho = $derived.by(() => {
    const shown: string[] = []
    for (const spec of numberArgs) {
      const raw = rawNumber(spec)
      if (raw === '' || isPlainNumber(raw)) continue
      const evaluated = evaluateExpression(raw)
      if (evaluated.ok) shown.push(`${spec.label} = ${round4(evaluated.value)}${spec.unit}`)
    }
    return shown.join(' / ')
  })

  /** 画面に出す1行(何を待っているか)。無起動では空文字。 */
  const prompt = $derived.by(() => {
    if (command === null) return ''
    if (expressionError !== null) return expressionError
    // 選び間違い(選ばれすぎ・食えない面)は、ほかの引数を打つより先に直してもらう。
    if (overflow !== '') return overflow
    if (missing !== null) return promptFor(missing, values)
    if (built && !built.ok) return built.reason
    // 断られている間は「Enterで確定」と言わない(押しても同じ理由で断られる)。
    if (previewFailure !== null && previewFailure.key === previewKey) return `プレビューできない: ${previewFailure.message}`
    return expressionEcho === '' ? 'Enterで確定 / Escで中断' : `${expressionEcho} ・ Enterで確定`
  })

  /** いま直接ピックを待っている引数(なければ null)。必須は収集の順(`missing`)、省略可は「指す」ボタンで構えたときだけ(`armedPick`)。構えたほうが勝つ。 */
  const pendingPick = $derived.by<PickArgSpec | null>(() => {
    if (command === null) return null
    if (armedPick !== null) {
      const armed = liveArgs.find((spec) => spec.name === armedPick)
      if (armed !== undefined && armed.kind === 'pick') return armed
    }
    return missing !== null && missing.kind === 'pick' ? missing : null
  })

  /** このコマンドが選択から取る種別をアプリ層へ知らせる(起動中はこれだけが指せる。無起動では `[]` で、見えているもの全部が指せる)。`argKinds` を通す: `shapeKind` だけだと複数種別を食う引数(`project_to_sketch` の辺/面)で面が指せなくなる。 */
  $effect(() => {
    onWants(
      (command?.args ?? [])
        .filter((spec) => spec.kind === 'selection')
        .flatMap((spec) => argKinds(spec as SelectionArgSpec)),
    )
  })

  /** 基準平面を要求しているか。無起動では常にfalse。 */
  $effect(() => {
    onWantsPlane((command?.args ?? []).some((spec) => spec.kind === 'plane'))
  })

  /** スケッチ平面上の点を待っているか。無起動では常にfalse。 */
  $effect(() => {
    onWantsPoint(missing?.kind === 'point')
  })

  /** 直接ピックを待っているか(待っている種別を知らせる)。無起動では常に null。 */
  $effect(() => {
    onWantsPick(pendingPick?.target ?? null, pendingPick?.datum === true)
  })

  /** 頂点そのものを対象にするか(`SelectionArgSpec.pickVertices`)。無起動では常に false。 */
  $effect(() => {
    onWantsVertices(
      (command?.args ?? []).some((spec) => spec.kind === 'selection' && spec.pickVertices === true),
    )
  })

  $effect(() => {
    const shapes = (command?.args ?? []).find((spec) => spec.kind === 'selection' && spec.shapes !== undefined)
    const wanted = shapes?.kind === 'selection' ? shapes.shapes : undefined
    onWantsShapes({ faces: wanted?.faces ?? null, edges: wanted?.edges ?? null })
  })

  $effect(() => {
    onWantsGrain(grainOf(command))
  })

  // ---------------------------------------------------------------- プレビュー
  /** 選択の中身の印。対象集合は引数に載らないので、これを入れないと辺を足しても再送されず古い幽霊が残る。 */
  const selectionPrint = $derived(
    (command?.args ?? []).some((spec) => spec.kind === 'selection')
      ? (selection?.items ?? []).map(refKey).join('|')
      : '',
  )

  const previewInput = $derived.by<PreviewInput | null>(() => {
    // `cmd` へ一度だけ束ねる(`$derived` の読み直しは同じ値でも別の式として扱われ、TSのnarrowingが効かない)。
    const cmd = command
    // 確定を送った後は出さない(打った値は残すので、消した幽霊を同じ引数で出し直してしまう)。
    const source = cmd === null || cmd.preview === 'none' || committed ? null : ghostBuilt
    return source === null || cmd === null ? null : {
      verb: cmd.id,
      args: source.args,
      mode: cmd.preview as 'server' | 'local',
      selection: selectionPrint,
    }
  })

  $effect(() => {
    preview.update(previewInput)
  })

  /** サーバへ出すプレビューの入力の印(サーバへ出さないなら null)。断られた理由がいまの入力のものかを見分ける。 */
  const previewKey = $derived(
    previewInput === null || previewInput.mode !== 'server' ? null : previewInputKey(previewInput),
  )

  /** サーバのプレビューが断った理由と、そのときの入力の印。入力が変われば印が合わなくなり、出さなくなる。 */
  let previewFailure = $state<{ key: string; message: string } | null>(null)

  /**
   * プレビューが断られたときに `App.svelte` が呼ぶ(`input` は断られた要求の入力)。入力が既に変わっていれば何も出さない。
   * 待ちの1行の持ち主として受け取ったら `true`(無起動ならトーストへ回す)。
   */
  export function previewFailed(message: string, input: PreviewInput): boolean {
    if (command === null) return false
    previewFailure = { key: previewInputKey(input), message }
    return true
  }

  /** プレビューの形が届いたときに `App.svelte` が呼ぶ(同じ入力で出し直して通ったら、断りを消す)。 */
  export function previewSucceeded(): void {
    previewFailure = null
  }

  // ---------------------------------------------------------------- 即適用(Enterが要らないもの)
  /**
   * 即適用のうち、選択が揃うのを待つもの(現行は面に貼るときの `sketch_create` だけ)。点・基準平面のようにその場で決まるものは `addPoint` / `setBasePlane` で同期に判定するのでここに来ない。
   * 平面引数を含める理由: 面のクリックでは `plane` の値は即 `'face'` に決まるが、サーバが読むのはコアの選択なので、選択が届いて配信されるまで送ってはならない。
   * 選択引数(`kind: 'selection'`)も残す: 対象を1つだけ取りhowを持たないコマンドを足したとき、ここに落ちなくすると黙って「Enterが要る」側へ移る。
   */
  const appliesOnSelection = $derived(
    command !== null &&
      !needsEnter(command) &&
      command.args.some((spec) => spec.kind === 'selection' || spec.kind === 'plane'),
  )

  /**
   * 即適用の待ち受け(起動した時点の `selfSelections`)。`null` は待っていないこと: 起動していない / Escで降りた / 一度発火した(1起動につき1回)。
   * 1回だけなのは、確定後も構えていると次の1クリックが意図せず2つ目の複製になるから。もう一度やるならボタンを押し直す。
   * `$state` にしない: 読む `$effect` が自分で書き戻すので、依存に入れると余分な再実行が走る。
   */
  let armedSelections: number | null = null

  /**
   * 起動した後に、この画面のクリックで選択が変わったときだけ適用する。面のクリックは「クリック → WS往復 → 選択の配信」の後に引数が揃うので、点のようにその場で判定できない。
   * 選択の中身の変化では引き金にしない(AIの `modify_selection` でも走ってしまう)。数えるのは `App.svelte` が「自分が出した」と確かめた選択変更だけ。
   * `busy` / `ready` / `built.ok` の判定は `commit()` が持つ。
   */
  $effect(() => {
    const clicks = selfSelections
    if (!appliesOnSelection || armedSelections === null) return
    if (clicks === armedSelections) return // 起動してから、この画面では選び直していない
    armedSelections = clicks // 揃わないクリック(空振り・選び直し)も「見た」ことにする
    if (missing !== null) return
    // 適用できたときは `reset()` が待ち受けを降ろす。降ろさずに戻ったとき(応答待ち等)は、次に選び直せばもう一度試せる。
    commit()
  })

  // ---------------------------------------------------------------- 確定 / 中断
  function commit(): void {
    if (busy) {
      onNote('直前の操作の応答待ち')
      return
    }
    // `canCommit` は `command !== null` を含むが、TSは型へ持ち越さないのでここでも明示する。
    const source = commitSource
    if (command === null || !canCommit || source === null) {
      onNote(prompt)
      return
    }
    if (!source.ok) {
      onNote(source.reason)
      return
    }
    if (committed) {
      onNote('確定の応答待ち')
      return
    }
    if (sharedId !== null) {
      // 構えた verb: 打った値は消さない(引数はサーバの `shared.args` が持ち、失敗なら構えたまま残るので近い値で打ち直せる)。
      // 成功なら配信で `command` が null になり `ended()` が消す。降ろすのは幽霊と即適用の待ち受け、出しかけの引数だけ。
      // 印は `onRun` より先に立てる: 繋がっていなくて送れなかったときは `onRun` の中で同期に `commitFailed()` が呼ばれて降りる。
      committed = true
      armedSelections = null
      sharedArgs.cancel()
      preview.cancel()
      onRun(...source.request)
      return
    }
    onRun(...source.request)
    // ローカルのコマンド(測定・ファイル操作)は従来どおり。無起動へは戻さない(成否が未確定。戻すのは `commandSucceeded()`)。
    resetInputs()
  }

  /** 確定を送ったが成否が未確定の間。Enter の二度打ちと幽霊の出し直しを止める。失敗(`commitFailed()`)か終わり(`ended()`)で降りる。 */
  let committed = false

  /** 確定した verb がサーバで失敗した・送れなかったときに `App.svelte` が呼ぶ。打った値は残っているので、打ち直せるようにするだけ。 */
  export function commitFailed(): void {
    committed = false
    commitOnEntry = null
    focusFirstNumber()
  }

  /** 繋ぎ直したときに `App.svelte` が呼ぶ。送った確定の返事は来ないので、待っている印を降ろす(構えはサーバの配信が正)。 */
  export function reconnected(): void {
    committed = false
    commitOnEntry = null
  }

  /**
   * 確定した verb がサーバで実際に成功したときに `App.svelte` が呼ぶ。`commit()` 自身では呼ばない(成否が未確定で、失敗時に打った値を見せる入力欄が消える)。
   * 呼び出し側は自分が出した操作(`client_op`)の応答だと確認してから呼ぶこと: AIが別の対象へ実行したverbの成功で人の入力中のコマンドを畳んではならない。
   */
  export function commandSucceeded(): void {
    // 構えた verb はサーバが畳み、配信で `command` が null になって `ended()` が走る。ここで畳むのはローカルのコマンド(測定)だけ。
    if (sharedId !== null) return
    localCommandId = idleCommandId
  }

  /** 中断。プレビューは投機的実行でサーバ側に何も残らないので、消すのは画面の幽霊と打ちかけの数値だけ。選択は消さない(構えた verb はサーバが、ローカルのものは `onEnd` でアプリ層が全解除する)。 */
  function abort(): void {
    commitOnEntry = null
    const wasArmed =
      Object.values(numberText).some((text) => text !== '') ||
      Object.keys(points).length > 0 ||
      Object.keys(choices).length > 0 ||
      Object.keys(planes).length > 0 ||
      Object.keys(picks).length > 0 ||
      ready
    if (sharedId !== null) {
      onEndShared() // 配信で `command` が null になり `ended()` が走る
    } else {
      localCommandId = idleCommandId // 入れ替わりを `ended()` が拾う
    }
    if (wasArmed) onNote('中断した(文書は変わっていない)')
  }

  /** 集めかけの引数を全部捨てる。起動の入れ替わり(`entered()` / `ended()`)とローカルのコマンドの確定が呼ぶ。 */
  function resetInputs(): void {
    const forgotten = new Set(
      commands.flatMap((candidate) => candidate.args).filter((spec) => spec.kind === 'text' && spec.remember === false).map((spec) => spec.name),
    )
    textValues = Object.fromEntries(Object.entries(textValues).filter(([name]) => !forgotten.has(name)))
    numberText = {}
    points = {}
    choices = {}
    planes = {}
    picks = {}
    armedPick = null
    previewFailure = null
    committed = false
    // 即適用の待ち受けも降ろす。構え直すのは `entered()` / `restart()` だけ。
    armedSelections = null
    sharedArgs.reset()
    preview.cancel()
  }

  /**
   * 起動中のコマンドの入れ替わりを1箇所で捕まえる(配信された `shared` とローカルの両方)。
   * 入ったら引数を空にして即適用の待ち受けを構え、配信された引数(AI が付けて起動した半径)を欄へ写す。
   * 作り直しの直後に既に起動していれば(再読み込み・モードの作り直し)それも「入った」として扱う。
   */
  let seenCommandId: string | null = idleCommandId
  $effect(() => {
    const now = commandId
    if (now === seenCommandId) return
    const before = seenCommandId
    seenCommandId = now
    untrack(() => {
      if (before !== null && before !== idleCommandId) ended(before)
      if (now !== null && now !== idleCommandId) entered(now)
    })
  })

  function entered(verb: string): void {
    const commitNow = commitOnEntry !== null && commitOnEntry.verb === verb && commitOnEntry.selection === selectionKey()
    commitOnEntry = null
    restart()
    onStart(verb)
    syncFromShared()
    focusFirstNumber()
    if (commitNow) {
      commit()
      return
    }
    // 読み取り専用(`query`)だけは、起動時に揃っていればその場で走らせる(`needsEnter` のdocstring末尾の例外)。測るだけなら取り返しのつかないものが無い。
    if (command !== null && categoryOf(command) === 'query' && !needsEnter(command) && missing === null) {
      commit()
    }
  }

  function ended(verb: string): void {
    resetInputs()
    onEnd(verb)
  }

  /** 引数を空にして即適用の待ち受けを構える。ここで控えた数より後のクリックだけが即適用の引き金になる(起動前から揃っていた選択では発火せず、Enterかボタンの押し直しが適用の1手)。 */
  function restart(): void {
    resetInputs()
    armedSelections = selfSelections
  }

  /** 起動中のコマンドのボタンをもう一度押したとき。揃っていれば確定、揃っていなければ構え直す(即適用は1起動につき1回なので、続けてやるための1手がここに要る)。 */
  function pressActive(): void {
    if (command === null) return
    commitOnEntry = null
    if (canCommit) {
      commit()
      return
    }
    restart()
    onStart(command.id)
    // 構えた verb は引数もサーバで置き直す(同じ verb の起動し直しは選択を残す)。
    if (sharedId !== null) onBegin(command.id, {})
    focusFirstNumber()
  }

  /**
   * Delete・Backspace で起動した verb と、押したときの選択の印。構える対象の起動は配信を待つので、入った(`entered()`)ところで確定する。
   * 選択が変わっていたら確定しない(起動が届かず残った予約で、後から AI が起動した `delete` に別の対象を消させない)。
   */
  let commitOnEntry: { verb: string; selection: string } | null = null

  function selectionKey(): string {
    return (selection?.items ?? []).map(refKey).join('|')
  }

  /**
   * Delete・Backspace = 「削除する」を押して Enter。消せるものが選ばれていれば無起動から起動して確定し、「削除する」の起動中なら確定する。
   * 別のコマンドの最中は何もしない(打ちかけの引数を捨てて別の verb を走らせない)。キーを使ったときだけ `true`。
   */
  function pressDelete(): boolean {
    const def = commands.find((candidate) => candidate.id === DELETE_VERB)
    if (def === undefined) return false
    if (commandActive) {
      if (command?.id !== DELETE_VERB) return false
      commit()
      return true
    }
    const target = def.args.find((spec): spec is SelectionArgSpec => spec.kind === 'selection')
    if (target === undefined || itemsForArg(target, selection).length < target.min) return false
    start(DELETE_VERB)
    commitOnEntry = { verb: DELETE_VERB, selection: selectionKey() }
    return true
  }

  // 外から呼ばれない(ツールバーのボタンがこの中から呼ぶだけ)。
  function start(nextId: string): void {
    commitOnEntry = null
    if (isStageable(nextId)) {
      // 構える対象はサーバへ頼むだけ。配信された `shared` で `entered()` が走る(起動の瞬間に自分で状態を持たない)。
      localCommandId = idleCommandId
      onBegin(nextId, {})
      return
    }
    if (sharedId !== null) onEndShared() // 別の verb へ移るのは前の verb の終わり
    localCommandId = nextId // 入れ替わりを `entered()` が拾う
  }

  /** モードに入った瞬間から起動しておく(`startCommandId`)。既に同じ verb が構えてあれば(作り直し)頼み直さない。`onMount` で1回だけ(モード変更は `App.svelte` が `{#key}` で作り直す)。 */
  onMount(() => {
    if (startCommandId !== undefined && shared?.verb !== startCommandId) start(startCommandId)
  })

  // ---------------------------------------------------------------- サーバの作業空間との同期(issue #153)
  const sharedArgs = createSharedArgsController({
    context: () => ({ active: sharedId !== null, committed, specs: command?.args ?? [] }),
    send: (args) => onSetArgs(args),
    render: (draft) => {
      numberText = draft.numberText
      textValues = draft.textValues
      choices = draft.choices
      points = draft.points
      picks = draft.picks
      planes = draft.planes
    },
    note: (message) => onNote(message),
    setTimer: (callback, delay) => setTimeout(callback, delay),
    clearTimer: (timer) => clearTimeout(timer),
  })

  const published = $derived(sharedId === null || command === null ? null : publishedArgs(command, liveArgs, values, built))

  $effect(() => {
    const next = published
    untrack(() => sharedArgs.update(next))
  })

  $effect(() => {
    const names = liveArgs.map((spec) => spec.name)
    untrack(() => sharedArgs.visible(names))
  })

  function flushNow(): void {
    sharedArgs.flush(published)
  }

  $effect(() => {
    const remote = shared?.args
    if (sharedId === null || remote === undefined) return
    untrack(() => syncFromShared())
  })

  function syncFromShared(): void {
    if (shared === null || command === null || sharedId === null) return
    const active = document.activeElement
    sharedArgs.receive(shared.args, command.args, { numberText, textValues, choices, points, picks, planes },
      active instanceof HTMLElement ? active.id : null)
  }

  /** verb 引数 overlay の実高さ。`.app` の `--toolbar-overlay-h` とサイドバー位置に渡す。 */
  let overlayEl = $state<HTMLDivElement | null>(null)
  let overlayObserver: ResizeObserver | undefined

  function syncOverlayHeight(el: HTMLDivElement | null): void {
    const root = document.querySelector('.app')
    if (!(root instanceof HTMLElement)) return
    const h = el?.getBoundingClientRect().height ?? 0
    root.style.setProperty('--toolbar-overlay-h', h > 0 ? `${Math.ceil(h)}px` : '0px')
    root.classList.toggle('toolbar-overlay-open', h > 0)
  }

  $effect(() => {
    overlayObserver?.disconnect()
    overlayObserver = undefined
    if (!overlayOpen) {
      syncOverlayHeight(null)
      return
    }
    const el = overlayEl
    if (el === null) return
    const sync = () => syncOverlayHeight(el)
    sync()
    overlayObserver = new ResizeObserver(sync)
    overlayObserver.observe(el)
    return () => overlayObserver?.disconnect()
  })

  onDestroy(() => {
    overlayObserver?.disconnect()
    syncOverlayHeight(null)
    sharedArgs.dispose()
    preview.detach()
  })

  /** スケッチ平面のクリックを、待っている点引数へ入れる。待っている点が無ければ何もしない(上書きすると、揃った後のクリックで図形が動いて見える)。 */
  export function addPoint(u: number, v: number): boolean {
    if (command === null) return false
    const spec = firstMissingArg(command, values)
    if (spec === null || spec.kind !== 'point') return false
    points = { ...points, [spec.name]: [u, v] }
    // この1クリックで引数が揃い Enter が要らないなら、そのまま適用する(線分の2点目)。確定成功で無起動へ戻る(`commandSucceeded()`)ので、続けるにはツールボタンを押し直す。
    if (!needsEnter(command) && firstMissingArg(command, values) === null) {
      commit()
      return true
    }
    focusFirstNumber()
    return true
  }

  /** スケッチモードで待っている点があるか(App.svelte がクリックの行き先を決める)。 */
  export function wantsPoint(): boolean {
    return missing?.kind === 'point'
  }

  /** スケッチを貼る先(基準平面 or 面)を待っているか(`App.svelte` がクリックの行き先を決める)。 */
  export function wantsPlane(): boolean {
    return missing?.kind === 'plane'
  }

  /** 基準平面のグリッドをクリックした。点(`addPoint`)と同じくその場で決まるので、揃って Enter が要らないならそのまま適用する。待っている平面引数が無ければ `false`。 */
  export function setBasePlane(name: string): boolean {
    if (command === null) return false
    const spec = firstMissingArg(command, values)
    if (spec === null || spec.kind !== 'plane') return false
    planes = { ...planes, [spec.name]: name }
    if (!needsEnter(command) && firstMissingArg(command, values) === null) {
      commit()
      return true
    }
    focusFirstNumber()
    return true
  }

  /** 直接ピックを待っている種別(待っていなければ null)。`App.svelte` がクリックの行き先を決める。 */
  export function wantsPick(): PickTarget | null {
    return pendingPick?.target ?? null
  }

  /** 待っているピック引数が原点パーツも受けるか(`PickArgSpec.datum`)。 */
  export function wantsPickDatum(): boolean {
    return pendingPick?.datum === true
  }

  /**
   * 指した参照を、待っているピック引数へ入れる(待っていなければ `false`)。呼び出し側は `wantsPick()` が返した種別に合う参照だけを渡すこと(ここでは形しか見ない)。
   * その場で決まる入力なので、揃って Enter が要らないコマンドならそのまま適用する。
   */
  export function setPick(ref: SelectionRef): boolean {
    const spec = pendingPick
    if (command === null || spec === null) return false
    picks = { ...picks, [spec.name]: ref }
    armedPick = null
    dropPickedBody(command, spec, ref)
    if (!needsEnter(command) && firstMissingArg(command, values) === null) {
      commit()
      return true
    }
    focusFirstNumber()
    return true
  }

  /**
   * 起動中のコマンドがこの面を選ばせないなら理由(選ばせるなら null)。面を食う選択引数がどれも `surfaceKinds` で断るときだけ断る。
   * 選んである面を外すクリックは通す(起動前に選んであった平面を外せなくなる)。
   */
  export function refusesFace(bodyId: string, faceIndex: number): string | null {
    if (!commandActive || command === null) return null
    const ref: SelectionRef = { body: bodyId, kind: 'face', index: faceIndex }
    if ((selection?.items ?? []).some((item) => refKey(item) === refKey(ref))) return null
    const eaters = command.args.filter(
      (spec): spec is SelectionArgSpec => spec.kind === 'selection' && argKinds(spec).includes('face') && argApplies(spec, values),
    )
    const refusing = eaters.filter((spec) => !acceptsSurface(spec, ref, faceKindOf))
    if (eaters.length === 0 || refusing.length < eaters.length) return null
    return `${refusing[0].label}ではない面は選べない`
  }

  /** 指したボディを `excludesFrom` の選択引数から外す(ブーリアンのターゲットにしたボディをツールの選択から抜く)。 */
  function dropPickedBody(cmd: CommandDef, spec: PickArgSpec, ref: SelectionRef): void {
    if (spec.excludesFrom === undefined || !('body' in ref)) return
    const from = cmd.args.find(
      (candidate): candidate is SelectionArgSpec => candidate.kind === 'selection' && candidate.name === spec.excludesFrom,
    )
    if (from === undefined) return
    const clash = (selection?.items ?? []).filter(
      (item) => 'body' in item && item.body === ref.body && argKinds(from).includes(refKind(item)),
    )
    if (clash.length > 0) onDeselect(clash)
  }

  /**
   * ソリッドの面をクリックした(貼る先を「選択にある面」にする)。ここでは適用しない: 面はサーバがコアの選択から取るので、同時に出した選択変更が配信されるまで送れない。
   * 適用は上の `$effect`(`selfSelections` が増えたとき)が行う。
   */
  export function useSelectedFaceAsPlane(): boolean {
    if (command === null) return false
    const spec = firstMissingArg(command, values)
    if (spec === null || spec.kind !== 'plane') return false
    planes = { ...planes, [spec.name]: 'face' }
    return true
  }

  /** 省略できるピックを外して空へ戻す。既定(コアが決める重心・代表点の平均)へ戻す唯一の道。 */
  function clearPick(name: string): void {
    const { [name]: _removed, ...rest } = picks
    picks = rest
    if (armedPick === name) armedPick = null
  }

  /** `fromSelection`: 選択の配信がきっかけのとき。配信はクリックから遅れて届き、続けて選んだぶんが打っている途中に届くので、この中の欄で打っていても奪わない。 */
  function focusFirstNumber(fromSelection = false): void {
    // 収集の先頭が打ち込む引数ならそこへ入れる。点を待つ間も空欄の `fromPoints` 欄へ入れる(マウスを3Dに残したまま寸法を打てる。Escは `svelte:window` が拾う)。
    // 揃っていれば `default` で埋まっている空欄へ入れる(移動の dx・包むの R は既定0で揃って見えるが、人が打つのはそこ)。選択・ピック・選択肢を待つときは奪わない。
    queueMicrotask(() => {
      // 外の入力欄(ブラウザの名前変更など)で打っている最中に、そのクリックが出した選択の配信で奪わない。
      const active = document.activeElement
      if (
        active instanceof HTMLElement &&
        (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA' || active.isContentEditable) &&
        (fromSelection || !overlayEl?.contains(active))
      ) {
        return
      }
      // 「指す」で構えた省略可・指し直しのピックは `missing` に出ないので別に見る。
      if (pendingPick !== null) return
      if (missing?.kind === 'number' || missing?.kind === 'text') {
        document.getElementById(missing.inputId)?.focus()
        return
      }
      if (missing !== null && missing.kind !== 'point') return
      const spec = numberArgs.find(
        (candidate) =>
          (missing === null || candidate.fromPoints === true) && (numberText[candidate.name] ?? '') === '',
      )
      if (spec !== undefined) document.getElementById(spec.inputId)?.focus()
    })
  }

  /** この画面のクリックで選択が変わったら打つ欄へ入れる(面・辺を選んでそのまま寸法を打てる)。AIが書き換えた選択では動かない(`selfSelections` の数え方)。 */
  let seenSelfSelections = untrack(() => selfSelections)
  $effect(() => {
    const clicks = selfSelections
    if (clicks === seenSelfSelections) return
    seenSelfSelections = clicks
    untrack(() => focusFirstNumber(true))
  })

  /**
   * 空欄の `fromPoints` 欄に出す薄字(いまマウスが指している値。無ければ従来のプレースホルダ)。
   * まず `spec.name` で `build` の結果を引く(`NumberArgSpec.fromPoints` の層をまたぐ契約)。引けなければ `spec.live`(`add_line` の長さ・角度のように名前で引けない寸法)。
   */
  /** 数値欄に打ってある文字列(前後の空白を除く。空欄なら空文字)。 */
  function rawNumber(spec: NumberArgSpec): string {
    return (numberText[spec.name] ?? '').trim()
  }

  function round4(value: number): string {
    return String(Math.round(value * 1e4) / 1e4)
  }

  function placeholderFor(spec: NumberArgSpec): string {
    if (spec.optional === true && (numberText[spec.name] ?? '') === '') return '既定'
    const resting = spec.unit
    if (spec.fromPoints !== true || (numberText[spec.name] ?? '') !== '') return resting
    const named = ghostBuilt?.args[spec.name]
    const live =
      typeof named === 'number' && Number.isFinite(named)
        ? named
        : (ghostBuilt !== null ? (spec.live?.(ghostBuilt.args) ?? null) : null)
    return typeof live === 'number' && Number.isFinite(live) ? live.toFixed(2) : resting
  }

  function onKeydown(event: KeyboardEvent): void {
    // IME の変換中の Esc(変換の取り消し)・Enter(候補の確定)はコマンドの中断・確定ではない。
    if (isImeKey(event)) return
    const focused = event.target instanceof HTMLElement ? event.target : null
    if (event.key === 'Escape') {
      if (!commandActive) return
      // 外の入力欄・ダイアログの Esc はその欄のもの。このコマンドの引数の欄の Esc は中断。
      if (ownsEscapeKey(focused) && !(focused !== null && overlayEl?.contains(focused))) return
      event.preventDefault()
      abort()
      return
    }
    if (event.key === 'Delete' || event.key === 'Backspace') {
      if (event.repeat || event.ctrlKey || event.metaKey || event.altKey || isTextEntry(focused)) return
      if (pressDelete()) event.preventDefault()
      return
    }
    // 点だけで引数が揃うコマンド(線分など)はEnterの受け皿が無いので、数値欄にフォーカスが無いときのEnterをここで拾う(欄にあるときは `onNumberKeydown` が拾う)。
    if (event.key !== 'Enter' || !canCommit) return
    // 押しっぱなしの自動の繰り返しでは確定しない(断られた確定を打ち直し続け、選択が広がった直後にも確定してしまう)。
    if (event.repeat) return
    const target = event.target as HTMLElement | null
    // ボタン・選択肢も除く: フォーカスの残ったボタン(undo等)のEnterを奪うと、そのボタンでなくコマンドが確定する。
    if (ownsEnterKey(target)) return
    event.preventDefault()
    commit()
  }

  /** 打ち込む欄(数値・文字列)でのEnter(確定)とTab(次の引数へ)。 */
  function onNumberKeydown(event: KeyboardEvent, spec: NumberArgSpec | TextArgSpec): void {
    if (event.key === 'Enter') {
      if (isImeKey(event)) return
      event.preventDefault()
      if (event.repeat) return
      commit()
      return
    }
    if (event.key !== 'Tab' || event.shiftKey || command === null) return
    const remaining = command.args
      .slice(command.args.indexOf(spec) + 1)
      .find(
        (candidate) =>
          (candidate.kind === 'number' || candidate.kind === 'text') &&
          !argSatisfied(candidate, values),
      )
    if (!remaining || (remaining.kind !== 'number' && remaining.kind !== 'text')) return
    event.preventDefault()
    document.getElementById(remaining.inputId)?.focus()
  }
</script>

<svelte:window onkeydown={onKeydown} />

<section
  class="command-host"
  data-command={command?.id}
  data-phase={command === null ? 'idle' : ready ? 'ready' : 'collecting'}
>
  <div class="row">
    <span class="label">コマンド</span>
    <div class="command-body">
      {#each commands.filter((c) => !c.hidden) as candidate (candidate.id)}
        <button
          id={candidate.buttonId}
          class:on={command !== null && candidate.id === command.id}
          onclick={() => (command !== null && candidate.id === command.id ? pressActive() : start(candidate.id))}
          disabled={command !== null && candidate.id === command.id && (busy || (!ready && needsEnter(command)))}
        >
          {candidate.label}{command !== null && candidate.id === command.id && needsEnter(candidate) ? ' (Enter)' : ''}
        </button>
      {/each}
      {#if !commandActive}
        <span id="command-prompt" class:dim={!ready}>{prompt}</span>
      {/if}
    </div>
  </div>
</section>

{#if overlayOpen}
  <div class="toolbar-overlay" bind:this={overlayEl}>
  {#if choiceArgs.length}
    <div class="row">
      {#each choiceArgs as spec (spec.name)}
        <span class="label">{spec.label}</span>
        {#each spec.options as option (option.value)}
          <button
            id={option.buttonId}
            class:on={choices[spec.name] === option.value}
            onclick={(event) => {
              choices = { ...choices, [spec.name]: option.value }
              // **押した直後にフォーカスを外す**。ボタンにフォーカスが残ったままだと、
              // 続けて押すEnterを `onKeydown` が(ボタンの操作とみなして)無視するので、
              // 「向きを選んで Enter」がどうやっても確定しない。
              ;(event.currentTarget as HTMLElement).blur()
              focusFirstNumber()
            }}
          >
            {option.label}
          </button>
        {/each}
      {/each}
    </div>
  {/if}

  {#if pickArgs.length}
    <!-- 直接ピック(共有選択集合を経由しない別チャンネル)。必須のぶんは収集の順番で構えるので、このボタンは「指し直す」と「省略できるほうを指す」ための1手。 -->
    <div class="row">
      {#each pickArgs as spec (spec.name)}
        <span class="label">{spec.label}{spec.optional === true ? '(任意)' : ''}</span>
        <button
          id={spec.buttonId}
          class:on={pendingPick?.name === spec.name}
          onclick={(event) => {
            armedPick = armedPick === spec.name ? null : spec.name
            // 選択肢ボタンと同じ理由で押した直後にフォーカスを外す(Enterを奪わせない)。
            ;(event.currentTarget as HTMLElement).blur()
          }}
        >
          {picks[spec.name] ? '指し直す' : '指す'}
        </button>
        <span id={`${spec.buttonId}-value`} class="value" class:dim={!picks[spec.name]}>
          {pickedLabel(picks[spec.name], spec.target) || '未指定'}
        </span>
        {#if spec.optional === true && picks[spec.name]}
          <button id={`${spec.buttonId}-clear`} onclick={() => clearPick(spec.name)}>外す</button>
        {/if}
      {/each}
    </div>
  {/if}

  {#if textArgs.length}
    <div class="row">
      {#each textArgs as spec (spec.name)}
        <label class="label" for={spec.inputId}>{spec.label}</label>
        <input
          id={spec.inputId}
          class="path"
          type="text"
          autocomplete="off"
          spellcheck="false"
          placeholder={spec.placeholder}
          value={textValues[spec.name] ?? ''}
          oninput={(event) => {
            textValues = { ...textValues, [spec.name]: (event.target as HTMLInputElement).value }
          }}
          onblur={flushNow}
          onkeydown={(event) => onNumberKeydown(event, spec)}
        />
      {/each}
    </div>
  {/if}

  {#if commandActive}
    <!-- 無起動・スケッチの「選ぶ」では出さない。`#command-abort` はDOMに無いことが完了条件なので、`{#if}` でDOMから外す。 -->
    <div class="row">
      {#each numberArgs as spec (spec.name)}
        <label class="label" for={spec.inputId} title={spec.hint}>{spec.label} {spec.unit}</label>
        <input
          id={spec.inputId}
          type="text"
          inputmode="decimal"
          autocomplete="off"
          title={spec.hint}
          placeholder={placeholderFor(spec)}
          value={numberText[spec.name] ?? ''}
          oninput={(event) => {
            numberText = { ...numberText, [spec.name]: (event.target as HTMLInputElement).value }
          }}
          onblur={flushNow}
          onkeydown={(event) => onNumberKeydown(event, spec)}
        />
      {/each}
      <button id="command-abort" onclick={abort}>中断 (Esc)</button>
      <!-- 起動中は引数の行に置いて折り返す(コマンド行は in-flow で、伸びるとビューポートの実寸が変わる)。 -->
      <span id="command-prompt" class:dim={!ready}>{prompt}</span>
    </div>
  {/if}
  </div>
{/if}

<style>
  .command-host {
    display: flex;
    flex-direction: column;
  }

  /* ツールバーの「押しているコマンド」を1つだけ強調する。 */
  button.on {
    border-color: var(--accent);
    color: var(--accent);
    background: var(--accent-soft);
  }

  input.path {
    flex: 1 1 320px;
    min-width: 160px;
  }
</style>
