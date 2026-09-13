"""形状最適化の**配線**(`duocad/optimize_run.py`)の契約。

ここで固定するのは、**外れても例外が出ないまま静かに間違う**ところと、**入口の断り方**である。
端から端まで実際に SIMP を回す受入検査は `topopt_checks/check_optimize_wiring.py`
(約20秒。pytest には入れない —— `topopt_checks/_bootstrap.py` と同じ理由)。

1. **面の対応** —— 条件面が複数のボディに散っていると `kernel.make_compound` で1つにまとめる。
   面添字は `face_count` の累積だけずれるが、**ずれても例外は出ない**(荷重が別の面に入り、
   最適化はもっともらしく収束する)。だから対応を測り直して確かめる層があり、
   ここではその**検出力**(わざとずらすと落ちる)まで見る。
2. **入口の断り** —— 条件が足りない/この版で扱えない/メッシュボディを指した、を名指しする。
3. **メッシュボディ** —— 保持・除外に指定できない。ただし
   **過去のログの再生は落とさない**(門は verb の入口だけで、`_apply_bodies` には無い)。
4. **解像度** —— 上限は「最小部材幅からの要求」と「条件面からの要求」の min。
5. **座標系** —— 結果メッシュは要素添字空間で来るので、ワールド[mm]へ直す。
6. **中断** —— 旗が立っていれば文書を1ビットも変えずに降りる。
7. **`safety_loop` の穴** —— 本番解像度で条件面が空のとき、生の `AttributeError` ではなく
   `ConditionFacesEmptyError` になる(`20260904-1556` の契約④が名指ししていた未修正の穴)。
8. **import の費用** —— `import duocad` で scipy / skimage が来ない。
9. **面アンカーの門** —— 比べる相手は面の**面積重心**であって、条件に焼き込んだ
   `Face.center()` ではない。取り違えると曲面の条件面で解像度を上げるほど厳しくなる。
"""
from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import pytest

from duocad import optimize_run
from duocad.server import Session

REPO_ROOT = Path(__file__).resolve().parent.parent
DEVIATION = 0.2


# --------------------------------------------------------------------------------------
# 題材(20mm立方体を並べるだけ。OCCT を1回も余計に回さない)
# --------------------------------------------------------------------------------------
@pytest.fixture()
def session() -> Session:
    return Session(None, DEVIATION)


def _select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(refs, [])


def _box(session: Session, width: float, depth: float, height: float, corner=(0.0, 0.0)):
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, list(corner), width, depth)
    regions = len(session.state_meta()["sketches"][-1]["regions"])
    _select(session, [{"sketch": sketch_id, "index": regions}])
    session.extrude(height, mode="new")
    session.clear_selection()
    return session.document.bodies[-1]


def _face_with_normal(body, nx: float, ny: float, nz: float) -> int:
    from duocad import query

    for face in query.describe_faces(body.shape):
        normal = face["normal"]
        if normal[0] * nx + normal[1] * ny + normal[2] * nz > 0.9:
            return int(face["index"])
    raise AssertionError("そのような法線の面が無い")


def _two_pads(session: Session):
    """離れた2つのパッド。**条件面が別々のボディに載る**(compound の経路を必ず通る)。"""
    left = _box(session, 10.0, 40.0, 40.0)
    right = _box(session, 10.0, 40.0, 10.0, corner=(60.0, 0.0))
    _select(session, [{"body": right.id, "kind": "body"}])
    session.move_body(0.0, 0.0, 30.0)
    session.clear_selection()
    right = session.document.get_body(right.id)
    return left, right


def _setup_four_conditions(session: Session, magnitude: float = 500.0):
    left, right = _two_pads(session)
    fixed = _face_with_normal(left, -1.0, 0.0, 0.0)
    load = _face_with_normal(right, 0.0, 0.0, 1.0)
    _select(
        session,
        [{"body": left.id, "kind": "body"}, {"body": right.id, "kind": "body"}],
    )
    session.set_preserve_bodies()
    _select(session, [{"body": left.id, "kind": "face", "index": fixed}])
    session.set_fixtures()
    _select(session, [{"body": right.id, "kind": "face", "index": load}])
    session.set_loads(magnitude, [0.0, 0.0, -1.0])
    session.clear_selection()
    return left, right, fixed, load


# --------------------------------------------------------------------------------------
# 1. 入口の断り
# --------------------------------------------------------------------------------------
def test_conditions_report_names_every_missing_piece(session: Session) -> None:
    report = optimize_run.conditions_report(session.document)
    assert report["ready"] is False
    joined = "。".join(report["problems"])
    assert "保持ソリッド" in joined
    assert "拘束面" in joined
    assert "荷重面" in joined


def test_conditions_report_is_ready_once_all_four_are_set(session: Session) -> None:
    _setup_four_conditions(session)
    report = optimize_run.conditions_report(session.document)
    assert report["ready"] is True
    assert report["problems"] == []
    assert report["load_total_n"] == 500.0
    assert report["loads"][0]["vector"] == [0.0, 0.0, -1.0]


def test_two_fixture_faces_are_accepted(session: Session) -> None:
    """拘束面は**何枚でもよい**(拘束する自由度の和集合を取るだけで、力学的な曖昧さが無い)。

    落ちるなら `Conditions.fixed_faces` から下のどこかが1枚しか受けていない。
    """
    left, right, fixed, load = _setup_four_conditions(session)
    other = _face_with_normal(left, 1.0, 0.0, 0.0)
    _select(
        session,
        [
            {"body": left.id, "kind": "face", "index": fixed},
            {"body": left.id, "kind": "face", "index": other},
        ],
    )
    session.set_fixtures()
    session.clear_selection()

    report = optimize_run.conditions_report(session.document)
    assert report["ready"] is True, report["problems"]
    bundle = optimize_run.build_conditions(session.document)
    assert len(bundle.fixed_faces) == 2
    assert bundle.conditions.fixed_faces == bundle.fixed_faces


