"""確定前の幽霊(ブラウザだけで描く `preview: 'local'`)が、コアの確定結果と同じ場所に出る。

幽霊は TS(`entityKinds.ghostCopies` / `ghostArgs` / `viewportTypes.bodyGhostMatrix`)、確定結果は
Python のコアが別々に計算する。落ちたら**プレビューと違うものが出来る**(反復の刻み・回転の符号・
点だけ動かす規則・軸の位置のどれかが片方だけ変わった)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from duocad import sketch
from duocad.region import expanded_entities
from duocad.server import Session
from duocad.verbs import pattern_circular, pattern_rectangular
from node_support import run_node
from session_support import box as _box, select as _select

EVALUATOR = Path(__file__).resolve().parent / "eval_ghosts.mjs"


def _ghost(session: Session, verb: str, args: dict, *, editing=None, bodies=(), probe=()) -> dict:
    """いまの文書と選択に対して、画面が描く幽霊を TS で評価する。"""
    case = {
        "verb": verb,
        "args": args,
        "sketches": session.view.sketches_state(),
        "editingId": editing,
        "selection": session.get_selection(),
        "edges": {body_id: session.edges(body_id) for body_id in bodies},
        "vertices": {body_id: session.vertices(body_id) for body_id in bodies},
        "probe": [list(point) for point in probe],
    }
    (result,) = run_node(EVALUATOR, "幽霊と確定結果の一致", stdin=[case], json_out=True)
    return result


def _canonical(entities: list) -> list:
    """図形の並びを、順序に依らず比べられる形にする(種別と、丸めた params の並び)。"""
    return sorted(
        (item["kind"], tuple(sorted((key, round(value, 9) + 0.0) for key, value in item["params"].items())))
        for item in entities
    )


def _entity(session: Session, sketch_id: str, entity_id: str) -> dict:
    kind = next(item.kind for item in sketch.get_sketch(session.document, sketch_id).entities if item.id == entity_id)
    return {"kind": kind, "params": sketch.entity_params(session.document, sketch_id, entity_id)}


def _pattern_agrees(session: Session, sketch_id: str, source: str, verb: str, args: dict, apply) -> int:
    ghosts = _ghost(session, verb, {"entities": [source], **args}, editing=sketch_id)["sketch"]
    original = _entity(session, sketch_id, source)
    apply()
    assert _canonical(ghosts + [original]) == _canonical(expanded_entities(session.document, sketch_id))
    return len(ghosts)


def test_a_rectangular_pattern_ghost_lands_where_the_copies_do(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    circle = session.add_circle(sketch_id, [0.0, 0.0], 2.0)["entity_id"]
    count = _pattern_agrees(
        session, sketch_id, circle, "pattern_rectangular", {"dx": 10.0, "dy": 8.0, "nx": 3, "ny": 2},
        lambda: pattern_rectangular(session.document, sketch_id, [circle], 10.0, 8.0, 3, 2),
    )
    assert count == 3 * 2 - 1


@pytest.mark.parametrize(
    ("count", "total_angle"),
    [pytest.param(8, 360.0, id="全周は360/count"), pytest.param(4, 90.0, id="全周未満は端点を含む")],
)
def test_a_circular_pattern_ghost_uses_the_same_step_as_the_core(session: Session, count: int, total_angle: float) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    circle = session.add_circle(sketch_id, [0.0, 0.0], 2.0)["entity_id"]
    made = _pattern_agrees(
        session, sketch_id, circle, "pattern_circular",
        {"center": [20.0, 0.0], "count": count, "total_angle": total_angle},
        lambda: pattern_circular(session.document, sketch_id, [circle], (20.0, 0.0), count, total_angle=total_angle),
    )
    assert made == count - 1


def test_a_circular_pattern_of_a_tilted_rectangle_keeps_rectangles(session: Session) -> None:
    """長方形の複製は `angle` で向きを持つ。TS の `rotate` とコアの展開が同じ5値を出す。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    rectangle = sketch.add_rectangle(session.document, sketch_id, (14.0, 0.0), 6.0, 4.0, angle=20.0)
    _pattern_agrees(
        session, sketch_id, rectangle, "pattern_circular", {"center": [0.0, 0.0], "count": 4, "total_angle": 360.0},
        lambda: pattern_circular(session.document, sketch_id, [rectangle], (0.0, 0.0), 4),
    )


def _line(session: Session) -> tuple:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_line(sketch_id, [60.0, 0.0], [80.0, 10.0])["entity_id"]
    return sketch_id, entity_id


@pytest.mark.parametrize(
    ("point", "args"),
    [
        pytest.param("x1", {"dx": 0.0, "dy": 10.0}, id="点だけを並進(反対の端は動かない)"),
        pytest.param(None, {"dx": 3.0, "dy": -4.0}, id="図形まるごとを並進"),
        pytest.param("x1", {"mode": "rotate", "angle": 90.0, "center": [0.0, 0.0]}, id="点だけを回す"),
        pytest.param(None, {"mode": "rotate", "angle": 90.0, "center": [60.0, 0.0]}, id="図形まるごとを回す"),
        pytest.param(None, {"mode": "rotate", "angle": -30.0, "center": [5.0, 5.0]}, id="負の角度"),
    ],
)
def test_a_sketch_move_ghost_is_the_moved_entity(session: Session, point, args: dict) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": point}])
    ghosts = _ghost(session, "move_sketch_entities", {"copy": False, **args}, editing=sketch_id)["sketch"]

    session.move_sketch_entities(**args)
    assert _canonical(ghosts) == _canonical([_entity(session, sketch_id, entity_id)])


