"""干渉チェックの組み立て(`duocad/interference.py`。受入条件の幾何の部分)。落ちたら、区分か隙間の判定か対象の決め方が崩れている。"""
from __future__ import annotations

import math

import pytest

from duocad import interference, selection
from interference_support import (
    box, check, cut, cylinder, document_of, filleted_box, nested_boxes, pair_of, plate_with_hole, rotated,
)

A = box(0, 0, 0, 10, 10, 10)


def two(shape_b, clearance=0.0):
    return check(document_of(("a", A), ("b", shape_b)), clearance)


# ---------------------------------------------------------------------------------------------- 受入条件
def test_half_overlapping_boxes_are_an_overlap_with_the_exact_volume():
    result = two(box(5, 0, 0, 10, 10, 10))
    entry = pair_of(result, "a", "b")
    assert entry["relation"] == "overlap"
    assert entry["volume"] == pytest.approx(500.0, rel=1e-9)
    assert entry["thickness"] == pytest.approx(2.5, rel=1e-9)
    assert entry["center"] == pytest.approx([7.5, 5, 5])
    assert result["counts"]["overlap"] == 1 and result["complete"]


def test_separated_boxes_are_not_listed_and_touching_ones_are_contacts():
    gapped = two(box(10.1, 0, 0, 10, 10, 10))
    assert gapped["pairs"] == [] and gapped["counts"]["separated"] == 1
    face = pair_of(two(box(10, 0, 0, 10, 10, 10)), "a", "b")
    assert face["relation"] == "contact" and face["contact"] == "face" and face["area"] == pytest.approx(100.0)
    assert pair_of(two(box(10, 10, 0, 10, 10, 10)), "a", "b")["contact"] == "edge"
    assert pair_of(two(box(10, 10, 10, 10, 10, 10)), "a", "b")["contact"] == "vertex"


def test_with_a_clearance_a_gap_of_a_tenth_is_short_and_exactly_the_clearance_is_at():
    short = pair_of(two(box(10.1, 0, 0, 10, 10, 10), 0.2), "a", "b")
    assert short["relation"] == "separated" and short["clearance"] == "short"
    assert short["distance"] == pytest.approx(0.1, abs=1e-9) and len(short["points"]) == 2
    at = pair_of(two(box(10.2, 0, 0, 10, 10, 10), 0.2), "a", "b")
    assert at["clearance"] == "at"
    ok = two(box(10.3, 0, 0, 10, 10, 10), 0.2)
    assert ok["pairs"] == [] and ok["counts"]["ok"] == 1


def test_touching_pairs_are_short_when_a_clearance_is_asked():
    entry = pair_of(two(box(10, 0, 0, 10, 10, 10), 0.2), "a", "b")
    assert entry["relation"] == "contact" and entry["clearance"] == "short"


def test_three_bodies_list_only_the_pairs_that_need_attention():
    document = document_of(("a", A), ("b", box(5, 0, 0, 10, 10, 10)), ("c", box(15, 0, 0, 10, 10, 10)))
    # a-b 重なり、b-c 面の接触(b は x 5..15、c は 15..25)、a-c 離れている(a は 0..10、c は 15..25)
    result = check(document)
    assert [(p["a"], p["b"], p["relation"]) for p in result["pairs"]] == [("a", "b", "overlap"), ("b", "c", "contact")]
    assert result["counts"]["separated"] == 1 and sum(result["counts"][k] for k in interference._RELATIONS) == 3


# ---------------------------------------------------------------------------------------------- 実測と相談で見つけた形
def test_a_box_inside_a_box_is_an_overlap():
    assert pair_of(two(box(2, 2, 2, 3, 3, 3)), "a", "b")["relation"] == "overlap"


def test_a_flat_overlap_of_a_thousandth_is_tiny_but_a_pin_of_five_hundredths_is_an_overlap():
    assert pair_of(two(box(9.999, 0, 0, 10, 10, 10)), "a", "b")["relation"] == "tiny_overlap"
    pin = check(document_of(("plate", plate_with_hole(5.0)), ("pin", cylinder(0, 0, -5, 5.05, 20))))
    assert pair_of(pin, "plate", "pin")["relation"] == "overlap"


def test_the_order_of_the_pair_does_not_change_the_result():
    forward = pair_of(check(document_of(("a", A), ("b", box(5, 0, 0, 10, 10, 10)))), "a", "b")
    backward = pair_of(check(document_of(("b", box(5, 0, 0, 10, 10, 10)), ("a", A))), "a", "b")
    assert forward["relation"] == backward["relation"]
    assert forward["volume"] == pytest.approx(backward["volume"], rel=1e-12)


