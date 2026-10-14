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
   * 選択の中身は送らない(サーバがコアの選択から取る)。`onRun` / `onPreview` へ渡るのは数値・選択肢・パスだけ。
   * 引数収集中にAIが選択を書き換えても止めない(選択に出所の区別は無い)。
   *
   * ## プレビューは投機的実行
   *
   * 引数が揃ったら `preview` を投げる(サーバがverbを試して形だけ返し、文書は元へ戻す)。押し出しは実測で数百ms〜1秒かかるので、打鍵ごとには投げず、打ち終わり(`PREVIEW_DEBOUNCE_MS`)を待って1回だけ投げる。
   * Enterはプレビューを待たない(プレビューは確認用で、確定の前提ではない)。
   */
  import { onDestroy, onMount, untrack } from 'svelte'
  import {
    argApplies,
    argKinds,
    argSatisfied,
    buildArgs,
    choiceSends,
    commitsGhost,
    findCommand,
    firstMissingArg,
    itemsForArg,
    needsEnter,
    overflowNote,
    pickedLabel,
    promptFor,
    refKey,
    type ArgSpec,
    type ArgValues,
    type ChoiceArgSpec,
    type CommandDef,
    type NumberArgSpec,
    type PickArgSpec,
    type PickTarget,
    type Point2,
    type SelectableKind,
    type SelectionArgSpec,
    type TextArgSpec,
  } from './command'
  import { categoryOf } from './category'
  import { evaluateExpression, isPlainNumber } from '../expression'
  import type { CommandState, SelectionRef, SelectionState } from '../session'
  import { COMMAND_CATEGORY, COMMAND_PARAMS } from '../generated/protocol'

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
    onPreview,
    onCancelPreview,
    onNote,
    onWants = () => {},
    onWantsPlane = () => {},
    onWantsPoint = () => {},
    onWantsPick = () => {},
    onWantsVertices = () => {},
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
    onRun: (verb: string, args: Record<string, unknown>) => void
    /** `mode` は `CommandDef.preview`。`local` はブラウザだけで描く(サーバへ投げない)。 */
    onPreview: (verb: string, args: Record<string, unknown>, mode: 'server' | 'local') => void
    onCancelPreview: () => void
    onNote: (message: string) => void
    /** 起動中のコマンドが選択から取る種別。辺を要求したときだけアプリ層が辺の折れ線を取りに行く(常時出すと27部品ぶんの辺が要り、面のピックも辺に取られて押し出しが遅くなる)。 */
    onWants?: (kinds: SelectableKind[]) => void
    /** 起動中のコマンドが基準平面を要求しているか。アプリ層が `viewport.showBasePlanes()` を切り替える(常時指せると面を狙うクリックが手前のグリッドに取られる)。 */
    onWantsPlane?: (wants: boolean) => void
    /** スケッチ平面上の点を待っているか。 */
    onWantsPoint?: (wants: boolean) => void
    /** 起動中のコマンドが図形の直接ピックを待っているか(待っていなければ `null`)。頂点マーカーは選択に入ったものしか出ないので、中心に使う頂点を指せるようにするにはこの知らせが要る。 */
    onWantsPick?: (target: PickTarget | null) => void
    /** 起動中のコマンドが頂点そのものを対象にするか(`SelectionArgSpec.pickVertices`。測定)。アプリ層が対象ボディの頂点を候補として出し、辺より先に取る。 */
    onWantsVertices?: (wants: boolean) => void
  } = $props()

  /** 打ち終わってからプレビューを投げるまでの待ち[ms]。 */
  const PREVIEW_DEBOUNCE_MS = 250
  /** 打ってからサーバの作業空間へ出すまでの待ち[ms]。打鍵のたびに送らない(AI には落ち着いた値だけ見える)。 */
  const PUBLISH_DEBOUNCE_MS = 250

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

  /** 打ち込んだ文字列(引数名 -> 値。今はファイルのパスだけ)。確定しても消さない(下の `reset` 参照): 同じ場所への保存し直しが普通の使い方。 */
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
        collected[spec.name] = itemsForArg(spec, selection)
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
  const overflow = $derived(command === null ? '' : overflowNote(command, selection, values))

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

  /** Enter・ボタン押し直しで確定できるか(`ready` か、幽霊が出ているか)。 */
  const canCommit = $derived(commitSource !== null && overflow === '')

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
  const expressionError = $derived.by<string | null>(() => {
    for (const spec of numberArgs) {
      const raw = rawNumber(spec)
      if (raw === '') continue
      const evaluated = evaluateExpression(raw)
      if (!evaluated.ok) return `${spec.label}: ${evaluated.reason}`
    }
    return null
  })

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
    if (missing !== null) return promptFor(missing, values)
    if (overflow !== '') return overflow
    if (built && !built.ok) return built.reason
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

  /** このコマンドが選択から取る種別をアプリ層へ知らせる。無起動では `[]`(面・頂点は常に指せる)。`argKinds` を通す: `shapeKind` だけだと複数種別を食う引数(`move_body` の面/辺/頂点)で辺が指せなくなる。 */
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
    onWantsPick(pendingPick?.target ?? null)
  })

  /** 頂点そのものを対象にするか(`SelectionArgSpec.pickVertices`)。無起動では常に false。 */
  $effect(() => {
    onWantsVertices(
      (command?.args ?? []).some((spec) => spec.kind === 'selection' && spec.pickVertices === true),
    )
  })

  // ---------------------------------------------------------------- プレビュー
  let previewTimer: ReturnType<typeof setTimeout> | null = null
  /** 直前に投げたプレビューの印(同じものを投げ直さないため)。 */
  let previewedKey: string | null = null

  /** 選択の中身の印。対象集合は引数に載らないので、これを入れないと辺を足しても再送されず古い幽霊が残る。 */
  const selectionPrint = $derived(
    (command?.args ?? []).some((spec) => spec.kind === 'selection')
      ? (selection?.items ?? []).map(refKey).join('|')
      : '',
  )

  $effect(() => {
    // `cmd` へ一度だけ束ねる(`$derived` の読み直しは同じ値でも別の式として扱われ、TSのnarrowingが効かない)。
    const cmd = command
    // 確定を送った後は出さない(打った値は残すので、消した幽霊を同じ引数で出し直してしまう)。
    const source = cmd === null || cmd.preview === 'none' || committed ? null : ghostBuilt
    const key = source ? JSON.stringify([cmd!.id, source.args, selectionPrint]) : null
    if (key === previewedKey) return
    if (previewTimer) clearTimeout(previewTimer)
    previewedKey = key
    if (key === null || source === null || cmd === null) {
      onCancelPreview()
      return
    }
    const mode = cmd.preview as 'server' | 'local'
    const args = source.args
    // ブラウザだけで描ける幽霊はデバウンスしない(デバウンスは投機実行が重いことへの対処)。
    if (mode === 'local') {
      onPreview(cmd.id, args, mode)
      return
    }
    previewTimer = setTimeout(() => {
      previewTimer = null
      onPreview(cmd.id, args, mode)
    }, PREVIEW_DEBOUNCE_MS)
  })

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
      cancelPublish()
      cancelPreview()
      onRun(command.id, source.args)
      return
    }
    onRun(command.id, source.args)
    // ローカルのコマンド(測定・ファイル操作)は従来どおり。無起動へは戻さない(成否が未確定。戻すのは `commandSucceeded()`)。
    resetInputs()
  }

  /** 確定を送ったが成否が未確定の間。Enter の二度打ちと幽霊の出し直しを止める。失敗(`commitFailed()`)か終わり(`ended()`)で降りる。 */
  let committed = false

  /** 確定した verb がサーバで失敗した・送れなかったときに `App.svelte` が呼ぶ。打った値は残っているので、打ち直せるようにするだけ。 */
  export function commitFailed(): void {
    committed = false
    focusFirstNumber()
  }

  /** 繋ぎ直したときに `App.svelte` が呼ぶ。送った確定の返事は来ないので、待っている印を降ろす(構えはサーバの配信が正)。 */
  export function reconnected(): void {
    committed = false
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

  /** 幽霊を消す(投げかけも止める)。 */
  function cancelPreview(): void {
    if (previewTimer) clearTimeout(previewTimer)
    previewTimer = null
    previewedKey = null
    onCancelPreview()
  }

  /** 集めかけの引数を全部捨てる。起動の入れ替わり(`entered()` / `ended()`)とローカルのコマンドの確定が呼ぶ。 */
  function resetInputs(): void {
    numberText = {}
    points = {}
    choices = {}
    planes = {}
    picks = {}
    armedPick = null
    committed = false
    // 即適用の待ち受けも降ろす。構え直すのは `entered()` / `restart()` だけ。
    armedSelections = null
    cancelPublish()
    lastPublished = {}
    sentKeys = new Set()
    unmapped = new Set()
    cancelPreview()
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
    restart()
    onStart(verb)
    syncFromShared()
    focusFirstNumber()
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

  // 外から呼ばれない(ツールバーのボタンがこの中から呼ぶだけ)。
  function start(nextId: string): void {
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
  /** サーバが持っていると信じている引数(自分の送信と配信の写し)。配信がこれと同じなら自分の送信の返りなので欄に触らない。 */
  let lastPublished: Record<string, unknown> = {}
  let publishTimer: ReturnType<typeof setTimeout> | null = null

  /**
   * サーバへ出す引数。verb のサーバ側引数名で、宣言(`COMMAND_PARAMS`)にある名前だけ。選択・図形ID・貼る先の面は載せない
   * (対象は共有選択から)。出すのは今要る引数(`liveArgs`。`when` で隠れた欄の値は出さない)で、数値・文字列・点・ピックは
   * そのまま、選択肢は `sends` でサーバの値に読み替える。
   */
  const published = $derived.by<Record<string, unknown> | null>(() => {
    if (sharedId === null || command === null) return null
    const allowed = new Set<string>(COMMAND_PARAMS[command.id as keyof typeof COMMAND_PARAMS] ?? [])
    const out: Record<string, unknown> = {}
    for (const spec of liveArgs) {
      if (spec.kind === 'selection' || spec.kind === 'entities' || spec.kind === 'plane') continue
      let value: unknown = values[spec.name]
      if (spec.kind === 'choice') {
        const option = spec.options.find((candidate) => candidate.value === value)
        value = option === undefined ? undefined : choiceSends(option)
      }
      if (value === undefined || value === null || value === '' || !allowed.has(spec.name)) continue
      out[spec.name] = value
    }
    // 揃っていれば `build` の結果が勝つ(点から導いた寸法・貼る先の平面名はここでしかサーバ側の名前にならない)。
    if (built?.ok) {
      for (const [name, value] of Object.entries(built.args)) {
        if (allowed.has(name) && value !== undefined && value !== null) out[name] = value
      }
    }
    return out
  })

  $effect(() => {
    const next = published
    if (next === null) return
    const snapshot = { ...next }
    untrack(() => {
      if (publishTimer) clearTimeout(publishTimer)
      publishTimer = setTimeout(() => {
        publishTimer = null
        flushPublish(snapshot)
      }, PUBLISH_DEBOUNCE_MS)
    })
  })

  function cancelPublish(): void {
    if (publishTimer) clearTimeout(publishTimer)
    publishTimer = null
  }

  /** 自分がこの起動で実際に出したことのあるキー(`build` が作る `base_plane` / `mode` のように欄の名前と違うものを含む)。 */
  let sentKeys = new Set<string>()
  /** 配信された値を欄へ写せなかったキー(3択に無い荷重の向き・`sends: null` の選択肢に紐づく値など)。欄が空でも「人が消した」ではない。 */
  let unmapped = new Set<string>()

  /**
   * `lastPublished` との差分だけ送る。確定を送った後と無起動では送らない(畳まれた後に届くと「起動中のコマンドが無い」になる)。
   * 欄から消えたキーを null で外すのは、**いま出ている欄(`liveArgs`)のキーで、配信を欄へ写せなかったものでないか、
   * 自分が出したことのあるキーだけ**: 人が欄を空にしたのは「まだ決めていない」へ戻す意思で、AI が付けた値でも外す。
   * `when` で隠れている欄・欄が無いキー(AI が付けた `scale`・サーバが補う `sketch_id`)・写せなかった値は触らない
   * (再読み込みで `base_plane: 'face'` が戻らず隠れた `plane_face` を消していた)。
   */
  function flushPublish(next: Record<string, unknown>): void {
    if (sharedId === null || committed) return
    const fields = new Set(
      liveArgs
        .filter((spec) => spec.kind === 'number' || spec.kind === 'text' || spec.kind === 'point' || spec.kind === 'pick' || spec.kind === 'choice')
        .map((spec) => spec.name),
    )
    const delta: Record<string, unknown> = {}
    for (const [name, value] of Object.entries(next)) {
      if (JSON.stringify(value) !== JSON.stringify(lastPublished[name])) delta[name] = value
    }
    for (const name of Object.keys(lastPublished)) {
      if (!(name in next) && ((fields.has(name) && !unmapped.has(name)) || sentKeys.has(name))) delta[name] = null
    }
    if (Object.keys(delta).length === 0) return
    for (const [name, value] of Object.entries(delta)) {
      if (value !== null) sentKeys.add(name)
      unmapped.delete(name)
    }
    lastPublished = { ...lastPublished, ...delta }
    for (const name of Object.keys(delta)) if (delta[name] === null) delete lastPublished[name]
    onSetArgs(delta)
  }

  /** 欄を離れたら待たずに出す(打鍵中に外から来た値を欄に写さなかったぶんを、ここで自分の値で上書きする)。 */
  function flushNow(): void {
    cancelPublish()
    if (published !== null) flushPublish(published)
  }

  $effect(() => {
    const remote = shared?.args
    if (sharedId === null || remote === undefined) return
    untrack(() => syncFromShared())
  })

  /**
   * 配信された引数を欄へ写す(AI か別の画面が触ったぶん)。自分の送信の返り(`lastPublished` と同じ)なら何もしない。
   * 打っている最中の欄は奪わず(サーバの値は後勝ちだが、打ち終わりの blur / Enter で自分の値が勝つ)、札で知らせる。
   */
  function syncFromShared(): void {
    if (shared === null || command === null || sharedId === null) return
    const remote = shared.args
    if (JSON.stringify(remote) === JSON.stringify(lastPublished)) return
    const active = document.activeElement
    const kept: string[] = []
    unmapped = new Set()
    for (const spec of command.args) {
      const value = remote[spec.name]
      if (spec.kind === 'number') {
        const text = value === undefined ? '' : String(value)
        if ((numberText[spec.name] ?? '') === text) continue
        if (active instanceof HTMLElement && active.id === spec.inputId) {
          kept.push(`${spec.label}=${text === '' ? '(空)' : text}`)
          continue
        }
        numberText = { ...numberText, [spec.name]: text }
      } else if (spec.kind === 'choice') {
        if (value === undefined) {
          // サーバの値にならない選択肢(鏡映面の「面を選ぶ」。`sends: null`)は、サーバに無いことが「選んでいない」を意味しない。
          const current = spec.options.find((candidate) => candidate.value === choices[spec.name])
          if (current === undefined || choiceSends(current) !== null) {
            const { [spec.name]: _removed, ...rest } = choices
            choices = rest
          }
        } else {
          // サーバの値から選択肢を逆引きする(`copy: true` → 'copy'、`direction: [0,0,-1]` → 'down')。対応が無ければ触らず、消しもしない。
          const option = spec.options.find((candidate) => JSON.stringify(choiceSends(candidate)) === JSON.stringify(value))
          if (option !== undefined) choices = { ...choices, [spec.name]: option.value }
          else unmapped.add(spec.name)
        }
      } else if (spec.kind === 'text') {
        const text = value === undefined ? '' : String(value)
        if ((textValues[spec.name] ?? '') === text) continue
        if (active instanceof HTMLElement && active.id === spec.inputId) {
          kept.push(`${spec.label}=${text === '' ? '(空)' : text}`)
          continue
        }
        textValues = { ...textValues, [spec.name]: text }
      } else if (spec.kind === 'pick') {
        if (value !== null && typeof value === 'object') {
          picks = { ...picks, [spec.name]: value as SelectionRef }
        } else if (value === undefined) {
          const { [spec.name]: _removed, ...rest } = picks
          picks = rest
        } else {
          unmapped.add(spec.name)
        }
      } else if (spec.kind === 'point') {
        if (Array.isArray(value) && value.length === 2) {
          points = { ...points, [spec.name]: [Number(value[0]), Number(value[1])] }
        } else if (value === undefined) {
          const { [spec.name]: _removed, ...rest } = points
          points = rest
        } else {
          unmapped.add(spec.name)
        }
      } else if (spec.kind === 'plane') {
        // サーバ側の名前は `base_plane`(面は載らない: 面は選択にあるので、無ければ局所の値をそのまま保つ)。
        const base = remote.base_plane
        if (typeof base === 'string') planes = { ...planes, [spec.name]: base }
      }
    }
    lastPublished = { ...remote }
    if (kept.length > 0) onNote(`外から ${kept.join(' / ')} に変わったが、打っている途中なので欄は変えていない`)
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
    cancelPublish()
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

  /**
   * 指した参照を、待っているピック引数へ入れる(待っていなければ `false`)。呼び出し側は `wantsPick()` が返した種別に合う参照だけを渡すこと(ここでは形しか見ない)。
   * その場で決まる入力なので、揃って Enter が要らないコマンドならそのまま適用する。
   */
  export function setPick(ref: SelectionRef): boolean {
    const spec = pendingPick
    if (command === null || spec === null) return false
    picks = { ...picks, [spec.name]: ref }
    armedPick = null
    if (!needsEnter(command) && firstMissingArg(command, values) === null) {
      commit()
      return true
    }
    focusFirstNumber()
    return true
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
    const resting = spec.placeholder ?? spec.unit
    if (spec.fromPoints !== true || (numberText[spec.name] ?? '') !== '') return resting
    const named = ghostBuilt?.args[spec.name]
    const live =
      typeof named === 'number' && Number.isFinite(named)
        ? named
        : (ghostBuilt !== null ? (spec.live?.(ghostBuilt.args) ?? null) : null)
    return typeof live === 'number' && Number.isFinite(live) ? live.toFixed(2) : resting
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      if (!commandActive) return
      event.preventDefault()
      abort()
      return
    }
    // 点だけで引数が揃うコマンド(線分など)はEnterの受け皿が無いので、数値欄にフォーカスが無いときのEnterをここで拾う(欄にあるときは `onNumberKeydown` が拾う)。
    if (event.key !== 'Enter' || !canCommit) return
    const target = event.target as HTMLElement | null
    // ボタンも除く: フォーカスの残ったボタン(undo等)のEnterを奪うと、そのボタンでなくコマンドが確定する。
    if (target && ['INPUT', 'TEXTAREA', 'BUTTON'].includes(target.tagName)) return
    event.preventDefault()
    commit()
  }

  /** 打ち込む欄(数値・文字列)でのEnter(確定)とTab(次の引数へ)。 */
  function onNumberKeydown(event: KeyboardEvent, spec: NumberArgSpec | TextArgSpec): void {
    if (event.key === 'Enter') {
      event.preventDefault()
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
      <span id="command-prompt" class:dim={!ready}>{prompt}</span>
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
        <label class="label" for={spec.inputId}>{spec.label} {spec.unit}</label>
        <input
          id={spec.inputId}
          type="text"
          inputmode="decimal"
          autocomplete="off"
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