def test_a_rotated_rectangle_ghost_is_still_the_same_rectangle(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    rectangle = session.add_rectangle(sketch_id, [0.0, 0.0], 40.0, 30.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": rectangle, "point": None}])
    args = {"mode": "rotate", "angle": 30.0, "center": [0.0, 0.0]}
    ghosts = _ghost(session, "move_sketch_entities", {"copy": False, **args}, editing=sketch_id)["sketch"]

    session.move_sketch_entities(**args)
    assert _canonical(ghosts) == _canonical([_entity(session, sketch_id, rectangle)])


def test_a_picked_centre_is_resolved_to_the_same_point_the_core_uses(session: Session) -> None:
    """中心を既存の点で指す道(`center_entity`)。画面は参照を数値へ解決してから幽霊を組む。"""
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    args = {"mode": "rotate", "angle": 90.0, "center_entity": {"sketch": sketch_id, "entity": entity_id, "point": None}}
    ghosts = _ghost(session, "move_sketch_entities", {"copy": False, **args}, editing=sketch_id)["sketch"]

    session.move_sketch_entities(**args)
    assert _canonical(ghosts) == _canonical([_entity(session, sketch_id, entity_id)])


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"mode": "rotate", "angle": 360.0, "center": [0.0, 0.0]}, id="360度はコアが断る"),
        pytest.param({"mode": "rotate", "angle": 90.0}, id="中心が無い(コアの既定の中心を写さない)"),
    ],
)
def test_no_sketch_ghost_is_drawn_when_the_screen_cannot_know_the_result(session: Session, args: dict) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    assert _ghost(session, "move_sketch_entities", {"copy": False, **args}, editing=sketch_id)["sketch"] == []


def _sorted_points(points) -> list:
    return sorted([round(value, 6) + 0.0 for value in point] for point in points)


def _corners(session: Session, body_id: str) -> list:
    return _sorted_points(item["point"] for item in session.vertices(body_id)["vertices"])


def _straight_edge(session: Session, body_id: str) -> dict:
    return next(item for item in session.edges(body_id)["edges"] if item["kind"] == "line")


@pytest.mark.parametrize(
    "args",
    [
        # 軸は原点を通さない(通すと「軸の位置を落とした実装」でも合う)
        pytest.param({"axis_point": [10.0, 0.0, 0.0], "axis_dir": [0.0, 0.0, 1.0], "angle": 90.0}, id="Z軸・正の角度"),
        pytest.param({"axis_point": [10.0, 0.0, 0.0], "axis_dir": [0.0, 2.0, 0.0], "angle": -90.0}, id="Y軸・負・非単位の向き"),
        pytest.param({"axis_point": [3.0, -2.0, 1.0], "axis_dir": [1.0, 2.0, 3.0], "angle": 37.5}, id="斜めの軸"),
    ],
)
def test_a_body_rotation_ghost_matrix_matches_the_core(session: Session, args: dict) -> None:
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    before = _corners(session, body.id)
    probe = _ghost(session, "move_body", {"mode": "rotate", **args}, probe=before)["probe"]

    session.move_body(mode="rotate", **args)
    after = _corners(session, body.id)
    for turned, moved in zip(_sorted_points(probe), after, strict=True):
        assert turned == pytest.approx(moved, abs=1e-5)


@pytest.mark.parametrize("with_centre", [False, True], ids=["辺を軸に", "辺を軸に・頂点へ寄せる"])
def test_a_picked_axis_edge_is_resolved_to_the_axis_the_core_uses(session: Session, with_centre: bool) -> None:
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    edge = _straight_edge(session, body.id)
    args = {"mode": "rotate", "angle": 90.0, "axis_edge": {"body": body.id, "index": edge["index"]}}
    if with_centre:
        # 軸の辺から離れた頂点(軸の上の頂点では、寄せても寄せなくても同じ回転になる)
        start, end = edge["points"][:3], edge["points"][-3:]
        far = max(
            session.vertices(body.id)["vertices"],
            key=lambda item: min(
                sum((a - b) ** 2 for a, b in zip(item["point"], start)),
                sum((a - b) ** 2 for a, b in zip(item["point"], end)),
            ),
        )
        args["center_vertex"] = {"body": body.id, "index": far["index"]}
    before = _corners(session, body.id)
    probe = _ghost(session, "move_body", args, bodies=[body.id], probe=before)["probe"]

    session.move_body(**args)
    after = _corners(session, body.id)
    for turned, moved in zip(_sorted_points(probe), after, strict=True):
        assert turned == pytest.approx(moved, abs=1e-5)


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"angle": 90.0}, id="軸が無い(重心を近似しない)"),
        pytest.param({"axis_point": [10.0, 0.0, 0.0], "axis_dir": [0.0, 0.0, 0.0], "angle": 90.0}, id="長さ0の軸"),
        pytest.param({"axis_point": [10.0, 0.0, 0.0], "axis_dir": [0.0, 0.0, 1.0], "angle": 360.0}, id="360度"),
    ],
)
def test_no_body_ghost_is_drawn_when_the_rotation_is_unusable(session: Session, args: dict) -> None:
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    assert _ghost(session, "move_body", {"mode": "rotate", **args})["rotation"] is None
