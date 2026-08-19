"""M3.5契約: 差分送信と性能の回帰(タスクファイル項番11〜14)。

サーバ層(`duocad/server.py` の `Session`)の契約。**アプリ層(web/)は次々タスクの範囲**で、
ここで固定するのは「サーバが何を再計算し、何を送るか」だけ。

## 何を直しているのか(実測)

PCケース(2835面)を1ボディとして持っていたため、1部品を押し出すたびに全2835面を
測り直していた: kernel(OCCT) 4326–5101ms / 幾何更新 合計 6602–7275ms。本人の所見は
「押し出しをEnterしてから反映まで5秒くらいかかる」。ソリッドごとにBodyへ分ければ、
変わるのは押した部品1つだけなので、**再テセレーションも再送も1部品分で済む**。

## 実測(このタスクで測った。閾値の根拠)

deviation=0.2 でPCケースを27ソリッドに分けたとき:

- 27ソリッド全部のテセレーション: **892.5ms**(読み込みは別に1272.6ms)
- 全Body分のJSON: **6,003,765バイト**
- 1Bodyで最大のもの: **600,764バイト = 全体の10.0%**(面数最大の233面Bodyは433,768B = 7.2%)
- 面数233のソリッド1個の `extrude_face_on_shape`: 面1 **726.0ms** / 面2 517.7ms / 面3 757.9ms
  (面数94のソリッドなら 169.5 / 203.2 / 244.8ms)

したがって「再送が全Body分より明確に小さい」は **全体の1/4未満** を閾値にする
(実測の最悪値10.0%に対して2.5倍の余裕)。押し出しの閾値はタスクファイル指定の
**1000ms**(分解前4326–5101msの1/4以下、実測726msに対しては約1.4倍の余裕しかないので、
落ちたときは環境ではなく実装を疑うより先に**下の実測値と比べること**)。
"""
from __future__ import annotations

import json
import statistics
import time
import warnings
from pathlib import Path

import pytest

# 押し出せる面を探すヘルパは可視性のテストと共有する(面の添字の決め打ちを避けるため)
from test_visibility import EXTRUDE_MM, first_extrudable_face

from duocad import kernel
from duocad.document import Document
from duocad.io_step import import_step
from duocad.server import Session
from duocad.verbs import extrude_face, make_editable_copy, set_visible

from models import MODELS  # テスト共通のSTEPの置き場(tests/models.py)
from models import PC_CASE  # noqa: F401
from models import SHELF  # noqa: F401

DEVIATION = 0.2

#: 押し出しの kernel 時間の上限[ms](タスクファイル指定)。分解前の実測は4326–5101ms。
KERNEL_BUDGET_MS = 1000.0

#: 差分の送信量が全Body分に対して占めてよい上限(実測の最悪値は10.0%)
DELTA_SIZE_RATIO = 0.25

#: 1Bodyあたりの状態に必ず含まれるキー
BODY_STATE_KEYS = {
    "body_id",
    "name",
    "kind",
    "visible",
    "revision",
    "face_count",
    "volume",
    "geometry",
}


class MeasurementReport(UserWarning):
    """計測値をpytestの出力へ必ず出すための入れ物。

    タスクファイルは「実測値をテスト出力に必ず出すこと — 閾値の合否より数値の推移が重要」
    と要求する。`print` は**通ったテストでは既定で表示されない**(`-s` が要る)ので、
    既定の実行でも必ず出る warnings summary を使う。`print` も併せて出しておくので、
    `-s` を付ければそちらでも読める。
    """


def _report(message: str) -> None:
    """計測値を pytest の warnings summary と標準出力の両方へ出す。"""
    print(message)
    warnings.warn(message, MeasurementReport, stacklevel=2)


def _session(step_path: Path, tmp_path: Path) -> Session:
    return Session(step_path, DEVIATION)


def _editable_bodies(session: Session) -> list:
    """押し出しの対象になるBody(kind="solid")。"""
    return [body for body in session.document.bodies if body.kind == "solid"]


def _spy_on_tessellate(monkeypatch) -> list:
    """`kernel.tessellate` を包んで、渡された形状を記録するリストを返す。

    `server.py` は `from . import kernel` で同じモジュールオブジェクトを見ているので、
    ここで差し替えるとサーバ側の呼び出しも捕まる。
    """
    calls: list = []
    real_tessellate = kernel.tessellate

    def spy(shape, *args, **kwargs):
        calls.append(shape)
        return real_tessellate(shape, *args, **kwargs)

    monkeypatch.setattr(kernel, "tessellate", spy)
    return calls


