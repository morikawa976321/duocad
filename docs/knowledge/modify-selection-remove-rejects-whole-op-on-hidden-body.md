---
name: modify-selection-remove-rejects-whole-op-on-hidden-body
description: 事実(2026-09-06・実機検査で実測) — duocadのmodify_selectionはadd/removeを1操作として原子的に検証するため、removeに「今は非表示のBodyへの古い参照」が1つでも混じるとadd側もろとも拒否される。UIにはトーストしか出ず選択は古いまま変わらない。Bodyを隠す・消す操作の直後は、次に選択を変える前に空の場所をクリックして選択を空にする
created: 2026-09-06
updated: 2026-09-06
---

# `modify_selection` は隠れたBodyへの古い参照が混じると操作ごと弾く

duocadのフロント側 `selectOnly()`/`editSelection()` は選択の置き換えを `modify_selection(add=[新しい参照], remove=[古い選択全部])` の1回にまとめて送る。

**この1操作はサーバ側で原子的に検証される**: `remove` に入っている参照のうち1つでも「今は見えない(`visible=false`)Bodyの辺/面/頂点」を指していると、サーバは `ValueError: body '...' is hidden: 見えないものは指せない` を返し、**add側も含めて操作ごと弾かれる**(2026-09-06実測)。UIからはトースト(`エラー[...]`)にしか出ず、選択は古いまま静かに変化しない。

**踏んだ状況**: Bodyの辺を選択したあと `#parts-hide-all` でそのBodyを隠し、続けてスケッチの図形をクリックして選ぼうとした。クリックは正しく効いて `event.sketchEntity` は解決できていた(`editSelection` は呼ばれていた)のに、`selection.items` がいつまでも古い辺のままで、原因は「新しい参照を足す」側ではなく「隠れたBodyの古い辺を外す」側の検証だった。

**対策**: Bodyを隠す・削除するなど「以前の選択が指せなくなる」操作の直後は、次に選択を変える前に一度**空の場所をクリックして選択を空にする**(`session.clearSelection`)。「選択を残したままBodyを隠す」という操作列自体が稀な組み合わせなので製品側の不具合とは扱っていない(裏取りはしていない)。

関連: [[duocad-box-select-also-picks-regions]] / [[hidden-bodies-stay-uneditable]](非表示Bodyを指せない規律そのもの)
