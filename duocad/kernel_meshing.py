"""OCCT の面分割を段階ごとに制御し、面分割の直前に境界折れ線の交差を独自に検査して共有辺を再分割する。

`BRepMesh_IncrementalMesh` は BuildModel → DiscretizeEdges → HealModel → PreProcessModel →
DiscretizeFaces → PostProcessModel を一括で回す(OCCT の `IMeshTools_MeshBuilder::Perform`)。
ここでは同じ部品(`BRepMesh_Context` が組む標準の6部品。OCP には `BRepMesh_Context` 自体が無い)を
`IMeshTools_Context` に組み、**PreProcessModel の後・DiscretizeFaces の前**で `boundary_check` を当てる。
OCCT 自身の `BRepMesh_FaceChecker` は 5 度未満の交差角と `π(2·面偏差)²` 未満の小ループを無視するので、
そこを通り抜けた交差が面分割へ渡り、面の欠落・非多様体の三角形になる(docs/TOPOPT_GAP_CAUSALITY.md)。

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
from OCP.BRepMesh import (
    BRepMesh_EdgeDiscret,
    BRepMesh_FaceDiscret,
    BRepMesh_MeshAlgoFactory,
    BRepMesh_ModelBuilder,
    BRepMesh_ModelHealer,
    BRepMesh_ModelPostProcessor,
    BRepMesh_ModelPreProcessor,
)
from OCP.BRepTools import BRepTools
from OCP.IMeshData import IMeshData_Status
from OCP.IMeshTools import IMeshTools_Context, IMeshTools_Parameters
from OCP.Message import Message_ProgressRange
from OCP.Precision import Precision
from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS

from . import boundary_check
from .mesh_geometry import indexed_map

#: 再分割の反復上限の既定(仮置き。docs/TOPOPT_GAP_CAUSALITY.md「案Aの実装」)。
DEFAULT_MAX_ROUNDS = 12
#: 全辺の離散点数の予算。超えたら未解決として止める(メモリの歯止め)。
DEFAULT_MAX_POINTS = 4_000_000

_FAILURE = IMeshData_Status.IMeshData_Failure
_SELF_INTERSECTING = IMeshData_Status.IMeshData_SelfIntersectingWire
_FORWARD = TopAbs_Orientation.TopAbs_FORWARD


def _parameters(deviation: float, angular_deflection: float, parallel: bool) -> IMeshTools_Parameters:
    """`BRepMesh_IncrementalMesh(shape, dev, False, ang, parallel)` が `initParameters` で補う値まで揃える。"""
    p = IMeshTools_Parameters()
    p.Deflection = float(deviation)
    p.Angle = float(angular_deflection)
    p.Relative = False
    p.InParallel = bool(parallel)
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
        self.face_map = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_FACE)
        edge_map = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
        vertex_map = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_VERTEX)
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


def mesh_shape(shape, deviation: float, angular_deflection: float, *, parallel: bool = True,
               max_rounds: int = DEFAULT_MAX_ROUNDS, max_points: int = DEFAULT_MAX_POINTS) -> dict:
    """形状を三角形分割し(結果は形状に付く)、境界検査・再分割の診断を返す。

    戻り値(JSON 化できる値だけ):
        faces, edges: 内部モデルの面数・辺数
        points_initial, points_final: 全 pcurve の離散点数
        rounds: 再分割を行った回数
        termination: 'resolved' | 'max_rounds' | 'no_change' | 'cycle' | 'budget' | 'unfixable'
        unresolved: [{'face': CAD 1始まり, 'issues': [...]}]  面分割へ渡さず(Failure)三角形なしにした面
        endpoint: {'mismatches': n, 'gap_max': mm}  同じ CAD 頂点に着くのに座標が一致しない終端の組(boundary_check)
        reinstated: [CAD 面番号]  OCCT の healer が SelfIntersectingWire|Failure にしたが独自検査で解消していたので分割対象に戻した面
        amplified_edges: 再分割した辺の数(重複なし), amplifications: 再分割の延べ回数
        rounds_log: 各回の {round, faces_checked, issues, edges, points}
        count_mismatch: 面分割後に点数が検査時と食い違った use(空が正常)
        seconds: 段階ごとの秒
    """
    if shape is None or shape.IsNull():
        raise ValueError('cannot mesh an empty shape')
    seconds: Dict[str, float] = {}
    clock = time.perf_counter
    BRepTools.Clean_s(shape)
    parameters = _parameters(deviation, angular_deflection, parallel)
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
        return _mesh_with_model(model, context, parameters, deviation, max_rounds, max_points, seconds)
    finally:
        model.close()
        context = None


def _mesh_with_model(model: _Model, context, parameters, deviation, max_rounds, max_points, seconds) -> dict:
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
        if model.points_total() > max_points:
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
    if not context.DiscretizeFaces(Message_ProgressRange()):
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
        amplified_edges=len(amplified), amplifications=int(sum(amplified.values())),
        amplified_edge_ids=sorted(amplified),
        rounds_log=rounds_log, count_mismatch=count_mismatch,
        # CAD 頂点を共有するのに pcurve の座標が一致しない終端の組(件数と最大のずれ)
        endpoint=endpoint,
        seconds={k: float(v) for k, v in seconds.items()},
    )


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
        rounds=int(sum(diag['rounds'] for diag in passes)),
        amplified_edges=int(sum(diag['amplified_edges'] for diag in passes)),
        amplifications=int(sum(diag['amplifications'] for diag in passes)),
        passes=passes,
    )
