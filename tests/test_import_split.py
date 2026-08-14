"""M3.5契約: STEP読み込みのソリッド分解(タスクファイル項番1〜6・2026-08-11)。

**なぜこれが要るのか(実機で測った問題)**: `import_step` は `OneShape()` が返す compound を
そのまま1つの `Body` に入れていた。PCケース(27部品・2835面)を読むと2835面の塊が1ボディになり、
`extrude_face` はその塊全体にOCCT演算をかけ、更新のたびに全面を測り直す。2026-08-10に本人が
M1のビューアで実測した値:

| モデル | 面数 | kernel(OCCT) | 幾何更新 合計 |
|---|---|---|---|
| 小さい部品 | 57 | 42–48ms | 107–113ms |
| **PCケース** | **2895–2971** | **4326–5101ms** | **6602–7275ms** |

さらにピック失敗12回/押し出し7回(失敗率63%)。**性能の問題ではなくデータモデルの問題**で、
ソリッドごとにBodyへ分けるという1つの変更が、選択・更新速度・パーツ表示を同時に解く。

## この契約が変えるもの(実装者への申し送り・重要)

`import_step` の戻り値が `BodyId` から **`list[BodyId]`** に変わり、操作ログの `args` も
`{"path":.., "body_id":..}` から `{"path":.., "body_ids": [..]}` に変わる。**M0のテストが
これに依存しているので、実装時には下記を書き換える必要がある**(tests/README.md の
「M3.5」節に一覧がある)。テストを先に書くこのタスクの時点では、M0〜M3の141件は
1行も触っていない(＝全部緑のまま)。

## 実測値(2026-08-11・OCPで直接数えた。タスクファイル記載値と独立に一致)

| ファイル | ソリッド | 全面数 | 1ソリッドあたり最大/中央/最小 |
|---|---|---|---|
| PC_case_20251122.step | 27 | 2835 | 233 / 94 / 6 |
| shelf_attachment_GD.step | 17 | 56 | 4 / 3 / 3 |
| monitor_attachment_for_24inch_GD.step | 26 | 92 | 6 / 3 / 3 |
| plate_1by1_24.step | 1 | 74 | 74 / 74 / 74 |

数え方は `TopExp.MapShapes_s(shape, TopAbs_SOLID)` と同 `TopAbs_FACE`(どちらも重複除去)。
"""
from __future__ import annotations

import statistics
from pathlib import Path

import pytest

from duocad import kernel
from duocad.document import Document
from duocad.io_step import import_step
from duocad.oplog import OpLog
from duocad.verbs import import_step_editable

MODELS = Path(r"C:\Users\tm976\mf\3d-models")
PC_CASE = MODELS / "mATX_PC_case_20251122" / "PC_case_20251122.step"
SHELF = MODELS / "shelf_attachment_GD.step"
MONITOR = MODELS / "monitor_attachment_for_24inch_GD.step"
PLATE = MODELS / "stackable_plate" / "plate_1by1_24.step"

#: (パス, ソリッド数, 全面数)。2026-08-11実測。
MEASURED = [
    (PC_CASE, 27, 2835),
    (SHELF, 17, 56),
    (MONITOR, 26, 92),
    (PLATE, 1, 74),  # 単一ソリッド。分解が単一部品を壊さないことの確認用
]


def _imported(path: Path):
    """STEPを読んだ文書と、返ってきたBodyIdの並びを返す。"""
    doc = Document()
    body_ids = import_step(doc, str(path))
    return doc, body_ids


# --------------------------------------------------------------------------------------
# 項番1・3: ソリッドごとに1つのBodyになる(単一ソリッドも壊さない)
# --------------------------------------------------------------------------------------
def test_pc_case_is_split_into_27_bodies():
    """PC_case_20251122.step を読むと27個のBodyができる(1個ではない)。

    これが失敗して `len(doc.bodies) == 1` なら、compoundを丸ごと1ボディに入れる
    分解前の実装のままである。
    """
    doc, body_ids = _imported(PC_CASE)

    assert len(doc.bodies) == 27, (
        f"PCケースは27ソリッドなので27 Bodyになる(実際は{len(doc.bodies)}個)。"
        "1個ならcompoundをそのままBodyに入れている"
    )
    assert isinstance(body_ids, list), (
        f"import_step は分解した全BodyIdのリストを返す(実際は{type(body_ids).__name__})"
    )
    assert body_ids == [body.id for body in doc.bodies], (
        "戻り値の並びは doc.bodies に追加した順と一致すること"
    )


