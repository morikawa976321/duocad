<script lang="ts">
  /**
   * コマンドの状態機械 — **verbごとに作り直さないための1つだけの漏斗**。
   *
   * ```
   * 起動 → 引数収集 → プレビュー → 確定(Enter) / 中断(Esc)
   * ```
   *
   * ## Enterが要らないコマンドがある
   *
   * 要否は `needsEnter(command)` が `args` から導出する(規則は `command.ts` 側)。
   * ここが持つのは**発火の条件**だけで、それは1つ ——
   * **「起動した後の1手で引数が揃ったとき」にだけ `commit()` する**。
   *
   * - **点で揃うもの**(線分)… `addPoint` の中で同期に判定する。
   * - **基準平面で揃うもの**(スケッチ作成)… `setBasePlane` の中で同期に判定する
   *   (グリッドのピックはブラウザだけで決まる)。
   * - **選択で揃うもの**(編集用に複製 / 面に貼るスケッチ)… 面のクリックはWS往復の後に届くので、
   *   **この画面が出した選択変更が起動後に届いた回数**(`selfSelections`)を見る。
   *   起動時の値を控え、増えたときだけ発火させる(`armedSelections`)。
   *   **起動前から面が選ばれていても、AIが選択を書き換えても発火しない。**
   *
   * この形にすると「**起動しただけで文書が変わる**」が構造的に起きない。
   *
   * ## 既定は「無起動」
   *
   * `commandId` は `string | null` で、**`null` が「どのコマンドも起動していない」を表す**。
   * 見ているだけ・触っていない状態を画面に存在させるための表現である
   * (方針 [[duocad-show-only-what-is-needed]])。`null` の間は引数の収集・プレビュー・Enterの確定を
   * すべて素通りさせ、`#command-abort` も描かない(見えないだけではなくDOMに無い)。
   * `onWants([])` / `onWantsPlane(false)` を送るので、辺・領域・基準平面のピックも止まる —
   * ただし**面・頂点は常に指せる**ので「見ているだけ、選べる」が既定状態になる。
   *
   * 初期値は `defaultCommandId ?? null`。`App.svelte` は通常モードで `undefined` を渡す
   * (→ 無起動)。スケッチモードは `DEFAULT_SKETCH_COMMAND_ID`(= `select_in_sketch`)を渡すので
   * 今までどおり「選ぶ」が既定のまま(描くコマンドが常に点を待つと領域をクリックで選べなくなる
   * ため、こちらは無起動にしない)。**Escも、確定が実際に成功したときも、この初期値へ戻る**
   * (`abort()` / `commandSucceeded()` が共有する `returnToIdle()`)——
   * 通常モードは無起動、スケッチモードは「選ぶ」。**成功の確認を待つ**のが要点
   * (`commit()` 直後ではない) —— 先に降りると、失敗したときに打ち直す入力欄まで消える
   * (`commit()` のコメント参照)。同じverbをもう一度使うにはツールバーのボタンを押し直す。
   *
   * **「入った時点で起動済み」は `startCommandId` が別に持つ**。戻り先
   * (`defaultCommandId`)と起動状態は別の問いで、前者は代入しかしないため即適用の
   * 待ち受けを構えない —— 混ぜると、スケッチモードで貼る先の面をクリックしても
   * 何も起きない。
   *
   * ## 引数収集は、まず共有選択集合を見る
   *
   * 選択引数の値は**コアの選択集合から導出する**(ここには持たない)。空のときだけ
   * 「面を1つ選ぶ」と問う。AIが提案した集合を人が承認すれば、そのまま verb の対象になる。
   *
   * **選択の中身は送らない**。verbが食う面・辺・領域はサーバ側がコアの確定選択から取るので、
   * `onRun` / `onPreview` へ渡るのは数値・選択肢・パスだけ。ここで選択を読むのは、何を待って
   * いるかを出すことと、確定を許すかを決めることに使う。
   *
   * ## AIの提案のままでも確定できる
   *
   * 人が引数を集めている最中にAIが選択を書き換えることがある。**ここでは止めない** ——
   * コマンドを実行する行為そのものを承認とみなし、サーバが
   * verbを走らせる直前に `commit_selection` する(`duocad/server.py` の
   * `_commit_before_verb`)。人がクリックで直せば `modify_selection` を通って `origin` は
   * その場で `"user"` に戻るので、どちらの順でも同じ結果になる。
   *
   * ## プレビューは投機的実行
   *
   * 引数が揃ったら `preview` を投げる(サーバがverbを試して形だけ返し、文書は元へ戻す)。
   * **打鍵ごとには投げない** —— 押し出しは実測で数百ms〜1秒かかるので、打っている間ずっと
   * 投げると入力が重くなる。打ち終わり(`PREVIEW_DEBOUNCE_MS`)を待ってから1回だけ投げる。
   *
   * **Enterはプレビューを待たない。** プレビューは確認のための表示であって、確定の前提
   * ではない。
   */
  import { onMount } from 'svelte'
  import {
    COMMANDS,
    argApplies,
    argKinds,
    argSatisfied,
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
  import type { SelectionRef, SelectionState } from '../session'

  let {
    selection,
    busy,
    selfSelections = 0,
    commands = COMMANDS,
    defaultCommandId,
    startCommandId,
    hover = null,
    entities = [],
    onStart = () => {},
    onRun,
    onPreview,
    onCancelPreview,
    onNote,
    onWants = () => {},
    onWantsPlane = () => {},
    onWantsPick = () => {},
  }: {
    /** コアの選択集合(state.selection)。 */
    selection: SelectionState | null
    /** 直前の操作の応答待ち。待っている間は確定を受け付けない。 */
    busy: boolean
    /**
     * **この画面のクリックで選択が変わった回数**(`App.svelte` が数える)。
     *
     * Enterの要らないコマンドの即適用は、**これが起動後に増えたとき**にだけ発火する。
     * 選択の中身を見て発火させると、AIの `modify_selection` でも走ってしまう ——
     * 「引数を完成させる**クリック**でのみ発火」を成立させているのがこの1つの数である。
     * **数以外の意味を持たせないこと**(誰が変えたかの判断は `App.svelte` の側にある)。
     *
     * **数えているのは共有選択集合の変更だけ**である。外から非同期に届く別の引数で揃う
     * コマンドを後から足すときは、その引数にも同じ形の数を用意すること ——
     * **中身が変わったこと**を引き金にすると、AIが書き換えたときにも発火する。
     */
    selfSelections?: number
    /**
     * このモードで使えるコマンド。既定はソリッドを触るぶん。
     * スケッチモードでは `commands/sketch.ts` の一覧が渡る —— **状態機械は同じ1つ**で、
     * 変わるのは一覧と既定だけ(モードごとにUIを作り直さない)。
     */
    commands?: CommandDef[]
    /** 起動していない状態を表すには渡さない(`undefined`)。渡さなければ `null`(無起動)になる。 */
    defaultCommandId?: string
    /**
     * **最初から起動しておく**コマンド(`start()` を1回だけ通す)。`defaultCommandId` とは
     * 別物である —— あちらは「Esc・確定成功のあとで戻る先」で、代入しかしないので
     * **即適用の待ち受け(`armedSelections`)が構えない**。モードに入った時点で人の
     * クリックを待たせたい側(スケッチモードの貼る先待ち)はこちらを使う。
     *
     * **戻り先にはしない。** Escで降りたら無起動(`defaultCommandId ?? null`)になり、
     * 基準平面も面も指せなくなる —— 「起動 → Esc のあとのクリックでは発火しない」
     * (`smoke-files.mjs` の(c))を、モードを新設しても同じ形で保つ。
     */
    startCommandId?: string
    /**
     * スケッチ平面上でポインタが今指している点。**幽霊にだけ使う**。
     * これが無いと、線分は2点目を置くまで何も見えない。
     */
    hover?: Point2 | null
    /**
     * スケッチパネルで選ばれている図形のID。**反復のverbだけが取る引数**で、
     * 共有選択集合には入らない(`command.ts` の `EntitiesArgSpec` 参照)。
     */
    entities?: string[]
    /**
     * **起動した**(`start()`)瞬間に呼ばれる。既定は何もしない。
     *
     * 形状最適化の4つのverb(`App.svelte`)だけが使う ——起動時に既存の割り当てを
     * 選択状態にして見せるため、`App.svelte` 側でそのverbのプリセレクトを行う。
     * ここでは何が起きるかを知らない(verb名を渡すだけ)。
     */
    onStart?: (verb: string) => void
    onRun: (verb: string, args: Record<string, unknown>) => void
    /** `mode` は `CommandDef.preview`。`local` はブラウザだけで描く(サーバへ投げない)。 */
    onPreview: (verb: string, args: Record<string, unknown>, mode: 'server' | 'local') => void
    onCancelPreview: () => void
    onNote: (message: string) => void
    /**
     * 起動中のコマンドが選択から取る種別。**辺を要求したときだけ**アプリ層が
     * 辺の折れ線を取りに行き、ビューポートで指せるようにする —— 常時出すと
     * 27部品ぶんの辺が要り、面のピックも辺に取られて押し出しが遅くなる。
     */
    onWants?: (kinds: SelectableKind[]) => void
    /**
     * 起動中のコマンドが**基準平面を要求している**か。`onWants`(辺)と同じ形の受動的な
     * 知らせで、アプリ層はこれを見て `viewport.showBasePlanes()` を切り替える ——
     * 常時指せると、面を狙うクリックが手前のグリッドに取られる。
     */
    onWantsPlane?: (wants: boolean) => void
    /**
     * 起動中のコマンドが**図形の直接ピックを待っている**か(待っていなければ `null`)。
     * `onWants`(辺)・`onWantsPlane`(基準平面)と同じ形の受動的な知らせで、アプリ層は
     * これを見て**指せるもの**を出す(頂点マーカーは選択に入ったものしか出ていないので、
     * 中心に使う頂点を指せるようにするにはこの知らせが要る)。
     */
    onWantsPick?: (target: PickTarget | null) => void
  } = $props()

  /** 打ち終わってからプレビューを投げるまでの待ち[ms]。 */
  const PREVIEW_DEBOUNCE_MS = 250

  // 初期値を1回だけ捕まえるのが正しい —— モードが変わるときは呼び出し側(`App.svelte`)が
  // `{#key}` でこのコンポーネントごと作り直す(集めかけの引数を持ち越さないため)。
  // `defaultCommandId` が無ければ無起動(`null`)から始まる。
  // svelte-ignore state_referenced_locally
  let commandId = $state<string | null>(defaultCommandId ?? null)
  const command = $derived<CommandDef | null>(commandId === null ? null : findCommand(commandId, commands))

  /** 数値引数の生の入力(引数名 -> 打った文字列)。確定・中断のたびに空へ戻す。 */
  let numberText = $state<Record<string, string>>({})

  /**
   * クリックで集めた点(引数名 -> [u,v])。**選択集合には入れない** ——
   * 線の始点が共有選択に溜まると、次のコマンドがそれを引数として拾ってしまう。
   */
  let points = $state<Record<string, Point2>>({})

  /** 選んだ選択肢(引数名 -> 値)。**既定は入れない**(`ChoiceArgSpec` のdocstring)。 */
  let choices = $state<Record<string, string>>({})

  /**
   * 指したスケッチ平面(引数名 -> `'XY' | 'XZ' | 'YZ' | 'face'`)。
   *
   * **点と同じくここに持つ**(選択集合には入れない)。基準平面は選択集合に入れられる
   * 種別ではないし、面の側も**入るのは面の参照であって「面に貼る」という意思ではない**。
   */
  let planes = $state<Record<string, string>>({})

  /**
   * 打ち込んだ文字列(引数名 -> 値。今のところファイルのパスだけ)。
   *
   * **確定しても消さない**のがここだけ違う(下の `reset` 参照)。同じ場所へ保存し直す・
   * 書き出し先を1文字だけ直す、が普通の使い方で、確定のたびにパスが消えると毎回打ち直しになる。
   */
  let textValues = $state<Record<string, string>>({})

  /**
   * 直接ピックで指した参照(引数名 -> 参照1つ)。**共有選択集合には入れない** ——
   * 回転軸に使う辺を選択へ入れると、コアの `require_bodies` がその辺のボディまで
   * 「動かす対象」に数える(`PickArgSpec` のdocstring)。
   */
  let picks = $state<Record<string, SelectionRef>>({})

  /**
   * **人が「いまここを指す」と言った省略可能なピック引数**の名前(言っていなければ `null`)。
   *
   * 必須のピックは `firstMissingArg` が指すので構える必要が無いが、省略できるピック
   * (回転中心)は永久に「揃っている」側なので、**指したいと言う手**がここに要る。
   * `$state` にするのは、テンプレートのボタンが押した結果を待ち受け表示へ反映するため。
   */
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
        // 選択集合と同じく**導出する**(ここには持たない)。スケッチパネルで選び直せば追従する。
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
        const raw = (numberText[spec.name] ?? '').trim()
        // 空欄で `default` を持つ引数(move系のdx/dy/dz)は、打っていない軸を0扱いにして
        // 空欄のまま確定できる(`NumberArgSpec.default`のdocstring参照)。
        const parsed = raw === '' ? (spec.default ?? Number.NaN) : Number.parseFloat(raw)
        if (Number.isFinite(parsed)) collected[spec.name] = parsed
      }
    }
    return collected
  })

  /** `command` が `null`(無起動)の間は待っている引数も無い。 */
  const missing = $derived(command === null ? null : firstMissingArg(command, values))

  /** 選ばれすぎ(`max` 超え)の理由。空でない間は確定させない。 */
  const overflow = $derived(command === null ? '' : overflowNote(command, selection, values))

  /** **無起動では確定できない**(`command === null` を明示的に含める。Enterが素通りしないため)。 */
  const ready = $derived(command !== null && missing === null && overflow === '')
  const built = $derived(command !== null && ready ? command.build(values) : null)

  /**
   * 幽霊に使う引数。**待っている点をポインタの位置で仮に埋める**ので、線分なら
   * 2点目を置く前から線が見えている(ゴムひも)。確定に使うのは `built` のほうで、
   * ここは表示専用。
   */
  const ghostBuilt = $derived.by(() => {
    if (built?.ok) return built
    if (command === null || missing?.kind !== 'point' || hover === null) return null
    const provisional = { ...values, [missing.name]: hover }
    if (firstMissingArg(command, provisional) !== null) return null
    const result = command.build(provisional)
    return result.ok ? result : null
  })

  /**
   * **確定に使う引数**。揃っていれば `built`、まだ点を待っていても幽霊が出ている
   * コマンド(`commitsGhost`)なら**画面に見えているその形**(`ghostBuilt`)。
   *
   * **線分で「長さ50・角度30を打ってEnter」が通るのはこの口である** —— 始点だけ置いた
   * 状態のEnterがゴムひもの線をその場で確定するのは暴発ではなく、意図した挙動である。
   */
  const commitSource = $derived(
    built ?? (command !== null && commitsGhost(command) ? ghostBuilt : null),
  )

  /** Enter・ボタン押し直しで確定できるか(`ready` か、幽霊が出ているか)。 */
  const canCommit = $derived(commitSource !== null && overflow === '')

  /** 画面に出す1行(何を待っているか)。**無起動では空文字**(プレースホルダ文を新設しない)。 */
  const prompt = $derived.by(() => {
    if (command === null) return ''
    if (missing !== null) return promptFor(missing, values)
    if (overflow !== '') return overflow
    if (built && !built.ok) return built.reason
    return 'Enterで確定 / Escで中断'
  })

  /**
   * いま**要る**引数だけ(`when` が偽のものを落とす)。入力欄・ボタンの描画はこれを通す ——
   * 出したままにすると、並進を選んでいるのに角度の欄が並び、打っても送られない。
   */
  const liveArgs = $derived((command?.args ?? []).filter((spec: ArgSpec) => argApplies(spec, values)))

  const numberArgs = $derived(liveArgs.filter((spec) => spec.kind === 'number') as NumberArgSpec[])
  const choiceArgs = $derived(liveArgs.filter((spec) => spec.kind === 'choice') as ChoiceArgSpec[])
  const textArgs = $derived(liveArgs.filter((spec) => spec.kind === 'text') as TextArgSpec[])
  const pickArgs = $derived(liveArgs.filter((spec) => spec.kind === 'pick') as PickArgSpec[])

  /**
   * **いま直接ピックを待っている引数**(待っていなければ null)。
   *
   * 2つの経路がある: 必須のピックは収集の順番で来る(`missing`)。省略できるピックは
   * 「指す」ボタンで人が構えたときだけ(`armedPick`)。**構えたほうが勝つ** ——
   * 中心を指し直したい人が、まだ埋まっていない別の必須引数に邪魔されないため。
   */
  const pendingPick = $derived.by<PickArgSpec | null>(() => {
    if (command === null) return null
    if (armedPick !== null) {
      const armed = liveArgs.find((spec) => spec.name === armedPick)
      if (armed !== undefined && armed.kind === 'pick') return armed
    }
    return missing !== null && missing.kind === 'pick' ? missing : null
  })

  /** このコマンドが選択から取る種別をアプリ層へ知らせる(辺のときだけ辺を取りに行く)。
   *  無起動では何も要求しない(`[]`)——面・頂点は常に指せるので、それだけが既定状態に残る。
   *
   *  **`argKinds` を通す**。`shapeKind` 1つだけを見ていると、複数の種別を食う
   *  引数(`move_body` の面/辺/頂点)で**主たる種別しか要求されず、辺が指せないまま**になる ——
   *  サーバは辺の選択を受け付けるのに画面から選べない、という穴が静かに開く。 */
  $effect(() => {
    onWants(
      (command?.args ?? [])
        .filter((spec) => spec.kind === 'selection')
        .flatMap((spec) => argKinds(spec as SelectionArgSpec)),
    )
  })

  /** 基準平面を要求しているか(辺と同じ規律で、起動している間だけ指せるようにする)。無起動では常にfalse。 */
  $effect(() => {
    onWantsPlane((command?.args ?? []).some((spec) => spec.kind === 'plane'))
  })

  /** 直接ピックを待っているか(待っている**種別**をそのまま知らせる)。無起動では常に null。 */
  $effect(() => {
    onWantsPick(pendingPick?.target ?? null)
  })

  // ---------------------------------------------------------------- プレビュー
  let previewTimer: ReturnType<typeof setTimeout> | null = null
  /** 直前に投げたプレビューの印(同じものを投げ直さないため)。 */
  let previewedKey: string | null = null

  /**
   * 選択の中身の印。**引数だけでは再送の判定にならない** —— 対象集合が引数から消えた
   * (サーバがコアの選択から取る)ので、辺を1本足しても引数は同じままで、
   * 印に入れないと「選び直したのに古い幽霊が残る」。
   */
  const selectionPrint = $derived(
    (command?.args ?? []).some((spec) => spec.kind === 'selection')
      ? (selection?.items ?? []).map(refKey).join('|')
      : '',
  )

  $effect(() => {
    // `cmd` へ一度だけ束ねる —— 以後この関数内では `command` を読み直さない(TSの型narrowing
    // を素直に効かせるため。`$derived` の読み直しは同じ値でも別の式として扱われる)。
    const cmd = command
    const source = cmd === null || cmd.preview === 'none' ? null : ghostBuilt
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
    // **ブラウザだけで描ける幽霊は待たない**。デバウンスは「投機実行が重い」ことへの
    // 対処(押し出しは実測で数百ms〜1秒)であって、線を1本引くのに待つ理由は無い。
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
   * 即適用のうち、**選択が揃うのを待つ**もの —— 現行では**面に貼るときの `sketch_create`**
   * だけである。点・基準平面のように**その場で決まる**ものは `addPoint` / `setBasePlane` の
   * 中で同期に判定するので、ここには来ない。
   *
   * 平面引数を含める理由: 面をクリックしてスケッチを貼る経路は、`plane` の値こそ
   * その場で `'face'` に決まるが、**サーバが読むのはコアの確定選択**なので、
   * 選択がサーバに届いて配信されるまで送ってはならない。
   *
   * **選択引数(`kind: 'selection'`)の側も残してある。** いま該当するコマンドは無いが、
   * 対象を1つだけ取りhowを持たないコマンドを足せばまたここへ落ちる —— 落ちなくすると、そのコマンドだけが黙って
   * 「Enterが要る」側へ移る。
   */
  const appliesOnSelection = $derived(
    command !== null &&
      !needsEnter(command) &&
      command.args.some((spec) => spec.kind === 'selection' || spec.kind === 'plane'),
  )

  /**
   * 即適用の待ち受け —— **起動した時点の `selfSelections`**。`null` は待っていないこと。
   *
   * `null` になるのは3つ: **起動していない**(既定で構えているだけ)/ **Escで降りた** /
   * **一度発火した**(1起動につき1回だけ)。1回だけにしてあるのは、確定した後もずっと
   * 構えていると、**次の1クリックが意図せず2つ目の複製になる**からである ——
   * もう一度やるならボタンを押し直す(1手)。
   *
   * `$state` にしない: この値を読む `$effect` が自分で書き戻すので、依存に入れると
   * 発火のたびに余分な再実行が走る(挙動は同じだが、読む人が無限ループを疑うことになる)。
   */
  let armedSelections: number | null = null

  /**
   * **起動した後に、この画面のクリックで選択が変わった**ときだけ適用する。
   *
   * 面のクリックは「クリック → WS往復 → 選択の配信」の後に引数が揃うので、点のように
   * その場で判定できない。**選択の中身が変わったこと**では引き金にならない —— それだと
   * AIの `modify_selection` でも走り、人が何も指していないのに文書が変わる。数えているのは
   * `App.svelte` が「自分が出した」と確かめた選択変更だけである。
   *
   * `busy` / `ready` / `built.ok` の判定は `commit()` が持っているので、ここでは見ない。
   */
  $effect(() => {
    const clicks = selfSelections
    if (!appliesOnSelection || armedSelections === null) return
    if (clicks === armedSelections) return // 起動してから、この画面では選び直していない
    armedSelections = clicks // 揃わないクリック(空振り・選び直し)も「見た」ことにする
    if (missing !== null) return
    // 適用できたときは `reset()` が待ち受けを降ろす(1起動につき1回)。**降ろさずに
    // 戻ってきたとき**(応答待ち・提案が載っている等)は数を進めただけなので、
    // 次に選び直せばもう一度試せる。
    commit()
  })

  // ---------------------------------------------------------------- 確定 / 中断
  function commit(): void {
    if (busy) {
      onNote('直前の操作の応答待ち')
      return
    }
    // `canCommit` は `command !== null` を含むが、TSはそれを`command`の型へ持ち越さないので
    // ここでも明示する(無起動でEnterやクリックがすり抜けて呼ばれた場合の防御でもある)。
    const source = commitSource
    if (command === null || !canCommit || source === null) {
      onNote(prompt)
      return
    }
    if (!source.ok) {
      onNote(source.reason)
      return
    }
    lastInputs = {
      numbers: { ...numberText },
      points: { ...points },
      choices: { ...choices },
      picks: { ...picks },
    }
    onRun(command.id, source.args)
    reset()
    // **ここでは無起動へ戻さない。** `onRun` はサーバへ投げるだけの楽観的な発火で、
    // 成否はまだ分からない —— OCCTのフィレットのように、半径次第で `IsDone()=False` に
    // 落ちることがある(`lastInputs`/`restoreLastInputs` のdocstring参照)。ここで
    // 無起動にすると、失敗が返ってきて `restoreLastInputs()` が半径を戻しても
    // `command === null` なので入力欄ごと消えたままになり、「失敗したらすぐ近くの値で
    // 打ち直す」という文書化済みの必須ワークフロー(`tests/README.md`)が壊れる
    // (実機smoke `smoke-features.mjs` で実際に踏んだ)。**無起動へ戻すのは
    // 確定成功が実サーバから返ってきたとき**(`commandSucceeded()`)だけにする。
  }

  /**
   * 無起動(通常モードは`null`・スケッチモードは「選ぶ」)へ戻す。**`abort()` から呼ぶ**ほか、
   * **確定が実際に成功したとき**は `commandSucceeded()` 経由で呼ばれる。
   */
  function returnToIdle(): void {
    const idleId = defaultCommandId ?? null
    if (commandId !== idleId) commandId = idleId
  }

  /**
   * **確定した verb がサーバで実際に成功した**ときに呼ぶ(`App.svelte` の外側から)。
   *
   * **`commit()` 自身では呼ばない**のが要点: `onRun` の時点ではまだ成否が
   * 分からず、先に無起動へ戻すと失敗時に打った値を見せる場所(入力欄)が消える
   * (`commit()` のコメント参照)。呼び出し側は自分が出した操作(`client_op`)の応答だと
   * 確認してから呼ぶこと —— 無関係な操作(AIが別の対象へ実行したverb等)の成功で
   * 人の入力中のコマンドを畳んではならない。
   */
  export function commandSucceeded(): void {
    returnToIdle()
  }

  /**
   * 直前に確定したときの入力(**失敗したときだけ**戻す)。
   *
   * OCCTのフィレットは半径に対して単調でなく(§7: r=10.0 失敗 / r=10.5 成功)、
   * **失敗したらすぐ近くの値で打ち直す**のが実際の使い方になる。確定のたびに欄を
   * 空にする骨格のままだと、失敗のたびに全部打ち直しになる。選択はコア側にあって
   * 消えないので、ここで戻すのは打った値だけでよい。
   */
  let lastInputs: {
    numbers: Record<string, string>
    points: Record<string, Point2>
    choices: Record<string, string>
    picks: Record<string, SelectionRef>
  } | null = null

  export function restoreLastInputs(): void {
    if (lastInputs === null) return
    numberText = { ...lastInputs.numbers }
    points = { ...lastInputs.points }
    choices = { ...lastInputs.choices }
    // **指した辺・頂点も戻す**(打った値と同じ理由)。回転軸を指し直すのは
    // 数値の打ち直しより手数が多く、失敗のたびに指し直しになると使い物にならない。
    picks = { ...lastInputs.picks }
    focusFirstNumber()
  }

  /**
   * 中断。**文書は一切変わらない** —— プレビューは投機的実行なのでサーバ側に何も
   * 残っておらず、ここで消すのは画面の幽霊と打ちかけの数値だけ。
   *
   * **選択は消さない**。選ぶことは文書の変更ではなく、選び直しのたびに
   * 選択が飛ぶと「Escで打ち直す」ができなくなる(選択を空にしたいときは
   * 選択パネルの「全解除」)。
   *
   * **戻り先は`defaultCommandId ?? null`**(通常モードは無起動・スケッチモードは
   * 「選ぶ」)。**確定が実際に成功したとき(`commandSucceeded()`)も同じ
   * 戻り先(`returnToIdle()`)へ降りる**。
   */
  function abort(): void {
    const wasArmed =
      Object.values(numberText).some((text) => text !== '') ||
      Object.keys(points).length > 0 ||
      Object.keys(choices).length > 0 ||
      Object.keys(planes).length > 0 ||
      Object.keys(picks).length > 0 ||
      ready
    reset()
    returnToIdle()
    if (wasArmed) onNote('中断した(文書は変わっていない)')
  }

  function reset(): void {
    numberText = {}
    points = {}
    choices = {}
    planes = {}
    picks = {}
    armedPick = null
    // **即適用の待ち受けも降ろす**。確定・中断のどちらでも「起動している間」は終わりで、
    // 構え直すのは `start()`(ボタンを押す1手)だけにする。
    armedSelections = null
    if (previewTimer) clearTimeout(previewTimer)
    previewTimer = null
    previewedKey = null
    onCancelPreview()
  }

  /**
   * **起動中のコマンドのボタンをもう一度押した**とき。
   *
   * 揃っていれば確定(Enterと同じ)。揃っていなければ**構え直す** —— 即適用は1起動に
   * つき1回なので、続けてもう一度やるための1手がここに要る(Escで既定へ降りてから
   * 押し直す2手にしない)。Enterが要るコマンドではボタン自体が無効なので通らない。
   */
  function pressActive(): void {
    if (command === null) return // 呼び出し元(テンプレート)は起動中のボタンからしか呼ばない
    if (canCommit) {
      commit()
      return
    }
    start(command.id)
  }

  // 外から呼ばれない(ツールバーのボタンがこの中から呼ぶだけ)。
  function start(nextId: string): void {
    reset()
    commandId = nextId
    // **ここで控えた数より後のクリックだけ**が即適用の引き金になる。
    // 起動より前から揃っていた選択(AIが選んだ面のまま起動した等)では発火せず、
    // Enter か、ボタンをもう一度押すことが適用の1手として残る。
    armedSelections = selfSelections
    onStart(nextId)
    focusFirstNumber()
  }

  /**
   * **モードに入った瞬間から起動しておく**(`startCommandId`)。
   *
   * `commandId` への代入ではなく `start()` を通すのが要点 —— 即適用の待ち受け
   * (`armedSelections`)を構えるのは `start()` だけなので、代入だけでは
   * 「面をクリックしてスケッチを貼る」経路が黙って死ぬ。
   *
   * **`onMount` で1回だけ**。モードが変わるときは `App.svelte` が `{#key}` でこの
   * コンポーネントごと作り直すので、これが再入の唯一の口になる。
   */
  onMount(() => {
    if (startCommandId !== undefined) start(startCommandId)
  })

  /**
   * スケッチ平面のクリックを、待っている点引数へ入れる。
   *
   * 待っている点が無ければ**何もしない**(黙って最後の点を上書きすると、
   * 引数が揃った後のクリックで図形が動いて見える)。
   */
  export function addPoint(u: number, v: number): boolean {
    if (command === null) return false
    const spec = firstMissingArg(command, values)
    if (spec === null || spec.kind !== 'point') return false
    points = { ...points, [spec.name]: [u, v] }
    // **この1クリックで引数が揃い、Enterが要らないなら、そのまま適用する**(線分の2点目)。
    // `values` は `$derived` なので、上の代入の直後に読めば新しい点が入っている。
    // **確定が成功すると無起動(スケッチモードは「選ぶ」)へ戻る**(`commandSucceeded()`)
    // ——続けて次の線を引くには、確定のたびにツールボタンを押し直す。
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

  /**
   * スケッチを貼る先(基準平面 or 面)を待っているか。
   * `wantsPoint` と同じで、`App.svelte` がクリックの行き先を決めるために見る。
   */
  export function wantsPlane(): boolean {
    return missing?.kind === 'plane'
  }

  /**
   * **基準平面のグリッドをクリックした**。点(`addPoint`)と同じく**その場で決まる**ので、
   * これで引数が揃い Enter が要らないなら、そのまま適用する(2手目のクリックが確定を兼ねる)。
   *
   * 待っている平面引数が無ければ何もしない(`false` を返す)。
   */
  export function setBasePlane(name: string): boolean {
    if (command === null) return false
    const spec = firstMissingArg(command, values)
    if (spec === null || spec.kind !== 'plane') return false
    planes = { ...planes, [spec.name]: name }
    if (!needsEnter(command) && firstMissingArg(command, values) === null) commit()
    return true
  }

  /**
   * **直接ピックを待っている種別**(待っていなければ null)。`wantsPoint` / `wantsPlane` と
   * 同じで、`App.svelte` がクリックの行き先を決めるために見る。
   */
  export function wantsPick(): PickTarget | null {
    return pendingPick?.target ?? null
  }

  /**
   * **指した参照を、待っているピック引数へ入れる**(待っていなければ何もせず `false`)。
   *
   * 呼び出し側は種別が合っていることを確かめてから呼ぶこと(`wantsPick()` が返した
   * 種別に対応するピックだけを渡す) —— ここでは参照の形しか見ない。
   *
   * `addPoint` / `setBasePlane` と同じく**その場で決まる**入力なので、この1手で引数が
   * 揃い Enter が要らないコマンドならそのまま適用する。
   */
  export function setPick(ref: SelectionRef): boolean {
    const spec = pendingPick
    if (command === null || spec === null) return false
    picks = { ...picks, [spec.name]: ref }
    armedPick = null // 1回指したら構えを降ろす(続けて指し直すならボタンを押し直す)
    if (!needsEnter(command) && firstMissingArg(command, values) === null) commit()
    return true
  }

  /**
   * **ソリッドの面をクリックした** —— 貼る先を「選択にある面」にする。
   *
   * `setBasePlane` と違い**ここでは適用しない**。面はサーバがコアの確定選択から取るので、
   * 呼び出し側が同時に出した選択の変更がサーバへ届いて配信されるまで送れない。
   * 適用は上の `$effect`(`selfSelections` が増えたとき)が行う ——
   * **引き金は「人がこの画面で指した回数」のままで、選択の中身ではない**。
   */
  export function useSelectedFaceAsPlane(): boolean {
    if (command === null) return false
    const spec = firstMissingArg(command, values)
    if (spec === null || spec.kind !== 'plane') return false
    planes = { ...planes, [spec.name]: 'face' }
    return true
  }

  /**
   * 省略できるピックを**外す**(指し直しではなく空へ戻す)。
   *
   * 「指したものを取り消す手」が無いと、回転中心を一度指したら最後まで外せない ——
   * 既定(コアが決める重心・代表点の平均)へ戻す唯一の道がここになる。
   */
  function clearPick(name: string): void {
    const { [name]: _removed, ...rest } = picks
    picks = rest
    if (armedPick === name) armedPick = null
  }

  function focusFirstNumber(): void {
    // 収集の先頭が打ち込む引数(数値・文字列)なら、そこへ入れる。
    // **点を待っている間も空欄の `fromPoints` 欄へ入れる** —— マウスを3Dに残したまま
    // 寸法を打てる(ビューポートのクリックはフォーカスに関係なく届く。Escは
    // `svelte:window` が拾うので入力欄にフォーカスがあっても効く)。選択待ちのときは
    // 奪わない。
    queueMicrotask(() => {
      if (missing?.kind === 'number' || missing?.kind === 'text') {
        document.getElementById(missing.inputId)?.focus()
        return
      }
      if (missing?.kind !== 'point') return
      const spec = numberArgs.find(
        (candidate) => candidate.fromPoints === true && (numberText[candidate.name] ?? '') === '',
      )
      if (spec !== undefined) document.getElementById(spec.inputId)?.focus()
    })
  }

  /**
   * 空欄の `fromPoints` 欄に出す薄字 —— **いまマウスが指している値**(無ければ従来の
   * プレースホルダ)。「空欄=マウス追従」の唯一の見える手がかりになる。
   *
   * まず `spec.name` で `build` の結果を引く(`NumberArgSpec.fromPoints` の層をまたぐ契約)。
   * 引けなければ `spec.live` に作らせる(`add_line` の長さ・角度のように `p0`/`p1` へ
   * 畳まれて名前で引けない寸法)。どちらも数値でなければ従来のプレースホルダ。
   */
  function placeholderFor(spec: NumberArgSpec): string {
    if (spec.fromPoints !== true || (numberText[spec.name] ?? '') !== '') return spec.placeholder
    const named = ghostBuilt?.args[spec.name]
    const live =
      typeof named === 'number' && Number.isFinite(named)
        ? named
        : (ghostBuilt !== null ? (spec.live?.(ghostBuilt.args) ?? null) : null)
    return typeof live === 'number' && Number.isFinite(live) ? live.toFixed(2) : spec.placeholder
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault()
      abort()
      return
    }
    // **点だけで引数が揃うコマンド(線分など)はEnterの受け皿が無い**。数値欄に
    // フォーカスが無いときのEnterをここで拾う —— 欄にフォーカスがあるときは
    // `onNumberKeydown` が拾うので二重に確定しない。
    if (event.key !== 'Enter' || !canCommit) return
    const target = event.target as HTMLElement | null
    // ボタンも除く —— フォーカスの残ったボタン(undo等)のEnterを奪うと、押したつもりの
    // ボタンではなくコマンドが確定する。
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
    <span id="command-prompt" class="value" class:dim={!ready}>{prompt}</span>
  </div>

  {#if choiceArgs.length}
    <!-- 選択肢。**既定を選んでおかない** — 押されるまで引数は揃わない -->
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
    <!-- 直接ピック(共有選択集合を経由しない別チャンネル)。**必須のぶんは収集の順番で
         勝手に構える**ので、このボタンは「指し直す」と「省略できるほうを指す」ための1手。 -->
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
    <!-- 打ち込む文字列(今のところファイルのパスだけ)。**確定しても消さない** -->
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
          onkeydown={(event) => onNumberKeydown(event, spec)}
        />
      {/each}
    </div>
  {/if}

  {#if command !== null}
    <!-- **無起動ではこの行ごと出さない**。`#command-abort` はDOMに無いことが
         完了条件そのものなので、`disabled`/非表示ではなく `{#if}` でDOMから外す。 -->
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
          onkeydown={(event) => onNumberKeydown(event, spec)}
        />
      {/each}
      <button id="command-abort" onclick={abort}>中断 (Esc)</button>
    </div>
  {/if}
</section>

<style>
  .command-host {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    row-gap: 6px;
    position: relative;
  }

  /* コマンド行と、その後に続く引数行(選択肢/パス/数値)との区切り。`:first-child`だけ
     外すのは、選択肢/パス行が`{#if}`で出たり消えたりしても先頭行は常にコマンド行だから。
     position:absoluteの疑似要素は通常フローの高さに数えられないので、`.command-host`の
     gap(4px)の中に線を描いても`.toolbar`ひいてはビューポートの実寸(styles.css冒頭の
     不変条件)は動かない。border/paddingだと各行の高さがそのままビューポートを押し縮める。 */
  .row:not(:first-child)::before {
    content: '';
    position: absolute;
    left: 0;
    right: 0;
    top: -2px;
    height: 1px;
    background: var(--line);
  }

  .label {
    flex: 0 0 auto;
    color: var(--dim);
  }

  .value {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .dim {
    color: var(--dim);
  }

  /* ツールバーの「押しているコマンド」を1つだけ強調する。 */
  button.on {
    border-color: var(--accent, #ffb454);
    color: var(--accent, #ffb454);
    background: var(--accent-soft, rgba(255, 180, 84, 0.16));
  }

  /* パスは数値より長い(見た目の作り込みではなく、打った先が読めないと確認できない) */
  input.path {
    flex: 1 1 320px;
    min-width: 160px;
  }
</style>