def test_a_third_body_crossing_the_overlap_does_not_change_the_pair():
    other = box(5, 0, 0, 10, 10, 10)
    alone = pair_of(check(document_of(("a", A), ("b", other))), "a", "b")
    crossed = pair_of(check(document_of(("a", A), ("b", other), ("c", box(7, -5, 4, 1, 20, 2)))), "a", "b")
    assert crossed["volume"] == pytest.approx(alone["volume"], rel=1e-12)
    assert crossed["thickness"] == pytest.approx(alone["thickness"], rel=1e-12)


def test_a_thin_flat_overlap_joined_to_a_deep_pin_is_an_overlap_not_tiny():
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

    lid = BRepAlgoAPI_Fuse(box(0, 0, 9.999999, 200, 200, 5), cylinder(100, 100, 5, 1.0, 6)).Shape()
    entry = pair_of(check(document_of(("base", box(0, 0, 0, 200, 200, 10)), ("lid", lid))), "base", "lid")
    assert entry["relation"] == "overlap" and entry["thickness"] <= 1e-3 < entry["obb_min"]


def test_a_box_distance_between_the_clearance_and_the_clearance_plus_the_band_is_at():
    entry = pair_of(two(box(10.2 + 5e-7, 0, 0, 10, 10, 10), 0.2), "a", "b")
    assert entry["clearance"] == "at"


def test_the_rounded_core_with_a_concentric_gap_is_at():
    """形全体の最短距離が落ちるかどうかは形の表現(BRep の往復)で変わる。どちらの段で測れても at(上界の印は下の素の値の試験が見る)。"""
    shell, core = nested_boxes(0.2)
    entry = pair_of(check(document_of(("shell", shell), ("core", core)), 0.2), "shell", "core")
    assert entry["relation"] == "separated" and entry["clearance"] == "at"
    assert entry["distance"] == pytest.approx(0.2, abs=1e-6)


def test_a_pin_in_a_big_hole_measures_the_gap_not_the_tessellation_error():
    plate = plate_with_hole(30.1, size=70.0)
    pin = rotated(cylinder(0, 0, -5, 30.0, 20), 3.1)
    entry = pair_of(check(document_of(("plate", plate), ("pin", pin)), 0.2), "plate", "pin")
    assert entry["distance"] == pytest.approx(0.1, abs=1e-6) and entry["clearance"] == "short"


# ---------------------------------------------------------------------------------------------- 引数と対象
@pytest.mark.parametrize("value", [-1, -0.001, math.nan, math.inf, -math.inf, "0.2", None, True])
def test_a_bad_clearance_is_refused(value):
    with pytest.raises(ValueError):
        interference.check_clearance(value)


def test_the_clearance_defaults_to_zero_and_accepts_ints():
    assert interference.check_clearance(0) == 0.0 and interference.check_clearance(0.2) == 0.2


def select(document, *refs):
    selection.set_selection(document, refs=list(refs))


def _targets(document):
    targets = interference.resolve_targets(document)
    return targets.mode, sorted(tuple(pair) for pair in targets.pairs), targets.excluded_hidden


def _four():
    return document_of(("a", A), ("b", box(5, 0, 0, 10, 10, 10)), ("c", box(30, 0, 0, 10, 10, 10)), ("h", box(0, 0, 0, 1, 1, 1)), hidden=("h",))


def test_the_targets_follow_the_visibility_and_the_selection():
    document = _four()
    assert _targets(document) == ("visible_all", [("a", "b"), ("a", "c"), ("b", "c")], 1)
    select(document, selection.select_body(document, "b"))
    assert _targets(document) == ("one_vs_rest", [("a", "b"), ("b", "c")], 1)
    select(document, selection.select_body(document, "b"), selection.select_body(document, "c"))
    assert _targets(document) == ("selected_set", [("b", "c")], 1)
    select(document, selection.select_face(document, "c", 1))
    assert _targets(document)[0] == "one_vs_rest" and interference.resolve_targets(document).selected == ("c",)


def test_a_document_with_one_visible_body_is_refused():
    document = document_of(("a", A), ("h", box(0, 0, 0, 1, 1, 1)), hidden=("h",))
    with pytest.raises(ValueError, match="見えているボディ"):
        interference.resolve_targets(document)
    with pytest.raises(ValueError):
        interference.resolve_targets(document_of(("a", A)))


