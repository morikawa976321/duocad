"""形状最適化の**配線**(`duocad/optimize_run.py`)の契約。

ここで固定するのは、**外れても例外が出ないまま静かに間違う**ところと、**入口の断り方**である。
端から端まで実際に SIMP を回す受入検査は `topopt_checks/check_optimize_wiring.py`
(約20秒。pytest には入れない —— `topopt_checks/_bootstrap.py` と同じ理由)。

1. **面の対応** —— 条件面が複数のボディに散っていると `kernel.make_compound` で1つにまとめる。
   面添字は `face_count` の累積だけずれるが、**ずれても例外は出ない**(荷重が別の面に入り、
   最適化はもっともらしく収束する)。だから対応を測り直して確かめる層があり、
   ここではその**検出力**(わざとずらすと落ちる)まで見る。
2. **入口の断り** —— 条件が足りない/この版で扱えない/メッシュボディを指した、を名指しする。
3. **メッシュボディ** —— 保持・除外に指定できない(2026-09-05の判断)。ただし
   **過去のログの再生は落とさない**(門は verb の入口だけで、`_apply_bodies` には無い)。
4. **解像度** —— 上限は「最小部材幅からの要求」と「条件面からの要求」の min。
5. **座標系** —— 結果メッシュは要素添字空間で来るので、ワールド[mm]へ直す。
6. **中断** —— 旗が立っていれば文書を1ビットも変えずに降りる。
7. **`safety_loop` の穴** —— 本番解像度で条件面が空のとき、生の `AttributeError` ではなく
   `ConditionFacesEmptyError` になる(`20260904-1556` の契約④が名指ししていた未修正の穴)。
8. **import の費用** —— `import duocad` で scipy / skimage が来ない。
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
    assert report["load"]["magnitude"] == 500.0
    assert report["load"]["vector"] == [0.0, 0.0, -1.0]


def test_two_fixture_faces_are_refused_with_the_reason(session: Session) -> None:
    """**v1は1枚だけ。** `Conditions.fixed_face` が面添字を1つしか持たないからである。"""
    left, right, fixed, _load = _setup_four_conditions(session)
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
    assert report["ready"] is False
    assert any("拘束面が 2 枚ある" in problem for problem in report["problems"])
    with pytest.raises(optimize_run.OptimizeInputError, match="拘束面が 2 枚ある"):
        optimize_run.build_conditions(session.document)


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
    assert bundle.fixed_face == fixed
    assert bundle.load_face == kernel.face_count(left.shape) + load
    assert bundle.conditions.load_n == 500.0
    assert bundle.force == pytest.approx((0.0, 0.0, -1.0))


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


def test_estimate_keeps_the_coarse_stage_in_the_total() -> None:
    """案5の粗い段は**候補ごとの固定費**。落とすと予想時間が実測から系統的にずれる。"""
    estimate = optimize_run.estimate_seconds(10_000, max_iter=10, evaluations=4)
    per_run = (
        optimize_run.COARSE_DERIVE_SECONDS
        + 10 * 10_000 * optimize_run.SECONDS_PER_ELEMENT_ITERATION
    )
    assert estimate["seconds_per_evaluation"] == pytest.approx(per_run)
    assert estimate["seconds_typical"] == pytest.approx(per_run * 4)
    assert estimate["seconds_typical"] > 4 * optimize_run.COARSE_DERIVE_SECONDS


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
    """**修正前は生の `AttributeError`** だった(`run["result"]` が None のまま `.model`)。"""
    from duocad.topopt import design_space as ds
    from duocad.topopt import safety_loop

    diagnoses = [{"side": "load", "face_index": 7, "cad_area_mm2": 0.5}]

    monkeypatch.setattr(ds, "derive_default", lambda conditions, **kwargs: object())
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

    monkeypatch.setattr(ds, "derive_default", lambda conditions, **kwargs: object())
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


def test_the_candidate_material_reaches_the_coarse_stage(monkeypatch) -> None:
    """**粗い段に本番と同じ材料量を渡す**(`20260904-1416` の契約。塞ぐと別の形の周りを膨らませる)。"""
    from duocad.topopt import design_space as ds
    from duocad.topopt import safety_loop

    seen: dict = {}

    def fake_derive_default(conditions, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(ds, "derive_default", fake_derive_default)
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
        )
    assert seen["coarse_material_mm3"] == 1234.5
    assert seen["spacing"] == 2.0


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
    ここが崩れると pytest 875件が毎回 skimage と `duocad.topopt` の起動費用を払う。

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
