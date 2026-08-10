<script lang="ts">
  /**
   * 「面を選ぶ → 距離を打つ → Enter」の状態機械と、その4段階の時刻の記録。
   *
   * 文書・操作ログ・undo履歴はコア側にある。**ただしM1では「選択」だけがここにある** —
   * 実装計画書§2/§3.5は選択集合もコア側(永続・双方向)と定めているが、それが必要になるのは
   * AIが選択を提案しイベントが上ってくるM4であり、M1のタスクはMCPを範囲外と明記している。
   * **M1の選択の正はこのファイルの `selected`** で、コアへ移すのはM4の債務(引継ぎに記載)。
   * それ以外にアプリ層が持つのはコマンドの途中状態だけ(§4.1「状態機械1個」)。
   *
   * 時刻の定義は MEASURE.md が正。ここで測るのは単調時計 `performance.now()` の値で、
   * 区間のms換算はサーバ側で一括して行う(定義が2箇所に分かれないようにするため)。
   */
  import { onMount } from 'svelte'
  import { createSession, type Applied, type ConnectionStatus, type ServerState } from './session'
  import { createViewport, type Viewport } from './viewport'

  let container: HTMLDivElement
  let viewport: Viewport | null = null

  let status = $state<ConnectionStatus>('connecting')
  let model = $state<ServerState | null>(null)
  let selected = $state<number | null>(null)
  // 既定値を入れない。値が入ったままだと「打たずにEnter」ができてしまい、
  // 打つ時間が0の行が「速い」として記録される(2026-08-10のQA指摘)。確定のたびに空へ戻す。
  let distanceText = $state('')
  let messages = $state<string[]>([])
  let misses = $state(0)
  let lastRecord = $state<Record<string, unknown> | null>(null)

  /** 送信中のコマンド(応答が返るまでの間だけ生きる)。 */
  let pending: {
    op: number
    faceIndex: number
    distance: number
    misses: number
    reused: boolean
    command: number
    picked: number
    value: number
    committed: number
  } | null = null

  let opCounter = 0
  let tCommandStart: number | null = null
  let tFacePicked: number | null = null
  let tValueEntered: number | null = null
  let reusedSelection = false

  const session = createSession({
    onState: handleState,
    onError: (message, where) => {
      note(`エラー[${where}] ${message}`)
      // 失敗した操作の計測は捨てる(壊れた行を貯めない)。**計測の状態は触らない** —
      // 応答待ちの間に始めた次の操作の時刻を消してしまうため(2026-08-10の再QA指摘)。
      pending = null
    },
    onWarning: (message) => note(`警告 ${message}`),
    onStatus: (next) => {
      status = next
    },
  })

  function note(message: string): void {
    messages = [message, ...messages].slice(0, 4)
  }

  function resetTimings(): void {
    tCommandStart = null
    tFacePicked = null
    tValueEntered = null
    reusedSelection = false
  }

  function handleState(next: ServerState, applied: Applied | null): void {
    model = next
    viewport?.load(next.geometry)

    if (applied?.verb === 'extrude_face' && typeof applied.after_face_index === 'number') {
      // 押し出した面の後継を選び直す(「同じ面を10回押す」を再ピックなしで回せるようにする)
      selected = applied.after_face_index
    } else if (applied?.verb === 'undo') {
      // 取り消したら押し出す**前**の添字へ。添字は形状変更で壊れるので持ち越さない
      selected = typeof applied.face_index === 'number' ? applied.face_index : null
    } else if (applied?.verb === 'redo') {
      selected = typeof applied.after_face_index === 'number' ? applied.after_face_index : null
    }
    if (selected !== null && selected > next.face_count) selected = null
    viewport?.highlight(selected === null ? [] : [selected])

    if (pending && applied && applied.client_op === pending.op) {
      const finished = pending
      pending = null
      const record: Record<string, unknown> = {
        kind: 'extrude',
        op: finished.op,
        face_id: `faces_${finished.faceIndex}`,
        face_index: finished.faceIndex,
        distance: finished.distance,
        pick_misses: finished.misses,
        reused_selection: finished.reused,
        seq: applied.seq,
        after_face_index: applied.after_face_index ?? null,
        face_count_after: next.face_count,
        volume_after: next.volume,
        clock: 'performance.now',
        time_origin_epoch_ms: Math.round(performance.timeOrigin),
        t_command_start: finished.command,
        t_face_picked: finished.picked,
        t_value_entered: finished.value,
        t_committed: finished.committed,
      }
      // 「形が画面に出た時刻」は**描画された次のフレーム**で測る(2重のrAF)。
      // rAF 1回だと描画前の時刻になるので、最大1フレーム(60Hzで約16.7ms)ぶん早く出る。
      // ここでは時刻の代入と送信だけを行う — 計測の状態(時刻・外した回数)のリセットは
      // commit() が同期でやる。ここでリセットすると、応答待ちの間に始めた次の操作の
      // 時刻を消してしまう(2026-08-10のQA指摘)。
      requestAnimationFrame(() =>
        requestAnimationFrame(() => {
          record.t_geometry_updated = performance.now()
          session.record(record)
          lastRecord = record
        }),
      )
    }
  }

  function commit(): void {
    const distance = Number.parseFloat(distanceText)
    if (!Number.isFinite(distance) || distance === 0) {
      note('距離が数値でない(0も不可)')
      return
    }
    if (selected === null) {
      note('面が選ばれていない')
      return
    }
    if (pending) {
      note('直前の操作の応答待ち')
      return
    }
    const committed = performance.now()
    if (tCommandStart === null) {
      tCommandStart = committed
      reusedSelection = true
    }
    if (tFacePicked === null) tFacePicked = tCommandStart
    if (tValueEntered === null) tValueEntered = committed
    opCounter += 1
    pending = {
      op: opCounter,
      faceIndex: selected,
      distance,
      misses,
      reused: reusedSelection,
      command: tCommandStart,
      picked: tFacePicked,
      value: tValueEntered,
      committed,
    }
    session.extrude(selected, distance, opCounter)
    // この操作ぶんの計測値は pending が持ったので、ここから先は次の操作の計測を始める。
    // 応答待ちの間に打った時刻が消えないよう、リセットは**同期のここだけ**で行う
    // (rAF側でリセットする実装に戻すと `scripts/smoke.mjs` の2回目の検査が落ちる。実測で確認済み)。
    resetTimings()
    misses = 0
    distanceText = ''
  }

  function onDistanceInput(): void {
    const now = performance.now()
    if (tCommandStart === null) {
      // 選択を再利用して数値から始めた操作。この場合は「選ぶ」に時間0がかかったと記録し、
      // 行に reused_selection=true を立てる(ms_to_pick=0 の理由が後から分かるように)。
      tCommandStart = now
      tFacePicked = now
      reusedSelection = true
    }
    tValueEntered = now
  }

  function onDistanceKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter') {
      event.preventDefault()
      commit()
    }
  }

  function onWindowKeydown(event: KeyboardEvent): void {
    if (!(event.ctrlKey || event.metaKey)) return
    const key = event.key.toLowerCase()
    if (key === 'z' && !event.shiftKey) {
      event.preventDefault()
      session.undo()
      resetTimings()
    } else if (key === 'y' || (key === 'z' && event.shiftKey)) {
      event.preventDefault()
      session.redo()
      resetTimings()
    }
  }

  onMount(() => {
    viewport = createViewport(container)
    viewport.onPick((event) => {
      if (event.faceIndex === null) {
        misses += 1
        session.record({
          kind: 'pick_miss',
          t_pointer_down: event.tPointerDown,
          t_resolved: event.tResolved,
          clock: 'performance.now',
        })
        note('面を外した(狙い外れとして記録)')
        return
      }
      if (tCommandStart === null) {
        tCommandStart = event.tPointerDown
      }
      // 実際にクリックで選んだのだから、数値から始めていても再利用ではない
      reusedSelection = false
      tFacePicked = event.tResolved
      selected = event.faceIndex
      viewport?.highlight([event.faceIndex])
    })
    session.start()
    return () => {
      session.dispose()
      viewport?.dispose()
      viewport = null
    }
  })

  const selectedFace = $derived(
    selected === null || !model
      ? null
      : (model.geometry.faces.find((face) => face.index === selected) ?? null),
  )

  // 自動テスト(scripts/smoke.mjs)がブラウザの中を見るための穴。ローカル専用ツールなので
  // 常に生やしてある(「動くはず」で納品しないための道具であり、隠すと使えない)。
  $effect(() => {
    ;(window as unknown as Record<string, unknown>).__duocad = {
      ready: status === 'open' && model !== null,
      status,
      state: model,
      selected,
      misses,
      lastRecord,
      messages,
      projectFace: (index: number) => viewport?.project(index) ?? null,
    }
  })
