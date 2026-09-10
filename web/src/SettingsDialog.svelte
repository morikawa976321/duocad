<script lang="ts">
  /**
   * 設定ダイアログ — **一度決めたらほとんど変更しない設定**の置き場。
   *
   * 線引きは「**作業中に切り替えるものはその場のパネル、一度決めたら触らないものはここ**」。
   * AIの接続先(プロバイダ・モデル・エンドポイント)は作業中に触る側なので `AiPanel` に残る。
   *
   * **仕組みを作らない。** スキーマ駆動・カテゴリ・タブは持たず、平らな並びに行を足していく。
   *
   * **ビューポートを覆わない・暗くしない・操作を止めない。** 画面の端に小さく出す。
   * FOVは**見ながら決める**設定なので、判断材料を隠したらこの機能は使えないものになる。
   *
   * **`showModal()` ではなく `show()`(非モーダル)で開く。**
   * `showModal()` は**背後の文書を inert にする** —— 見えてはいても、ビューポートも
   * ビューキューブも pointer/keyboard イベントを一切受け取れなくなる。「見ながら決める」の
   * 「見る」だけが残り「回して確かめる」が死ぬので、覆わない・暗くしないという設計と
   * 同じ理由で inert も外す。
   *
   * 代わりに `showModal()` がただで付けていたものを2つ自前で持つ:
   * - **Esc で閉じる** —— 非モーダルの `<dialog>` は Esc を拾わないので `svelte:window` で拾う。
   * - **重なり順** —— 非モーダルは top-layer に入らないので、CSSで `z-index` を明示する。
   *
   * フォーカストラップは**戻さない**(ビューポートを触らせるのが目的なので、閉じ込めては困る)。
   * **レイアウトの大きさを持たないのは `position: absolute` による**(top-layer が理由ではない)ので、
   * 非モーダルでも `.viewport` の寸法＝カメラのアスペクトには触らない(styles.css の不変条件)。
   *
   * **状態の正は `App.svelte`**。ここは値と口を受け取るだけの presentational な部品で、
   * 双方向束縛をコンポーネントの境界に持ち込まない。
   */
  import { FOV_DEFAULT, FOV_ORTHO } from './viewport'

  const { open, fov, onFov, onClose }: {
    open: boolean
    /**
     * 今の画角[度]。`FOV_ORTHO`(0)なら完全正投影。**UIからは 0 か `FOV_DEFAULT` しか
     * 選べない**が、コンソール等の内部APIが選べない値(70等)を入れていても、チェック
     * ボックスの状態(=正投影かどうか)は `fov === FOV_ORTHO` の1点だけを見て決まる。
     */
    fov: number
    /** 選ばれた画角[度]。**その場で効く**(「適用」で確定させない)。 */
    onFov: (deg: number) => void
    onClose: () => void
  } = $props()

  /** 完全正投影の状態か(チェックボックスの生死はこの1つで決まる)。 */
  const ortho = $derived(fov === FOV_ORTHO)

  let dialog: HTMLDialogElement

  // 開閉は props に従う。**開いている <dialog> に `show()` を呼ぶと例外**なので
  // 現在の状態を見てから呼ぶ。`close()` は `onclose` が App 側へ返す。
  $effect(() => {
    if (open && !dialog.open) dialog.show()
    if (!open && dialog.open) dialog.close()
  })

  /**
   * Esc で閉じる。**非モーダルの `<dialog>` は Esc を拾わない**(close request は
   * `showModal()` で開いたものにしか来ない)ので、開いているあいだだけ window で拾う。
   *
   * **`preventDefault()` も `stopPropagation()` もしない** —— 同じ Esc を
   * `CommandHost` が構え中のコマンドの中断に使っており(`CommandHost.svelte` の
   * `onKeydown`)、モーダルだった頃も両方が起きていた。片方を止めると、この修正が
   * 変えるつもりの無い挙動まで変わる。
   */
  function onWindowKeydown(event: KeyboardEvent): void {
    if (!open || event.key !== 'Escape') return
    onClose()
  }
</script>

<svelte:window onkeydown={onWindowKeydown} />

<dialog id="settings-dialog" bind:this={dialog} onclose={onClose}>
  <div class="settings-head">
    <strong>設定</strong>
    <span class="spacer"></span>
    <button id="settings-close" onclick={onClose} title="設定を閉じる">閉じる</button>
  </div>

  <!-- **平らな並び1つだけ。** 項目が増えたらここに行を足す。
       **画角は正投影/透視(30度)の2値だけ。** 30〜90の1度刻みは
       実用上の差が無い選択肢を61個並べるだけで、Fusion 360もFOVの数値を露出していない。
       戻る先は常に `FOV_DEFAULT` なので「戻る角度を覚える」仕組みは要らない
       (コンソールから内部APIで70等を入れてから正投影を入切すると30に落ちるのは許容する)。 -->
  <label>
    正投影
    <input
      id="settings-ortho"
      type="checkbox"
      checked={ortho}
      onchange={(event) => onFov(event.currentTarget.checked ? FOV_ORTHO : FOV_DEFAULT)}
    />
  </label>
  <p class="hint dim">
    切り替えても見かけの大きさは変わらない。正投影ではパースが完全に消え、
    同じ長さの辺は奥行きが違っても同じ長さに写る。
  </p>
</dialog>

<style>
  /* **`::backdrop` の指定は置かない。** 非モーダル(`show()`)の <dialog> は backdrop を
     そもそも描かないので、透明化する相手がいない。暗くしないという要件は、モーダルを
     やめたこと自体が満たしている。 */

  /* **ビューポート左上のブラウザ(`BrowserPanel.svelte`)のすぐ右**に小さく出す
     (`margin: auto` の中央寄せを打ち消す)。位置の基準は `.stage`(App.svelte で
     `position: relative`)なので、**左右のサイドバーを開閉してもブラウザとの隣接が保たれる**。

     `10px` はブラウザが浮いている余白と同じ値(`.browser-panel` の `top`/`left`)で、
     幅は `--browser-panel-w` を共有している —— 直値で持つと、片方だけ動かした日に
     重なるか妙な隙間が空く。

     `position: absolute` はフローの外なので、ブラウザや Toast と同じく `.viewport` の
     寸法＝カメラのアスペクトには触らない(styles.css の不変条件)。**非モーダルは
     top-layer に入らない**ので重なり順は `z-index` で決める —— 隣り合うブラウザが
     `z-index: 5` なので、それより上に置く。 */
  #settings-dialog {
    position: absolute;
    inset: auto;
    top: calc(10px + var(--toolbar-overlay-h, 0px));
    left: calc(10px + var(--browser-panel-w) + 10px);
    z-index: 10;
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
    color: var(--dim);
    /* AiPanel と同じ理由でラベルは折らない。 */
    white-space: nowrap;
  }

  /* 幅・面・accent-color は styles.css の `input[type='checkbox']` が持つ。
     ここは既定の外余白を消すだけ。 */
  #settings-ortho {
    margin: 0;
  }

  /* 大きさは styles.css の `.hint` が持つ(--fs-meta)。 */
  .hint {
    margin: 6px 0 0;
  }
</style>
