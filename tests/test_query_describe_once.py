"""辺・面の一覧(`query.describe_edges` / `describe_faces`)が、本数に比例した時間で済むことの回帰テスト。"""

from build123d import Box

from duocad import brep_topology, query


def _count_map_builds(monkeypatch, call):
    built = []
    original = brep_topology._subshape_map

    def counting(shape, kind):
        built.append(kind)
        return original(shape, kind)

    monkeypatch.setattr(brep_topology, "_subshape_map", counting)
    call(Box(10, 10, 10).wrapped)
    return built


def test_describe_edges_builds_the_edge_index_once(monkeypatch):
    """落ちたら、辺ごとに索引を作り直して、辺数千本のボディの一覧が数十秒かかる(MCP の述語選択がイベントループを止める)。"""
    assert _count_map_builds(monkeypatch, query.describe_edges) == ["edge"]


def test_describe_faces_builds_the_face_index_once(monkeypatch):
    """落ちたら、面ごとに索引を作り直して、面数千枚のボディの一覧が遅くなる。"""
    assert _count_map_builds(monkeypatch, query.describe_faces) == ["face"]