def test_the_result_carries_the_target_and_the_pair_count():
    document = _four()
    select(document, selection.select_body(document, "a"))
    result = check(document)
    assert result["target"] == {"mode": "one_vs_rest", "selected": ["a"], "bodies": ["a", "b", "c"], "excluded_hidden": 1, "pairs": 2}
    assert result["units"] == "mm" and result["thresholds"] == interference.THRESHOLDS


# ---------------------------------------------------------------------------------------------- 分類(素の値から)
@pytest.mark.parametrize(
    "relation, expected",
    [
        ({"kind": "overlap", "volume": 1.0, "thickness": 0.5, "obb_min": 1.0}, "overlap"),
        ({"kind": "overlap", "volume": 1.0, "thickness": 1e-3, "obb_min": 1e-3}, "tiny_overlap"),
        ({"kind": "overlap", "volume": 1.0, "thickness": 1e-6, "obb_min": 5.0}, "overlap"),
        ({"kind": "overlap", "volume": math.nan, "thickness": 0.5, "obb_min": 1.0}, "unknown"),
        ({"kind": "overlap", "volume": 0.0, "thickness": 0.5, "obb_min": 1.0}, "unknown"),
        ({"kind": "face", "area": 0.0}, "unknown"),
        ({"kind": "face", "area": 3.0}, "contact"),
        ({"kind": "edge"}, "contact"),
        ({"kind": "none"}, "separated"),
        ({"kind": "unknown", "reason": "x"}, "unknown"),
        ({"kind": "something_new"}, "unknown"),
    ],
)
def test_classification_of_raw_values(relation, expected):
    classified = interference.classify_relation(relation)
    assert classified["relation"] == expected
    if expected == "unknown":
        assert classified["reason"]


@pytest.mark.parametrize(
    "relation, distance, clearance, expected",
    [
        ("separated", 0.1, 0.2, "short"), ("separated", 0.2, 0.2, "at"), ("separated", 0.2 + 1e-7, 0.2, "at"),
        ("separated", 0.3, 0.2, "ok"), ("separated", None, 0.2, None), ("contact", None, 0.2, "short"),
        ("overlap", 0.0, 1e-9, "short"), ("unknown", 0.0, 1e-7, "short"), ("unknown", 0.5, 0.2, "ok"), ("unknown", None, 0.2, None),
    ],
)
def test_clearance_rules(relation, distance, clearance, expected):
    assert interference.classify_clearance(relation, distance, clearance) == expected


