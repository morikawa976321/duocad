"""三角形境界の接続検査と、向き・空洞を保ったCADソリッド化。

位相の列挙と compound は `brep_topology` から再輸出する。
形状検査は `brep_checks` を参照し、個別のカーネル操作は参照しない。
"""
from collections import defaultdict

import numpy as np

from OCP.BOPAlgo import BOPAlgo_CheckerSI
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing,
)
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepLib import BRepLib
from OCP.Precision import Precision
from OCP.TopAbs import TopAbs_IN, TopAbs_OUT, TopAbs_SHELL
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Pnt

from .brep_checks import is_valid as _is_valid
from .boundary_check import degenerate_mask
from .brep_topology import subshapes, indexed_map, compound


TOLERANCE_MM = Precision.Confusion_s()


def degenerate_triangles(vertices, faces):
    """三角形ごとの縮退(面積が厳密に 0)を返す (T,) bool(`boundary_check.degenerate_mask`)。"""
    return degenerate_mask(np.asarray(vertices, dtype=np.float64)[np.asarray(faces)])


#: 接続検査(`topology`)の6項目。**1つでも 0 でなければ成果物にできない**(`topopt.finalize` と検査が同じ並びで読む)。
TOPOLOGY_DEFECTS = ('boundary_edges', 'nonmanifold_edges', 'inconsistent_edges',
                    'nonmanifold_vertices', 'degenerate_triangles', 'duplicate_triangles')


def weld_exact(triangles):
    """三角形 (N,3,3) を、**座標が完全一致する頂点だけ**を共有した (vertices, faces) にする。

    近い頂点は融着しない(亀裂を隠さない)。三角形の無い面を除くかどうかは呼び出し側が決める。
    """
    vertices, inverse = np.unique(np.asarray(triangles).reshape(-1, 3), axis=0, return_inverse=True)
    return vertices, inverse.reshape(-1, 3)


def topology(vertices: np.ndarray, faces: np.ndarray) -> tuple[dict, list[list[int]]]:
    if (vertices.ndim != 2 or vertices.shape[1] != 3 or not len(vertices)
            or faces.ndim != 2 or faces.shape[1] != 3 or not len(faces)
            or not np.isfinite(vertices).all() or not np.issubdtype(faces.dtype, np.integer)
            or faces.min() < 0 or faces.max() >= len(vertices)):
        raise ValueError('頂点・三角形の配列が不正')
    degenerate = int(np.count_nonzero(degenerate_triangles(vertices, faces)))
    duplicates = len(faces) - len(np.unique(np.sort(faces, axis=1), axis=0))
    edges = defaultdict(list)
    links = defaultdict(list)
    for index, face in enumerate(faces):
        a, b, c = map(int, face)
        for u, v in ((a, b), (b, c), (c, a)):
            edges[min(u, v), max(u, v)].append((index, u < v))
        for v, u, w in ((a, b, c), (b, c, a), (c, a, b)):
            links[v].append((u, w))
    adjacency = [[] for _ in faces]
    boundary = nonmanifold = winding = 0
    for incident in edges.values():
        if len(incident) == 1:
            boundary += 1
        elif len(incident) != 2:
            nonmanifold += 1
        else:
            (a, direction_a), (b, direction_b) = incident
            winding += direction_a == direction_b
            adjacency[a].append(b)
            adjacency[b].append(a)
    bad_vertices = 0
    for incident in links.values():
        graph = defaultdict(list)
        for a, b in incident:
            graph[a].append(b)
            graph[b].append(a)
        seen, pending = set(), [next(iter(graph))]
        while pending:
            vertex = pending.pop()
            if vertex not in seen:
                seen.add(vertex)
                pending.extend(graph[vertex])
        if len(seen) != len(graph) or any(len(x) != 2 for x in graph.values()):
            bad_vertices += 1
    components, seen = [], set()
    for first in range(len(faces)):
        if first in seen:
            continue
        component, pending = [], [first]
        while pending:
            face = pending.pop()
            if face not in seen:
                seen.add(face)
                component.append(face)
                pending.extend(adjacency[face])
        components.append(component)
    return dict(triangles=len(faces), vertices=len(vertices), boundary_edges=boundary,
                nonmanifold_edges=nonmanifold, inconsistent_edges=int(winding),
                nonmanifold_vertices=bad_vertices, degenerate_triangles=degenerate,
                duplicate_triangles=duplicates, boundary_components=len(components)), components


