<script lang="ts">
  /**
   * コマンドの状態機械 — **verbごとに作り直さないための1つだけの漏斗**。
   *
   * ```
   * 起動 → 引数収集 → プレビュー → 確定(Enter) / 中断(Esc)
   * ```
   *
   * ## Enterが要らないコマンドがある(2026-08-22)
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
   * この形にすると「**起動しただけで文書が変わる**」が構造的に起きない ——
   * verb→target→how の順序原則(engine `2026-08-22-duocad-ui-interaction-principles`)を
   * 状態機械の側で担保している。**verbごとの分岐は1つも増えていない。**
   *
   * ## 引数収集は、まず共有選択集合を見る
   *
   * 選択引数の値は**コアの選択集合から導出する**(ここには持たない)。空のときだけ
   * 「面を1つ選ぶ」と問う。AIが提案した集合を人が承認すれば、そのまま verb の対象になる
   * —— これがAI→人間の受け渡しの実体である(実装計画書§8b)。
   *
   * **選択の中身は送らない**(2026-08-20)。verbが食う面・辺・領域はサーバ側が
   * コアの確定選択から取るので、`onRun` / `onPreview` へ渡るのは数値・選択肢・パスだけ。
   * ここで選択を読むのは、何を待っているかを出すことと、確定を許すかを決めることに使う。
   *
   * ## AIが割り込んでいる間は確定させない
   *
   * 実装計画書§5「競合」: 人が引数を集めている最中にAIが選択を書き換えることがある。
   * 対象集合を送っていた頃は「最後に見た確定選択」を凍結して使っていたが、
   * **対象を決めるのがサーバになった以上、ブラウザが凍結しても意味が無い**
   * (送っても無視される)。代わりに `needsCommit` で確定を止め、承認するか
   * クリックで直すことを促す —— 人がクリックすれば `modify_selection` を通って
   * `origin` は `"user"` に戻るので、そのまま続けられる。
   *
   * ## プレビューは投機的実行
   *
   * 引数が揃ったら `preview` を投げる(サーバがverbを試して形だけ返し、文書は元へ戻す)。
   * **打鍵ごとには投げない** —— 押し出しは実測で数百ms〜1秒かかるので、打っている間ずっと
   * 投げると入力が重くなる(4手の速さを殺さない、が上位の制約)。打ち終わり
   * (`PREVIEW_DEBOUNCE_MS`)を待ってから1回だけ投げる。
   *
   * **Enterはプレビューを待たない。** プレビューは確認のための表示であって、確定の前提
   * ではない(§5「AIの応答待ちでUIがブロックされないこと」)。
   */
  import {
    DEFAULT_COMMAND_ID,
    COMMANDS,
    argSatisfied,
    findCommand,
    firstMissingArg,
    itemsForArg,
    needsEnter,
    overflowNote,
    promptFor,
    refKey,
    type ArgValues,
    type ChoiceArgSpec,
    type CommandDef,
    type NumberArgSpec,
    type Point2,
    type SelectableKind,
    type TextArgSpec,
  } from './command'
  import type { SelectionState } from '../session'

  let {
    selection,
    busy,
    selfSelections = 0,
    commands = COMMANDS,
    defaultCommandId = DEFAULT_COMMAND_ID,
    hover = null,
    entities = [],
    onRun,
    onPreview,
    onCancelPreview,
    onNote,
    onWants = () => {},
    onWantsPlane = () => {},
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
    defaultCommandId?: string
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
  } = $props()

  /** 打ち終わってからプレビューを投げるまでの待ち[ms]。 */
  const PREVIEW_DEBOUNCE_MS = 250

  // 初期値を1回だけ捕まえるのが正しい —— モードが変わるときは呼び出し側(`App.svelte`)が
  // `{#key}` でこのコンポーネントごと作り直す(集めかけの引数を持ち越さないため)。
  // svelte-ignore state_referenced_locally
  let commandId = $state(defaultCommandId)
  const command = $derived<CommandDef>(findCommand(commandId, commands))

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

  /** 集まった引数(選択は導出・数値は打った文字列から・点はクリックから)。 */
  const values = $derived.by<ArgValues>(() => {
    const collected: ArgValues = {}
    for (const spec of command.args) {
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
      } else {
        const raw = (numberText[spec.name] ?? '').trim()
        const parsed = raw === '' ? Number.NaN : Number.parseFloat(raw)
        if (Number.isFinite(parsed)) collected[spec.name] = parsed
      }
    }
    return collected
  })

  const missing = $derived(firstMissingArg(command, values))

  /** 選ばれすぎ(`max` 超え)の理由。空でない間は確定させない。 */
  const overflow = $derived(overflowNote(command, selection))

  /**
   * AIの提案が載っている間は確定できない。**verbが食うのはコアの確定選択**なので、
   * 提案のまま送るとサーバが `SelectionNotCommitted` で断る —— 往復して同じことを
   * 言われるより、承認が要ることをここで出す(選択パネルの「確定」か、クリックで直す)。
   * 選択を使わないコマンド(ファイル・反復)は関係ないので、選択引数があるときだけ見る。
   */
  const needsCommit = $derived(
    selection?.origin === 'proposed' && command.args.some((spec) => spec.kind === 'selection'),
  )

  const ready = $derived(missing === null && overflow === '' && !needsCommit)
  const built = $derived(ready ? command.build(values) : null)

  /**
   * 幽霊に使う引数。**待っている点をポインタの位置で仮に埋める**ので、線分なら
   * 2点目を置く前から線が見えている(ゴムひも)。確定に使うのは `built` のほうで、
   * ここは表示専用。
   */
  const ghostBuilt = $derived.by(() => {
    if (built?.ok) return built
    if (missing?.kind !== 'point' || hover === null) return null
    const provisional = { ...values, [missing.name]: hover }
    if (firstMissingArg(command, provisional) !== null) return null
    const result = command.build(provisional)
    return result.ok ? result : null
  })

  /** 画面に出す1行(何を待っているか)。 */
  const prompt = $derived.by(() => {
    if (selection?.origin === 'proposed') {
      return 'AIの提案を承認するか、クリックして直してください(下の選択パネル)'
    }
    if (missing !== null) return promptFor(missing, values)
    if (overflow !== '') return overflow
    if (built && !built.ok) return built.reason
    return 'Enterで確定 / Escで中断'
  })

  const numberArgs = $derived(command.args.filter((spec) => spec.kind === 'number') as NumberArgSpec[])
  const choiceArgs = $derived(command.args.filter((spec) => spec.kind === 'choice') as ChoiceArgSpec[])
  const textArgs = $derived(command.args.filter((spec) => spec.kind === 'text') as TextArgSpec[])

  /** このコマンドが選択から取る種別をアプリ層へ知らせる(辺のときだけ辺を取りに行く)。 */
  $effect(() => {
    onWants(
      command.args
        .filter((spec) => spec.kind === 'selection')
        .map((spec) => (spec as { shapeKind: SelectableKind }).shapeKind),
    )
  })

  /** 基準平面を要求しているか(辺と同じ規律で、起動している間だけ指せるようにする)。 */
  $effect(() => {
    onWantsPlane(command.args.some((spec) => spec.kind === 'plane'))
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
    command.args.some((spec) => spec.kind === 'selection')
      ? (selection?.items ?? []).map(refKey).join('|')
      : '',
  )

  $effect(() => {
    const source = command.preview === 'none' ? null : ghostBuilt
    const key = source ? JSON.stringify([command.id, source.args, selectionPrint]) : null
    if (key === previewedKey) return
    if (previewTimer) clearTimeout(previewTimer)
    previewedKey = key
    if (key === null || source === null) {
      onCancelPreview()
      return
    }
    const mode = command.preview as 'server' | 'local'
    const args = source.args
    // **ブラウザだけで描ける幽霊は待たない**。デバウンスは「投機実行が重い」ことへの
    // 対処(押し出しは実測で数百ms〜1秒)であって、線を1本引くのに待つ理由は無い。
    if (mode === 'local') {
      onPreview(command.id, args, mode)
      return
    }
    previewTimer = setTimeout(() => {
      previewTimer = null
      onPreview(command.id, args, mode)
    }, PREVIEW_DEBOUNCE_MS)
  })

  // ---------------------------------------------------------------- 即適用(Enterが要らないもの)
  /**
   * 即適用のうち、**選択が揃うのを待つ**もの(`make_editable_copy` と、
   * 面に貼るときの `sketch_create`)。点・基準平面のように**その場で決まる**ものは
   * `addPoint` / `setBasePlane` の中で同期に判定するので、ここには来ない。
   *
   * 平面引数を含める理由: 面をクリックしてスケッチを貼る経路は、`plane` の値こそ
   * その場で `'face'` に決まるが、**サーバが読むのはコアの確定選択**なので、
   * 選択がサーバに届いて配信されるまで送ってはならない。待ち方は面の複製と同じでよい。
   */
  const appliesOnSelection = $derived(
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
   * その場で判定できない。**選択の中身が変わったこと**では引き金にならない ——
   * それだとAIの `modify_selection` でも走り、人が何も指していないのに文書が変わる
   * (2026-08-22のQA指摘)。数えているのは `App.svelte` が「自分が出した」と確かめた
   * 選択変更だけである。
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
    if (!ready || built === null) {
      onNote(prompt)
      return
    }
    if (!built.ok) {
      onNote(built.reason)
      return
    }
    lastInputs = { numbers: { ...numberText }, points: { ...points }, choices: { ...choices } }
    onRun(command.id, built.args)
    reset()
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
  } | null = null

  export function restoreLastInputs(): void {
    if (lastInputs === null) return
    numberText = { ...lastInputs.numbers }
    points = { ...lastInputs.points }
    choices = { ...lastInputs.choices }
    focusFirstNumber()
  }

  /**
   * 中断。**文書は一切変わらない** —— プレビューは投機的実行なのでサーバ側に何も
   * 残っておらず、ここで消すのは画面の幽霊と打ちかけの数値だけ。
   *
   * **選択は消さない**。選ぶことは文書の変更ではなく、選び直しのたびに
   * 選択が飛ぶと「Escで打ち直す」ができなくなる(選択を空にしたいときは
   * 選択パネルの「全解除」)。
   */
  function abort(): void {
    const wasArmed =
      Object.values(numberText).some((text) => text !== '') ||
      Object.keys(points).length > 0 ||
      Object.keys(choices).length > 0 ||
      Object.keys(planes).length > 0 ||
      ready
    reset()
    if (commandId !== defaultCommandId) commandId = defaultCommandId
    if (wasArmed) onNote('中断した(文書は変わっていない)')
  }

  function reset(): void {
    numberText = {}
    points = {}
    choices = {}
    planes = {}
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
    if (ready) {
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
    focusFirstNumber()
  }

  /**
   * スケッチ平面のクリックを、待っている点引数へ入れる。
   *
   * 待っている点が無ければ**何もしない**(黙って最後の点を上書きすると、
   * 引数が揃った後のクリックで図形が動いて見える)。
   */
  export function addPoint(u: number, v: number): boolean {
    const spec = firstMissingArg(command, values)
    if (spec === null || spec.kind !== 'point') return false
    points = { ...points, [spec.name]: [u, v] }
    // **この1クリックで引数が揃い、Enterが要らないなら、そのまま適用する**(線分の2点目)。
    // `values` は `$derived` なので、上の代入の直後に読めば新しい点が入っている。
    // 確定してもコマンドは構えたまま(`reset()` が消すのは集めた点だけ)なので、
    // 続けて次の線を引ける —— これは `commands/sketch.ts` の設計で、崩さない。
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
    const spec = firstMissingArg(command, values)
    if (spec === null || spec.kind !== 'plane') return false
    planes = { ...planes, [spec.name]: name }
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
    const spec = firstMissingArg(command, values)
    if (spec === null || spec.kind !== 'plane') return false
    planes = { ...planes, [spec.name]: 'face' }
    return true
  }

  function focusFirstNumber(): void {
    // 収集の先頭が打ち込む引数(数値・文字列)なら、そこへ入れる
    // (選択待ち・点待ちのときは3Dを触るのでフォーカスを奪わない)
    queueMicrotask(() => {
      if (missing?.kind !== 'number' && missing?.kind !== 'text') return
      document.getElementById(missing.inputId)?.focus()
    })
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
    if (event.key !== 'Enter' || !ready) return
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
    if (event.key !== 'Tab' || event.shiftKey) return
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

<section class="command-host" data-command={command.id} data-phase={ready ? 'ready' : 'collecting'}>
  <div class="row">
    <span class="label">コマンド</span>
    {#each commands as candidate (candidate.id)}
      <button
        id={candidate.buttonId}
        class:on={candidate.id === command.id}
        onclick={() => (candidate.id === command.id ? pressActive() : start(candidate.id))}
        disabled={candidate.id === command.id && (busy || (!ready && needsEnter(command)))}
      >
        {candidate.label}{candidate.id === command.id && needsEnter(candidate) ? ' (Enter)' : ''}
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

  <div class="row">
    {#each numberArgs as spec (spec.name)}
      <label class="label" for={spec.inputId}>{spec.label} {spec.unit}</label>
      <input
        id={spec.inputId}
        type="text"
        inputmode="decimal"
        autocomplete="off"
        placeholder={spec.placeholder}
        value={numberText[spec.name] ?? ''}
        oninput={(event) => {
          numberText = { ...numberText, [spec.name]: (event.target as HTMLInputElement).value }
        }}
        onkeydown={(event) => onNumberKeydown(event, spec)}
      />
    {/each}
    <button id="command-abort" onclick={abort}>中断 (Esc)</button>
  </div>
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
  }

  .label {
    flex: 0 0 auto;
    color: var(--dim);
    font-size: 12px;
  }

  .value {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .dim {
    color: var(--dim);
  }

  /* ツールバーの「押しているコマンド」を1つだけ強調する(方向性Bのリボンで
     アクティブなツールを塗り分けていたのに合わせた)。 */
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
