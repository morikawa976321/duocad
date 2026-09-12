---
name: duocad-settings-dialog-goes-non-modal
description: 判断+実装(2026-09-04) — duocadの設定ダイアログをモーダル(showModal)から非モーダル(show)へ変え、FOVを調整している最中もビューポート・ビューキューブを操作できるようにした
created: 2026-09-04
updated: 2026-09-04
---

duocadの設定ダイアログは native `<dialog>` の `showModal()` で開いていた。`showModal()` は
**背後の文書を inert にする**ので、開いているあいだはビューポートもビューキューブも
pointer/keyboard イベントを一切受け取れない([[modal-dialog-backdrop-hides-canvas-from-elementfrompoint]])。
FOVは「見ながら決める」設定として作られており、覆わない・暗くしないという設計([[duocad-fov-adjustable-from-ui]])に
していたのに、**「見る」だけが残り「回して確かめる」が死んでいた**。

**非モーダル(`show()`)へ変えると本人が決めた**(2026-09-04)。覆わない・暗くしないのと同じ理由で
inert も外す。

`showModal()` がただで付けていたもののうち、自前で持ち直したのは2つだけ:

- **Esc で閉じる** — 非モーダルの `<dialog>` は close request を受けない。`svelte:window` で拾う。
- **重なり順** — 非モーダルは top-layer に入らないので `z-index` を明示する。duocadは
  `styles.css` に z-index を1つも持たず全て文書順で解決しているので、ここが唯一の指定になった。

**フォーカストラップは戻さない**(ビューポートを触らせるのが目的なので、閉じ込めては困る)。
**「レイアウトの大きさを持たない」**(＝`.viewport` の寸法＝カメラのアスペクトに触らない)という
不変条件は top-layer ではなく **`position: fixed` の側が持っている**ので、非モーダルでも保たれる。

実測(2026-09-04): ダイアログを開いたままビューキューブのクリックでカメラが533動き、
ビューポートの右ドラッグ回転で500動く。`elementFromPoint` はキューブの画素で `CANVAS` を返す
(モーダルだった頃は `DIALOG` を返していた)。ダイアログとキューブの画面矩形は重なっていない
(右サイドバーが開いているぶんキューブが左に寄るため)。
