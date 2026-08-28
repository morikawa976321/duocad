<script lang="ts">
  /**
   * 設定ダイアログ — **一度決めたらほとんど変更しない設定**の置き場(2026-08-27本人決定)。
   *
   * 線引きは「**作業中に切り替えるものはその場のパネル、一度決めたら触らないものはここ**」。
   * AIの接続先(プロバイダ・モデル・エンドポイント)は作業中に触る側なので `AiPanel` に残る。
   *
   * **仕組みを作らない。** スキーマ駆動・カテゴリ・タブは持たず、平らな並びに行を足していく。
   *
   * **ビューポートを覆わない・暗くしない。** 画面の端に小さく出し、`::backdrop` は透明。
   * FOVは**見ながら決める**設定なので、判断材料を隠したらこの機能は使えないものになる。
   * native `<dialog>` + `showModal()` を使うので Esc・フォーカストラップ・top-layer が
   * ただで付き、**position:fixed / top-layer なのでレイアウトの大きさを持たない**
   * (＝`.viewport` の寸法＝カメラのアスペクトに触らない。styles.css の不変条件)。
   *
   * **状態の正は `App.svelte`**。ここは値と口を受け取るだけの presentational な部品で、
   * 双方向束縛をコンポーネントの境界に持ち込まない。
   */
  import { FOV_MAX, FOV_MIN, FOV_ORTHO } from './viewport'

  const { open, fov, angle, onFov, onClose }: {
    open: boolean
    /** 今の画角[度]。`FOV_ORTHO`(0)なら完全正投影で、スライダーは効かない。 */
    fov: number
    /**
     * スライダーが指す角度(`FOV_MIN`〜`FOV_MAX`)。**正投影のあいだも「戻る先」を
     * 指したまま止まる**ので、`fov` とは別に受け取る —— 0はスライダーの範囲外であり、
     * `fov` をそのまま流すとブラウザが30へ丸めて戻る先が失われる。
     */
    angle: number
    /** 選ばれた画角[度]。**その場で効く**(「適用」で確定させない)。 */
    onFov: (deg: number) => void
    onClose: () => void
  } = $props()

  /** 完全正投影の状態か(チェックボックスとスライダーの生死はこの1つで決まる)。 */
  const ortho = $derived(fov === FOV_ORTHO)

  let dialog: HTMLDialogElement

  // 開閉は props に従う。**`showModal()` は開いている <dialog> に呼ぶと例外**なので
  // 現在の状態を見てから呼ぶ。Esc で閉じたときは `onclose` が App 側へ返す。
  $effect(() => {
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  })
</script>

<dialog id="settings-dialog" bind:this={dialog} onclose={onClose}>
  <div class="settings-head">
    <strong>設定</strong>
    <span class="spacer"></span>
    <button id="settings-close" onclick={onClose} title="設定を閉じる">閉じる</button>
  </div>

  <!-- **平らな並び1つだけ。** 項目が増えたらここに行を足す。 -->
  <label>
    画角(FOV)
    <input
      id="settings-fov"
      type="range"
      min={FOV_MIN}
      max={FOV_MAX}
      step="1"
      value={angle}
      disabled={ortho}
      oninput={(event) => onFov(Number.parseFloat(event.currentTarget.value))}
    />
    <span id="settings-fov-value">{Math.round(fov)}°</span>
  </label>
  <!-- **正投影はスライダーの続きに置かない**(2026-08-28本人指示)。0と30〜90のあいだに
       選べる値は無いので、1本の目盛りに乗せると1〜29という「押せるのに効かない帯」が
       できる。別の口(チェックボックス)にすれば、選べる値がそのまま操作の形になる。 -->
  <label>
    正投影(0°)
    <input
      id="settings-ortho"
      type="checkbox"
      checked={ortho}
      onchange={(event) => onFov(event.currentTarget.checked ? FOV_ORTHO : angle)}
    />
  </label>
  <p class="hint dim">
    小さいほどパースが弱くなる。見かけの大きさは変わらない。正投影ではパースが完全に消え、
    同じ長さの辺は奥行きが違っても同じ長さに写る。
  </p>
</dialog>

<style>
  /* **暗くしない。** 覆うと、見ながら決めるという設定の前提が壊れる。 */
  #settings-dialog::backdrop {
    background: transparent;
  }

  /* 画面の右端に小さく出す(`margin: auto` の中央寄せを打ち消す)。
     top-layer にいるので、この位置指定はどのパネルのレイアウトにも影響しない。 */
  #settings-dialog {
    position: fixed;
    inset: auto;
    top: 64px;
    right: 16px;
    width: 260px;
    margin: 0;
    padding: 8px 10px;
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--line);
  }

  .settings-head {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }

  .spacer {
    flex: 1 1 auto;
  }

  /* AiPanel の `<label>` + input に揃える。 */
  label {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--dim);
  }

  /* 素の `input` は width:7em / text-align:right(styles.css)。スライダーには効かせない。 */
  #settings-fov {
    flex: 1 1 auto;
    width: auto;
    min-width: 0;
    padding: 0;
    accent-color: var(--accent);
  }

  /* 正投影のあいだは角度が効かない。**薄くして触れないことを見せる**(隠すと行が消えて
     ダイアログの高さが動き、戻る先の角度も読めなくなる)。 */
  #settings-fov:disabled {
    opacity: 0.4;
  }

  /* チェックボックスは幅を持たせない(素の `input` の width:7em を打ち消す)。 */
  #settings-ortho {
    flex: 0 0 auto;
    width: auto;
    min-width: 0;
    margin: 0;
    accent-color: var(--accent);
  }

  #settings-fov-value {
    width: 3.5em;
    text-align: right;
    color: var(--text);
    flex: 0 0 auto;
  }

  .hint {
    margin: 6px 0 0;
    font-size: 11px;
  }
</style>