def test_a_single_solid_step_still_yields_exactly_one_body():
    """plate_1by1_24.step(単一ソリッド・74面)を読むと1個のBodyになる。

    分解が単一部品を壊さないこと。**1要素のリストを返す**のであって、
    「1個のときだけ素のBodyIdを返す」ではない(戻り値の型が入力に依存すると
    呼び出し側が必ず分岐を書く羽目になる)。
    """
    doc, body_ids = _imported(PLATE)

    assert len(doc.bodies) == 1
    assert isinstance(body_ids, list) and len(body_ids) == 1
    assert kernel.face_count(doc.bodies[0].shape) == 74


@pytest.mark.parametrize(
    "path, solids, faces",
    MEASURED,
    ids=[path.stem for path, _solids, _faces in MEASURED],
)
def test_every_measured_model_is_split_into_its_solids(path, solids, faces):
    """実測した4モデルすべてで、Bodyの数＝ソリッドの数になる(2026-08-11実測値)。"""
    doc, _body_ids = _imported(path)
    assert len(doc.bodies) == solids


# --------------------------------------------------------------------------------------
# 項番2: 分解で面が増減しない
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path, solids, faces",
    MEASURED,
    ids=[path.stem for path, _solids, _faces in MEASURED],
)
def test_split_preserves_the_total_face_count(path, solids, faces):
    """各Bodyの面数の合計が、分解前の全面数と一致する(PCケースなら2835)。

    分解は形状を切り分けるのではなく**塊を部品へ振り分ける**操作なので、面は増えも減りもしない。
    ここが合わないと、ソリッド以外の部分形状(裸の面・シェル)を拾っているか、
    逆に取りこぼしている。
    """
    doc, _body_ids = _imported(path)
    per_body = [kernel.face_count(body.shape) for body in doc.bodies]
    assert sum(per_body) == faces, (
        f"面の合計が {sum(per_body)}(期待 {faces})。分解で面が増減している"
    )


def test_pc_case_face_counts_per_body_match_the_measured_spread():
    """PCケースの1ソリッドあたりの面数は 最大233・中央94・最小6(2026-08-11実測)。

    合計だけを見ていると「27個に分けたが中身が偏っている」を見逃す。
    最大233は**押し出しの最悪ケース**で、性能テスト(test_incremental_update.py 項番13)が
    このBodyを対象にする。
    """
    doc, _body_ids = _imported(PC_CASE)
    counts = sorted(kernel.face_count(body.shape) for body in doc.bodies)

    assert max(counts) == 233
    assert statistics.median(counts) == 94
    assert min(counts) == 6


# --------------------------------------------------------------------------------------
# 項番4: 各Bodyが独立した visible を持つ
# --------------------------------------------------------------------------------------
def test_each_body_has_its_own_visible_flag():
    """分解した各Bodyは既定で可視で、1つを変えても他に波及しない。

    `Body.visible` は M0 から `document.py` にあるが(既定 True)、compoundを1ボディで
    持っている間は「部品ごとに消す」という意味を持てなかった。分解して初めて機能する。
    """
    doc, _body_ids = _imported(SHELF)
    assert len(doc.bodies) == 17
    assert all(body.visible is True for body in doc.bodies), "既定は全部可視"

    doc.bodies[0].visible = False

    assert doc.bodies[0].visible is False
    assert all(body.visible is True for body in doc.bodies[1:]), (
        "1つのBodyの可視性を変えても他のBodyに波及しないこと(共有しているとここで落ちる)"
    )


# --------------------------------------------------------------------------------------
# 項番5: 一意のidと人間可読な名前
# --------------------------------------------------------------------------------------
def test_split_bodies_have_unique_ids_and_non_empty_names():
    """分解した27個のBodyは、idも名前も互いに重複しない。

    idが重複すると `Document.add_body` が ValueError で落ちる(M0の契約)ので実質の関門は
    名前の側。名前は画面のパーツ一覧(次々タスク)でそのまま並ぶので、空文字や同名が
    混ざると人間には区別できなくなる。
    """
    doc, body_ids = _imported(PC_CASE)

    ids = [body.id for body in doc.bodies]
    names = [body.name for body in doc.bodies]

    assert len(set(ids)) == len(ids) == 27, "BodyIdが重複している"
    assert len(set(names)) == len(names) == 27, f"Body名が重複している: {names}"
    assert all(isinstance(name, str) and name.strip() for name in names), (
        "名前は空でない文字列であること"
    )


