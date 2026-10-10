<script module lang="ts">
  /**
   * トースト — エラー(`fail`、琥珀の札)と、見落とされると困る知らせ(`inform`、無彩色の札)。
   * 公開APIは `fail` / `inform` / `errorLog` だけ。種別は関数名で分け、引数は文字列1つ(`fail(msg, 'warn')` のような口を
   * 開けると呼ぶ側が分類を判断することになり、エラー経路を1本にした意味が消える)。
   *
   * ```ts
   * import Toast, { fail, inform, errorLog } from './Toast.svelte'
   * fail('エラー[fillet] 半径が大きすぎる')
   * inform('STEPを書き出した')
   * ```
   *
   * `fail` は控え(`errorLog`)に積むのと札を出すのを1回でやる(札だけ出して控えを飛ばす書き方が無い。
   * 飛ばすと `__duocad.errors` を見る実機検査からそのエラーだけ見えなくなる)。
   * 状態はモジュールに置くのでどこからでも呼べる。構造で守れず、呼ぶ側が守ること:
   * - 同じ失敗を2つの層から報告しない。まとめるのは同じ文言だけで、前置きが違えば2枚出て控えにも2件入る。
   * - エラーの検出をパネルの `$effect` に置かない。そのパネルを畳むと検出ごと止まる。
   *
   * 1. `position: absolute` で `.main` の上に浮かせる(`styles.css` の不変条件: `.main` の中で大きさを持つのは `.viewport` だけ)。
   *    フローに置くとエラーが出た瞬間にカメラのアスペクトが変わり、見えていた面がずれる。
   * 2. 本体は `pointer-events: none`、コピー・閉じるのボタンだけ受ける。失敗直後の札が次の canvas クリックを飲まないように(実機検査も生の座標で叩く)。
   * 3. 同じ文言は積まずに数える(再接続の試行のように同じエラーが連発する経路がある)。
   */

  type ToastKind = 'error' | 'info'

  /** 1枚。`timer` は自動消滅の予約。 */
  type ToastItem = {
    id: number
    kind: ToastKind
    message: string
    /** この札が出ている間に同じ文言が来た回数。2以上のときだけ画面に出す。 */
    count: number
    copied: boolean
    timer: ReturnType<typeof setTimeout> | null
  }

  /** 自動で消えるまで。エラーは読み切れる長さが要るので、よくある3秒より長い。 */
  const LIFETIME_MS = 15000
  const MAX_VISIBLE = 5

  let items = $state<ToastItem[]>([])
  let lastId = 0

  const LOG_SIZE = 20
  let log = $state<string[]>([])

  /**
   * これまでに `fail` へ来たエラー(新しい順)。表示には使わない: 札は消えるので、`smoke-files.mjs` などが
   * 最後に一度「走行中にエラーが出ていないか」を見る場所である。
   * `$state` に置き、この関数が呼ばれた時点で読む: `testHooks` の `$effect` が依存し、新しいエラーで
   * `window.__duocad.errors` が作り直される(素の `let` だと古い写しで止まる)。
   */
  export function errorLog(): string[] {
    return log
  }

  function stop(item: ToastItem): void {
    if (item.timer === null) return
    clearTimeout(item.timer)
    item.timer = null
  }

  function restart(item: ToastItem): void {
    stop(item)
    item.timer = setTimeout(() => dismiss(item.id), LIFETIME_MS)
  }

  function dismiss(id: number): void {
    const found = items.find((item) => item.id === id)
    if (found === undefined) return
    stop(found)
    items = items.filter((item) => item.id !== id)
  }

  async function copyMessage(item: ToastItem): Promise<void> {
    if (item.copied) return
    try {
      await navigator.clipboard.writeText(item.message)
      item.copied = true
    } catch {
      // 権限・非HTTPS等で失敗しても札の操作は続けられる(無言で戻す)
    }
  }

  /** 札を1枚出す(同じ種別・同じ文言が出ていれば数えるだけ)。公開しない。 */
  function show(kind: ToastKind, text: string): void {
    // 出ている札の全部を見る(直近の1枚だけだと、WS切断中に交互に届く2種のエラーが A,B,A,B… と積み上がる)。
    const same = items.find((item) => item.kind === kind && item.message === text)
    if (same !== undefined) {
      same.count += 1
      restart(same)
      return
    }
    const created: ToastItem = {
      id: (lastId += 1),
      kind,
      message: text,
      count: 1,
      copied: false,
      timer: null,
    }
    const next = [created, ...items]
    // 溢れた分は予約も取り消してから捨てる(捨てた札のタイマーが後から起きないように)
    for (const dropped of next.slice(MAX_VISIBLE)) stop(dropped)
    items = next.slice(0, MAX_VISIBLE)
    // `created` ではなく `items[0]` に予約を書く: `$state` に入れた時点で中身はプロキシに包まれ、素のオブジェクトは別の口になる。
    restart(items[0])
  }

  export function fail(message: string): void {
    const text = message.trim()
    if (text === '') return
    log = [text, ...log].slice(0, LOG_SIZE)
    show('error', text)
  }

  /** 失敗ではない知らせを札に出す。控え(`errorLog`)には積まない(控えはエラーの記録)。 */
  export function inform(message: string): void {
    const text = message.trim()
    if (text === '') return
    show('info', text)
  }
