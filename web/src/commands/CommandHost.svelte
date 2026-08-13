<script lang="ts">
  /**
   * コマンドの状態機械 — **verbごとに作り直さないための1つだけの漏斗**(タスクB・2026-08-13)。
   *
   * ```
   * 起動 → 引数収集 → プレビュー → 確定(Enter) / 中断(Esc)
   * ```
   *
   * ## 引数収集は、まず共有選択集合を見る
   *
   * 選択引数の値は**コアの選択集合から導出する**(ここには持たない)。空のときだけ
   * 「面を1つ選ぶ」と問う。AIが提案した集合を人が承認すれば、そのまま引数になる
   * —— これがAI→人間の受け渡しの実体である(実装計画書§8b B)。
   *
   * ## AIが割り込んだときだけスナップショットを使う
   *
   * 実装計画書§5「競合」: **人がコマンドの引数収集中にAIが選択を書き換えた場合は、
   * コマンド開始時の選択をスナップショットして使う**。
   *
   * ここでの実装は「`origin === "user"` の選択だけを引数に採り、`"proposed"` が
   * 届いている間は最後に見た確定選択を使い続ける」である。人が自分でクリックして
   * 直す操作は `modify_selection` を通るので必ず `"user"` になり、**人の編集には
   * 追従したまま、AIの割り込みからだけ守られる**。凍結してしまうと、面を選び直す
   * (M1からある操作)が効かなくなる。
   *
   * ## プレビューは投機的実行
   *
   * 引数が揃ったら `preview` を投げる(サーバがverbを試して形だけ返し、文書は元へ戻す)。
   * **打鍵ごとには投げない** —— 押し出しは実測で数百ms〜1秒かかるので、打っている間ずっと
   * 投げると入力が重くなる(M1の4手の速さを殺さない、が上位の制約)。打ち終わり
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
    overflowNote,
    promptFor,
    type ArgValues,
    type ChoiceArgSpec,
    type CommandDef,
    type NumberArgSpec,
    type Point2,
    type SelectableKind,
  } from './command'
  import type { SelectionState } from '../session'

  let {
    selection,
    busy,
    commands = COMMANDS,
    defaultCommandId = DEFAULT_COMMAND_ID,
    hover = null,
    entities = [],
    onRun,
    onPreview,
    onCancelPreview,
    onNote,
    onWants = () => {},
  }: {
    /** コアの選択集合(state.selection)。 */
    selection: SelectionState | null
    /** 直前の操作の応答待ち。待っている間は確定を受け付けない。 */
    busy: boolean
    /**
     * このモードで使えるコマンド(C・2026-08-13)。既定はソリッドを触るぶん。
     * スケッチモードでは `commands/sketch.ts` の一覧が渡る —— **状態機械は同じ1つ**で、
     * 変わるのは一覧と既定だけ(モードごとにUIを作り直さない)。
     */
    commands?: CommandDef[]
    defaultCommandId?: string
    /**
     * スケッチ平面上でポインタが今指している点(C)。**幽霊にだけ使う**。
     * これが無いと、線分は2点目を置くまで何も見えない。
     */
    hover?: Point2 | null
    /**
     * スケッチパネルで選ばれている図形のID(D)。**反復のverbだけが取る引数**で、
     * 共有選択集合には入らない(`command.ts` の `EntitiesArgSpec` 参照)。
     */
    entities?: string[]
    onRun: (verb: string, args: Record<string, unknown>) => void
    /** `mode` は `CommandDef.preview`。`local` はブラウザだけで描く(サーバへ投げない)。 */
    onPreview: (verb: string, args: Record<string, unknown>, mode: 'server' | 'local') => void
    onCancelPreview: () => void
    onNote: (message: string) => void
    /**
     * 起動中のコマンドが選択から取る種別(D)。**辺を要求したときだけ**アプリ層が
     * 辺の折れ線を取りに行き、ビューポートで指せるようにする —— 常時出すと
     * 27部品ぶんの辺が要り、面のピックも辺に取られて押し出しが遅くなる。
     */
    onWants?: (kinds: SelectableKind[]) => void
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
   * 最後に見た**確定済み**の選択(`origin === "user"`)。AIが提案を書き込んでいる間、
   * 引数はこちらを使う(上のdocstring「競合」)。
   */
  let lastUserSelection = $state<SelectionState | null>(null)
  $effect(() => {
    if (selection && selection.origin === 'user') lastUserSelection = selection
  })

  /** 引数に使う選択。提案が載っている間は最後の確定選択。 */
  const argSelection = $derived(
    selection === null || selection.origin === 'user' ? selection : lastUserSelection,
  )

  /** 集まった引数(選択は導出・数値は打った文字列から・点はクリックから)。 */
  const values = $derived.by<ArgValues>(() => {
    const collected: ArgValues = {}
    for (const spec of command.args) {
      if (spec.kind === 'selection') {
        collected[spec.name] = itemsForArg(spec, argSelection)
      } else if (spec.kind === 'point') {
        const point = points[spec.name]
        if (point) collected[spec.name] = point
      } else if (spec.kind === 'choice') {
        const chosen = choices[spec.name]
        if (chosen) collected[spec.name] = chosen
      } else if (spec.kind === 'entities') {
        // 選択集合と同じく**導出する**(ここには持たない)。スケッチパネルで選び直せば追従する。
        collected[spec.name] = entities
      } else {
        const raw = (numberText[spec.name] ?? '').trim()
        const parsed = raw === '' ? Number.NaN : Number.parseFloat(raw)
        if (Number.isFinite(parsed)) collected[spec.name] = parsed
      }
    }
    return collected
  })

  const missing = $derived(firstMissingArg(command, values))
  const ready = $derived(missing === null)
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
    if (built && !built.ok) return built.reason
    const overflow = overflowNote(command, argSelection)
    return overflow === '' ? 'Enterで確定 / Escで中断' : `${overflow} — Enterで確定 / Escで中断`
  })

  const numberArgs = $derived(command.args.filter((spec) => spec.kind === 'number') as NumberArgSpec[])
  const choiceArgs = $derived(command.args.filter((spec) => spec.kind === 'choice') as ChoiceArgSpec[])

  /** このコマンドが選択から取る種別をアプリ層へ知らせる(D。辺のときだけ辺を取りに行く)。 */
  $effect(() => {
    onWants(
      command.args
        .filter((spec) => spec.kind === 'selection')
        .map((spec) => (spec as { shapeKind: SelectableKind }).shapeKind),
    )
  })

  // ---------------------------------------------------------------- プレビュー
  let previewTimer: ReturnType<typeof setTimeout> | null = null
  /** 直前に投げたプレビューの引数(同じものを投げ直さないため)。 */
  let previewedKey: string | null = null

  $effect(() => {
    const source = command.preview === 'none' ? null : ghostBuilt
    const key = source ? JSON.stringify([command.id, source.args]) : null
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
   * 直前に確定したときの入力(**失敗したときだけ**戻す。D・2026-08-14)。
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
      ready
    reset()
    if (commandId !== defaultCommandId) commandId = defaultCommandId
    if (wasArmed) onNote('中断した(文書は変わっていない)')
  }

  function reset(): void {
    numberText = {}
    points = {}
    choices = {}
    if (previewTimer) clearTimeout(previewTimer)
    previewTimer = null
    previewedKey = null
    onCancelPreview()
  }

  export function start(nextId: string): void {
    reset()
    commandId = nextId
    focusFirstNumber()
  }

  /**
   * スケッチ平面のクリックを、待っている点引数へ入れる(C)。
   *
   * 待っている点が無ければ**何もしない**(黙って最後の点を上書きすると、
   * 引数が揃った後のクリックで図形が動いて見える)。
   */
  export function addPoint(u: number, v: number): boolean {
    const spec = firstMissingArg(command, values)
    if (spec === null || spec.kind !== 'point') return false
    points = { ...points, [spec.name]: [u, v] }
    focusFirstNumber()
    return true
  }

  /** スケッチモードで待っている点があるか(App.svelte がクリックの行き先を決める)。 */
  export function wantsPoint(): boolean {
    return missing?.kind === 'point'
  }

  function focusFirstNumber(): void {
    // 収集の先頭が数値引数なら、そこへ入れる(選択待ちのときは3Dを触るので奪わない)
    queueMicrotask(() => {
      if (missing?.kind !== 'number') return
      document.getElementById(missing.inputId)?.focus()
    })
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.preventDefault()
      abort()
      return
    }
    // **点だけで引数が揃うコマンド(線分など)はEnterの受け皿が無い**(C)。数値欄に
    // フォーカスが無いときのEnterをここで拾う —— 欄にフォーカスがあるときは
    // `onNumberKeydown` が拾うので二重に確定しない。
    if (event.key !== 'Enter' || !ready) return
    const target = event.target as HTMLElement | null
    // ボタンも除く —— フォーカスの残ったボタン(undo等)のEnterを奪うと、押したつもりの
    // ボタンではなくコマンドが確定する(QA 2026-08-13)。
    if (target && ['INPUT', 'TEXTAREA', 'BUTTON'].includes(target.tagName)) return
    event.preventDefault()
    commit()
  }

  /** 数値欄でのEnter(確定)とTab(次の引数へ)。 */
  function onNumberKeydown(event: KeyboardEvent, spec: NumberArgSpec): void {
    if (event.key === 'Enter') {
      event.preventDefault()
      commit()
      return
    }
    if (event.key !== 'Tab' || event.shiftKey) return
    const remaining = command.args
      .slice(command.args.indexOf(spec) + 1)
      .find((candidate) => candidate.kind === 'number' && !argSatisfied(candidate, values))
    if (!remaining || remaining.kind !== 'number') return
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
        onclick={() => (candidate.id === command.id ? commit() : start(candidate.id))}
        disabled={candidate.id === command.id && (!ready || busy)}
      >
        {candidate.label}{candidate.id === command.id ? ' (Enter)' : ''}
      </button>
    {/each}
    <span id="command-prompt" class="value" class:dim={!ready}>{prompt}</span>
  </div>

  {#if choiceArgs.length}
    <!-- 選択肢(D)。**既定を選んでおかない** — 押されるまで引数は揃わない -->
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

  button.on {
    border-color: var(--accent, #ffb454);
  }
</style>
