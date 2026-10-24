"""`inspect_solid` の自己干渉カウントが面-面のペアだけを数えること(`_count_face_interferences`)。

スタブで見るのは、実物の `BOPAlgo_CheckerSI` を自己交差のある小さな題材で安定に動かせないため
(実測: 2ソリッドのタッチだけでも `HasWarnings()=True` で `inspect_solid` の前段ガードに阻まれる)。
"""

from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX

from duocad import (
    brep_checks as _brep_checks,
)


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


def test_only_face_face_pairs_are_counted():
    """落ちたら: 頂点・辺を含むペアを数えている(全ペア・片方だけ面のペア)か、面-面まで落としている。"""
    ds = _StubDS({1: TopAbs_FACE, 2: TopAbs_FACE, 3: TopAbs_FACE, 4: TopAbs_VERTEX,
                  5: TopAbs_VERTEX, 6: TopAbs_EDGE})
    pairs = [_StubPair((1, 2)), _StubPair((3, 4)), _StubPair((5, 6))]
    assert _brep_checks._count_face_interferences(ds, pairs) == 1