def _extrude_face(session, body, face_index=None, distance=None):
    """`body` の面を1枚選んでから押し出す(人がクリックしてEnterを押す順序)。

    `Session.extrude` は押す面を引数で受け取らず**確定選択から取る**(2026-08-20)ので、
    差分送信の検査もまず選択を置く。選択は文書を変えないので、ここで測っている
    「版が上がったか」「再送されたか」の対象には入らない。
    """
    index = first_extrudable_face(body.shape) if face_index is None else face_index
    session.clear_selection()
    session.modify_selection([{"body": body.id, "kind": "face", "index": index}], [])
    return session.extrude(EXTRUDE_MM if distance is None else distance)


def _shelf_parts(tmp_path: Path):
    """shelf を読んだSessionと、その17個の編集可能Bodyを返す。

    差分送信の契約は「複数のBodyのうち1つだけを送る」ものなので、**分解が効いていることが
    前提**である。ここで数を確かめておかないと、分解前は編集可能Bodyが1個しか無いために
    各テストが `IndexError` で落ち、何の契約が破れたのか読めなくなる。
    """
    session = _session(SHELF, tmp_path)
    bodies = _editable_bodies(session)
    assert len(bodies) == 17, (
        f"shelf_attachment_GD.step は17ソリッド(押し出せるBodyは{len(bodies)}個)。"
        "差分送信のテストはソリッド分解(tests/test_import_split.py)の上に載っている"
    )
    return session, bodies


# --------------------------------------------------------------------------------------
# 前提: サーバは部品ごとの状態を持つ
# --------------------------------------------------------------------------------------
def test_load_step_makes_one_editable_body_per_solid(tmp_path):
    """起動時のSTEP読み込みで、**ソリッドの数だけ編集できるBody**ができる。

    分解しても編集できるのが1つだけなら、27部品のうち26個は触れないままになる。
    参照ボディ(`kind="reference"`)をどう扱うか(残す/隠す)は実装の自由 —
    ここで縛るのは「1ソリッドにつき1つ押し出せるBodyがある」ことだけ。
    """
    session = _session(SHELF, tmp_path)
    assert len(_editable_bodies(session)) == 17


def test_the_startup_load_is_one_operation_that_cannot_be_undone(tmp_path):
    """`Session(step_path, DEVIATION)` の起動経路が動くことを固定する。

    CLIに起動オプション `--step` は無いが、Python APIの `Session(step_path, ...)` はある
    (pytestがこの形で土台を作る)。中身は verb `import_step_editable` 1本なので、
    **操作ログは1行**で、`_setup_floor_seq`(土台はundoできない)も同じく効く ——
    `import_step` + `make_editable_copy`×N + `set_visible`×N を個別に積むと 1+2N 行になる。
    """
    session = _session(SHELF, tmp_path)

    entries = session.document.log.entries
    assert [entry.verb for entry in entries] == ["import_step_editable"], (
        f"起動時の読み込みが{len(entries)}行(期待1行): {[entry.verb for entry in entries]}"
    )
    assert session.document.log.can_undo is True, "ログ自体は積まれている(前提)"
    assert session.can_undo is False, "起動時に読んだ土台はundoの対象外"
    with pytest.raises(ValueError):
        session.undo()


def test_load_step_does_not_show_the_same_solid_twice(tmp_path):
    """読み込み直後に**形状を送るBodyはソリッドの数と同じ**(17個)。

    参照ボディ(`kind="reference"`)と編集用コピーの両方を可視のまま持つと、同じ形が
    二重にテセレーションされて二重に送られ、しかも手前の部品を消しても参照側のメッシュが
    残ってピック失敗が再発する。参照を隠すか、出さないか、
    そもそも作らないかは実装の自由 — **同時に見えるのは1ソリッドにつき1つ**が契約。
    """
    session = _session(SHELF, tmp_path)

    state = session.state()
    assert "bodies" in state, "state は bodies の配列を持つ(単一ボディ前提をやめる)"
    shown = [
        entry
        for entry in state["bodies"]
        if entry["visible"] and entry["geometry"] is not None
    ]

    assert len(shown) == 17, (
        f"形状を送るBodyが{len(shown)}個(ソリッドは17個)。"
        "参照ボディと編集用コピーが二重に見えている"
    )


