"""丸めの曲がり角が B-spline の区間になった形(Fusion の STEP と同じ作り)の面の削除。docs/FACE_EDIT.md「Fusion の STEP の丸めが消えない」。"""
from __future__ import annotations

import pytest
from OCP.BRep import BRep_Tool
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepTools import BRepTools_ReShape
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS

from duocad import brep_checks, kernel_face_edit, kernel_worker, query
from duocad import brep_topology as _brep_topology
from duocad.document import Body
from session_support import select

W, D, H = 40.0, 30.0, 20.0
SIDE_RADIUS, RIM_RADIUS = 5.0, 2.0


def _edges(shape) -> list:
    edges = _brep_topology.indexed_map(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
    return [TopoDS.Edge_s(edges.FindKey(i)) for i in range(1, edges.Extent() + 1)]


def _filleted(shape, radius: float, keep) -> object:
    maker = BRepFilletAPI_MakeFillet(shape)
    for edge in _edges(shape):
        if keep([tuple(BRep_Tool.Pnt_s(v).XYZ().Coord()) for v in (TopExp.FirstVertex_s(edge), TopExp.LastVertex_s(edge))]):
            maker.Add(radius, edge)
    maker.Build()
    return maker.Shape()


@pytest.fixture(scope="module")
def corners():
    """縦の4辺を r5、上の縁を一周 r2 で丸め、上の縁の丸めの角の区間(トーラス)を同じ形の B-spline に置き換えた箱。"""
    box = BRepPrimAPI_MakeBox(W, D, H).Shape()
    sides = _filleted(box, SIDE_RADIUS, lambda ends: abs(ends[0][0] - ends[1][0]) < 1e-6 and abs(ends[0][1] - ends[1][1]) < 1e-6)
    rimmed = _filleted(sides, RIM_RADIUS, lambda ends: all(abs(p[2] - H) < 1e-6 for p in ends))
    faces = _brep_topology._face_map(rimmed)
    reshape = BRepTools_ReShape()
    for described in query.describe_faces(rimmed):
        if described["kind"] == "torus":
            face = _brep_topology._face_at(faces, described["index"])
            reshape.Replace(face, kernel_face_edit._laid_on(face, *kernel_face_edit._as_bspline(face)))
    shape = reshape.Apply(rimmed)
    assert brep_checks.is_valid(shape)
    return shape


def _cylinders(shape, radius: float) -> list:
    faces = _brep_topology._face_map(shape)
    found = []
    for index in range(1, faces.Extent() + 1):
        definition = kernel_face_edit._describe(_brep_topology._face_at(faces, index))
        if definition is not None and definition[0] == "cylinder" and definition[3] == pytest.approx(radius):
            found.append(index)
    return found


def _b_splines(shape) -> list:
    return [face["index"] for face in query.describe_faces(shape) if face["kind"] == "bspline"]


def test_one_click_on_a_rim_blend_takes_its_b_spline_corner_pieces(corners):
    """落ちたら、丸めの1クリックが曲がり角の B-spline の区間で止まり、そのままでは閉じない(本人のカバーの手前上の縁と同じ)。"""
    corner_pieces = set(_b_splines(corners))
    for straight in _cylinders(corners, RIM_RADIUS):
        chain = kernel_face_edit.blend_chain(corners, straight)
        assert straight in chain and len(chain) == 3 and set(chain) - {straight} <= corner_pieces


def test_the_rim_blend_alone_is_refused_but_its_chain_is_deleted(corners):
    straight = _cylinders(corners, RIM_RADIUS)[0]
    with pytest.raises(kernel_worker.Refused):
        kernel_face_edit.delete_faces(corners, [straight])

    result = kernel_face_edit.delete_faces(corners, kernel_face_edit.blend_chain(corners, straight))

    assert query.volume(result["shape"]) > query.volume(corners)


def test_a_blend_of_another_radius_is_not_taken_into_the_chain(corners):
    """落ちたら、縦の r5 の丸めの1クリックが、それに乗った上の縁の r2 の角の区間(別の丸め)まで選ぶ。"""
    for side in _cylinders(corners, SIDE_RADIUS):
        assert kernel_face_edit.blend_chain(corners, side) == [side]


def test_a_refused_deletion_is_offered_the_range_that_closes(corners):
    straight = _cylinders(corners, RIM_RADIUS)[0]
    chain = kernel_face_edit.blend_chain(corners, straight)

    assert next(kernel_face_edit.deletion_candidates(corners, [straight])) == chain
    assert kernel_face_edit.wider_deletion(corners, [straight]) == chain


def test_the_preview_shows_the_wider_range_and_the_commit_deletes_only_what_is_selected(session, monkeypatch, corners):
    """落ちたら、プレビューで断られた選択に代わりの範囲が付かないか、確定・再生が自分で範囲を広げている(記録は選んだ面だけ)。"""
    body = Body(id="cover", name="cover", kind="solid", shape=corners)
    session.document.add_body(body)
    straight = _cylinders(corners, RIM_RADIUS)[0]
    chain = kernel_face_edit.blend_chain(corners, straight)
    select(session, [{"body": "cover", "kind": "face", "index": straight}])

    job = session.preview_job("delete_faces")
    preview = job.run()
    job.adopt()

    assert preview["suggestion"] == {"body_id": "cover", "faces": chain}
    assert preview["bodies"][0]["volume"] > query.volume(corners)
    with pytest.raises(kernel_worker.Refused):
        session.apply("delete_faces")
    assert body.shape is corners

    select(session, [{"body": "cover", "kind": "face", "index": index} for index in chain])
    monkeypatch.setattr(kernel_worker, "run", lambda *args, **kwargs: pytest.fail("確定がプレビューで計算した結果を使わなかった"))
    session.apply("delete_faces")

    assert session.document.log.entries[-1].args["faces"] == chain
    assert query.volume(body.shape) == pytest.approx(preview["bodies"][0]["volume"])


def test_a_preview_that_times_out_is_not_widened(session, monkeypatch, corners):
    """落ちたら、時間切れ(形の失敗ではない)を理由に範囲を広げている。"""
    session.document.add_body(Body(id="cover", name="cover", kind="solid", shape=corners))
    straight = _cylinders(corners, RIM_RADIUS)[0]
    select(session, [{"body": "cover", "kind": "face", "index": straight}])

    def slow(verb, shape, arguments, *, timeout, op="face_edit"):
        raise ValueError(f"{verb}: {timeout:.0f} 秒以内に返事が無かった")

    monkeypatch.setattr(kernel_worker, "run", slow)
    monkeypatch.setattr(kernel_face_edit, "wider_deletion", lambda *args: pytest.fail("時間切れで範囲を探した"))
    with pytest.raises(ValueError, match="秒以内に返事が無かった"):
        session.preview_job("delete_faces").run()


def test_the_search_moves_past_refused_and_slow_ranges_but_stops_on_a_broken_worker(monkeypatch):
    """落ちたら、代案探しが時間切れの候補で止まるか、子の異常を握りつぶして次の候補を試し続ける。"""
    box = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    monkeypatch.setattr(kernel_face_edit, "deletion_candidates", lambda shape, faces: iter([[1, 2], [1, 3], [1, 4]]))
    tried = []

    def run(verb, shape, arguments, *, timeout, op="face_edit"):
        tried.append(arguments["faces"])
        failure = failures.get(tuple(arguments["faces"]))
        if failure is not None:
            raise failure
        return {"shape": shape, "provenance": {}}

    monkeypatch.setattr(kernel_worker, "run", run)
    failures = {(1, 2): kernel_worker.Refused("refused"), (1, 3): kernel_worker.TimedOut("slow")}
    assert kernel_face_edit.wider_deletion(box, [1]) == [1, 4]

    tried.clear()
    failures = {(1, 2): ValueError("delete_faces: the child died")}
    with pytest.raises(ValueError, match="child died"):
        kernel_face_edit.wider_deletion(BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), [1])
    assert tried == [[1, 2]]
