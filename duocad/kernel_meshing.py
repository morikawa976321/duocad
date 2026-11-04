"""OCCT の面分割を段階ごとに制御し、面分割の直前に境界折れ線の交差を独自に検査して共有辺を再分割する。

`BRepMesh_IncrementalMesh` は BuildModel → DiscretizeEdges → HealModel → PreProcessModel →
DiscretizeFaces → PostProcessModel を一括で回す(OCCT の `IMeshTools_MeshBuilder::Perform`)。
ここでは同じ部品(`BRepMesh_Context` が組む標準の6部品。OCP には `BRepMesh_Context` 自体が無い)を
`IMeshTools_Context` に組み、**PreProcessModel の後・DiscretizeFaces の前**で `boundary_check` を当てる。
OCCT 自身の `BRepMesh_FaceChecker` は 5 度未満の交差角と `π(2·面偏差)²` 未満の小ループを無視するので、
そこを通り抜けた交差が面分割へ渡り、面の欠落・非多様体の三角形になる(docs/TOPOPT_GAP_CAUSALITY.md)。

面分割は OCCT の既定(`BRepMesh_MeshAlgoFactory`)で行い、境界の点が多い平面だけを Delabella
(`BRepMesh_DelabellaMeshAlgoFactory`)で先に分割し、辺の裏返しで Delaunay に戻す(`_discretize_faces`)。

再分割は OCCT の `BRepMesh_ModelHealer` 内の EdgeAmplifier と同じ手順(辺の偏差を 1/3 にして元の曲線から
点列を作り直し、端点は保ち、全 pcurve へ伝播)。CAD の曲線・曲面・位相は変更しない。

**ハンドルの寿命(実測、落とし穴)**: モデル(`IMeshData_Model`)の面・辺・pcurve のハンドルは、モデルの
アロケータが持つメモリを指す。モデルより長生きしたハンドルは解放時に 0xC0000005 でプロセスごと落ちる
(モジュール変数に残した場合・context を先に捨てた場合に再現。関数内で作って順に捨てれば落ちない)。
ここでは派生ハンドルを `_Model` にだけ持たせ、`close()` で **pcurve → 辺 → 面 → モデル → context** の順に
必ず手放す。この層の外へハンドルを出さない。

`kernel.py` から呼ぶ下位層。`kernel` を import しない(循環参照を作らない)。
"""
from __future__ import annotations

import hashlib
import time
from typing import Dict, List, Optional

import numpy as np

from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepMesh import (
    BRepMesh_DelabellaMeshAlgoFactory,
    BRepMesh_EdgeDiscret,
    BRepMesh_FaceDiscret,
    BRepMesh_MeshAlgoFactory,
    BRepMesh_ModelBuilder,
    BRepMesh_ModelHealer,
    BRepMesh_ModelPostProcessor,
    BRepMesh_ModelPreProcessor,
)
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.IMeshData import IMeshData_Status
from OCP.IMeshTools import IMeshTools_Context, IMeshTools_Parameters
from OCP.Message import Message_ProgressRange
from OCP.Poly import Poly_Triangle
from OCP.Precision import Precision
from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS

from . import boundary_check

from . import (
    brep_topology as _brep_topology,
)


#: 再分割の反復上限の既定(仮置き。docs/TOPOPT_GAP_CAUSALITY.md「案Aの実装」)。
DEFAULT_MAX_ROUNDS = 12
#: 全辺の離散点数の予算。超えたら未解決として止める(メモリの歯止め)。
DEFAULT_MAX_POINTS = 4_000_000

#: 平面の面の境界の点がこの数以上なら Delabella で分割する。OCCT の既定(Watson の逐次挿入)は1面の中で並列にならず、
#: 境界の点 n に対しておよそ n^1.7 で伸びる(実測: 円板 1,257 点 69 ms・2,514 点 347 ms、穴 384 の板の面 13,828 点 1.6 秒。
#: Delabella は同じ順に 9 ms・13 ms・0.04 秒)。少ない面は既定のまま(三角形の並びが既定の分割と同じ)。
DELABELLA_PLANE_POINTS = 1000
#: 辺の裏返しの回数の上限(1回に、三角形を共有しない辺をまとめて裏返す)。届いたらその時点の分割を使う(どの時点でも正しい分割)。
_FLIP_ROUNDS = 10_000
#: Shewchuk, "Adaptive Precision Floating-Point Arithmetic", incircle の iccerrboundA。
_INCIRCLE_BOUND = (10.0 + 96.0 * np.finfo(np.float64).eps) * np.finfo(np.float64).eps