</script>

<svelte:window onkeydown={onWindowKeydown} />

<div class="app">
  <header>
    <strong>duocad M1</strong>
    <span class="tag" class:on={status === 'open'}>{status}</span>
    {#if model}
      <span>{model.body_name}</span>
      <span>面 {model.face_count}</span>
      <span>体積 {model.volume.toFixed(3)} mm³</span>
      <span>操作 {model.applied_count}</span>
    {/if}
  </header>

  <!-- canvas はこの div の中で three.js が作る。Svelte はここの中身を触らない -->
  <div class="viewport" bind:this={container}></div>

  <section class="panel">
    <div class="row">
      <span class="label">選択</span>
      {#if selectedFace}
        <span class="value">
          面 {selectedFace.index} / {selectedFace.kind} / 面積 {selectedFace.area.toFixed(2)} mm²
          / 法線 [{selectedFace.normal.map((n) => n.toFixed(2)).join(', ')}]
        </span>
      {:else}
        <span class="value dim">面をクリックして選ぶ</span>
      {/if}
    </div>

    <div class="row">
      <label class="label" for="distance">距離 mm</label>
      <input
        id="distance"
        type="text"
        inputmode="decimal"
        autocomplete="off"
        placeholder="mm"
        bind:value={distanceText}
        oninput={onDistanceInput}
        onkeydown={onDistanceKeydown}
      />
      <button id="extrude" onclick={commit} disabled={selected === null}>押し出す (Enter)</button>
      <button id="undo" onclick={() => { session.undo(); resetTimings() }} disabled={!model?.can_undo}>
        undo
      </button>
      <button id="redo" onclick={() => { session.redo(); resetTimings() }} disabled={!model?.can_redo}>
        redo
      </button>
      <span class="value dim">正=足す / 負=削る ・ 外した回数 {misses}</span>
    </div>

    <div class="row">
      <span class="label">直前</span>
      {#if lastRecord}
        <span class="value">
          op {lastRecord.op} ・ 面 {lastRecord.face_index} ・ {lastRecord.distance} mm
          {#if typeof lastRecord.t_geometry_updated === 'number' && typeof lastRecord.t_command_start === 'number'}
            ・ 総 {(
              (lastRecord.t_geometry_updated as number) - (lastRecord.t_command_start as number)
            ).toFixed(1)} ms
          {/if}
        </span>
      {:else}
        <span class="value dim">まだ計測なし</span>
      {/if}
    </div>

    {#if model}
      <div class="row">
        <span class="label">計測ログ</span><span class="value dim">{model.measure_log}</span>
      </div>
    {/if}

    {#if messages.length}
      <div class="row">
        <span class="label">通知</span>
        <span class="value">{messages.join(' / ')}</span>
      </div>
    {/if}
  </section>
</div>