def test_split_body_names_are_the_file_stem_with_a_1_based_index():
    """既定の名前は `<ファイル名(拡張子なし)>_1` … `_N`。

    **STEP由来の部品名を使う実装に替えてもよい**(タスクファイルの許可)。その場合はこの
    テストだけを書き換えること — ただし上の
    `test_split_bodies_have_unique_ids_and_non_empty_names` が課す
    「一意・非空」は名前の付け方によらず動かせない契約である。
    **そのためにXDE/XCAFへ移行しない**(取れるなら使ってよい、という程度の要求)。
    """
    doc, _body_ids = _imported(SHELF)

    stem = SHELF.stem
    assert [body.name for body in doc.bodies] == [
        f"{stem}_{n}" for n in range(1, 18)
    ]


def test_split_bodies_are_reference_bodies():
    """分解しても kind は "reference" のまま(M0の読み取り専用契約は変わらない)。

    編集するには従来どおり `make_editable_copy` を通す。分解は「読み込んだものを部品に
    分ける」だけで、読み取り専用かどうかとは独立している。
    """
    doc, _body_ids = _imported(SHELF)
    assert {body.kind for body in doc.bodies} == {"reference"}


# --------------------------------------------------------------------------------------
# 項番6: 操作ログ1行・undoで全Bodyがまとめて消える
# --------------------------------------------------------------------------------------
def test_the_split_import_is_a_single_operation():
    """17個のBodyができても操作ログは1行(verbの粒度＝undo1回で消える単位)。

    17行積むと、undoを17回押さないと読み込みが取り消せない。実装計画書§2の
    「undo1回で消える単位＝操作ログ1行＝MCP1呼び出し」に反する。
    """
    doc, body_ids = _imported(SHELF)

    assert len(doc.bodies) == 17
    assert len(doc.log.entries) == 1, (
        f"操作ログが{len(doc.log.entries)}行(期待1行)。分解はBodyごとに記録しない"
    )
    operation = doc.log.entries[0]
    assert operation.verb == "import_step"
    assert operation.args["path"] == str(SHELF)
    assert operation.args["body_ids"] == list(body_ids), (
        'args は {"path":.., "body_ids": [..]}。'
        "作ったIDを全部記録しないと再生で別のIDになり、保存した選択集合が指す先を失う"
    )


def test_undo_removes_every_body_of_the_split_import():
    """undo 1回で17個のBodyがまとめて消える(1個だけ残る・1個だけ消えるを許さない)。"""
    doc, _body_ids = _imported(SHELF)
    assert len(doc.bodies) == 17

    doc.log.undo(doc)

    assert doc.bodies == [], "undoで全Bodyが消えること"
    assert doc.log.entries == []


def test_redo_restores_every_body_with_the_same_ids():
    """redoで17個が同じIDで戻る(操作を積み直さない)。"""
    doc, body_ids = _imported(SHELF)
    before = [body.id for body in doc.bodies]
    assert len(before) == 17, "分解前は1個しかないので、この検査は分解後にだけ意味を持つ"

    doc.log.undo(doc)
    doc.log.redo(doc)

    assert [body.id for body in doc.bodies] == before
    assert len(doc.log.entries) == 1, "redoは既存のエントリを有効化し直すだけ(積み直さない)"


def test_replay_reproduces_every_body_with_the_same_ids():
    """保存したログを再生すると、同じIDの17個が同じ順序で復元される。

    `args["body_ids"]` を**そのまま使って**復元すること。新しいIDを振り直すと、
    保存済みの選択集合・押し出しの `body_id` が全部迷子になる。
    """
    doc, _body_ids = _imported(SHELF)
    before = [(body.id, body.name, body.kind) for body in doc.bodies]
    assert len(before) == 17, "分解前は1個しかないので、この検査は分解後にだけ意味を持つ"

    restored = Document()
    restored.log = OpLog.from_json(doc.log.to_json())
    restored.log.replay(restored)

    assert [
        (body.id, body.name, body.kind) for body in restored.bodies
    ] == before


# --------------------------------------------------------------------------------------
# verb `import_step_editable`(2026-08-15)
#
# **読んだら使える**ことの契約。起動オプション `--step` が廃止できるかどうかは、
# 起動時にしかできなかったこの手順(参照 + 編集用コピー + 参照を隠す)が
# **1本のverb**としてアプリ内・MCPから踏めるかどうかにかかっている。
# --------------------------------------------------------------------------------------
def _imported_editable(path: Path):
    """新verbでSTEPを読んだ文書と、返ってきた編集用BodyIdの並びを返す。"""
    doc = Document()
    body_ids = import_step_editable(doc, str(path))
    return doc, body_ids