def test_state_lists_every_body_separately(tmp_path):
    """`state()` は `bodies` の配列を返し、各Bodyが自分の形状と可視性を持つ。

    単一ボディ前提で `body_id` / `geometry` を平らに持つ形では、
    パーツ一覧も差分送信も表現できない。
    """
    session = _session(SHELF, tmp_path)

    state = session.state()

    assert "bodies" in state, "state は bodies の配列を持つ(単一ボディ前提をやめる)"
    assert len(state["bodies"]) == len(session.document.bodies)
    for entry in state["bodies"]:
        assert BODY_STATE_KEYS <= set(entry), (
            f"Bodyの状態に足りないキー: {sorted(BODY_STATE_KEYS - set(entry))}"
        )
    assert {entry["body_id"] for entry in state["bodies"]} == {
        body.id for body in session.document.bodies
    }
    # 可視なBodyには形状が付いている(画面に出せる)
    visible = [entry for entry in state["bodies"] if entry["visible"]]
    assert visible, "可視なBodyが1つも無い"
    assert all(entry["geometry"] is not None for entry in visible)


def test_hidden_bodies_are_not_tessellated(tmp_path, monkeypatch):
    """不可視のBodyには `geometry` を付けない(測らない)。

    テセレーションはこのシステムで一番高い処理(PCケース全体で892.5ms)。見えないものを
    測って送るなら、部品ごとの表示・非表示を作った意味が半分無くなる。
    参照ボディを隠して保持する実装を選んだ場合も、この契約があれば費用は増えない。
    """
    session, bodies = _shelf_parts(tmp_path)
    target = bodies[0]
    hidden_shape = target.shape
    session.state()  # 初回の全テセレーションを済ませておく

    set_visible(session.document, target.id, False)
    calls = _spy_on_tessellate(monkeypatch)
    entry = next(
        item for item in session.state()["bodies"] if item["body_id"] == target.id
    )

    assert entry["visible"] is False
    assert entry["geometry"] is None
    assert hidden_shape not in calls, "不可視にしたBodyをまだ測っている"


def test_geometry_refuses_a_hidden_body(tmp_path):
    """`geometry()` は不可視Bodyに対して ValueError（隠したものは測らない）。

    「測らない」を `state()` の出力だけで縛ると、低い層の `geometry()` を直接呼ぶ実装が
    隠したBodyを測り続ける穴が残る。入口の側で閉じる。
    """
    session, bodies = _shelf_parts(tmp_path)
    target = bodies[0]
    assert session.geometry(target.id) is not None

    set_visible(session.document, target.id, False)

    with pytest.raises(ValueError):
        session.geometry(target.id)


# --------------------------------------------------------------------------------------
# 項番11: 変更のあったBodyだけが再テセレーション・再送される
# --------------------------------------------------------------------------------------
def test_only_the_changed_body_is_resent_after_an_extrusion(tmp_path):
    """押し出しの後、`delta()` が返すのは押したBodyだけ。残りは `unchanged` に入る。

    送信対象のbody_idの集合そのものを検証する(タスクファイル項番11の指定どおり)。
    """
    session, bodies = _shelf_parts(tmp_path)
    target = bodies[0]
    known = {
        body.id: session.body_revision(body.id) for body in session.document.bodies
    }
    face_index = first_extrudable_face(target.shape)

    applied = _extrude_face(session, target, face_index)
    delta = session.delta(known)

    assert applied["changed_bodies"] == [target.id]
    assert {entry["body_id"] for entry in delta["bodies"]} == {target.id}, (
        "押していないBodyまで再送している"
    )
    assert set(delta["unchanged"]) == {
        body.id for body in session.document.bodies
    } - {target.id}


def test_a_client_that_knows_nothing_receives_every_body(tmp_path):
    """`known_revisions` が空なら全Bodyが `bodies` に入る(初回接続)。

    差分の最適化が「常に1つしか送らない」に化けていないことの裏取り。
    """
    session = _session(SHELF, tmp_path)

    delta = session.delta({})

    assert {entry["body_id"] for entry in delta["bodies"]} == {
        body.id for body in session.document.bodies
    }
    assert delta["unchanged"] == []


def test_the_revision_of_an_untouched_body_does_not_move(tmp_path):
    """押し出しで版が上がるのは押したBodyだけ。

    版が全Bodyで共有されていると、差分送信は成立しているように見えて毎回全部送ることになる。
    """
    session, bodies = _shelf_parts(tmp_path)
    target, other = bodies[0], bodies[1]
    target_before = session.body_revision(target.id)
    other_before = session.body_revision(other.id)

    _extrude_face(session, target)

    assert session.body_revision(target.id) != target_before
    assert session.body_revision(other.id) == other_before


