<script lang="ts">
  /**
   * 操作履歴パネル(タスクE・2026-08-14) — **文書が操作の並びであることが見える場所**。
   *
   * 保存形式は操作ログのJSONで、形状はそこから導出される(実装計画書§3.1)。その
   * 「正のほう」をそのまま人に見せるのがここで、**undoでどこまで戻れるか**が分かる。
   *
   * ## 位置は件数で出す
   *
   * `can_undo` / `can_redo` の真偽だけでは、3回undoした画面と1回undoした画面が同じに
   * 見える。コアが配る `redo_count`(あと何回やり直せるか)を使って
   * 「N 件のうち M 件目まで有効」と出す。**undoされた操作は `state.log` に出てこない**
   * (`OpLog.entries` はカーソルまでの写し)ので、消えた行の数がそのまま先の残りになる。
   *
   * ## 「AIがやった操作」と「人がやった操作」の区別 — **半分しかできない**
   *
   * タスクEは「区別できると望ましい(できなければ理由を書く)」としている。結論から
   * 書くと、**この画面が出した操作にだけ印を付けられる**。理由は、
   * **操作ログに主体が入っていない**から(§3.6の `Operation` は seq / verb / args /
   * selection / provenance のみ)。区別を本物にするならコアの永続形式を変えることになり、
   * それは保存済みのファイルの互換とMCP・再生経路すべてに波及するので、Eの範囲では
   * **画面側の印**にとどめてある。したがって:
   *
   * - この画面から出した操作 … 「自分」と出る
   * - AI(MCP)が出した操作・**再読込より前に自分が出した操作** … 印なし
   *
   * 印が無い＝AIの操作、ではない(そう読めてしまうと嘘になるので、見出しも
   * 「自分」だけを立てて、他方には何も書かない)。
   */
  let {
    log,
    redoCount,
    mine,
  }: {
    /** 有効な操作(`state.log`。undoで消えた分は入っていない)。 */
    log: Array<{ seq: number; verb: string; args: Record<string, unknown> }>
    /** あと何回やり直せるか(`state.redo_count`)。 */
    redoCount: number
    /** この画面が出した操作の seq。上のdocstringの「半分しかできない」側。 */
    mine: number[]
  } = $props()

  const owned = $derived(new Set(mine))

  /**
   * 新しい操作が上に来るように**配列を逆順にして描く**。
   *
   * **CSSの `flex-direction: column-reverse` でやってはいけない**(QA指摘 2026-08-14。
   * headless Edgeで実測): 反転した並びで溢れた分は `scrollHeight === clientHeight` の
   * まま**スクロールで到達できない**(先頭側へ押し出されたぶんは到達不能なオーバーフローに
   * なる、というCSSの仕様どおりの挙動)。既定の起動では開幕から55件積まれるので、
   * 「操作ログを一覧で見せる」が最新4行のリストに縮退していた。
   */
  const newestFirst = $derived([...log].reverse())

  /** 主要な引数だけを1行にする(全部出すと読めない)。 */
  function summarize(verb: string, args: Record<string, unknown>): string {
    const number = (value: unknown): string =>
      typeof value === 'number' ? String(Math.round(value * 1000) / 1000) : String(value ?? '')
    const count = (value: unknown): number => (Array.isArray(value) ? value.length : 0)
    switch (verb) {
      case 'extrude_face':
        return `面 ${args.face_index} / ${number(args.distance)}mm`
      case 'extrude':
        return `領域 ${count(args.regions)}個 / ${number(args.distance)}mm / ${args.mode}`
      case 'fillet':
        return `辺 ${count(args.edges)}本 / r${number(args.radius)}`
      case 'chamfer':
        return `辺 ${count(args.edges)}本 / ${number(args.distance)}mm`
      case 'pattern_rectangular':
        return `${args.nx}×${args.ny} / ${number(args.dx)}, ${number(args.dy)}mm`
      case 'pattern_circular':
        return `${args.count}個 / ${number(args.total_angle)}度`
      case 'set_parameter':
        return `${args.name} = ${number(args.value)}`
      case 'set_visible':
        return `${args.visible ? '表示' : '非表示'}`
      case 'sketch_create':
        return typeof args.plane === 'object' && args.plane !== null
          ? String((args.plane as { name?: string }).name ?? '面に貼る')
          : ''
      case 'import_step_editable':
        return `${String(args.path ?? '')} / ${count(args.body_ids)}部品`
      case 'import_step':
      case 'make_editable_copy':
        return String(args.path ?? args.source_body_id ?? '')
      default:
        // 未知のverbでも黙って空にしない(足したverbが履歴で無名になるのを防ぐ)
        return Object.keys(args).slice(0, 2).join(', ')
    }
  }
</script>

<section class="history-panel">
  <div class="row">
    <strong>操作履歴</strong>
    <span id="history-position" class="value" data-applied={log.length} data-redo={redoCount}>
      {log.length} 件が有効{redoCount > 0 ? ` ・ あと ${redoCount} 件やり直せる` : ''}
    </span>
  </div>

  <div class="list" id="history-list">
    {#if log.length === 0}
      <span class="value dim">まだ操作が無い</span>
    {/if}
    {#each newestFirst as entry (entry.seq)}
      <div class="item" data-seq={entry.seq} data-verb={entry.verb}>
        <span class="seq">{entry.seq}</span>
        <span class="verb">{entry.verb}</span>
        <span class="value dim">{summarize(entry.verb, entry.args)}</span>
        {#if owned.has(entry.seq)}<span class="mine">自分</span>{/if}
      </div>
    {/each}
  </div>
</section>

<style>
  /* 高さ固定(SketchPanel・ParametersPanelと同じ理由: ビューポートを伸縮させない) */
  .history-panel {
    display: flex;
    flex-direction: column;
    gap: 4px;
    height: 96px;
    overflow: hidden;
    padding: 6px 8px;
    border-top: 1px solid #2a3038;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
  }

  .list {
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
    display: flex;
    flex-direction: column; /* 並べ替えは配列側でやる(上の `newestFirst` のdocstring) */
    gap: 2px;
  }

  .item {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .seq {
    color: var(--dim);
    font-size: 11px;
    min-width: 22px;
    text-align: right;
  }

  .verb {
    font-size: 12px;
  }

  .mine {
    font-size: 11px;
    border: 1px solid #ffb454;
    color: #ffb454;
    border-radius: 3px;
    padding: 0 4px;
  }

  .value {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .dim {
    color: var(--dim);
  }
</style>