def checked_solid(shell):
    maker = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(shell))
    if not maker.IsDone():
        raise RuntimeError('閉シェルをソリッドへ変換できない')
    solid = maker.Solid()
    if not BRepLib.OrientClosedSolid_s(solid) or not _is_valid(solid):
        raise RuntimeError('OCCTがソリッドを不正と判定した')
    return solid


def make_material(vertices, faces, components, *, check_intersections=True):
    shells, enclosed = [], []
    for component in components:
        sewing = BRepBuilderAPI_Sewing(TOLERANCE_MM)
        for face in faces[component]:
            polygon = BRepBuilderAPI_MakePolygon()
            for point in vertices[face]:
                polygon.Add(gp_Pnt(*map(float, point)))
            polygon.Close()
            if not polygon.IsDone():
                raise RuntimeError('三角形の輪郭を構築できない')
            maker = BRepBuilderAPI_MakeFace(polygon.Wire())
            if not maker.IsDone():
                raise RuntimeError('三角形の面を構築できない')
            sewing.Add(maker.Face())
        sewing.Perform()
        found = subshapes(sewing.SewedShape(), TopAbs_SHELL)
        if len(found) != 1 or sewing.NbFreeEdges() or sewing.NbMultipleEdges() or sewing.NbDeletedFaces():
            raise RuntimeError('縫合で面が欠落したか、閉シェルを再現できなかった')
        shell = found[0]
        shells.append(shell)
        enclosed.append(checked_solid(shell))
    if check_intersections:
        checker = BOPAlgo_CheckerSI()
        checker.AddArgument(compound(shells))
        checker.SetLevelOfCheck(5)
        checker.Perform()
        if checker.HasErrors() or checker.HasWarnings():
            raise RuntimeError('自己交差検査が完了しなかった')
        intersections = checker.DS().Interferences().Extent()
        if intersections:
            return None, dict(self_intersections=intersections)

    # 内部空洞は別の島ではない。閉シェルの包含深さが偶数のものを材料と数える。
    contains = np.zeros((len(shells), len(shells)), dtype=bool)
    for i, component in enumerate(components):
        point = gp_Pnt(*map(float, vertices[faces[component[0], 0]]))
        for j, solid in enumerate(enclosed):
            if i == j:
                continue
            state = BRepClass3d_SolidClassifier(solid, point, TOLERANCE_MM).State()
            if state not in (TopAbs_IN, TopAbs_OUT):
                raise RuntimeError('シェル間の包含関係が数値精度内で曖昧')
            contains[j, i] = state == TopAbs_IN
    depth = contains.sum(axis=0)
    solids = []
    for i, solid in enumerate(enclosed):
        tri = vertices[faces[components[i]]]
        tri = tri - tri[0, 0]
        signed = np.einsum('ij,ij->i', tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum()/6
        if signed == 0 or (signed > 0) != (depth[i] % 2 == 0):
            return None, dict(self_intersections=0, invalid_shell_orientation=True)
        if depth[i] % 2:
            continue
        maker = BRepBuilderAPI_MakeSolid(solid)
        for j in range(len(shells)):
            if contains[i, j] and depth[j] == depth[i]+1:
                shell = subshapes(enclosed[j], TopAbs_SHELL)[0]
                maker.Add(TopoDS.Shell_s(shell.Reversed()))
        material = maker.Solid()
        if not _is_valid(material):
            raise RuntimeError('空洞を含むソリッドが不正')
        solids.append(material)
    return compound(solids), dict(self_intersections=0, solids=len(solids), cavities=int(sum(depth % 2)))