def test_hiding_a_body_is_delivered_as_a_delta(tmp_path):
    """`set_visible` の結果が `delta()` で届く(版が可視性でも上がる)。

    **版を「形状の版」に限ると起きる穴**: 非表示にしたBodyは
    `unchanged` に入り、クライアントは消したことを一度も知らされない。パーツを消して
    奥の面を選ぶという、このマイルストーンの目的そのものが差分の上で成立しなくなる。
    形状と可視性の**両方**で版が動くのが正しい。
    """
    session, bodies = _shelf_parts(tmp_path)
    target = bodies[0]
    known = {
        body.id: session.body_revision(body.id) for body in session.document.bodies
    }

    set_visible(session.document, target.id, False)
    delta = session.delta(known)

    assert {entry["body_id"] for entry in delta["bodies"]} == {target.id}, (
        "非表示にしたことがクライアントへ届いていない"
    )
    entry = delta["bodies"][0]
    assert entry["visible"] is False
    assert entry["geometry"] is None


def test_showing_a_body_again_is_delivered_as_a_delta(tmp_path):
    """再表示も `delta()` で届き、形状が戻ってくる。

    非表示だけを版に反映する実装（「隠したときだけ版を上げる」）でも他のテストは全部通る
    ため、**戻す方向も別に縛る**。これが無いと、消した
    パーツを画面へ戻せないまま緑になる。
    """
    session, bodies = _shelf_parts(tmp_path)
    target = bodies[0]
    set_visible(session.document, target.id, False)
    known = {
        body.id: session.body_revision(body.id) for body in session.document.bodies
    }

    set_visible(session.document, target.id, True)
    delta = session.delta(known)

    assert {entry["body_id"] for entry in delta["bodies"]} == {target.id}, (
        "再表示したことがクライアントへ届いていない"
    )
    entry = delta["bodies"][0]
    assert entry["visible"] is True
    assert entry["geometry"] is not None, "戻したのに形状が付いてこない"


def test_undo_and_redo_of_set_visible_are_delivered_as_deltas(tmp_path):
    """`set_visible` の undo / redo も `delta()` で届く。

    版を「verbを適用したとき」だけ上げる実装（`_undo_set_visible` が `previous` に戻すだけ）
    でも、他の可視性テストは全部緑になる。その場合クライアントは隠したままの版を持ち続け、
    **undoしても画面から消えたまま**になる（前述の
    「再表示が届かない」問題と同型で、経路がundo/redoなだけ）。

    経路を1つずつ数え上げるのではなく、**版を状態から導く**実装にすること
    （`body_revision` のdocstring参照）。そうすれば新しい経路が増えても取りこぼさない。
    """
    session, bodies = _shelf_parts(tmp_path)
    target = bodies[0]
    set_visible(session.document, target.id, False)
    hidden_revisions = {
        body.id: session.body_revision(body.id) for body in session.document.bodies
    }

    session.document.log.undo(session.document)  # 非表示を取り消す＝画面へ戻す
    undo_delta = session.delta(hidden_revisions)

    assert {entry["body_id"] for entry in undo_delta["bodies"]} == {target.id}, (
        "set_visible のundoがクライアントへ届いていない"
    )
    assert undo_delta["bodies"][0]["visible"] is True
    assert undo_delta["bodies"][0]["geometry"] is not None

    shown_revisions = {
        body.id: session.body_revision(body.id) for body in session.document.bodies
    }
    session.document.log.redo(session.document)  # もう一度隠す
    redo_delta = session.delta(shown_revisions)

    assert {entry["body_id"] for entry in redo_delta["bodies"]} == {target.id}, (
        "set_visible のredoがクライアントへ届いていない"
    )
    assert redo_delta["bodies"][0]["visible"] is False


def test_showing_a_body_again_does_not_retessellate_it(tmp_path, monkeypatch):
    """可視性を戻しても測り直さない(テセレーションの再利用は**形状**で決まる)。

    版は可視性でも上がる(上のテスト)が、キャッシュを版で無効化すると、表示を切り替える
    たびにPCケース全体で892.5msを払うことになる。版と再利用の判定は別物である。
    """
    session, bodies = _shelf_parts(tmp_path)
    target = bodies[0]
    before = session.geometry(target.id)

    set_visible(session.document, target.id, False)
    set_visible(session.document, target.id, True)
    calls = _spy_on_tessellate(monkeypatch)

    assert session.geometry(target.id) is before, "可視性を戻しただけで測り直している"
    assert calls == []


