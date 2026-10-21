"""干渉チェックのメッシュ経路(manifold3d。Issue #186。手順と限界は docs/INTERFERENCE.md「メッシュのボディが混ざるとき」)。

メッシュのボディを含む組は、B-rep のような General Fuse の像の共有が無いので、交差の体積と `min_gap` で決める。返す欄は
`kernel_interference.pair_relation` と揃える(`interference.classify` が同じ表で区分できるように)が、接触の種類(面・辺・点)は分からない
(`kind="contact"`)。B-rep は 0.01 mm で三角形分割して使うので、結果は**近似**。

OCP を直には import しない(B-rep の三角形分割は `topopt.finalize.tessellate_body` 越し)。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np

#: 隙間を「ゼロ」とみなす距離の帯[mm]。`interference.GAP_BAND_MM` と同じ値(依存を逆向きにしないため複製。`tests/test_interference_mesh.py` が一致を見る)。
GAP_BAND_MM = 1e-6

#: 交差の体積を「正」とみなす、小さい方のボディの体積に対する相対しきい値。-1e-15 などの丸めの負・ごく小さい値を 0 に寄せる。
VOLUME_RELATIVE = 1e-9


def manifold_of(vertices: np.ndarray, faces: np.ndarray) -> Any:
    import manifold3d

    mesh = manifold3d.Mesh64(
        vert_properties=np.ascontiguousarray(vertices, dtype=np.float64),
        tri_verts=np.ascontiguousarray(faces, dtype=np.uint64),
    )
    solid = manifold3d.Manifold(mesh)
    if solid.status() != manifold3d.Error.NoError:
        raise ValueError(f"manifold3d が入力を受け付けない: {solid.status()}")
    return solid


def manifold_of_triangles(positions: List[float], indices: List[int]) -> Tuple[Any, np.ndarray]:
    """メッシュのボディ(平らな頂点列・三角形列)から。座標が完全一致する頂点だけを共有させる。戻りの配列は頂点(境界箱用)。"""
    from .mesh_geometry import weld_exact

    points = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    triangles = np.asarray(indices, dtype=np.int64).reshape(-1, 3)
    vertices, faces = weld_exact(points[triangles])
    return manifold_of(vertices, faces), vertices


def manifold_of_shape(shape: Any) -> Tuple[Any, np.ndarray]:
    """B-rep を 0.01 mm(STL と同じ)で三角形分割して。三角形分割できない形は例外。

    分割は写しの上で作る(子プロセスは同じ形を後の組の GF に使い回す。分割が面に書き込まれると GF の結果が変わる。
    `kernel_interference.triangles` と同じ罠)。
    """
    from .kernel import copy_shape
    from .topopt.finalize import tessellate_body

    vertices, faces, _boundary = tessellate_body(copy_shape(shape))
    return manifold_of(vertices, faces), vertices


def oriented_min_side(vertices: np.ndarray) -> float:
    """頂点の主成分方向の箱の最小辺(有向境界箱の近似)。"""
    centered = vertices - vertices.mean(axis=0)
    if len(centered) < 4:
        return 0.0
    _values, axes = np.linalg.eigh(centered.T @ centered)
    projected = centered @ axes
    return float((projected.max(axis=0) - projected.min(axis=0)).min())


def mesh_pair(first: Any, second: Any, search: float, deviation: Optional[float] = None) -> Dict[str, Any]:
    """2つのメッシュの関係。手順: 交差の体積が正なら重なり(連結成分ごとに 2V/A と有向箱の最小辺を測り、最も厚い成分を返す)、
    体積が 0 なら `min_gap`(探索幅 `search`)が帯以下で接触、それより大きければ離れている。

    `min_gap` は結果を [0, search] に丸める(search を 0 にすると全部 0=接触になる)。丸めに当たったら `distance_lower_bound` を真にする。
    戻り値は `kernel_interference.pair_relation` と同じ `kind`(overlap / contact / none / unknown)。
    """
    volume_a, volume_b = first.volume(), second.volume()
    smallest = min(volume_a, volume_b)
    if not (smallest > 0):
        return {"kind": "unknown", "reason": "メッシュの体積が 0 以下", "warnings": []}
    intersection = first ^ second
    volume = max(intersection.volume(), 0.0)
    if volume > VOLUME_RELATIVE * smallest:
        return _overlap(intersection, volume, deviation)
    gap = first.min_gap(second, search)
    if gap <= GAP_BAND_MM:
        return {"kind": "contact", "warnings": []}
    return {"kind": "none", "distance": gap, "distance_lower_bound": gap >= search, "warnings": []}


def _overlap(intersection: Any, volume: float, deviation: Optional[float]) -> Dict[str, Any]:
    thickness, obb_min, components = 0.0, 0.0, intersection.decompose()
    for component in components:
        area = component.surface_area()
        if not (area > 0):
            return {"kind": "unknown", "reason": f"重なりの面積が不正: A={area}", "warnings": []}
        mesh = component.to_mesh64()
        thickness = max(thickness, 2 * component.volume() / area)
        obb_min = max(obb_min, oriented_min_side(np.asarray(mesh.vert_properties)[:, :3]))
    whole = intersection.to_mesh64()
    points = np.asarray(whole.vert_properties)[:, :3]
    result: Dict[str, Any] = {
        "kind": "overlap", "volume": volume, "thickness": thickness, "obb_min": obb_min,
        "regions": len(components), "pieces": len(components), "warnings": [],
        "center": _centroid(points, np.asarray(whole.tri_verts)),
        "bbox": {"min": points.min(axis=0).tolist(), "max": points.max(axis=0).tolist()},
    }
    if deviation:
        mesh = whole
        result["render"] = {"solid": {
            "positions": np.asarray(mesh.vert_properties)[:, :3].reshape(-1).tolist(),
            "indices": np.asarray(mesh.tri_verts).reshape(-1).astype(int).tolist(),
        }}
    return result


def _centroid(points: np.ndarray, triangles: np.ndarray) -> List[float]:
    """閉じた三角形メッシュの体積重心(原点との四面体の符号つき体積で重みづけ)。"""
    a, b, c = (points[triangles[:, k]] for k in range(3))
    volumes = np.einsum("ij,ij->i", a, np.cross(b, c)) / 6
    total = volumes.sum()
    if not total:
        return points.mean(axis=0).tolist()
    return ((((a + b + c) / 4) * volumes[:, None]).sum(axis=0) / total).tolist()