</script>

<!-- `id="toasts"` は実機検査が文言を読む穴(`.toast-text` がエラー1枚の本文)。中身が空でも枠は出す。
     知らせの本文は `.toast-info-text` に分ける: smoke 群は `#toasts .toast-text` を「エラーが出た」と読むので、
     同じクラスにすると知らせの札で検査が赤くなる。 -->
<div id="toasts" class="toasts" role="log" aria-live="assertive" aria-relevant="additions">
  {#each items as item (item.id)}
    <div class="toast" class:info={item.kind === 'info'} data-toast-id={item.id} data-toast-kind={item.kind}>
      <span class={item.kind === 'info' ? 'toast-info-text' : 'toast-text'}>{item.message}</span>
      {#if item.count > 1}
        <span class="toast-count">{item.count}回</span>
      {/if}
      <div class="toast-actions">
        <button
          type="button"
          class="toast-copy"
          class:copied={item.copied}
          aria-label={item.copied ? 'コピーしました' : item.kind === 'info' ? '文をコピー' : 'エラー文をコピー'}
          onclick={() => copyMessage(item)}
        >
          {#if item.copied}
            <svg class="toast-icon" viewBox="0 0 16 16" aria-hidden="true">
              <path
                d="M3 8.5 L6.5 12 L13 4"
                fill="none"
                stroke="currentColor"
                stroke-width="1.5"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
          {:else}
            <svg class="toast-icon" viewBox="0 0 16 16" aria-hidden="true">
              <rect x="5" y="5" width="9" height="9" fill="none" stroke="currentColor" stroke-width="1.5" />
              <path d="M3 11V3h8" fill="none" stroke="currentColor" stroke-width="1.5" />
            </svg>
          {/if}
        </button>
        <button type="button" class="toast-close" aria-label="この通知を閉じる" onclick={() => dismiss(item.id)}>
          ×
        </button>
      </div>
    </div>
  {/each}
</div>

<style>
  /* `.main` が位置の基準(styles.css で position:relative)。右端は `--sidebar-w` に合わせる
     (幅を直書きすると、変えた日にトーストだけサイドバーの下へ潜る)。 */
  .toasts {
    position: absolute;
    bottom: 12px;
    right: calc(var(--sidebar-w) + 16px);
    z-index: 20;
    display: flex;
    flex-direction: column-reverse;
    gap: 6px;
    width: 360px;
    max-width: 40%;
    pointer-events: none;
  }

  .toast {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    padding: 6px 8px 6px 10px;
    background: var(--panel);
    border: 1px solid var(--accent-line);
    box-shadow:
      inset 3px 0 0 var(--accent),
      0 2px 10px oklch(0% 0 0 / 0.45);
    pointer-events: none;
  }

  .toast.info {
    border-color: var(--line);
    box-shadow:
      inset 3px 0 0 var(--dim),
      0 2px 10px oklch(0% 0 0 / 0.45);
  }

  .toast-text,
  .toast-info-text {
    flex: 1 1 auto;
    overflow-wrap: anywhere;
  }

  .toast-count {
    flex: 0 0 auto;
    font-size: var(--fs-meta);
    color: var(--dim);
  }

  .toast-actions {
    flex: 0 0 auto;
    display: flex;
    align-items: flex-start;
    gap: 2px;
    pointer-events: auto;
  }

  .toast-copy,
  .toast-close {
    padding: 0 6px;
    line-height: 1.2;
    border-color: transparent;
    background: transparent;
    color: var(--dim);
  }

  .toast-copy.copied {
    color: var(--accent);
  }

  .toast-icon {
    display: block;
    width: 14px;
    height: 14px;
    pointer-events: none;
  }
</style>
