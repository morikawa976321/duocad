"""投機的実行 — verbを試しに実行して**文書を元へ戻す**(プレビューの土台。B・2026-08-13)。

コマンド層の状態機械「起動 → 引数収集 → **プレビュー** → 確定(Enter) / 中断(Esc)」のうち、
プレビューだけが「実行したのに残らない」性質を持つ。実装計画書§2の図が verb層を
**純関数** `f(文書, 選択, 引数) → 新しい文書` と描いているのはこの性質を指しているが、
**実装の verb は純関数ではない** —— `body.shape` を差し替え、操作ログへ1行積む。
そのままプレビューに使うと、確定していない形が操作ログに残る。

そこで「元へ戻す」側をここに1つ置く。**verbには一切手を入れない**(全verbを純粋にする
書き換えは、由来マップ・undoスナップショットまで巻き込む大手術になる)。

    with speculative(doc):
        extrude_face(doc, face_index=3, distance=5.0)   # 実際に文書が変わる
        payload = kernel.tessellate(body.shape, 0.2)    # 変わった形を測る
    # ここで文書は with に入る前と**完全に同じ**(ボディ・形状・スケッチ・
    # パラメータ・操作ログ・選択集合)

## なぜコアに置くのか(アプリ層ではなく)

実装計画書§2「外してはいけない3つ」の1: **コアはUIが無くても機能的に完全であること**。
プレビューをブラウザ側の仕掛けにすると、AIは「適用したらどうなるか」を試せない
(交互操作でAIが提案を検算する経路が塞がる)。ここに置けば MCP からも同じ手が使える。

## 何を戻すか

- `doc.bodies`      — リストの中身(追加・削除)と、各Bodyの `shape` / `visible` / `name` / `kind`
- アクティブボディ  — `add_body` が付け替えるので必ず戻す
- `doc.sketches`    — deepcopyで戻す(中身は `Sketch`/`Entity`/`Pattern` のdataclassで、
                      OCPの形状を持たないため deepcopy できる。`sketch.py` 参照)
- `doc.parameters`  — 同上
- `doc.log`         — エントリとカーソル(`OpLog._snapshot` / `_restore`)
- `doc.selection`   — items と origin

**形状(`shape`)は差し替えであって書き換えではない**(verbは `body.shape = 新しい形` と
代入する。OCCTのTopoDS_Shapeを破壊的に編集する経路はkernelに無い)。したがって
参照を控えて戻すだけで完全な巻き戻しになり、深いコピーは要らない —— ここが
「元へ戻す」を数十行で書ける理由である。

## 戻さないもの(意図的)

- `duocad.verbs._common.SNAPSHOTS` の undo用スナップショット。
  **2026-08-15まで同型のテーブルが4つ**(`extrude_face` / `fillet` / `chamfer` / `extrude`)
  あり、この節は4つ全部について同じ論法を主張していた。いまは**1つ**なので、
  下の検算も1箇所で足りる。
  `(body_id, seq)` で引く弱参照テーブルで、**同じseqで再適用されたら上書きされる**
  (`remember` の契約)。巻き戻した seq はこの後の確定操作が使うので、確定すれば
  正しい値で上書きされる。中断した場合に1件残るが、参照しているのは巻き戻し前の
  形状ではなく**巻き戻し後と同じ形状**なので、誤ったundoにはならない。

  **この論法が成立するのは「同一seqの再適用しか起こさないverb」に限る**
  (`extrude_face` / `fillet` / `chamfer` — 検算済み。QA 2026-08-13。
  **領域の `extrude` もこの側**である — `verbs/extrude.py` の `_do_extrude` は
  記録された領域とmodeで**その操作だけ**を再適用し、操作ログを頭から再生しない。
  D(2026-08-14)で `PREVIEWABLE_VERBS` へ載せる前に確認した。QA 2026-08-14)。
  `set_parameter` のように `parameters.rebuild` が**操作ログを頭から再生する**verbは、
  再生の途中で**過去のseqのスナップショットを別の値で作り直す**。文書は戻せても
  `SNAPSHOTS` は戻らないので、中断したプレビューの形状が後の実undoで復元されうる。
  **`PREVIEWABLE_VERBS` に再生系のverbを足すときは、ここも一緒に戻すこと**
  (単に1行足すだけでは壊れる)。
- `Session` 側のキャッシュ(版・テセレーション・`_applied_count`)。文書ではないので
  ここの責務ではない —— `Session.preview` が自分で退避する。
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Iterator

from .document import Document


@contextmanager
def speculative(doc: Document) -> Iterator[None]:
    """`with` の中で文書へ加えた変更を、抜けるときに**すべて元へ戻す**。

    中で例外が出ても戻す(`finally`)。中断(Esc)は「文書が一切変わらないこと」が
    契約なので、失敗したプレビューが半端な状態を残すことは許されない。

    **入れ子にしない**。入れ子にできない技術的な理由は無いが、巻き戻しの単位が
    分かりにくくなるだけで用途が無い(プレビューは1回のverbを試す道具)。
    """
    snapshot = _snapshot(doc)
    try:
        yield
    finally:
        _restore(doc, snapshot)


def _snapshot(doc: Document) -> dict:
    """巻き戻しに要るものを控える(下のモジュールdocstring「何を戻すか」がすべて)。"""
    return {
        "bodies": [(body, body.shape, body.visible, body.name, body.kind) for body in doc.bodies],
        # `active_body()` はBodyを返すのでIDに落として控える(Bodyごと消えていても戻せる)
        "active_body_id": doc._active_body_id,
        "sketches": copy.deepcopy(doc.sketches),
        "parameters": copy.deepcopy(doc.parameters),
        "log": doc.log._snapshot(),
        "selection_items": list(doc.selection.items),
        "selection_origin": doc.selection.origin,
    }


def _restore(doc: Document, snapshot: dict) -> None:
    """控えた状態へ戻す。**リストは中身を入れ替える**(オブジェクトを差し替えない)。

    `doc.bodies` を新しいリストで置き換えると、それを掴んでいる呼び出し側が
    古いリストを見続ける。中身の入れ替え(`[:] = ...`)なら参照が生きたまま戻る。
    """
    doc.bodies[:] = [entry[0] for entry in snapshot["bodies"]]
    for body, shape, visible, name, kind in snapshot["bodies"]:
        body.shape = shape
        body.visible = visible
        body.name = name
        body.kind = kind
    # `set_active_body` は None を受け付けない(存在しないIDとして KeyError)ので直接戻す。
    # 文書の外から書き換えてよい属性ではないが、ここは文書を元へ戻す側の実装である。
    doc._active_body_id = snapshot["active_body_id"]
    doc.sketches[:] = snapshot["sketches"]
    doc.parameters.clear()
    doc.parameters.update(snapshot["parameters"])
    doc.log._restore(snapshot["log"])
    doc.selection.items = snapshot["selection_items"]
    doc.selection.origin = snapshot["selection_origin"]
