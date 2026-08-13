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
    type CommandDef,
    type NumberArgSpec,
  } from './command'
  import type { SelectionState } from '../session'

  let {
    selection,
    busy,
    onRun,
    onPreview,
    onCancelPreview,
    onNote,
  }: {
    /** コアの選択集合(state.selection)。 */
    selection: SelectionState | null
    /** 直前の操作の応答待ち。待っている間は確定を受け付けない。 */
    busy: boolean
    onRun: (verb: string, args: Record<string, unknown>) => void
    onPreview: (verb: string, args: Record<string, unknown>) => void
    onCancelPreview: () => void
    onNote: (message: string) => void
  } = $props()

  /** 打ち終わってからプレビューを投げるまでの待ち[ms]。 */
  const PREVIEW_DEBOUNCE_MS = 250

  let commandId = $state(DEFAULT_COMMAND_ID)
  const command = $derived<CommandDef>(findCommand(commandId))

  /** 数値引数の生の入力(引数名 -> 打った文字列)。確定・中断のたびに空へ戻す。 */
  let numberText = $state<Record<string, string>>({})

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

  /** 集まった引数(選択は導出・数値は打った文字列から)。 */
  const values = $derived.by<ArgValues>(() => {
    const collected: ArgValues = {}
    for (const spec of command.args) {
      if (spec.kind === 'selection') {
        collected[spec.name] = itemsForArg(spec, argSelection)
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

  // ---------------------------------------------------------------- プレビュー
  let previewTimer: ReturnType<typeof setTimeout> | null = null
  /** 直前に投げたプレビューの引数(同じものを投げ直さないため)。 */
  let previewedKey: string | null = null

  $effect(() => {
    const key = built && built.ok && command.previewable ? JSON.stringify([command.id, built.args]) : null
    if (key === previewedKey) return
    if (previewTimer) clearTimeout(previewTimer)
    previewedKey = key
    if (key === null) {
      onCancelPreview()
      return
    }
    const args = (built as { ok: true; args: Record<string, unknown> }).args
    previewTimer = setTimeout(() => {
      previewTimer = null
      onPreview(command.id, args)
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
    onRun(command.id, built.args)
    reset()
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
    const wasArmed = Object.values(numberText).some((text) => text !== '') || ready
    reset()
    if (commandId !== DEFAULT_COMMAND_ID) commandId = DEFAULT_COMMAND_ID
    if (wasArmed) onNote('中断した(文書は変わっていない)')
  }

  function reset(): void {
    numberText = {}
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

  function focusFirstNumber(): void {
    // 収集の先頭が数値引数なら、そこへ入れる(選択待ちのときは3Dを触るので奪わない)
    queueMicrotask(() => {
      if (missing?.kind !== 'number') return
      document.getElementById(missing.inputId)?.focus()
    })
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Escape') return
    event.preventDefault()
    abort()
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
    {#each COMMANDS as candidate (candidate.id)}
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