def test_stale_condition_face_blocks_the_run(session: Session) -> None:
    """**指紋が合わない条件では走らせない。** 別の面へ荷重が入るのを黙って通さない。"""
    _setup_four_conditions(session)
    # 荷重面の指紋を人為的に壊す(形を変えるのと同じ効果。OCCTを回さない)。
    session.document.optimize.loads[0]["area"] *= 2.0
    report = optimize_run.conditions_report(session.document)
    assert report["ready"] is False
    assert any("今の形と合わない" in problem for problem in report["problems"])
    with pytest.raises(optimize_run.OptimizeInputError, match="今の形と合わない"):
        optimize_run.build_conditions(session.document)


# --------------------------------------------------------------------------------------
# 2. メッシュボディ
# --------------------------------------------------------------------------------------
def _mesh_body(session: Session) -> str:
    positions = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    indices = [0, 2, 1, 0, 1, 3, 1, 2, 3, 2, 0, 3]
    return session.add_mesh_body(positions, indices)["body_id"]


def test_mesh_body_cannot_be_a_preserve_or_exclude_solid(session: Session) -> None:
    body_id = _mesh_body(session)
    for verb in ("set_preserve_bodies", "set_exclude_bodies"):
        _select(session, [{"body": body_id, "kind": "body"}])
        with pytest.raises(ValueError, match="mesh body"):
            getattr(session, verb)()
        session.clear_selection()
    # **文書にもログにも何も残らない**(失敗したverbは集合を書き換えない)。
    assert session.document.optimize.preserve_bodies == []
    assert session.document.optimize.exclude_bodies == []


def test_mesh_body_left_over_from_a_replayed_log_is_refused_at_run_time(
    session: Session,
) -> None:
    """門は verb の入口にしか無い(再生は落とさない)ので、**走らせる側でも見る**。"""
    body_id = _mesh_body(session)
    _setup_four_conditions(session)
    session.document.optimize.preserve_bodies.append(body_id)  # 再生で入った状態を模す
    report = optimize_run.conditions_report(session.document)
    assert report["ready"] is False
    assert any("mesh body" in problem for problem in report["problems"])


# --------------------------------------------------------------------------------------
# 3. 面の対応(compound の面添字)
# --------------------------------------------------------------------------------------
def test_face_indices_shift_by_the_face_count_of_earlier_bodies(session: Session) -> None:
    from duocad import kernel

    left, right, fixed, load = _setup_four_conditions(session)
    bundle = optimize_run.build_conditions(session.document)

    assert bundle.anchor_body_ids == [left.id, right.id]
    assert bundle.face_offsets[left.id] == 0
    assert bundle.face_offsets[right.id] == kernel.face_count(left.shape)
    assert bundle.fixed_faces == (fixed,)
    assert bundle.load_faces == (kernel.face_count(left.shape) + load,)
    assert bundle.conditions.load_n == 500.0
    # **面ごとの力ベクトルは実単位[N]**(向き × 大きさ)。単位ベクトルではない。
    assert bundle.forces[0] == pytest.approx((0.0, 0.0, -500.0))


def test_two_loads_with_different_directions_become_two_force_vectors(
    session: Session,
) -> None:
    """面ごとに違う向き・大きさが、**面ごとの力ベクトル[N]**として翻訳される。

    落ちるなら、どこかが1本の向きを全ての荷重面へ使い回している。
    """
    left, right, _fixed, load = _setup_four_conditions(session)
    other = _face_with_normal(right, 1.0, 0.0, 0.0)
    _select(session, [{"body": right.id, "kind": "face", "index": other}])
    session.set_loads(20.0, "normal")
    session.clear_selection()

    bundle = optimize_run.build_conditions(session.document)
    assert len(bundle.load_faces) == 2 and len(bundle.forces) == 2
    assert bundle.forces[0] == pytest.approx((0.0, 0.0, -500.0))
    assert bundle.forces[1] == pytest.approx((20.0, 0.0, 0.0))
    # **総和であって合力ではない**(合力なら sqrt(500^2 + 20^2) = 500.4)
    assert bundle.conditions.load_n == pytest.approx(520.0)


def test_opposing_loads_still_have_a_usable_scale(session: Session) -> None:
    """**打ち消し合う荷重で `load_n` が0にならない。**

    合力(ベクトル和)で測っていると、ここが 0 になって応力の実単位換算が壊れる
    (`stress.UnitScale.for_model` が割り算の分母に使う)。大きさの総和なら0にならない。
    """
    left, right, _fixed, load = _setup_four_conditions(session, magnitude=50.0)
    other = _face_with_normal(right, 1.0, 0.0, 0.0)
    _select(session, [{"body": right.id, "kind": "face", "index": other}])
    session.set_loads(50.0, [0.0, 0.0, 1.0])
    session.clear_selection()

    bundle = optimize_run.build_conditions(session.document)
    resultant = sum(vector[2] for vector in bundle.forces)
    assert resultant == pytest.approx(0.0), "この配置は合力が0でなければ意味が無い"
    assert bundle.conditions.load_n == pytest.approx(100.0)


def test_the_load_scale_agrees_between_the_two_layers() -> None:
    """`build_conditions` の `load_n` と `stress` 側の測り方が**同じ量**である。

    片方が合力、もう片方が総和になっていると、荷重が1本のあいだは一致するので誰も
    気づかないまま、向きが混ざった日に応力が定数倍ずれる。
    """
    import numpy as np
    from types import SimpleNamespace

    from duocad.topopt import stress

    # 平行な2節点(荷重1本ぶん): 総和と合力は一致する
    parallel = SimpleNamespace(f=np.array([0.0, 0.0, -0.4, 0.0, 0.0, -0.6]))
    assert stress.load_magnitude_total(parallel) == pytest.approx(1.0)
    assert stress.load_resultant(parallel) == pytest.approx(1.0)

    # 打ち消し合う2節点: 合力は0だが総和は1
    opposing = SimpleNamespace(f=np.array([0.0, 0.0, 0.5, 0.0, 0.0, -0.5]))
    assert stress.load_resultant(opposing) == pytest.approx(0.0)
    assert stress.load_magnitude_total(opposing) == pytest.approx(1.0)


