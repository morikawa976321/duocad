<script lang="ts">
  /**
   * アプリ層の組み立て — 接続・ビューポート・パネルを繋ぐだけの場所。
   *
   * **B(2026-08-13)で「面を選ぶ → 距離を打つ → Enter」の状態機械はここから出た。**
   * 引数を集める仕組みは `commands/CommandHost.svelte` に1つだけあり、verbはそこへ
   * 載せるだけになる(C・D・Eがコマンドを足しても、ここは変わらない)。
   *
   * **選択の正はコアにある(M1由来の債務を返した)。** M1はブラウザ側の `selected` が
   * 正で、実装計画書§3.5が定める「選択集合もコア側(永続・双方向)」を満たしていなかった。
   * A2でコアに入り、Bでアプリ層がそれに乗り換えた —— ここは `state.selection` を
   * 描き直すだけで、**選択の値を1つも覚えていない**。可視性(M3.5)と同じ形である。
   *
   * ここに残っている責務は3つだけ:
   *
   * 1. **ピック → コアの選択への翻訳**(修飾キーで追加・除去・置き換えを決める)
   * 2. **プレビューの往復**(コマンド層が「何を試すか」を決め、ここが投げて幽霊を描く)
   * 3. **押し出した面の後継を選び直す**(「同じ面を10回押す」を再ピックなしで回す。M1から)
   */
  import { onMount } from 'svelte'
  import CommandHost from './commands/CommandHost.svelte'
  import { faceRef, sameRef } from './commands/command'
  import PartsPanel from './PartsPanel.svelte'
  import SelectionPanel from './SelectionPanel.svelte'
  import {
    createSession,
    type Applied,
    type BodyGeometry,
    type ConnectionStatus,
    type PreviewPayload,
    type SelectionRef,
    type ServerState,
    type ShapeRef,
  } from './session'
  import {
    createViewport,
    type GeometryPayload,
    type PickEvent,
    type Viewport,
  } from './viewport'

  let container: HTMLDivElement
  let viewport: Viewport | null = null

  let status = $state<ConnectionStatus>('connecting')
  let model = $state<ServerState | null>(null)
  let messages = $state<string[]>([])
  let lastApplied = $state<Applied | null>(null)
  // 選択中の面の面積・法線などを出すための表示用キャッシュ(body_id -> 直近のgeometry)。
  // ドメインの状態ではない(可視性・選択の正はコア) — viewport が three.jsの中に既に
  // 持っている形状を、UI表示とテスト用フック向けに読める形で複製しているだけ。
  let bodyGeometry = $state<Record<string, GeometryPayload>>({})

  /** 送信中のコマンドの `client_op`(応答が返るまでの間だけ生きる)。 */
  let pendingOp = $state<number | null>(null)
  let opCounter = 0

  /** 投げたプレビューの番号(古い応答を捨てるため。0は「投げていない」)。 */
  let previewId = 0
  /** 画面に幽霊が出ているか(テスト用フックが見る)。 */
  let previewing = $state(false)

  const session = createSession({
    onState: handleState,
    onError: (message, where) => {
      note(`エラー[${where}] ${message}`)
      pendingOp = null
      if (where === 'preview') clearPreview()
    },
    onWarning: (message) => note(`警告 ${message}`),
    onStatus: (next) => {
      status = next
      // 応答待ちの最中に接続が切れたら、その `client_op` はもう返ってこない。
      // 捨てないと再接続後も「直前の操作の応答待ち」が残り続け、Enterが永久に拒否される
      // (`onerror` を伴わない正常クローズで踏む。QA 2026-08-13)。
      if (next !== 'open' && pendingOp !== null) {
        pendingOp = null
        clearPreview()
      }
    },
    onPreview: handlePreview,
  })

  function note(message: string): void {
    if (!message) return
    messages = [message, ...messages].slice(0, 4)
  }

  function nextOp(): number {
    opCounter += 1
    return opCounter
  }

  // ---------------------------------------------------------------- コアからの状態
  function handleState(next: ServerState, changed: BodyGeometry[], applied: Applied | null): void {
    model = next

    // 差分更新: 版が変わったBodyだけをビューポートへ渡す。27部品のうち1つを押し出しても
    // 描き直すのはその1つ(サーバ側の差分送信をここで無駄にしない。§2)。
    viewport?.update(changed.map((body) => ({ bodyId: body.body_id, geometry: body.geometry })))
    // 文書から消えたBody(サーバの契約: 知っているのに state.bodies に無いID)があれば
    // ビューポートからも取り除く。
    viewport?.prune(next.bodies.map((body) => body.body_id))
    if (changed.length > 0) previewing = false // 本物が届いたら幽霊は消えている(viewport.update)

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

    drawHighlight()

    // **選択の変更イベントはverbの適用ではない**(`change: "selection"` で見分ける。
    // session.ts の `Applied` 参照)。verbの応答としてだけ `lastApplied` を動かす。
    if (applied && applied.change === 'selection') {
      if (applied.client_op !== null && applied.client_op === pendingOp) pendingOp = null
      return
    }
    if (applied) {
      reselectAfter(applied)
      if (applied.client_op !== null && applied.client_op === pendingOp) pendingOp = null
      lastApplied = applied
    }
  }

  function handlePreview(payload: PreviewPayload): void {
    if (payload.preview_id !== previewId) return // 追い越された応答・中断後の応答は捨てる
    viewport?.showPreview(
      payload.bodies.map((body) => ({ bodyId: body.body_id, geometry: body.geometry })),
    )
    previewing = payload.bodies.length > 0
  }

  // ---------------------------------------------------------------- 選択(コアを叩くだけ)
  const selection = $derived(model?.selection ?? null)
  /** 3Dで強調できるのは面だけ(辺・頂点は選択パネルが見せる。viewport.ts 参照)。 */
  const selectedFaces = $derived(
    (selection?.items ?? []).filter(
      (item): item is ShapeRef => !('sketch' in item) && (item as ShapeRef).kind === 'face',
    ),
  )

  function drawHighlight(): void {
    viewport?.highlight(
      selectedFaces.map((item) => ({ bodyId: item.body, faceIndex: item.index })),
      selection?.origin === 'proposed' ? 'proposed' : 'user',
    )
  }

  function modifySelection(add: SelectionRef[], remove: SelectionRef[]): void {
    if (add.length === 0 && remove.length === 0) return
    session.modifySelection(add, remove, nextOp())
  }

  /**
   * ピックを選択集合の編集へ翻訳する。**UI側に選択状態を持たない**ので、
   * ここが作るのは「何を足して何を引くか」だけで、結果はコアから返ってくる。
   */
  function onPick(event: PickEvent): void {
    const current = selection?.items ?? []
    if (event.hit === null) {
      // 何もない所を素でクリック = 選択解除(修飾キー付きは空振りにする)
      if (event.mode === 'replace' && current.length > 0) session.clearSelection(nextOp())
      else if (event.mode === 'replace') note('面を外した')
      return
    }
    const ref = faceRef(event.hit.bodyId, event.hit.faceIndex)
    const already = current.some((item) => sameRef(item, ref))
    if (event.mode === 'add') {
      modifySelection([ref], [])
    } else if (event.mode === 'remove') {
      modifySelection([], [ref])
    } else if (already && current.length === 1) {
      modifySelection([], [ref]) // 再クリックで外す
    } else {
      // 置き換え。**自分自身は remove に入れない**(コアは remove を後に適用するので、
      // 入れると足したそばから外れる。`duocad/selection.py` の契約)。
      modifySelection([ref], current.filter((item) => !sameRef(item, ref)))
    }
  }

  /**
   * 押し出し・undo・redo の後で、次に押す面を選び直す(M1からの挙動)。
   *
   * **添字は形状変更で壊れる**ので持ち越さず、サーバが返した後継の添字を使う。
   */
  function reselectAfter(applied: Applied): void {
    let next: ShapeRef | null = null
    if (applied.verb === 'extrude_face' && typeof applied.after_face_index === 'number' && applied.body_id) {
      next = faceRef(applied.body_id, applied.after_face_index)
    } else if (
      applied.verb === 'undo' &&
      applied.undone === 'extrude_face' &&
      typeof applied.face_index === 'number' &&
      applied.body_id
    ) {
      next = faceRef(applied.body_id, applied.face_index)
    } else if (
      applied.verb === 'redo' &&
      applied.redone === 'extrude_face' &&
      typeof applied.after_face_index === 'number' &&
      applied.body_id
    ) {
      next = faceRef(applied.body_id, applied.after_face_index)
    }
    if (next === null) return
    const target = next
    const current = selection?.items ?? []
    if (current.length === 1 && sameRef(current[0], target)) return
    modifySelection([target], current.filter((item) => !sameRef(item, target)))
  }

  // ---------------------------------------------------------------- コマンド層との繋ぎ
  function runCommand(verb: string, args: Record<string, unknown>): void {
    if (verb !== 'extrude_face') {
      note(`未知のコマンド: ${verb}`)
      return
    }
    const op = nextOp()
    pendingOp = op
    session.extrude(args.body_id as string, args.face_index as number, args.distance as number, op)
  }

  function requestPreview(verb: string, args: Record<string, unknown>): void {
    previewId += 1
    session.preview(verb, args, previewId)
  }

  function clearPreview(): void {
    previewId += 1 // 投げてある応答を無効にする(戻ってきても番号が合わない)
    previewing = false
    viewport?.clearPreview()
  }

  function onWindowKeydown(event: KeyboardEvent): void {
    if (!(event.ctrlKey || event.metaKey)) return
    const key = event.key.toLowerCase()
    if (key === 'z' && !event.shiftKey) {
      event.preventDefault()
      session.undo()
    } else if (key === 'y' || (key === 'z' && event.shiftKey)) {
      event.preventDefault()
      session.redo()
    }
  }

  // ---------------------------------------------------------------- 可視性(パーツ一覧から)
  // **コアの `set_visible` verb を呼ぶだけ**(アプリ層に可視性の状態を持たせない)。
  //
  // `model.bodies` には**参照ボディ(`kind="reference"`)も混ざっている**ので、
  // パーツ一覧・一括操作とも `kind==="solid"` だけを扱う(`duocad/server.py` の
  // `load_step` docstring参照)。ここを `bodies` 全部にすると「全表示」が参照ボディまで
  // 可視にしてしまい、1ソリッドにつき2つの同じ形が重なって描かれる。
  const partBodies = $derived((model?.bodies ?? []).filter((body) => body.kind === 'solid'))

  function setVisible(bodyId: string, visible: boolean): void {
    session.setVisible(bodyId, visible, nextOp())
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
    viewport.onPick(onPick)
    session.start()
    return () => {
      session.dispose()
      viewport?.dispose()
      viewport = null
    }
  })

  // 選択が変わったら描き直す(コア発 → 購読 → 再描画。上り方向の出口)
  $effect(() => {
    void selection
    drawHighlight()
  })

  const firstFace = $derived(selectedFaces[0] ?? null)
  const selectedFace = $derived(
    firstFace === null
      ? null
      : (bodyGeometry[firstFace.body]?.faces.find((face) => face.index === firstFace.index) ?? null),
  )
  const selectedBody = $derived(
    firstFace === null ? null : (model?.bodies.find((body) => body.body_id === firstFace.body) ?? null),
  )

  // 自動テスト(scripts/smoke.mjs)がブラウザの中を見るための穴。ローカル専用ツールなので
  // 常に生やしてある(「動くはず」で納品しないための道具であり、隠すと使えない)。
  $effect(() => {
    ;(window as unknown as Record<string, unknown>).__duocad = {
      ready: status === 'open' && model !== null,
      status,
      state: model,
      // ユーザーに見せる27部品(参照ボディを除いた kind==="solid" だけ)
      parts: partBodies,
      bodyGeometry,
      // **選択の正はコア**。ここは共有集合の写しであって、ブラウザ側の状態ではない。
      selection: model?.selection ?? null,
      // M1からの互換の穴: 選択のうち最初の面を `{bodyId, faceIndex}` で出す
      selected: firstFace === null ? null : { bodyId: firstFace.body, faceIndex: firstFace.index },
      previewing,
      lastApplied,
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
      {#if previewing}<span class="tag preview">プレビュー</span>{/if}
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

  <SelectionPanel
    {selection}
    bodies={model?.bodies ?? []}
    onRemove={(ref) => modifySelection([], [ref])}
    onClear={() => session.clearSelection(nextOp())}
    onCommit={() => session.commitSelection(nextOp())}
  />

  <section class="panel">
    <CommandHost
      {selection}
      busy={pendingOp !== null}
      onRun={runCommand}
      onPreview={requestPreview}
      onCancelPreview={clearPreview}
      onNote={note}
    />

    <div class="row">
      <span class="label">面</span>
      {#if selectedFace && selectedBody}
        <span class="value">
          {selectedBody.name} / 面 {selectedFace.index} / {selectedFace.kind} / 面積 {selectedFace.area.toFixed(2)} mm²
          / 法線 [{selectedFace.normal.map((n) => n.toFixed(2)).join(', ')}]
        </span>
      {:else}
        <span class="value dim">面をクリックして選ぶ</span>
      {/if}
      <button id="undo" onclick={() => session.undo()} disabled={!model?.can_undo}>undo</button>
      <button id="redo" onclick={() => session.redo()} disabled={!model?.can_redo}>redo</button>
      <span class="value dim">正=足す / 負=削る</span>
    </div>

    <div class="row">
      <span class="label">直前</span>
      {#if lastApplied?.verb === 'extrude_face'}
        <span class="value">
          seq {lastApplied.seq} ・ {lastApplied.body_id} ・ 面 {lastApplied.face_index} ・ {lastApplied.distance} mm
        </span>
      {:else}
        <span class="value dim">まだ操作なし</span>
      {/if}
    </div>

    {#if messages.length}
      <div class="row">
        <span class="label">通知</span>
        <span class="value">{messages.join(' / ')}</span>
      </div>
    {/if}
  </section>
</div>

<style>
  .tag.preview {
    background: #7ee081;
    color: #0b1a0d;
  }
</style>
