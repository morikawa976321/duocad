"""`inspect_solid` の自己干渉カウントが面-面のペアだけを数えることを検証する。

実際の `BOPAlgo_CheckerSI` を自己交差のある形状で動かす検査は、再現性のある小さな
題材を安定に作るのが難しい(実測: 単純な2ソリッドのタッチだけでも `BOPAlgo_CheckerSI` 自体が
`HasWarnings()=True` を返して `inspect_solid` の前段ガードに阻まれることがあり、OCCTのバージョンにも
依存しうる)。ここでは `_count_face_interferences` を、`Indices()` と `Shape().ShapeType()` だけを
持つ最小限のスタブで検査し、「面-面だけを数える」というロジック自体の退行(全ペアを数える/
全ペアを除外する等)を捕まえる。
"""
from duocad.kernel_brep import _count_face_interferences
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX


class _StubPair:
    def __init__(self, indices):
        self._indices = indices

    def Indices(self):
        return self._indices


class _StubDS:
    def __init__(self, types_by_index):
        self._types_by_index = types_by_index

    def Shape(self, index):
        shape_type = self._types_by_index[index]
        return type('StubShape', (), {'ShapeType': lambda self, t=shape_type: t})()


def test_face_face_pair_is_counted():
    ds = _StubDS({1: TopAbs_FACE, 2: TopAbs_FACE})
    assert _count_face_interferences(ds, [_StubPair((1, 2))]) == 1


def test_vertex_edge_and_vertex_vertex_pairs_are_not_counted():
    """落ちたら: 頂点・辺を含むペアを自己干渉として数えてしまっている(このタスクの変更の目的そのもの)。"""
    ds = _StubDS({1: TopAbs_VERTEX, 2: TopAbs_EDGE, 3: TopAbs_VERTEX, 4: TopAbs_VERTEX})
    pairs = [_StubPair((1, 2)), _StubPair((3, 4))]
    assert _count_face_interferences(ds, pairs) == 0


def test_face_paired_with_non_face_is_not_counted():
    """落ちたら: `all(...)` が `any(...)` に後退している(片方だけ面のペアを誤ってカウントする)。"""
    ds = _StubDS({1: TopAbs_FACE, 2: TopAbs_VERTEX, 3: TopAbs_FACE, 4: TopAbs_EDGE})
    pairs = [_StubPair((1, 2)), _StubPair((3, 4))]
    assert _count_face_interferences(ds, pairs) == 0


def test_mixed_pairs_count_only_the_face_face_one():
    """落ちたら: 面-面以外のペアが混ざっていると数え方がずれる(全数一致・全除外どちらの退行も検出する)。"""
    ds = _StubDS({1: TopAbs_FACE, 2: TopAbs_FACE, 3: TopAbs_VERTEX, 4: TopAbs_EDGE, 5: TopAbs_EDGE, 6: TopAbs_EDGE})
    pairs = [_StubPair((1, 2)), _StubPair((3, 4)), _StubPair((5, 6))]
    assert _count_face_interferences(ds, pairs) == 1