def test_a_wrong_offset_is_caught_instead_of_silently_loading_another_face(
    session: Session,
) -> None:
    """**検出力の変異検査。** ずれても例外は出ない経路なので、ここが最後の砦である。"""
    left, right, _fixed, _load = _setup_four_conditions(session)
    anchor_shape, offsets = optimize_run._compound_with_offsets(
        session.document, [left.id, right.id]
    )
    conditions = [
        dict(session.document.optimize.fixtures[0]),
        dict(session.document.optimize.loads[0]),
    ]
    # 正しい対応は通る。
    optimize_run._verify_face_mapping(session.document, anchor_shape, offsets, conditions)
    # 1つずらすと落ちる(**この検査を外すと荷重が別の面に入ったまま最適化が成功する**)。
    broken = dict(offsets)
    broken[right.id] -= 1  # +1 は最後の面で範囲外になる(それも落ちるが、見たいのは取り違え)
    with pytest.raises(optimize_run.FaceMappingError, match="別の面である"):
        optimize_run._verify_face_mapping(
            session.document, anchor_shape, broken, conditions
        )


def test_identical_bodies_are_told_apart_by_the_centroid(session: Session) -> None:
    """**重心を見ないと通ってしまう**配置(同じ形のボディが2つ)を名指しで押さえる。"""
    first = _box(session, 20.0, 20.0, 20.0)
    second = _box(session, 20.0, 20.0, 20.0, corner=(50.0, 0.0))
    anchor_shape, offsets = optimize_run._compound_with_offsets(
        session.document, [first.id, second.id]
    )
    from duocad import query

    index = _face_with_normal(second, 0.0, 0.0, 1.0)
    condition = dict(query.face_fingerprint(second.shape, index), body=second.id, index=index)
    optimize_run._verify_face_mapping(
        session.document, anchor_shape, offsets, [condition]
    )
    # 面積も法線も同じで**重心だけが違う**面へずらす。
    broken = dict(offsets)
    broken[second.id] = 0
    with pytest.raises(optimize_run.FaceMappingError, match="別の面である"):
        optimize_run._verify_face_mapping(
            session.document, anchor_shape, broken, [condition]
        )


# --------------------------------------------------------------------------------------
# 4. 解像度
# --------------------------------------------------------------------------------------
def test_max_element_size_is_the_min_of_the_two_requirements(session: Session) -> None:
    from duocad.topopt import simp

    _setup_four_conditions(session)
    limits = optimize_run.resolution_limits(session.document, rmin_mm=6.0)
    # 条件面からの要求 = min(sqrt(CAD面積)) = sqrt(400) = 20mm
    assert limits["from_condition_faces_mm"] == pytest.approx(20.0, rel=1e-6)
    # 最小部材幅からの要求 = rmin_mm / simp.MIN_RMIN(**床**であって既定値ではない。
    # 2026-09-05に `DEFAULT_RMIN` から分離した — 既定を上げても解像度の要求は動かない)
    assert limits["from_rmin_mm"] == pytest.approx(6.0 / simp.MIN_RMIN)
    assert limits["max_element_size_mm"] == pytest.approx(min(20.0, 6.0 / simp.MIN_RMIN))
    assert limits["default_element_size_mm"] == limits["max_element_size_mm"]
    assert limits["min_element_size_mm"] < limits["max_element_size_mm"]
    assert limits["feasible"] is True


def test_without_rmin_only_the_condition_faces_set_the_upper_bound(session: Session) -> None:
    _setup_four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    assert limits["from_rmin_mm"] is None
    assert limits["max_element_size_mm"] == pytest.approx(20.0, rel=1e-6)


def test_condition_face_requirement_uses_the_same_formula_as_the_probe(
    session: Session,
) -> None:
    """`anchor.AnchorTable.empty_face_diagnosis` の `suggested_max_spacing_mm` と同じ式。

    **2箇所に書いた式は片方だけ直る**ので、値そのものを突き合わせておく。
    """
    _setup_four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    for item in limits["condition_faces"]:
        assert item["suggested_max_spacing_mm"] == pytest.approx(
            math.sqrt(item["cad_area_mm2"])
        )


def test_element_size_outside_the_bounds_is_refused() -> None:
    limits = {"max_element_size_mm": 4.0, "min_element_size_mm": 1.0, "feasible": True}
    assert optimize_run._resolve_element_size(limits, None) == 4.0
    assert optimize_run._resolve_element_size(limits, 2.0) == 2.0
    with pytest.raises(optimize_run.OptimizeInputError, match="上限"):
        optimize_run._resolve_element_size(limits, 8.0)
    with pytest.raises(optimize_run.OptimizeInputError, match="下限"):
        optimize_run._resolve_element_size(limits, 0.5)
    infeasible = {"max_element_size_mm": 1.0, "min_element_size_mm": 4.0, "feasible": False}
    with pytest.raises(optimize_run.OptimizeInputError, match="下回っている"):
        optimize_run._resolve_element_size(infeasible, None)


def test_estimate_adds_the_reference_run_once_not_per_candidate() -> None:
    """**参照 run は探索を通して1回。** 候補ごとの固定費として掛けると系統的に上振れし、
    落とすと下振れする(案5の粗い段は候補ごとだったので、そこを取り違えやすい)。
    """
    from duocad.topopt import design_space as ds

    estimate = optimize_run.estimate_seconds(10_000, max_iter=10, evaluations=4)
    per_run = 10 * 10_000 * optimize_run.SECONDS_PER_ELEMENT_ITERATION
    reference = (
        ds.REFERENCE_MAX_ITER
        * 10_000
        * optimize_run.SECONDS_PER_ELEMENT_ITERATION
    )
    assert estimate["seconds_per_evaluation"] == pytest.approx(per_run)
    assert estimate["seconds_reference"] == pytest.approx(reference)
    assert estimate["seconds_typical"] == pytest.approx(reference + per_run * 4)
    # 候補を増やしても参照は1回ぶんしか増えない(掛け算の中に入っていない)。
    more = optimize_run.estimate_seconds(10_000, max_iter=10, evaluations=8)
    assert more["seconds_typical"] - estimate["seconds_typical"] == pytest.approx(per_run * 4)


