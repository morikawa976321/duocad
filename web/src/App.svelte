<script lang="ts">
  /**
   * 「面を選ぶ → 距離を打つ → Enter」の状態機械と、その4段階の時刻の記録。
   *
   * 文書・操作ログ・undo履歴・**可視性**はコア側にある。**ただし面の選択(どの面を狙っているか)
   * だけがここにある** — 実装計画書§2/§3.5は選択集合もコア側(永続・双方向)と定めているが、
   * それが必要になるのはAIが選択を提案しイベントが上ってくるM4であり、M1のタスクはMCPを
   * 範囲外と明記している。**選択の正はこのファイルの `selected`**、移すのはM4の債務(引継ぎに記載)。
   *
   * **M3.5(2026-08-11)でパーツ一覧UIを追加した。** 可視性の切り替えは
   * `PartsPanel.svelte` からここを経由してコアの `set_visible` verb を呼ぶ
   * (`session.setVisible`)。ここは可視性の値を覚えない — 毎回コアが返す `state.bodies`
   * を描き直すだけ(§2「コアはUIが無くても機能的に完全」)。
   *
   * 時刻の定義は MEASURE.md が正。ここで測るのは単調時計 `performance.now()` の値で、
   * 区間のms換算はサーバ側で一括して行う(定義が2箇所に分かれないようにするため)。
   */
  import { onMount } from 'svelte'
  import PartsPanel from './PartsPanel.svelte'
  import {
    createSession,
    type Applied,
    type BodyGeometry,
    type ConnectionStatus,
    type ServerState,
  } from './session'
  import { createViewport, type FaceRef, type GeometryPayload, type Viewport } from './viewport'

  let container: HTMLDivElement
  let viewport: Viewport | null = null

  let status = $state<ConnectionStatus>('connecting')
  let model = $state<ServerState | null>(null)
  let selected = $state<FaceRef | null>(null)
  // 既定値を入れない。値が入ったままだと「打たずにEnter」ができてしまい、
  // 打つ時間が0の行が「速い」として記録される(2026-08-10のQA指摘)。確定のたびに空へ戻す。
  let distanceText = $state('')
  let messages = $state<string[]>([])
  let misses = $state(0)
  let lastRecord = $state<Record<string, unknown> | null>(null)
  // 選択中の面の面積・法線などを出すための表示用キャッシュ(body_id -> 直近のgeometry)。
  // ドメインの状態ではない(可視性・選択の正はコア/上の `selected` のまま) — viewport が
  // three.jsの中に既に持っている形状を、UI表示(下のパネル)とテスト用フック向けに
  // 読める形で複製しているだけで、ここが増えても更新されるのはサーバのpushだけ。
  let bodyGeometry = $state<Record<string, GeometryPayload>>({})

  /** 送信中のコマンド(応答が返るまでの間だけ生きる)。 */
  let pending: {
    op: number
    bodyId: string
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

  function handleState(next: ServerState, changed: BodyGeometry[], applied: Applied | null): void {
    model = next

    // 差分更新: 版が変わったBodyだけをビューポートへ渡す。27部品のうち1つを押し出しても
    // 描き直すのはその1つ(サーバ側の差分送信をここで無駄にしない。§2)。
    viewport?.update(changed.map((body) => ({ bodyId: body.body_id, geometry: body.geometry })))
    // 文書から消えたBody(サーバの契約: 知っているのに state.bodies に無いID)があれば
    // ビューポートからも取り除く。M3.5で実際に到達する経路は無い(undoは文書の土台より
    // 前へ戻せない)が、契約に対応するコードを書いておかないと、次にBody削除が起きる
    // verbが増えたときに「画面から消えない」不具合が再発する(2026-08-11のQA指摘)。
    viewport?.prune(next.bodies.map((body) => body.body_id))

    if (changed.length > 0) {
      const nextGeometry = { ...bodyGeometry }
      for (const body of changed) {
        if (body.geometry) nextGeometry[body.body_id] = body.geometry
        else delete nextGeometry[body.body_id]
      }
      bodyGeometry = nextGeometry
    }
    // このキャッシュも、消えたBodyのぶんは掃除しておく(viewport.prune と同じ理由)。
    const validIds = new Set(next.bodies.map((body) => body.body_id))
    for (const bodyId of Object.keys(bodyGeometry)) {
      if (!validIds.has(bodyId)) {
        const pruned = { ...bodyGeometry }
        delete pruned[bodyId]
        bodyGeometry = pruned
      }
    }

    // 選択の再割り当ては「押し出しの結果」に対してだけ行う(可視性の変更では選択を動かさない —
    // 下の可視性チェックが別途、見えなくなったBody/面を選択から外す)。
    if (applied?.verb === 'extrude_face' && typeof applied.after_face_index === 'number' && applied.body_id) {
      // 押し出した面の後継を選び直す(「同じ面を10回押す」を再ピックなしで回せるようにする)
      selected = { bodyId: applied.body_id, faceIndex: applied.after_face_index }
    } else if (
      applied?.verb === 'undo' &&
      applied.undone === 'extrude_face' &&
      typeof applied.face_index === 'number' &&
      applied.body_id
    ) {
      // 取り消したら押し出す**前**の添字へ。添字は形状変更で壊れるので持ち越さない
      selected = { bodyId: applied.body_id, faceIndex: applied.face_index }
    } else if (
      applied?.verb === 'redo' &&
      applied.redone === 'extrude_face' &&
      typeof applied.after_face_index === 'number' &&
      applied.body_id
    ) {
      selected = { bodyId: applied.body_id, faceIndex: applied.after_face_index }
    }

    // **可視性は選択機構の一部**(duocad/selection.py と同じ判断)。選んでいたBody・面が
    // 見えなくなった/消えたなら選択を外す(手前を隠す・undoでBodyが消える等、経路によらず)。
    if (selected !== null) {
      const body = next.bodies.find((entry) => entry.body_id === selected!.bodyId)
      if (!body || !body.visible || selected.faceIndex > body.face_count) selected = null
    }
    viewport?.highlight(selected)

    if (pending && applied && applied.client_op === pending.op) {
      const finished = pending
      pending = null
      const record: Record<string, unknown> = {
        kind: 'extrude',
        op: finished.op,
        body_id: finished.bodyId,
        face_id: `${finished.bodyId}/faces_${finished.faceIndex}`,
        face_index: finished.faceIndex,
        distance: finished.distance,
        pick_misses: finished.misses,
        reused_selection: finished.reused,
        seq: applied.seq,
        after_face_index: applied.after_face_index ?? null,
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
      bodyId: selected.bodyId,
      faceIndex: selected.faceIndex,
      distance,
      misses,
      reused: reusedSelection,
      command: tCommandStart,
      picked: tFacePicked,
      value: tValueEntered,
      committed,
    }
    session.extrude(selected.bodyId, selected.faceIndex, distance, opCounter)
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

  // ---------------------------------------------------------------- 可視性(パーツ一覧から)
  // **コアの `set_visible` verb を呼ぶだけ**(アプリ層に可視性の状態を持たせない)。
  //
  // `model.bodies` には**参照ボディ(`kind="reference"`)も混ざっている**
  // (`Session.load_step` が「編集用コピー1つにつき参照1つ」を作り、参照は常に隠したまま
  // 保持する契約 — `duocad/server.py` の `load_step` docstring参照)。参照は常に不可視で
  // ユーザーが触る対象ではないので、**パーツ一覧・一括操作とも `kind==="solid"` だけを扱う**。
  // ここを `bodies` 全部にすると「全表示」が参照ボディまで可視にしてしまい、1ソリッドにつき
  // 2つの同じ形が重なって描かれる(M3.5ロジックタスクが塞いだのと同じ穴が再発する)。
  const partBodies = $derived((model?.bodies ?? []).filter((body) => body.kind === 'solid'))

  function setVisible(bodyId: string, visible: boolean): void {
    opCounter += 1
    session.setVisible(bodyId, visible, opCounter)
  }

  function showAllParts(): void {
    for (const body of partBodies) if (!body.visible) setVisible(body.body_id, true)
  }

  function hideAllParts(): void {
    for (const body of partBodies) if (body.visible) setVisible(body.body_id, false)
  }

  function showOnlyParts(bodyIds: string[]): void {
    const keep = new Set(bodyIds)
    for (const body of partBodies) {
      const desired = keep.has(body.body_id)
      if (body.visible !== desired) setVisible(body.body_id, desired)
    }
  }

  onMount(() => {
    viewport = createViewport(container)
    viewport.onPick((event) => {
      if (event.hit === null) {
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
      selected = event.hit
      viewport?.highlight(event.hit)
    })
    session.start()
    return () => {
      session.dispose()
      viewport?.dispose()
      viewport = null
    }
  })

  const selectedFace = $derived(
    selected === null ? null : (bodyGeometry[selected.bodyId]?.faces.find((face) => face.index === selected!.faceIndex) ?? null),
  )
  const selectedBody = $derived(
    selected === null ? null : (model?.bodies.find((body) => body.body_id === selected!.bodyId) ?? null),
  )

  // 自動テスト(scripts/smoke.mjs)がブラウザの中を見るための穴。ローカル専用ツールなので
  // 常に生やしてある(「動くはず」で納品しないための道具であり、隠すと使えない)。
  $effect(() => {
    ;(window as unknown as Record<string, unknown>).__duocad = {
      ready: status === 'open' && model !== null,
      status,
      state: model,
      // ユーザーに見せる27部品(参照ボディを除いた kind==="solid" だけ)。
      // パーツ一覧が実際に描いているのと同じ集合(自動テストが `state.bodies` を
      // 直接見て参照ボディを混ぜないための穴)。
      parts: partBodies,
      bodyGeometry,
      selected,
      misses,
      lastRecord,
      messages,
      projectFace: (bodyId: string, faceIndex: number) => viewport?.project(bodyId, faceIndex) ?? null,
    }
  })
</script>

<svelte:window onkeydown={onWindowKeydown} />

<div class="app">
  <header>
    <strong>duocad</strong>
    <span class="tag" class:on={status === 'open'}>{status}</span>
    {#if model}
      <span>部品 {partBodies.length}</span>
      <span>操作 {model.applied_count}</span>
    {/if}
  </header>

  <div class="main">
    <PartsPanel
      bodies={partBodies}
      onToggle={setVisible}
      onShowAll={showAllParts}
      onHideAll={hideAllParts}
      onShowOnly={showOnlyParts}
    />
    <!-- canvas はこの div の中で three.js が作る。Svelte はここの中身を触らない -->
    <div class="viewport" bind:this={container}></div>
  </div>

  <section class="panel">
    <div class="row">
      <span class="label">選択</span>
      {#if selectedFace && selectedBody}
        <span class="value">
          {selectedBody.name} / 面 {selectedFace.index} / {selectedFace.kind} / 面積 {selectedFace.area.toFixed(2)} mm²
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
          op {lastRecord.op} ・ {lastRecord.body_id} ・ 面 {lastRecord.face_index} ・ {lastRecord.distance} mm
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