def test_undo_resends_the_restored_body(tmp_path):
    """undoで形状が戻ったBodyも `delta()` で再送される。

    差分送信を押し出し方向にしか作っていないと、undoしても画面が押し出し後のまま残る
    (M3で実際に起きた「全部緑のまま壊れていた経路」と同型)。
    """
    session, bodies = _shelf_parts(tmp_path)
    target = bodies[0]
    _extrude_face(session, target)
    after_extrude = {
        body.id: session.body_revision(body.id) for body in session.document.bodies
    }
    geometry_after_extrude = session.geometry(target.id)

    session.undo()
    delta = session.delta(after_extrude)

    assert {entry["body_id"] for entry in delta["bodies"]} == {target.id}, (
        "undoで戻ったBodyが再送されていない"
    )
    assert session.geometry(target.id) is not geometry_after_extrude, (
        "undoしたのに押し出し後のテセレーションを使い回している"
    )


# --------------------------------------------------------------------------------------
# 項番12: 変更のないBodyのテセレーション結果が再利用される
# --------------------------------------------------------------------------------------
def test_unchanged_bodies_reuse_their_tessellation(tmp_path):
    """押していないBodyの `geometry()` は**前と同じオブジェクト**を返す。

    「同じ内容を作り直して返す」では再利用にならない(費用は変わらない)ので、
    同一性(`is`)で縛る。押したBody側は新しい結果になる。
    """
    session, bodies = _shelf_parts(tmp_path)
    target, other = bodies[0], bodies[1]
    target_geometry_before = session.geometry(target.id)
    other_geometry_before = session.geometry(other.id)

    _extrude_face(session, target)

    assert session.geometry(other.id) is other_geometry_before, (
        "変更していないBodyを測り直している"
    )
    assert session.geometry(target.id) is not target_geometry_before, (
        "押したBodyは測り直すこと"
    )


def test_tessellation_is_not_recomputed_for_unchanged_bodies(tmp_path, monkeypatch):
    """押し出し1回で `kernel.tessellate` が呼ばれるのは1回だけ(押したBody分)。

    同一性の検査(上)はキャッシュの持ち方に依存するので、**実際に測り直した回数**でも縛る。
    17部品を毎回測り直す実装はここで落ちる。
    """
    session, bodies = _shelf_parts(tmp_path)
    target = bodies[0]
    face_index = first_extrudable_face(target.shape)
    known = {
        body.id: session.body_revision(body.id) for body in session.document.bodies
    }
    session.state()  # 初回の全テセレーションを済ませておく

    calls = _spy_on_tessellate(monkeypatch)

    _extrude_face(session, target, face_index)
    session.delta(known)

    assert len(calls) == 1, (
        f"テセレーションが{len(calls)}回走った(期待1回)。"
        "変更のないBodyまで測り直している"
    )