def test_material_bounds_need_room_between_the_keep_and_the_capacity() -> None:
    options = optimize_run.OptimizeOptions()
    low, high = optimize_run._material_bounds(options, keep_mm3=100.0, capacity_mm3=10_000.0)
    assert low == pytest.approx(100.0 * optimize_run.MATERIAL_MIN_RATIO)
    assert high == pytest.approx(10_000.0 * optimize_run.MATERIAL_MAX_RATIO)
    with pytest.raises(optimize_run.OptimizeInputError, match="探索範囲が作れない"):
        optimize_run._material_bounds(options, keep_mm3=9_000.0, capacity_mm3=10_000.0)


# --------------------------------------------------------------------------------------
# 5. 座標系(要素添字空間 → ワールド[mm])
# --------------------------------------------------------------------------------------
def test_mesh_vertices_are_converted_to_world_millimetres() -> None:
    from duocad.topopt import voxelize as vx

    grid = vx.Grid(origin=(10.0, -5.0, 2.0), spacing=0.5, shape=(4, 4, 4))
    raw = [[0.0, 0.0, 0.0], [3.0, 3.0, 3.0]]
    flat = optimize_run._mesh_to_world(raw, grid)
    assert flat == pytest.approx([10.0, -5.0, 2.0, 11.5, -3.5, 3.5])
    optimize_run._check_mesh_placement(flat, grid)
    # **変換を落とすと格子の箱の外に出る**(=黙って原点付近へ縮んだメッシュを検出できる)。
    with pytest.raises(optimize_run.FaceMappingError, match="格子の箱の外"):
        optimize_run._check_mesh_placement(
            [value for point in raw for value in point], grid
        )


# --------------------------------------------------------------------------------------
# 6. 中断
# --------------------------------------------------------------------------------------
def test_cancelling_leaves_the_document_untouched(session: Session) -> None:
    """**旗が立っていれば1つも作らずに降りる。** 塊の導出より手前で見るので費用も掛からない。"""
    _setup_four_conditions(session)
    before = [body.id for body in session.document.bodies]
    applied = session.applied_count
    events: list = []
    with pytest.raises(optimize_run.OptimizeCancelled):
        optimize_run.run(
            session.document,
            optimize_run.OptimizeOptions(rmin_mm=6.0),
            progress=events.append,
            should_cancel=lambda: True,
        )
    assert events == []  # 最初の進捗を出す前に降りる
    assert [body.id for body in session.document.bodies] == before
    assert session.applied_count == applied


def test_run_optimization_clears_a_stale_cancel_flag(session: Session) -> None:
    """**旗を畳むのは実行の側**である。畳まないと、前回の中断が次の実行を即死させる。

    実際に走らせずに確かめる —— `run_optimization` は条件が無ければ
    `OptimizeInputError` で落ちるが、そこへ着くのは旗を畳んだ**後**である。
    """
    session.cancel_optimization()
    assert session._optimize_cancel.is_set()
    with pytest.raises(optimize_run.OptimizeInputError):
        session.run_optimization()
    assert not session._optimize_cancel.is_set()


# --------------------------------------------------------------------------------------
# 7. `safety_loop` の穴(`20260904-1556` の契約④が名指ししていた未修正の箇所)
# --------------------------------------------------------------------------------------
def test_safety_loop_turns_empty_condition_faces_into_a_named_exception(monkeypatch) -> None:
    """条件面が空のとき `ConditionFacesEmptyError` になる(`run["result"]` が None のまま
    `.model` を触ると素の `AttributeError` になる経路がある)。
    """
    from duocad.topopt import design_space as ds
    from duocad.topopt import safety_loop

    diagnoses = [{"side": "load", "face_index": 7, "cad_area_mm2": 0.5}]

    monkeypatch.setattr(ds, "derive_from_reference", lambda conditions, **kwargs: object())
    monkeypatch.setattr(
        ds,
        "run_topopt",
        lambda space, **kwargs: {"result": None, "empty_condition_faces": diagnoses},
    )
    with pytest.raises(safety_loop.ConditionFacesEmptyError) as caught:
        safety_loop._derive_and_run(
            conditions=object(),
            material_mm3=100.0,
            allowable_mpa=40.0,
            youngs_modulus_mpa=2000.0,
            max_iter=1,
            force=(0.0, 0.0, -1.0),
            solver=None,
            threads=None,
            rho_min=0.5,
            percentile=99.5,
            derivation_kwargs={},
        )
    assert caught.value.empty_condition_faces == diagnoses
    # **`_evaluate` の分類へ落とさない。** 材料量を振っても格子は変わらないので、
    # `"too_little"` として探索を続けると同じ失敗を評価回数ぶん繰り返すだけになる。
    with pytest.raises(safety_loop.ConditionFacesEmptyError):
        safety_loop._evaluate(
            object(),
            100.0,
            allowable_mpa=40.0,
            youngs_modulus_mpa=2000.0,
            max_iter=1,
            force=(0.0, 0.0, -1.0),
            solver=None,
            threads=None,
            rho_min=0.5,
            percentile=99.5,
            derivation_kwargs={},
        )