_FAILURE = IMeshData_Status.IMeshData_Failure
_REUSED = IMeshData_Status.IMeshData_Reused
_SELF_INTERSECTING = IMeshData_Status.IMeshData_SelfIntersectingWire
_FORWARD = TopAbs_Orientation.TopAbs_FORWARD
_PLANE = GeomAbs_SurfaceType.GeomAbs_Plane


def _parameters(deviation: float, angular_deflection: float) -> IMeshTools_Parameters:
    """`BRepMesh_IncrementalMesh(shape, dev, False, ang, parallel)` が `initParameters` で補う値まで揃える。"""
    p = IMeshTools_Parameters()
    p.Deflection = float(deviation)
    p.Angle = float(angular_deflection)
    p.Relative = False
    p.InParallel = True
    confusion = Precision.Confusion_s()
    if p.DeflectionInterior < confusion:
        p.DeflectionInterior = p.Deflection
    if p.MinSize < confusion:
        p.MinSize = max(IMeshTools_Parameters.RelMinSize_s() * min(p.Deflection, p.DeflectionInterior), confusion)
    if p.AngleInterior < Precision.Angular_s():
        p.AngleInterior = 2.0 * p.Angle
    p.CleanModel = False
    return p


def _copy_parameters(source: IMeshTools_Parameters) -> IMeshTools_Parameters:
    target = IMeshTools_Parameters()
    for name in ('Deflection', 'Angle', 'Relative', 'InParallel', 'DeflectionInterior', 'AngleInterior',
                 'MinSize', 'CleanModel', 'ControlSurfaceDeflection', 'ForceFaceDeflection',
                 'AllowQualityDecrease', 'InternalVerticesMode', 'AdjustMinSize',
                 'EnableControlSurfaceDeflectionAllSurfaces', 'MeshAlgo'):
        setattr(target, name, getattr(source, name))
    return target