# --------------------------------------------------------------------------------------
# 項番13: 性能の回帰(緩い閾値・数値を必ず出す)
# --------------------------------------------------------------------------------------
def test_extruding_one_part_of_the_pc_case_stays_under_1000ms():
    """PCケースの**1部品**(面数最大=233面)の押し出しが1000ms未満で終わる。

    **【無人drainへの注意】このテストはマシンが混むと閾値を超えて赤になる。**
    単独5回で 942 / 961 / 973 / 1076 / 1164 ms と振れた実測がある(コードは無変更)。
    **この1本だけが赤いなら性能回帰ではなくマシンの負荷なので、コードを直そうとしないこと。**
    他のテストが緑ならタスクを failed/ へ送らず、報告に「この1本のみ負荷由来で赤」と書いて進める。
    扱い(最小値採用 / -m perf で分離 / 比に変える / 廃止)は本人判断で保留中。

    分解前は 4326–5101ms(実機計測。
    `Session.extrude` 全体の時間)。分解後の実測は
    面1 726.0ms / 面2 517.7ms / 面3 757.9ms(実測)。

    **計測の区間**: ここで測るのは verb `extrude_face` の呼び出し全体である。上の
    分解後の実測値は `kernel.extrude_face_on_shape` 単体のものだが、verbが足すのは
    ログ1行とスナップショットの記録だけで、差はOCCT演算に対して無視できる
    (分解前の実機計測も verb 全体なので、比較の対象は揃っている)。

    閾値1000msは分解前に対して4〜5倍の余裕を取った値だが、**実測726msに対しては
    1.4倍しかない**。落ちたときは「閾値が厳しすぎる」ではなく、この実測値と比べて
    どれだけ悪化したかを見ること(合否より数値の推移が重要)。
    """
    doc = Document()
    import_step(doc, str(PC_CASE))
    assert len(doc.bodies) == 27, "分解できていないと1部品の押し出しを測れない"

    face_counts = [kernel.face_count(body.shape) for body in doc.bodies]
    worst = doc.bodies[face_counts.index(max(face_counts))]
    assert max(face_counts) == 233, "面数最大のソリッドは233面(2026-08-11実測)"

    edit_id = make_editable_copy(doc, worst.id)
    editable = next(body for body in doc.bodies if body.id == edit_id)

    elapsed_ms = None
    used_face = None
    for index in range(1, kernel.face_count(editable.shape) + 1):
        started = time.perf_counter()
        try:
            extrude_face(doc, index, EXTRUDE_MM, body_id=edit_id)
        except ValueError:
            continue  # 曲面など押せない面は飛ばす(失敗は文書に何も残さない契約)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        used_face = index
        break

    assert elapsed_ms is not None, "233面のソリッドに押し出せる面が1つも無い"
    _report(
        f"[M3.5 性能] PCケース1部品(233面)の extrude_face: {elapsed_ms:.1f}ms "
        f"(face_index={used_face}, 閾値{KERNEL_BUDGET_MS:.0f}ms, "
        f"分解前の実機実測4326-5101ms, 2026-08-11の実測726.0ms)"
    )
    assert elapsed_ms < KERNEL_BUDGET_MS, (
        f"1部品の押し出しに{elapsed_ms:.1f}ms かかった(閾値{KERNEL_BUDGET_MS:.0f}ms)。"
        "2026-08-11の実測は726.0ms"
    )


def test_pc_case_part_sizes_stay_within_the_measured_spread():
    """PCケースの1部品あたりの面数が 最大233・中央94 のまま(分解の粒度の回帰)。

    ここが跳ね上がっていれば、上の押し出し時間も一緒に悪化する。時間の閾値だけを見ていると
    「遅くなった」しか分からないので、原因側(部品の粒度)も一緒に固定する。
    """
    doc = Document()
    import_step(doc, str(PC_CASE))
    counts = sorted(kernel.face_count(body.shape) for body in doc.bodies)

    _report(
        f"[M3.5 性能] PCケースの部品: {len(counts)}個 / 全{sum(counts)}面 / "
        f"最大{max(counts)}・中央{statistics.median(counts)}・最小{min(counts)}面"
    )
    assert max(counts) == 233
    assert statistics.median(counts) == 94


# --------------------------------------------------------------------------------------
# 項番14: 再送データ量が全Body分より明確に小さい
# --------------------------------------------------------------------------------------
def test_the_delta_after_an_extrusion_is_much_smaller_than_the_full_state(tmp_path):
    """押し出し後の差分のJSONが、全Body分の1/4未満になる。

    実測: 全Body分 6,003,765バイト、1Bodyで最大のもの 600,764バイト(10.0%)。
    面数最大の233面Bodyでも 433,768バイト(7.2%)。閾値1/4は最悪値の2.5倍の余裕。
    **面数最大のBodyを押す**ことで、最悪ケースを測っている。
    """
    session = _session(PC_CASE, tmp_path)
    bodies = _editable_bodies(session)
    assert len(bodies) == 27

    face_counts = [kernel.face_count(body.shape) for body in bodies]
    target = bodies[face_counts.index(max(face_counts))]
    known = {
        body.id: session.body_revision(body.id) for body in session.document.bodies
    }

    _extrude_face(session, target)

    full_bytes = len(json.dumps(session.state(), ensure_ascii=False))
    delta_bytes = len(json.dumps(session.delta(known), ensure_ascii=False))

    _report(
        f"[M3.5 送信量] 全Body {full_bytes}B / 差分 {delta_bytes}B "
        f"= {delta_bytes / full_bytes:.1%}(閾値 {DELTA_SIZE_RATIO:.0%}未満, "
        f"2026-08-11の実測は最悪10.0%)"
    )
    assert delta_bytes < full_bytes * DELTA_SIZE_RATIO, (
        f"差分が{delta_bytes}B、全Body分が{full_bytes}B "
        f"({delta_bytes / full_bytes:.1%})。1部品しか変えていないのに大きすぎる"
    )