def test_safety_loop_passes_the_iteration_callback_through(monkeypatch) -> None:
    """**進捗と中断はここを通る。** 素通しが切れると SIMP の反復で止められなくなる。"""
    from duocad.topopt import design_space as ds
    from duocad.topopt import safety_loop

    seen: dict = {}

    def fake_run_topopt(space, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(ds, "derive_from_reference", lambda conditions, **kwargs: object())
    monkeypatch.setattr(ds, "run_topopt", fake_run_topopt)
    sentinel = object()
    with pytest.raises(RuntimeError, match="stop here"):
        safety_loop._derive_and_run(
            conditions=object(),
            material_mm3=100.0,
            allowable_mpa=40.0,
            youngs_modulus_mpa=2000.0,
            max_iter=1,
            force=(0.0, 0.0, -1.0),
            solver=None,
            threads=None,
            rho_min=0.5,
            percentile=99.5,
            derivation_kwargs={},
            run_progress=sentinel,
        )
    assert seen["progress"] is sentinel
    assert seen["keep_model"] is True  # 応力を測る経路(`20260904-0143` の契約)


def test_the_candidate_material_and_the_reference_reach_the_derivation(monkeypatch) -> None:
    """**導出に本番と同じ材料量と、探索が1回だけ作った参照 ρ を渡す。**

    材料量が届かないと別の量の周りを切り出す(`20260904-1416` の契約)。参照が届かないと
    `derive_from_reference` が材料量ごとに参照を作り直し、SIMP を1回で済ませるという
    連続値保持の利点がそのまま消える。
    """
    from duocad.topopt import design_space as ds
    from duocad.topopt import safety_loop

    seen: dict = {}
    sentinel = object()

    def fake_derive_from_reference(conditions, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(ds, "derive_from_reference", fake_derive_from_reference)
    with pytest.raises(RuntimeError, match="stop here"):
        safety_loop._derive_and_run(
            conditions=object(),
            material_mm3=1234.5,
            allowable_mpa=40.0,
            youngs_modulus_mpa=2000.0,
            max_iter=1,
            force=(0.0, 0.0, -1.0),
            solver=None,
            threads=None,
            rho_min=0.5,
            percentile=99.5,
            derivation_kwargs={"spacing": 2.0},
            reference=sentinel,
        )
    assert seen["material_mm3"] == 1234.5
    assert seen["reference"] is sentinel
    assert seen["spacing"] == 2.0


def test_the_filter_radius_reaches_both_the_derivation_and_the_run(monkeypatch) -> None:
    """**塊の導出と `run_topopt` の両方へ同じ `rmin` を配る。**

    落ちたら、塊が保証した筆の太さと実際に回す太さが食い違い、`run_topopt` の突き合わせが
    素の `ValueError` を上げる —— `_evaluate` はそれを分類しないので探索ごと死ぬ。
    """
    from duocad.topopt import design_space as ds
    from duocad.topopt import safety_loop

    seen: dict = {}

    def fake_derive_from_reference(conditions, **kwargs):
        seen["derive"] = kwargs
        return "塊"

    def fake_run_topopt(space, **kwargs):
        seen["run"] = kwargs
        raise RuntimeError("stop here")

    monkeypatch.setattr(ds, "derive_from_reference", fake_derive_from_reference)
    monkeypatch.setattr(ds, "run_topopt", fake_run_topopt)
    with pytest.raises(RuntimeError, match="stop here"):
        safety_loop._derive_and_run(
            conditions=object(),
            material_mm3=1234.5,
            allowable_mpa=40.0,
            youngs_modulus_mpa=2000.0,
            max_iter=1,
            force=(0.0, 0.0, -1.0),
            solver=None,
            threads=None,
            rho_min=0.5,
            percentile=99.5,
            derivation_kwargs={"spacing": 2.0, "rmin_mm": 1.0},
        )
    assert seen["derive"]["rmin_mm"] == 1.0
    assert seen["run"]["rmin_mm"] == 1.0
    assert seen["run"]["rmin"] is None


def test_the_reference_is_built_once_for_the_whole_search(monkeypatch) -> None:
    """**参照 ρ は探索の最初に1回だけ作る。** 段ごとに作り直すと案5より遅い形になる。

    併せて、切り出し側の引数名(`pick_ratio` など)を参照を作る側の名前へ translate している
    ことを見る —— 素通しにすると `derive` まで流れて `TypeError` になる。**`rmin` は
    落とさない**: 幅つき回廊はその半径で測るので、参照を作る側が知っていなければならない。
    """
    from duocad.topopt import design_space as ds
    from duocad.topopt import safety_loop

    calls: list[dict] = []

    def fake_build_reference(conditions, **kwargs):
        calls.append(kwargs)
        return "参照"

    monkeypatch.setattr(ds, "build_reference", fake_build_reference)
    got = safety_loop._build_reference(
        object(),
        {
            "spacing": 2.0,
            "pick_ratio": 0.2,
            "k_mat": 1.5,
            "rmin": 3.0,
            "reference_max_iter": 7,
        },
        force=(0.0, 0.0, -1.0),
        forces=None,
        solver=None,
        threads=None,
        progress=None,
    )
    assert got == "参照"
    assert len(calls) == 1
    assert calls[0]["spacing"] == 2.0
    assert calls[0]["max_iter"] == 7  # reference_max_iter → max_iter
    assert calls[0]["rmin"] == 3.0
    assert "pick_ratio" not in calls[0]
    assert "k_mat" not in calls[0]
    assert "reference_max_iter" not in calls[0]


def test_the_search_builds_one_reference_and_hands_it_to_every_evaluation(monkeypatch) -> None:
    """**探索1回につき参照1本、それが全段へ届く。**

    `_build_reference` 単体の検査では、`solve_for_safety_factor` が結果を捨てていても
    (= 段ごとに `derive_from_reference` が自分で作り直しても)通ってしまう。ここは
    配線そのものを見る —— 落ちたら「遅くなるだけ」なので、実行検査では気づけない。
    """
    from duocad.topopt import design_space as ds
    from duocad.topopt import safety_loop

    built: list = []
    seen: list = []

    def fake_build_reference(conditions, **kwargs):
        built.append(kwargs)
        return "参照"

    def fake_derive_from_reference(conditions, **kwargs):
        seen.append(kwargs)
        raise RuntimeError("stop after the derivation")

    monkeypatch.setattr(ds, "build_reference", fake_build_reference)
    monkeypatch.setattr(ds, "derive_from_reference", fake_derive_from_reference)
    with pytest.raises(RuntimeError, match="stop after the derivation"):
        safety_loop.solve_for_safety_factor(
            object(),
            target_safety_factor=1.2,
            material_min_mm3=1.0,
            material_max_mm3=100.0,
            derivation_kwargs={"spacing": 2.0},
        )
    assert len(built) == 1, "参照は探索の最初に1回だけ"
    assert seen and all(kw.get("reference") == "参照" for kw in seen)


# --------------------------------------------------------------------------------------
# 8. 許容応力と import の費用
# --------------------------------------------------------------------------------------
def test_allowable_stress_shows_where_the_40mpa_comes_from() -> None:
    from duocad.topopt import safety_loop

    allowable = optimize_run.allowable_stress()
    assert allowable["allowable_mpa"] == pytest.approx(40.0)
    assert allowable["yield_mpa"] == safety_loop.PETG_YIELD_MPA
    assert allowable["print_derate"] == safety_loop.PRINT_DERATE
    assert allowable["yield_mpa"] * allowable["print_derate"] == pytest.approx(
        allowable["allowable_mpa"]
    )


def test_importing_the_session_does_not_pull_in_the_topopt_layer() -> None:
    """`optimize_run` を `session.py` が読むようになっても、**最適化の層は来ない**。

    `duocad/topopt/*` の import は関数の中に置いてある(`done/20260904-1600` の契約②)。
    ここが崩れると pytest 全体が毎回 skimage と `duocad.topopt` の起動費用を払う。

    **`scipy` は見ない** —— `duocad/view.py` が(このタスクの前から)既に読んでいるので、
    scipy の有無ではこの配線の退行を検出できない(2026-09-05実測)。
    """
    code = (
        "import sys; import duocad; import duocad.session; "
        "print(sorted(m for m in sys.modules "
        "if m.startswith('skimage') or m.startswith('duocad.topopt')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout


# --------------------------------------------------------------------------------------
# 9. 開発用のデバッグ表示(`?debug=space` / `?debug=anchors` / `?voxel=1`。20260905-1700)
#
# 端から端まで(本物のSIMPを1回通して塊・節点・密度場が実ブラウザに出る)は
# `web/scripts/smoke-optimize-debug.mjs`。ここで固定するのは、**外れても例外が出ないまま
# 静かに間違う**2点である: (a) セル中心の規約(半要素ずれても絵は出る)、
# (b) 頼んでいない配信が起きない(＝既定の実行に費用が乗らない)。
# --------------------------------------------------------------------------------------
def test_debug_voxels_carry_the_cell_centre_convention() -> None:
    """通し添字はC順(`i*(ny*nz) + j*nz + k`)。**画面はこれを中心へ戻す。**

    ここがずれると、画面のボクセルが半要素ぶん(あるいは丸ごと)動く —— 絵は出るので
    目視では気づけない(`duocad/topopt/mesh.py:169` のセル中心の規約)。
    """
    import numpy as np

    from duocad.topopt import voxelize as vx

    grid = vx.Grid(origin=(1.0, 2.0, 3.0), spacing=0.5, shape=(2, 3, 4))
    mask = np.zeros(grid.shape, dtype=bool)
    mask[0, 0, 0] = True
    mask[1, 2, 3] = True
    payload = optimize_run._debug_voxel_payload("space", "derive", grid, mask, limit=10_000)

    assert payload["kind"] == "space"
    assert payload["stage"] == "derive"
    assert payload["grid"] == {"origin": [1.0, 2.0, 3.0], "spacing": 0.5, "shape": [2, 3, 4]}
    assert payload["elements"] == [0, 1 * (3 * 4) + 2 * 4 + 3]
    assert payload["total"] == 2 and payload["shown"] == 2 and payload["stride"] == 1

    # 画面がやる復元(`viewport.ts` の `buildDebugVoxels`)と同じ算術で中心へ戻す。
    _, ny, nz = grid.shape
    for flat, want in zip(payload["elements"], [(0, 0, 0), (1, 2, 3)]):
        ex, rest = divmod(flat, ny * nz)
        ey, ez = divmod(rest, nz)
        assert (ex, ey, ez) == want
        assert grid.center_of(ex, ey, ez).tolist() == pytest.approx(
            [
                1.0 + 0.5 * (ex + 0.5),
                2.0 + 0.5 * (ey + 0.5),
                3.0 + 0.5 * (ez + 0.5),
            ]
        )


def test_debug_voxels_thin_out_instead_of_shipping_a_million_cubes() -> None:
    """上限を超えたら**軸方向に間引く**。間引いたことが payload から読めること。

    立方体1個 = 12三角形なので、実物の部品の塊(5〜12万要素)をそのまま出すと
    60〜144万三角形になる。**黙って全部送らない**(画面が固まる)、
    **黙って切り捨てない**(「全部見えている」と誤解する)、の両方を守る。
    """
    import numpy as np

    from duocad.topopt import voxelize as vx

    grid = vx.Grid(origin=(0.0, 0.0, 0.0), spacing=1.0, shape=(10, 10, 10))
    mask = np.ones(grid.shape, dtype=bool)
    payload = optimize_run._debug_voxel_payload("space", "final", grid, mask, limit=200)

    assert payload["total"] == 1000
    assert payload["stride"] == 2  # 5*5*5 = 125 <= 200(stride=1 の 1000 は超える)
    assert payload["shown"] == 125
    assert len(payload["elements"]) == 125
    # 間引いても**格子の添字のまま**である(座標を作り直していない)。
    _, ny, nz = grid.shape
    for flat in payload["elements"]:
        ex, rest = divmod(flat, ny * nz)
        ey, ez = divmod(rest, nz)
        assert ex % 2 == 0 and ey % 2 == 0 and ez % 2 == 0


def test_debug_sink_builds_nothing_unless_asked() -> None:
    """**頼まれた種類だけ**を作る。頼まれなければ `numpy` の配列を1本も作らない。

    これが崩れると、フラグを付けていない利用者の実行にデバッグ配信の費用が乗る
    (数十万要素の `argwhere` + JSON 化)。`wants()` が門で、`space()` /
    `anchors()` / `rho()` はその門を必ず先に通る。
    """
    import numpy as np

    from duocad.topopt import voxelize as vx

    grid = vx.Grid(origin=(0.0, 0.0, 0.0), spacing=1.0, shape=(2, 2, 2))

    class _Space:
        def __init__(self) -> None:
            self.grid = grid
            self.mask = np.ones(grid.shape, dtype=bool)
            self.table = None

    events: list = []
    # (a) sink が無い → 何も呼ばれない・例外も出ない
    quiet = optimize_run._DebugSink(None, optimize_run.DEBUG_KINDS)
    assert not quiet.wants("space")
    quiet.space("derive", _Space())
    quiet.rho("final", grid, np.ones(grid.shape))

    # (b) 頼んだ種類だけ通る
    sink = optimize_run._DebugSink(events.append, ["space"])
    assert sink.wants("space") and not sink.wants("rho") and not sink.wants("anchors")
    sink.space("derive", _Space())
    sink.rho("final", grid, np.ones(grid.shape))
    sink.anchors("derive", _Space())
    assert [event["kind"] for event in events] == ["space"]

    # (c) 表が無い塊ではアンカーを配らない(検査が直接組んだ塊。**例外にしない**)
    events.clear()
    both = optimize_run._DebugSink(events.append, ["anchors", "rho"])
    both.anchors("derive", _Space())
    assert events == []
    both.rho("final", grid, np.ones(grid.shape))
    assert [event["kind"] for event in events] == ["rho"]
    assert events[0]["threshold"] == pytest.approx(optimize_run.DEBUG_RHO_THRESHOLD)


# --------------------------------------------------------------------------------------
# 9. 面アンカーの門が「面の上のどこか」と比べている
# --------------------------------------------------------------------------------------
def test_face_centroid_is_the_area_centroid_not_the_geometry_center() -> None:
    """**曲面では両者が大きく離れる。** 離れないなら片方をもう片方で代用してしまっている。"""
    from duocad import query
    from duocad.document import Document
    from duocad.region import region_ref
    from duocad.sketch import add_circle, add_rectangle, base_plane, sketch_create
    from duocad.verbs import extrude, fillet

    doc = Document()
    sketch = sketch_create(doc, base_plane("XY"))
    add_rectangle(doc, sketch, (0.0, 0.0), 20.0, 20.0)
    body_id = extrude(doc, [region_ref(sketch, 1)], 20.0, mode="join").args["body_id"]
    hole = sketch_create(doc, base_plane("XY"))
    add_circle(doc, hole, (10.0, 10.0), 4.0)
    extrude(doc, [region_ref(hole, 1)], 20.0, mode="cut", body_id=body_id)
    circles = [
        edge["index"]
        for edge in query.describe_edges(
            next(b for b in doc.bodies if b.id == body_id).shape
        )
        if edge["kind"] == "circle"
    ]
    fillet(doc, circles[:1], 1.5, body_id=body_id)
    shape = next(b for b in doc.bodies if b.id == body_id).shape

    faces = query.describe_faces(shape)
    curved = next(f["index"] for f in faces if f["kind"] == "torus")
    flat = next(f["index"] for f in faces if f["kind"] == "plane")

    def gap(index: int) -> float:
        centroid = query.face_centroid(shape, index)
        center = query.face_fingerprint(shape, index)["center"]
        return math.dist(centroid, center)

    assert gap(flat) == pytest.approx(0.0, abs=1e-6)
    assert gap(curved) > 1.0


def test_the_anchor_gate_does_not_read_the_baked_center(session: Session) -> None:
    """**門は焼き込んだ `center` を見ない。** 見ていると、曲面の条件面で解像度を上げるほど
    厳しくなり(焼き込みは `CenterOf.GEOMETRY` で面パッチから離れる)、いつか必ず落ちる。
    """
    from duocad.topopt import design_space as ds

    _setup_four_conditions(session)
    bundle = optimize_run.build_conditions(session.document)
    space = ds.derive(bundle.conditions, elements_long_axis=12)

    healthy = optimize_run._check_anchor_centroids(session.document, space)
    assert healthy["load_mm"] <= healthy["limit_mm"]

    # 焼き込みを 1000mm ずらしても判定は動かない(= 読んでいない)。
    for condition in session.document.optimize.loads:
        condition["center"] = [v + 1000.0 for v in condition["center"]]
    after = optimize_run._check_anchor_centroids(session.document, space)
    assert after["load_mm_each"] == pytest.approx(healthy["load_mm_each"])


def test_the_anchor_gate_still_catches_a_face_that_is_far_away(
    session: Session, monkeypatch
) -> None:
    """検出力の確認 —— 面の重心が本当に離れていれば落ちる(門を無力化していない)。"""
    from duocad import query
    from duocad.topopt import design_space as ds

    _setup_four_conditions(session)
    bundle = optimize_run.build_conditions(session.document)
    space = ds.derive(bundle.conditions, elements_long_axis=12)

    real = query.face_centroid

    def far(shape, face_index):
        return [v + 500.0 for v in real(shape, face_index)]

    monkeypatch.setattr(query, "face_centroid", far)
    with pytest.raises(optimize_run.FaceMappingError, match="離れている"):
        optimize_run._check_anchor_centroids(session.document, space)


def test_a_residual_failure_is_classified_as_too_little(monkeypatch) -> None:
    """**残差検算が通らないのは「材料が少なすぎる」側。** 素通しすると探索ごと落ちる。

    hanger 2.4mm の実測でここを踏んだ(材料 5.42e4mm^3 で自由度が 3,623,730 のうち
    43,161 しか残らず残差 2.93e-5)。落ちると、それまでの数時間の評価がすべて無駄になる。
    """
    from duocad.topopt import safety_loop, simp

    def boom(*args, **kwargs):
        raise simp.SolveResidualError("K U = F が解けていない(残差 2.930e-05 > 1e-06)")

    monkeypatch.setattr(safety_loop, "_derive_and_run", boom)
    outcome, payload = safety_loop._evaluate(object(), 54_233.0, derivation_kwargs={})
    assert outcome == "too_little"
    assert isinstance(payload, simp.SolveResidualError)


def test_a_residual_failure_at_the_bound_still_stops_the_search(monkeypatch) -> None:
    """**握りつぶしていない。** 本当の拘束不足なら上端で落ち、原因を連鎖したまま上がる。"""
    from duocad.topopt import design_space as ds
    from duocad.topopt import safety_loop, simp

    monkeypatch.setattr(ds, "build_reference", lambda conditions, **kw: "参照")

    def boom(*args, **kwargs):
        raise simp.SolveResidualError("拘束不足(剛体運動が止まっていない)の可能性が高い")

    monkeypatch.setattr(safety_loop, "_derive_and_run", boom)
    with pytest.raises(safety_loop.SafetyTargetError) as caught:
        safety_loop.solve_for_safety_factor(
            object(),
            target_safety_factor=2.0,
            material_min_mm3=1.0,
            material_max_mm3=100.0,
            derivation_kwargs={},
        )
    assert caught.value.side == "invalid_bound"
    assert caught.value.bound == "material_max_mm3"
    assert isinstance(caught.value.__cause__, simp.SolveResidualError)


# --------------------------------------------------------------------------------------
# 10. 探索器(Regula Falsi + Illinois)—— SIMP を回さずにループの規律だけを見る
# --------------------------------------------------------------------------------------
CURVE = lambda m: 6026.0 * (m / 246337.0) ** 0.75  # noqa: E731  bracket 実測に近い凹な形
BRACKET = dict(material_min_mm3=52786.0, material_max_mm3=246337.0)


def _fake_search(monkeypatch, *, too_little_below=0.0, too_much_between=None):
    """`_derive_and_run` を安全率の式(`CURVE`)に差し替える(SIMP を回さない)。

    `too_little_below` より軽い材料量は `DesignSpaceDisconnectedError`(→ `"too_little"`)、
    `too_much_between=(lo, hi)` の帯は `VolumeTargetError(side="above_capacity")`
    (→ `"too_much"`)にする。**帯は括りの内側に作ること** —— 上端が評価できないと
    `SafetyTargetError(invalid_bound)` になって探索本体に入らない。
    """
    import types

    from duocad.topopt import design_space as ds
    from duocad.topopt import safety_loop, simp

    monkeypatch.setattr(ds, "build_reference", lambda conditions, **kw: "参照")

    def fake_derive_and_run(conditions, material_mm3, **kwargs):
        if material_mm3 < too_little_below:
            raise ds.DesignSpaceDisconnectedError("塊が細すぎる", components=2)
        if too_much_between is not None and too_much_between[0] < material_mm3 < too_much_between[1]:
            raise simp.VolumeTargetError(
                "フィルタの目減りで材料が収まらない",
                material_mm3=material_mm3,
                min_mm3=0.0,
                max_mm3=too_much_between[0],
                shortfall_mm3=material_mm3 - too_much_between[0],
                side="above_capacity",
                element_volume_mm3=1.0,
            )
        space = types.SimpleNamespace(element_count=1000)
        run = {"achieved_material_mm3": material_mm3}
        report = types.SimpleNamespace(safety_factor=CURVE(material_mm3))
        return space, run, report

    monkeypatch.setattr(safety_loop, "_derive_and_run", fake_derive_and_run)
    return safety_loop


def test_the_searcher_keeps_the_contract_and_does_not_evaluate_more_than_bisection(
    monkeypatch,
) -> None:
    """**返すものの契約は探索器に依らない**(目標以上・括りの中・二分より悪くない)。

    落ちたら、補間が二分より悪い答え(重い材料量・目標割れ)を返している。
    """
    safety_loop = _fake_search(monkeypatch)
    results = {}
    for searcher in safety_loop.SEARCHERS:
        results[searcher] = safety_loop.solve_for_safety_factor(
            object(), target_safety_factor=3500.0, derivation_kwargs={}, searcher=searcher, **BRACKET
        )
    for searcher, result in results.items():
        assert result.safety_factor >= 3500.0, searcher
        assert BRACKET["material_min_mm3"] <= result.material_mm3 <= BRACKET["material_max_mm3"]
        assert result.converged, searcher
        assert result.evaluations == len(result.history), searcher
    assert results["illinois"].evaluations <= results["bisection"].evaluations
    # 「悪化しない」は材料量の側で見る —— どちらも幅 rel_tol の括りで止まるので答えはその幅だけ
    # 離れうるが、それを超えて重ければ補間が悪い答えを返している。
    assert results["illinois"].material_mm3 <= results["bisection"].material_mm3 * (
        1.0 + safety_loop.DEFAULT_REL_TOL
    )
    # 補間が実際に使われている(全部フォールバックでも上の assert は通ってしまう)。
    assert any(step.proposed_by == "illinois" for step in results["illinois"].history)
    assert all(
        step.proposed_by in ("bound", "bisection") for step in results["bisection"].history
    )


@pytest.mark.parametrize(
    "outcome,kwargs",
    [
        # **括りの内側で踏ませる。** 下端だけが `too_little` だと、括りに入るときの経路しか
        # 見ないままループ本体の分岐が素通しになる。
        ("too_little", {"too_little_below": 130000.0}),
        ("too_much", {"too_much_between": (120000.0, 240000.0)}),
    ],
)
def test_the_searcher_falls_back_to_bisection_right_after_an_unmeasured_evaluation(
    monkeypatch, outcome, kwargs
) -> None:
    """**安全率が測れなかった直後は補間しない。** 古い端点の安全率で補間すると提案が括りの外へ飛ぶ。

    落ちたら `too_little` / `too_much` の直後に割線法が走っている。
    """
    safety_loop = _fake_search(monkeypatch, **kwargs)
    result = safety_loop.solve_for_safety_factor(
        object(), target_safety_factor=3500.0, derivation_kwargs={}, **BRACKET
    )
    assert any(
        step.outcome == outcome and step.proposed_by != "bound" for step in result.history
    ), f"ループの中で {outcome} を踏む前提: {[(s.outcome, s.proposed_by) for s in result.history]}"
    for previous, step in zip(result.history, result.history[1:]):
        if previous.outcome != "ok":
            assert step.proposed_by == "bisection", step
            assert step.fallback_reason == safety_loop.FALLBACK_UNMEASURED_ENDPOINT, step
    assert result.safety_factor >= 3500.0
    # 括りは壊れていない —— 採用した材料量より軽い評価はどれも目標を満たしていない。
    for step in result.history:
        if step.material_mm3 < result.material_mm3 and step.outcome == "ok":
            assert step.safety_factor < 3500.0, step