class FakeWorker:
    """子の代わりに決まった答えを返す。`relations[(a, b)]` / `distances[(a, b)]` が例外なら投げる。"""

    def __init__(self, relations, distances):
        self.relations, self.distances = relations, distances

    def request(self, message, timeout):
        table = self.relations if message["op"] == "relation" else self.distances
        value = table[(message["a"], message["b"])]
        if isinstance(value, Exception):
            raise value
        return value

    def restart(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def fake_run(relations, distances, clearance=0.0, ids=("a", "b", "c")):
    from itertools import combinations

    document = document_of(*[(i, box(n * 3, 0, 0, 10, 10, 10)) for n, i in enumerate(ids)])
    return check(document, clearance, worker_factory=lambda load, cancel: FakeWorker(relations, distances))


def test_a_failed_distance_leaves_the_relation_and_nulls_the_clearance():
    from duocad.interference_worker import WorkerRemoteError

    result = fake_run(
        {("a", "b"): {"kind": "none"}, ("a", "c"): {"kind": "none"}, ("b", "c"): {"kind": "none"}},
        {("a", "b"): {"ok": False, "reason": "測れない"}, ("a", "c"): WorkerRemoteError("boom"), ("b", "c"): {"ok": True, "distance": 5.0, "points": [[0, 0, 0], [5, 0, 0]]}},
        clearance=0.2,
    )
    first, second = pair_of(result, "a", "b"), pair_of(result, "a", "c")
    assert first["relation"] == "separated" and first["clearance"] is None and "測れない" in first["clearance_reason"]
    assert second["relation"] == "separated" and second["clearance"] is None and "boom" in second["clearance_reason"]
    assert pair_of(result, "b", "c") is None
    assert result["counts"]["clearance_null"] == 2 and result["counts"]["ok"] == 1


def test_a_relation_the_fuse_could_not_answer_stays_unknown_but_keeps_a_measured_distance():
    from duocad.interference_worker import WorkerRemoteError

    result = fake_run(
        {("a", "b"): WorkerRemoteError("GF が落ちた"), ("a", "c"): {"kind": "mystery"}, ("b", "c"): {"kind": "none"}},
        {("a", "b"): {"ok": True, "distance": 0.0, "points": [[0, 0, 0], [0, 0, 0]]}, ("a", "c"): {"ok": True, "distance": 3.0, "points": [[0, 0, 0], [3, 0, 0]], "upper_bound": True}, ("b", "c"): {"ok": True, "distance": 9.0, "points": [[0, 0, 0], [9, 0, 0]]}},
        clearance=0.2,
    )
    first = pair_of(result, "a", "b")
    assert first["relation"] == "unknown" and "GF が落ちた" in first["reason"]
    assert first["distance"] == 0.0 and first["clearance"] == "short"
    second = pair_of(result, "a", "c")
    assert second["relation"] == "unknown" and "mystery" in second["reason"] and second["clearance"] == "ok"
    assert second["distance_upper_bound"] is True


def test_the_counts_add_up_to_the_pairs_even_with_unstarted_and_outside_pairs():
    document = document_of(("a", A), ("b", box(5, 0, 0, 10, 10, 10)), ("far", box(500, 0, 0, 10, 10, 10)), ("touch", box(10, 10, 10, 1, 1, 1)))
    result = check(document, 0.2)
    counts = result["counts"]
    assert sum(counts[k] for k in interference._RELATIONS) == result["target"]["pairs"] == 6
    assert counts["short"] + counts["at"] + counts["ok"] + counts["clearance_null"] == 6
    assert counts["ok"] >= 3  # far との3組は箱の段で離れている

    limited = check(document, 0.2, limits=interference.Limits(total_seconds=0.0))
    assert limited["complete"] is False and limited["not_started"] > 0
    assert sum(limited["counts"][k] for k in interference._RELATIONS) == 6


# ---------------------------------------------------------------------------------------------- メッシュ
def test_a_mesh_body_overlapping_a_box_is_reported_with_the_approximation_mark():
    from duocad.document import Body, Document, Mesh

    corners = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]]
    faces = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    scaled = [[x * 10 + 5, y * 10, z * 10] for x, y, z in corners]
    positions = [c for tri in faces for v in tri for c in scaled[v]]
    document = Document()
    document.add_body(Body(id="solid", name="solid", kind="solid", shape=A))
    document.add_body(Body(id="mesh", name="mesh", kind="mesh", mesh=Mesh(positions=tuple(positions), indices=tuple(range(len(faces) * 3)))))
    result = check(document)
    entry = pair_of(result, "solid", "mesh")
    assert entry["relation"] == "overlap" and entry["volume"] == pytest.approx(500.0, rel=1e-6)
    assert entry["approximate"] == interference.APPROXIMATE
    assert result["method"].endswith("+mesh")


def _mesh_cube(x: float):
    from duocad.document import Mesh

    corners = [[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]]
    faces = [[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]]
    scaled = [[cx * 10 + x, cy * 10, cz * 10] for cx, cy, cz in corners]
    positions = [c for tri in faces for v in tri for c in scaled[v]]
    return Mesh(positions=tuple(positions), indices=tuple(range(len(faces) * 3)))


def test_meshes_closer_than_the_band_are_a_contact_even_without_a_clearance():
    """隙間 0 でも、帯 ε(1e-6 mm)より近いメッシュ2体は接触。落ちたら箱の段の幅に ε が無く、離れているとして落としている
    (隙間 0.2 で測ると接触になり、問い方で区分が変わる)。"""
    from duocad.document import Body, Document

    document = Document()
    document.add_body(Body(id="m1", name="m1", kind="mesh", mesh=_mesh_cube(0.0)))
    document.add_body(Body(id="m2", name="m2", kind="mesh", mesh=_mesh_cube(10.0000005)))
    for clearance in (0.0, 0.2):
        entry = pair_of(check(document, clearance), "m1", "m2")
        assert entry is not None and entry["relation"] == "contact", clearance


def test_converting_a_brep_to_a_mesh_does_not_mesh_the_brep():
    """B-rep をメッシュの経路へ変換しても、元の形の面に三角形分割が書き込まれない。落ちたら子プロセスが使い回す形の GF の
    結果が、メッシュを含む組を先に測ったかどうかで変わる。"""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    from duocad import interference_mesh
    from duocad.mesh_geometry import subshapes

    shape = box(0, 0, 0, 10, 10, 10)
    interference_mesh.manifold_of_shape(shape)
    assert all(BRep_Tool.Triangulation_s(TopoDS.Face_s(face), TopLoc_Location()) is None for face in subshapes(shape, TopAbs_FACE))