class _Model:
    """内部モデルから取り出した use(wire 内の辺の利用箇所)と、そのハンドル。"""

    def __init__(self, context: IMeshTools_Context, shape):
        self.context = context
        self.model = context.GetModel()
        self.face_map = _brep_topology.indexed_map(shape, TopAbs_ShapeEnum.TopAbs_FACE)
        edge_map = _brep_topology.indexed_map(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
        vertex_map = _brep_topology.indexed_map(shape, TopAbs_ShapeEnum.TopAbs_VERTEX)
        self.faces = [self.model.GetFace(i) for i in range(self.model.FacesNb())]
        self.face_ids = [self.face_map.FindIndex(df.GetFace()) for df in self.faces]
        self.edges = [self.model.GetEdge(i) for i in range(self.model.EdgesNb())]
        self.edge_ids = [edge_map.FindIndex(de.GetEdge()) for de in self.edges]
        self.edge_model_index = {edge_id: i for i, edge_id in enumerate(self.edge_ids)}
        # 辺の点列はパラメータ増加方向(FORWARD)に並ぶので、先頭・末尾の CAD 頂点も FORWARD で取る
        edge_vertex = {}
        for edge_id, de in zip(self.edge_ids, self.edges):
            forward_edge = TopoDS.Edge_s(de.GetEdge().Oriented(_FORWARD))
            edge_vertex[edge_id] = (vertex_map.FindIndex(TopExp.FirstVertex_s(forward_edge)),
                                    vertex_map.FindIndex(TopExp.LastVertex_s(forward_edge)))
        # use ごと: pcurve ハンドル・点列(numpy)・所属
        self.use_pcurve: List = []
        self.use_points: List[np.ndarray] = []
        self.use_edge: List[int] = []
        self.use_face_position: List[int] = []
        self.use_wire: List[int] = []
        self.use_forward: List[bool] = []
        self.use_internal: List[bool] = []
        self.use_next: List[int] = []
        self.use_vertex: List[tuple] = []
        self.edge_uses: Dict[int, List[int]] = {}
        face_uses: List[List[int]] = []
        forward = _FORWARD
        for position, df in enumerate(self.faces):
            tf = df.GetFace()
            face_uses.append([])
            for wire_index in range(df.WiresNb()):
                dw = df.GetWire(wire_index)
                first_use = len(self.use_pcurve)
                for k in range(dw.EdgesNb()):
                    de = dw.GetEdge(k)
                    orientation = dw.GetEdgeOrientation(k)
                    pcurve = None
                    for j in range(de.PCurvesNb()):
                        candidate = de.GetPCurve(j)
                        if candidate.GetOrientation() == orientation and candidate.GetFace().GetFace().IsSame(tf):
                            pcurve = candidate
                            break
                    if pcurve is None:
                        raise ValueError('内部モデルの wire の辺に対応する pcurve が無い')
                    use = len(self.use_pcurve)
                    edge_id = edge_map.FindIndex(de.GetEdge())
                    self.use_pcurve.append(pcurve)
                    self.use_points.append(_pcurve_points(pcurve))
                    self.use_edge.append(edge_id)
                    self.use_face_position.append(position)
                    self.use_wire.append(wire_index)
                    self.use_forward.append(orientation == forward)
                    self.use_internal.append(bool(pcurve.IsInternal()))
                    self.edge_uses.setdefault(edge_id, []).append(use)
                    self.use_vertex.append(edge_vertex[edge_id])
                    self.use_next.append(use + 1)
                    face_uses[-1].append(use)
                if len(self.use_pcurve) > first_use:
                    self.use_next[-1] = first_use
        self.face_uses = [np.asarray(uses, dtype=np.int64) for uses in face_uses]
        self.face_ids_array = np.asarray(self.face_ids, dtype=np.int64)
        self.use_face_position_array = np.asarray(self.use_face_position, dtype=np.int64)
        self.use_edge_array = np.asarray(self.use_edge, dtype=np.int64)
        self.use_forward_array = np.asarray(self.use_forward, dtype=bool)
        self.use_internal_array = np.asarray(self.use_internal, dtype=bool)
        self.use_next_array = np.asarray(self.use_next, dtype=np.int64)
        self.use_vertex_array = np.asarray(self.use_vertex, dtype=np.int64).reshape(-1, 2)
        self.use_wire_array = np.asarray(self.use_wire, dtype=np.int64)

    def pack(self, positions: Optional[List[int]] = None) -> boundary_check.Packed:
        """`boundary_check` の入力を面の位置(モデル内 0 始まり)で選んで作る。面番号は CAD の 1 始まり。"""
        if positions is None:
            uses = np.arange(len(self.use_points))
        else:
            chosen = [self.face_uses[position] for position in positions]
            uses = np.concatenate(chosen) if chosen else np.zeros(0, dtype=np.int64)
        # 選んだ use だけを詰め直すので use_next は詰めた後の番号へ写す
        local = np.full(len(self.use_points), -1, dtype=np.int64)
        local[uses] = np.arange(len(uses))
        return boundary_check.Packed.from_arrays(
            [self.use_points[u] for u in uses], self.face_ids_array[self.use_face_position_array[uses]],
            self.use_edge_array[uses], self.use_forward_array[uses], self.use_internal_array[uses],
            local[self.use_next_array[uses]], self.use_vertex_array[uses], self.use_wire_array[uses])

    def refresh_edge(self, edge_id: int) -> bool:
        """辺の全 use の点列を内部モデルから読み直す。変化があれば True。"""
        changed = False
        for use in self.edge_uses[edge_id]:
            fresh = _pcurve_points(self.use_pcurve[use])
            old = self.use_points[use]
            if fresh.shape != old.shape or not np.array_equal(fresh, old):
                changed = True
            self.use_points[use] = fresh
        return changed

    def faces_of_edges(self, edge_ids) -> List[int]:
        positions = {self.use_face_position[u] for e in edge_ids for u in self.edge_uses.get(e, ())}
        return sorted(positions)

    def points_total(self) -> int:
        return int(sum(len(p) for p in self.use_points))

    def fingerprint(self, edge_ids) -> str:
        digest = hashlib.sha1()
        for edge_id in sorted(edge_ids):
            for use in self.edge_uses[edge_id]:
                digest.update(np.ascontiguousarray(self.use_points[use]).tobytes())
        return digest.hexdigest()

    def verify_counts(self) -> List[dict]:
        """検査した点列と内部モデルの現在の点数が一致しない use を返す(面分割後の整合確認)。"""
        mismatched = []
        for use, pcurve in enumerate(self.use_pcurve):
            if pcurve.ParametersNb() != len(self.use_points[use]):
                mismatched.append(dict(face=self.face_ids[self.use_face_position[use]], edge=self.use_edge[use],
                                       checked=len(self.use_points[use]), meshed=pcurve.ParametersNb()))
        return mismatched

    def close(self):
        """派生ハンドルをモデルより先に手放す(モジュール docstring の寿命規則)。"""
        self.use_pcurve = []
        self.edges = []
        self.faces = []
        self.model = None
        self.context = None


def _pcurve_points(pcurve) -> np.ndarray:
    get = pcurve.GetPoint
    return np.asarray([get(i).Coord() for i in range(pcurve.ParametersNb())], dtype=np.float64).reshape(-1, 2)


def _amplify(model: _Model, edge_id: int, parameters: IMeshTools_Parameters) -> dict:
    """OCCT の EdgeAmplifier と同じ手順で 1 辺を再分割する。

    `Clear(True)` で端点を残し、偏差を 1/3(下限 Precision::Confusion)にして、点数が減らないよう
    現在の点数を最小点数に渡す。3D 点列を作り直してから `Tessellate2d` が全 pcurve を更新する。
    """
    de = model.edges[model.edge_model_index[edge_id]]
    before = de.GetCurve().ParametersNb()
    de.Clear(True)
    deflection = max(de.GetDeflection() / 3.0, Precision.Confusion_s())
    de.SetDeflection(deflection)
    points_nb = before
    for j in range(de.PCurvesNb()):
        if de.GetPCurve(j).GetFace().GetWire(0).EdgesNb() <= 2:
            points_nb += 1
            break
    first = de.GetPCurve(0)
    tessellator = BRepMesh_EdgeDiscret.CreateEdgeTessellator_s(
        de, first.GetOrientation(), first.GetFace(), parameters, points_nb)
    BRepMesh_EdgeDiscret.Tessellate3d_s(de, tessellator, False)
    BRepMesh_EdgeDiscret.Tessellate2d_s(de, False)
    return dict(edge=edge_id, deflection=deflection, points_before=before, points_after=de.GetCurve().ParametersNb())


def _issue_summary(issues: List[dict]) -> List[dict]:
    return [dict(kind=i['kind'], edges=list(i['edges']), point=i.get('point'),
                 **({'gap': i['gap']} if 'gap' in i else {})) for i in issues]


def mesh_shape(shape, deviation: float, angular_deflection: float, *, max_rounds: int = DEFAULT_MAX_ROUNDS) -> dict:
    """形状を三角形分割し(結果は形状に付く)、境界検査・再分割の診断を返す。

    戻り値(JSON 化できる値だけ):
        faces, edges: 内部モデルの面数・辺数
        points_initial, points_final: 全 pcurve の離散点数
        rounds: 再分割を行った回数
        termination: 'resolved' | 'max_rounds' | 'no_change' | 'cycle' | 'budget' | 'unfixable'
        unresolved: [{'face': CAD 1始まり, 'issues': [...]}]  面分割へ渡さず(Failure)三角形なしにした面
        endpoint: {'mismatches': n, 'gap_max': mm}  同じ CAD 頂点に着くのに座標が一致しない終端の組(boundary_check)
        reinstated: [CAD 面番号]  OCCT の healer が SelfIntersectingWire|Failure にしたが独自検査で解消していたので分割対象に戻した面
        delabella: [CAD 面番号]  Delabella で分割した面(`_discretize_faces`)
        delabella_fallback: [CAD 面番号]  Delabella の結果が数の検査を通らず、既定の分割で測り直した面
        amplified_edges: 再分割した辺の数(重複なし), amplifications: 再分割の延べ回数
        rounds_log: 各回の {round, faces_checked, issues, edges, points}
        count_mismatch: 面分割後に点数が検査時と食い違った use(空が正常)
        seconds: 段階ごとの秒
    """
    if shape is None or _brep_topology.is_null(shape):
        raise ValueError('cannot mesh an empty shape')
    seconds: Dict[str, float] = {}
    clock = time.perf_counter
    BRepTools.Clean_s(shape)
    parameters = _parameters(deviation, angular_deflection)
    context = IMeshTools_Context()
    context.SetModelBuilder(BRepMesh_ModelBuilder())
    context.SetEdgeDiscret(BRepMesh_EdgeDiscret())
    context.SetModelHealer(BRepMesh_ModelHealer())
    context.SetPreProcessor(BRepMesh_ModelPreProcessor())
    context.SetFaceDiscret(BRepMesh_FaceDiscret(BRepMesh_MeshAlgoFactory()))
    context.SetPostProcessor(BRepMesh_ModelPostProcessor())
    target = context.ChangeParameters()
    for name in ('Deflection', 'Angle', 'Relative', 'InParallel', 'DeflectionInterior', 'AngleInterior',
                 'MinSize', 'CleanModel'):
        setattr(target, name, getattr(parameters, name))
    context.SetShape(shape)
    for stage in ('BuildModel', 'DiscretizeEdges', 'HealModel', 'PreProcessModel'):
        started = clock()
        if not getattr(context, stage)():
            raise ValueError(f'meshing failed at {stage} (deviation={deviation})')
        seconds[stage] = clock() - started

    started = clock()
    model = _Model(context, shape)
    seconds['extract'] = clock() - started
    try:
        return _mesh_with_model(model, context, parameters, deviation, max_rounds, seconds)
    finally:
        model.close()
        context = None


def _mesh_with_model(model: _Model, context, parameters, deviation, max_rounds, seconds) -> dict:
    clock = time.perf_counter
    # OCCT の healer が 5 回で解消できず Failure にした面(SelfIntersectingWire を伴う)。独自検査で
    # 解消できたら分割対象に戻す。それ以外の Failure(wire の構築失敗・例外)は触らない。
    healer_failed = [position for position, df in enumerate(model.faces)
                     if df.IsSet(_FAILURE) and df.IsSet(_SELF_INTERSECTING)]
    amplify_parameters = _copy_parameters(parameters)
    # healer と同じく再分割の最小サイズは Precision::Confusion(既定の MinSize=0.1·偏差 では細分できない)。
    amplify_parameters.MinSize = Precision.Confusion_s()

    points_initial = model.points_total()
    rounds_log: List[dict] = []
    unresolved: Dict[int, List[dict]] = {}
    seen_states = set()
    amplified: Dict[int, int] = {}
    termination = 'resolved'
    dirty: Optional[List[int]] = None
    rounds = 0
    seconds['check'] = 0.0
    seconds['amplify'] = 0.0
    while True:
        started = clock()
        result = boundary_check.check(model.pack(dirty))
        seconds['check'] += clock() - started
        checked = range(len(model.faces)) if dirty is None else dirty
        for position in checked:
            unresolved.pop(model.face_ids[position], None)
        for issue in result['issues']:
            unresolved.setdefault(issue['face'], []).append(issue)
        rounds_log.append(dict(round=rounds, faces_checked=result['faces_checked'], issues=len(result['issues']),
                               segments=result['segments'], points=model.points_total(),
                               endpoint_mismatches=result['endpoint_mismatches'],
                               endpoint_gap_max=result['endpoint_gap_max']))
        if rounds == 0:
            endpoint = dict(mismatches=result['endpoint_mismatches'], gap_max=result['endpoint_gap_max'])
        if not unresolved:
            break
        edges = boundary_check.fixable_edges(issue for issues in unresolved.values() for issue in issues)
        if not edges:
            termination = 'unfixable'
            break
        if rounds >= max_rounds:
            termination = 'max_rounds'
            break
        state = model.fingerprint(edges)
        if state in seen_states:
            termination = 'cycle'
            break
        seen_states.add(state)
        started = clock()
        changed = False
        for edge_id in sorted(edges):
            _amplify(model, edge_id, amplify_parameters)
            amplified[edge_id] = amplified.get(edge_id, 0) + 1
            changed = model.refresh_edge(edge_id) or changed
        # 円錐の seam は PreProcessModel が点を足す(再分割で 2 点に戻るので毎回かけ直す。他は冪等)
        if not context.PreProcessModel():
            raise ValueError('meshing failed at PreProcessModel after edge refinement')
        for edge_id in edges:
            changed = model.refresh_edge(edge_id) or changed
        seconds['amplify'] += clock() - started
        rounds += 1
        rounds_log[-1]['edges'] = len(edges)
        if not changed:
            termination = 'no_change'
            break
        if model.points_total() > DEFAULT_MAX_POINTS:
            termination = 'budget'
            break
        dirty = model.faces_of_edges(edges)

    # 面分割前の保証点: 再分割した後は全面を見直す(何も変えていなければ最初の全面検査がそのまま保証)。
    if amplified:
        started = clock()
        result = boundary_check.check(model.pack(None))
        seconds['final_check'] = clock() - started
        unresolved = {}
        for issue in result['issues']:
            unresolved.setdefault(issue['face'], []).append(issue)
        endpoint = dict(mismatches=result['endpoint_mismatches'], gap_max=result['endpoint_gap_max'])
        if unresolved and termination == 'resolved':
            termination = 'reappeared'

    position_of = {face_id: position for position, face_id in enumerate(model.face_ids)}
    for face_id in unresolved:
        model.faces[position_of[face_id]].SetStatus(_FAILURE)
    reinstated = []
    for position in healer_failed:
        if model.face_ids[position] not in unresolved:
            df = model.faces[position]
            df.UnsetStatus(_FAILURE)
            df.UnsetStatus(_SELF_INTERSECTING)
            reinstated.append(model.face_ids[position])

    started = clock()
    discretized, delabella, delabella_fallback = _discretize_faces(model, context)
    if not discretized:
        raise ValueError(f'meshing failed at DiscretizeFaces (deviation={deviation})')
    seconds['DiscretizeFaces'] = clock() - started
    started = clock()
    if not context.PostProcessModel():
        raise ValueError(f'meshing failed at PostProcessModel (deviation={deviation})')
    seconds['PostProcessModel'] = clock() - started
    started = clock()
    count_mismatch = model.verify_counts()
    seconds['verify'] = clock() - started
    if count_mismatch:
        raise ValueError(f'面分割が検査済みの境界点列を変更した: {count_mismatch[:5]}')

    return dict(
        faces=len(model.faces), edges=len(model.edges),
        points_initial=points_initial, points_final=model.points_total(),
        rounds=rounds, termination=termination,
        unresolved=[dict(face=face_id, issues=_issue_summary(issues)) for face_id, issues in sorted(unresolved.items())],
        reinstated=sorted(reinstated),
        delabella=[model.face_ids[position] for position in delabella],
        delabella_fallback=[model.face_ids[position] for position in delabella_fallback],
        amplified_edges=len(amplified), amplifications=int(sum(amplified.values())),
        amplified_edge_ids=sorted(amplified),
        rounds_log=rounds_log, count_mismatch=count_mismatch,
        # CAD 頂点を共有するのに pcurve の座標が一致しない終端の組(件数と最大のずれ)
        endpoint=endpoint,
        seconds={k: float(v) for k, v in seconds.items()},
    )


def _delabella_planes(model: _Model) -> List[int]:
    """Delabella で分割する面の位置。境界の点が `DELABELLA_PLANE_POINTS` 以上の平面で、全 wire を分割に使い(自己交差の印が
    無い)、INTERNAL の辺を持たないもの(`_complete` の数の検査が成り立つ面)。"""
    chosen = []
    for position, df in enumerate(model.faces):
        if df.IsSet(_FAILURE) or df.IsSet(_REUSED) or df.GetSurface().GetType() != _PLANE:
            continue
        uses = model.face_uses[position]
        if model.use_internal_array[uses].any():
            continue
        if sum(len(model.use_points[use]) for use in uses) < DELABELLA_PLANE_POINTS:
            continue
        if any(df.GetWire(wire).IsSet(_SELF_INTERSECTING) for wire in range(df.WiresNb())):
            continue
        chosen.append(position)
    return chosen


def _complete(df) -> bool:
    """内点を足さない平面の分割として三角形の数が合うか: 境界の点 N・wire W の領域を覆う三角形は N + 2W - 4 個。
    穴を塞いだ・領域の一部が欠けた(縁の点が浮いた)分割はこの数にならない。"""
    triangulation = BRep_Tool.Triangulation_s(df.GetFace(), TopLoc_Location())
    if triangulation is None:
        return False
    return triangulation.NbTriangles() == triangulation.NbNodes() + 2 * df.WiresNb() - 4


def _surely_inside(a: np.ndarray, b: np.ndarray, c: np.ndarray, d: np.ndarray, sense: int) -> np.ndarray:
    """d が (a, b, c) の外接円の内に**確かに**あるか(行ごと)。`sense` は (a, b, c) の向き(+1 反時計回り)。

    浮動小数点の誤差境界で決まらない組(同じ円の上とその近く)は False。穴を等間隔に並べた面では、同じ穴を平行移動した
    点どうしが長方形を作り、ちょうど同じ円の上に乗る組が多い(実測: 穴 54 の板 60 枚で 11 万組、整数演算で 1.2 秒)。
    その組はどちらの対角線でも Delaunay なので、裏返さない。
    """
    adx, ady = a[:, 0] - d[:, 0], a[:, 1] - d[:, 1]
    bdx, bdy = b[:, 0] - d[:, 0], b[:, 1] - d[:, 1]
    cdx, cdy = c[:, 0] - d[:, 0], c[:, 1] - d[:, 1]
    bc, cb = bdx * cdy, cdx * bdy
    ca, ac = cdx * ady, adx * cdy
    ab, ba = adx * bdy, bdx * ady
    alift, blift, clift = adx * adx + ady * ady, bdx * bdx + bdy * bdy, cdx * cdx + cdy * cdy
    det = alift * (bc - cb) + blift * (ca - ac) + clift * (ab - ba)
    permanent = (alift * (np.abs(bc) + np.abs(cb)) + blift * (np.abs(ca) + np.abs(ac))
                 + clift * (np.abs(ab) + np.abs(ba)))
    return sense * det > _INCIRCLE_BOUND * permanent


def _delaunay_flips(uv: np.ndarray, triangles: np.ndarray, fixed: np.ndarray) -> np.ndarray:
    """平面の三角形分割の内側の辺を、向かいの頂点が外接円の内に確かに入る限り裏返す(Lawson)。`fixed` の辺は裏返さない。
    結果は、同じ円の上(丸めの範囲)の組を除いて Delaunay。

    外部の罠: Delabella は点の外接箱を軸ごとに [-0.5, 0.5] へ縮めてから分割する(`BRepMesh_DelabellaBaseMeshAlgo`)ので、
    外接箱が正方形でない面では UV の上で Delaunay にならない。辺の裏返しは三角形の数・節点・境界の線分を変えない。

    `uv` は (N, 2)、`triangles` は (T, 3) の 0 始まりで、全部が同じ向き(違えば何もしない)。`fixed` は固定する辺の鍵
    `min·N + max`。戻り値は裏返した後の (T, 3)(向きは保つ)。
    """
    triangles = np.array(triangles, dtype=np.int64)
    count = len(triangles)
    if count < 2:
        return triangles
    signs = boundary_check.orient2d(uv[triangles[:, 0]], uv[triangles[:, 1]], uv[triangles[:, 2]])
    if not ((signs > 0).all() or (signs < 0).all()):
        return triangles
    sense = int(signs[0])
    nodes = len(uv)
    # 半辺 3t + k は三角形 t の k 番目の頂点から次の頂点へ。twin は同じ辺の逆向きの半辺(境界は -1)
    start = triangles.ravel()
    end = triangles[:, [1, 2, 0]].ravel()
    key = np.minimum(start, end) * nodes + np.maximum(start, end)
    order = np.argsort(key, kind='stable')
    _keys, first, counts = np.unique(key[order], return_index=True, return_counts=True)
    shared = first[counts == 2]
    one, two = order[shared], order[shared + 1]
    opposite = (start[one] == end[two]) & (end[one] == start[two])
    twin = np.full(3 * count, -1, dtype=np.int64)
    twin[one[opposite]] = two[opposite]
    twin[two[opposite]] = one[opposite]
    locked = np.isin(key, fixed)
    active = np.ones(count, dtype=bool)
    for _ in range(_FLIP_ROUNDS):
        half = (3 * np.flatnonzero(active)[:, None] + np.arange(3)).ravel()
        half = half[(twin[half] >= 0) & ~locked[half]]
        edge = np.unique(np.minimum(half, twin[half]))
        if len(edge) == 0:
            break
        other = twin[edge]
        t1, k1, t2, k2 = edge // 3, edge % 3, other // 3, other % 3
        a, b = triangles[t1, k1], triangles[t1, (k1 + 1) % 3]
        c, d = triangles[t1, (k1 + 2) % 3], triangles[t2, (k2 + 2) % 3]
        illegal = _surely_inside(uv[a], uv[b], uv[c], uv[d], sense)
        if not illegal.any():
            break
        t1, k1, t2, k2, a, b, c, d = (v[illegal] for v in (t1, k1, t2, k2, a, b, c, d))
        # 1回に裏返すのは三角形を共有しない辺だけ(三角形ごとに番号の小さい辺を採る)。残りは次の回に見直す
        ident = np.arange(len(t1))
        least = np.full(count, len(t1), dtype=np.int64)
        np.minimum.at(least, t1, ident)
        np.minimum.at(least, t2, ident)
        chosen = (least[t1] == ident) & (least[t2] == ident)
        active = np.zeros(count, dtype=bool)
        active[t1[~chosen]] = True
        active[t2[~chosen]] = True
        t1, k1, t2, k2, a, b, c, d = (v[chosen] for v in (t1, k1, t2, k2, a, b, c, d))
        convex = ((sense * boundary_check.orient2d(uv[a], uv[d], uv[c]) > 0)
                  & (sense * boundary_check.orient2d(uv[d], uv[b], uv[c]) > 0))
        t1, k1, t2, k2, a, b, c, d = (v[convex] for v in (t1, k1, t2, k2, a, b, c, d))
        if len(t1) == 0:
            break
        # (a, b, c) と (b, a, d) を (a, d, c) と (d, b, c) にする。外周の4辺の半辺は新しい位置へ移り、相手の twin も付け替える
        moved_from = np.concatenate((3 * t2 + (k2 + 1) % 3, 3 * t1 + (k1 + 2) % 3, 3 * t2 + (k2 + 2) % 3, 3 * t1 + (k1 + 1) % 3))
        moved_to = np.concatenate((3 * t1, 3 * t1 + 2, 3 * t2, 3 * t2 + 1))
        partners = twin[moved_from]
        moved_locks = locked[moved_from]
        remap = np.arange(3 * count)
        remap[moved_from] = moved_to
        bordered = partners >= 0
        twin[moved_to] = np.where(bordered, remap[np.maximum(partners, 0)], -1)
        twin[remap[partners[bordered]]] = moved_to[bordered]
        locked[moved_to] = moved_locks
        twin[3 * t1 + 1], twin[3 * t2 + 2] = 3 * t2 + 2, 3 * t1 + 1
        locked[3 * t1 + 1] = locked[3 * t2 + 2] = False
        triangles[t1] = np.column_stack((a, d, c))
        triangles[t2] = np.column_stack((d, b, c))
        active[t1] = True
        active[t2] = True
    return triangles


def _restore_delaunay(model: _Model, position: int) -> int:
    """Delabella で分割した平面の三角形を UV の上で Delaunay に戻して書き戻す。裏返した三角形の数を返す。

    固定する辺は、その面の pcurve の点列の隣り合う節点(`GetIndex` は面の三角形分割の節点番号。OCCT の `PostProcessModel`
    が辺の折れ線をこの番号で書く)。
    """
    triangulation = BRep_Tool.Triangulation_s(model.faces[position].GetFace(), TopLoc_Location())
    node = triangulation.UVNode
    uv = np.asarray([node(i).Coord() for i in range(1, triangulation.NbNodes() + 1)], dtype=np.float64).reshape(-1, 2)
    triangle = triangulation.Triangle
    before = np.asarray([triangle(i).Get() for i in range(1, triangulation.NbTriangles() + 1)],
                        dtype=np.int64).reshape(-1, 3) - 1
    fixed = []
    for use in model.face_uses[position]:
        pcurve = model.use_pcurve[use]
        index = np.asarray([pcurve.GetIndex(i) for i in range(pcurve.ParametersNb())], dtype=np.int64) - 1
        fixed.append(np.minimum(index[:-1], index[1:]) * len(uv) + np.maximum(index[:-1], index[1:]))
    after = _delaunay_flips(uv, before, np.concatenate(fixed) if fixed else np.zeros(0, dtype=np.int64))
    changed = np.flatnonzero((after != before).any(axis=1))
    for row in changed:
        first, second, third = (int(v) + 1 for v in after[row])
        triangulation.SetTriangle(int(row) + 1, Poly_Triangle(first, second, third))
    return len(changed)


def _discretize_faces(model: _Model, context) -> tuple:
    """`DiscretizeFaces` を2回に分ける: 境界の点が多い平面を Delabella で、残りを既定の分割で。

    OCCT の面分割は `IMeshData_Reused` の面を飛ばすので、片方の回はもう片方の面にその印を付けて走らせ、終わったら外す
    (印が残ると `PostProcessModel` が辺の折れ線を書かない)。Delabella の結果が `_complete` を通らない面は、付いた三角形を
    外して既定の分割で測り直す。通った面は `_restore_delaunay` で Delaunay に戻す。
    戻り値は `(成功したか, Delabella で分割した面の位置, 測り直した面の位置)`。
    """
    planes = _delabella_planes(model)
    if not planes:
        return context.DiscretizeFaces(Message_ProgressRange()), [], []
    chosen = set(planes)
    others = [position for position, df in enumerate(model.faces) if position not in chosen and not df.IsSet(_REUSED)]
    for position in others:
        model.faces[position].SetStatus(_REUSED)
    context.SetFaceDiscret(BRepMesh_FaceDiscret(BRepMesh_DelabellaMeshAlgoFactory()))
    try:
        discretized = context.DiscretizeFaces(Message_ProgressRange())
    finally:
        for position in others:
            model.faces[position].UnsetStatus(_REUSED)
        context.SetFaceDiscret(BRepMesh_FaceDiscret(BRepMesh_MeshAlgoFactory()))
    kept, fallback = [], []
    for position in planes:
        df = model.faces[position]
        if discretized and not df.IsSet(_FAILURE) and _complete(df):
            try:
                _restore_delaunay(model, position)
            except Exception:  # noqa: BLE001  書き戻しの途中で止まった面も、外して既定の分割で測り直す(`delabella_fallback` に出る)
                pass
            else:
                kept.append(position)
                continue
        df.UnsetStatus(_FAILURE)
        BRep_Builder().UpdateFace(df.GetFace(), None)
        fallback.append(position)
    for position in kept:
        model.faces[position].SetStatus(_REUSED)
    try:
        discretized = context.DiscretizeFaces(Message_ProgressRange())
    finally:
        for position in kept:
            model.faces[position].UnsetStatus(_REUSED)
    return discretized, kept, fallback


def merge_diagnostics(passes: List[dict]) -> dict:
    """複数回の `mesh_shape`(選択的テセレーションの粗・密パス)の診断を 1 つにまとめる。

    `unresolved` は全パスの和、`termination` は全部 resolved のときだけ resolved。各パスの全文は `passes`。
    """
    unresolved = [entry for diag in passes for entry in diag['unresolved']]
    terminations = [diag['termination'] for diag in passes]
    return dict(
        termination='resolved' if all(t == 'resolved' for t in terminations) else next(t for t in terminations if t != 'resolved'),
        unresolved=sorted(unresolved, key=lambda entry: entry['face']),
        reinstated=sorted(face for diag in passes for face in diag['reinstated']),
        delabella=sorted(face for diag in passes for face in diag['delabella']),
        delabella_fallback=sorted(face for diag in passes for face in diag['delabella_fallback']),
        rounds=int(sum(diag['rounds'] for diag in passes)),
        amplified_edges=int(sum(diag['amplified_edges'] for diag in passes)),
        amplifications=int(sum(diag['amplifications'] for diag in passes)),
        passes=passes,
    )