def test_editable_import_makes_one_editable_body_per_solid():
    """PCケースを読むと**編集可能なボディが27個**でき、参照ボディは隠れている。

    追加操作なしに面をクリックして押し出せる状態(=起動時の `--step` と同じ状態)が
    アプリ内の読み込みだけで揃うこと。ここが本タスクの目的そのもの。
    """
    doc, body_ids = _imported_editable(PC_CASE)

    editable = [body for body in doc.bodies if body.kind == "solid"]
    references = [body for body in doc.bodies if body.kind == "reference"]

    assert len(editable) == 27, f"編集可能なボディが{len(editable)}個(期待27個)"
    assert body_ids == [body.id for body in editable], "戻り値は編集可能なボディのIDの並び"
    assert all(doc.is_editable(body_id) for body_id in body_ids)
    assert len(references) == 27, "原本(参照ボディ)も1ソリッドにつき1つ残す"
    assert all(body.visible is False for body in references), (
        "参照ボディは隠すこと(可視のままだと同じ形が二重に描かれ、ピック失敗が再発する)"
    )
    assert all(body.visible is True for body in editable)


def test_editable_import_is_a_single_operation_with_every_id_recorded():
    """操作ログは**1行**。作った全ID(参照・編集用の両方)が `args` に載る。

    N行積むと27部品を読んだ後の Ctrl+Z が最後の1個だけを戻す挙動になる
    (実装計画書§2「undo1回で消える単位＝操作ログ1行＝MCP1呼び出し」)。
    IDを記録しないと再生で別のIDになり、保存した選択集合が指す先を失う。
    """
    doc, body_ids = _imported_editable(SHELF)

    assert len(doc.log.entries) == 1, (
        f"操作ログが{len(doc.log.entries)}行(期待1行)。"
        "コマンド層から import_step と make_editable_copy をN回発行していないか"
    )
    operation = doc.log.entries[0]
    assert operation.verb == "import_step_editable"
    assert operation.args["path"] == str(SHELF)
    assert operation.args["body_ids"] == list(body_ids)
    assert operation.args["reference_ids"] == [
        body.id for body in doc.bodies if body.kind == "reference"
    ]
    assert len(operation.args["reference_ids"]) == 17


def test_undo_removes_every_body_of_the_editable_import_and_redo_restores_them():
    """undo 1回で34個(参照17 + 編集用17)がまとめて消え、redoで**同じIDで**戻る。"""
    doc, _body_ids = _imported_editable(SHELF)
    before = [(body.id, body.kind, body.visible) for body in doc.bodies]
    assert len(before) == 34

    doc.log.undo(doc)

    assert doc.bodies == [], "undo1回で参照・編集用の両方が消えること"
    assert doc.log.entries == []

    doc.log.redo(doc)

    assert [(body.id, body.kind, body.visible) for body in doc.bodies] == before
    assert len(doc.log.entries) == 1, "redoは既存のエントリを有効化し直すだけ(積み直さない)"


def test_replay_of_the_editable_import_reproduces_the_same_document():
    """保存 → 再生(`open_document` が通る経路)で、IDも種別も可視性も同じ形に戻る。"""
    doc, _body_ids = _imported_editable(SHELF)
    before = [(body.id, body.name, body.kind, body.visible) for body in doc.bodies]

    restored = Document()
    restored.log = OpLog.from_json(doc.log.to_json())
    restored.log.replay(restored)

    assert [
        (body.id, body.name, body.kind, body.visible) for body in restored.bodies
    ] == before


def test_editable_import_leaves_nothing_behind_when_the_path_is_missing(tmp_path):
    """パスが無ければ FileNotFoundError。**document にもログにも何も残らない**。"""
    doc = Document()
    missing = tmp_path / "no-such-file.step"

    with pytest.raises(FileNotFoundError):
        import_step_editable(doc, str(missing))

    assert doc.bodies == []
    assert doc.log.entries == []


def test_editable_import_leaves_nothing_behind_when_the_file_is_broken(tmp_path):
    """STEPとして壊れていれば ValueError。documentにもログにも何も残らない。

    `kernel.read_step` が `ReadFile()` の戻り値を検査してから例外にすること
    (検査を飛ばすとPython例外ではなくプロセスごと落ちる。io_step.py の落とし穴)。
    """
    doc = Document()
    broken = tmp_path / "broken.step"
    broken.write_text("これはSTEPではない\n", encoding="utf-8")

    with pytest.raises(ValueError):
        import_step_editable(doc, str(broken))

    assert doc.bodies == []
    assert doc.log.entries == []
