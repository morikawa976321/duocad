"""三角形メッシュのボディ(`Body.kind == "mesh"`)を測る・配る・書き出す。

**このモジュールはOCCTを一切使わない**(素のPythonだけ)。メッシュは既に三角形なので
B-repカーネルへ渡す理由が無く、渡すと下記のとおり壊れる。

**なぜメッシュを B-rep に直して編集verbを効かせないのか**:

1. **marching cubes の出力は粗い解像度でも数千三角形**、実用解像度なら数万枚になる。
   三角形1枚を1面とするシェルとしてOCCTへ入れると**面数が数万**になる。
2. **`kernel.face_count` は既存ボディで既に律速**である(`TopExp.MapShapes_s` を参照ごとに
   再走査する)。数万面では操作のたびに秒単位で待つ。
3. **三角形の辺にフィレットを掛けるのは幾何的に無意味**である。まともなB-repにするには
   面の再構成(平面・曲面フィッティング)が要り、それは別の仕事。

したがってメッシュボディにできるのは **表示 / STLへの書き出し / 削除 / 表示の切り替え /
名前の変更(`rename`。種別を問わない)** だけで、編集verbは `reject()` で拒否する。
"""
from __future__ import annotations

import json
import math
import os
import struct
from typing import Iterable, Iterator, NoReturn

from .document import Mesh, make_mesh

#: バイナリSTLの80バイトヘッダ。**先頭5文字を "solid" にしない** —— ASCII STLと
#: 誤判定するスライサがある。
_STL_HEADER = b"duocad binary STL"


def triangles(mesh: Mesh) -> Iterator[tuple]:
    positions = mesh.positions
    indices = mesh.indices
    for i in range(0, len(indices), 3):
        yield tuple(
            (positions[3 * n], positions[3 * n + 1], positions[3 * n + 2])
            for n in indices[i : i + 3]
        )


def bbox_payload(lows, highs, *, size: bool = False) -> dict:
    """外接直方体の配りもの `{"min", "max", "center", ("size",) "diagonal"}`。

    **キーの形はここが唯一の正**(層をまたぐ契約): `kernel.bounding_box` / `kernel.tessellate` の
    `bbox` / `bbox` が同じ形で組み、画面側が同じ形で読む。`size` は `kernel.bounding_box` だけが付ける。
    """
    lows = list(lows)
    highs = list(highs)
    payload = {
        "min": lows,
        "max": highs,
        "center": [(lows[i] + highs[i]) / 2.0 for i in range(3)],
    }
    if size:
        payload["size"] = [highs[i] - lows[i] for i in range(3)]
    payload["diagonal"] = math.dist(lows, highs)
    return payload


def bbox(mesh: Mesh) -> dict:
    """外接直方体(`bbox_payload`。三角形0枚のメッシュは作れないので空は無い)。"""
    positions = mesh.positions
    xs = positions[0::3]
    ys = positions[1::3]
    zs = positions[2::3]
    return bbox_payload([min(xs), min(ys), min(zs)], [max(xs), max(ys), max(zs)])


def volume(mesh: Mesh) -> float:
    """体積[mm^3]。符号付き四面体の総和(発散定理)の絶対値(B-repの `query.volume` が常に正なので揃える)。

    閉じていないメッシュでは値に意味が無いが、開いているかの判定はしない(辺の多重度の走査が重い)。
    """
    total = 0.0
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in triangles(mesh):
        total += (
            ax * (by * cz - bz * cy)
            - ay * (bx * cz - bz * cx)
            + az * (bx * cy - by * cx)
        )
    return abs(total) / 6.0


def geometry(mesh: Mesh) -> dict:
    """画面へ配る payload。`kernel.tessellate_selective` と同じキー構成。

    **三角形群を「面1枚」として配る** —— 画面側(`web/src/viewport.ts` の `FaceGeometry`)は面ごとに
    THREE.Mesh を作るので、三角形ごとに面を作るとドローコールが数万になる。面添字は payload の形を
    合わせるためだけで、面は選べない(`selection.select_face` はメッシュボディを拒否する)。
    """
    area = 0.0
    weighted = [0.0, 0.0, 0.0]
    for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in triangles(mesh):
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        face_area = math.sqrt(nx * nx + ny * ny + nz * nz) / 2.0
        area += face_area
        weighted[0] += face_area * (ax + bx + cx) / 3.0
        weighted[1] += face_area * (ay + by + cy) / 3.0
        weighted[2] += face_area * (az + bz + cz) / 3.0
    box = bbox(mesh)
    centroid = (
        [value / area for value in weighted] if area > 0 else list(box["center"])
    )
    return {
        # 持っている三角形をそのまま配るので偏差は無い(画面の可変解像度は効かない)。
        "deviation": 0.0,
        "coarse_deviation": 0.0,
        "fine_face_count": 1,
        "face_count": 1,
        "closed": False,
        "untriangulated": [],
        # 境界検査はB-repの三角形分割にしか無い
        "boundary": None,
        "bbox": box,
        "faces": [
            {
                "index": 1,
                "kind": "mesh",
                "area": area,
                "centroid": centroid,
                "normal": [0.0, 0.0, 1.0],
                "positions": list(mesh.positions),
                "indices": list(mesh.indices),
                "detail": "fine",
            }
        ],
    }


def write_stl(path, sources: Iterable[Iterable[tuple]]) -> None:
    """バイナリSTLを1本書く。`sources` は「三角形の並び」の並び(複数ボディを1本へ連結する)。B-repもメッシュもここを通る(`io_step.export_stl`)。

    三角形が1枚も無ければ ValueError で、ファイルを作らない。

    **書く前に float32 に丸めた実際の座標を検証する**(`validate_float32`): float64 で正常な三角形が
    丸めで潰れる(2頂点が一致・共線)ことがある。不正なら ValueError で止め、既存の出力ファイルには
    触らない(検証は open の前)。頂点を動かしたり三角形を捨てて通すことはしない。
    """
    body = bytearray()
    rounded = []
    count = 0
    for source in sources:
        for (ax, ay, az), (bx, by, bz), (cx, cy, cz) in source:
            ux, uy, uz = bx - ax, by - ay, bz - az
            vx, vy, vz = cx - ax, cy - ay, cz - az
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            length = math.sqrt(nx * nx + ny * ny + nz * nz)
            if length > 0.0:
                nx, ny, nz = nx / length, ny / length, nz / length
            else:
                # 退化三角形(面積0)。法線を作れないのでゼロを書く(STLの規約で許される)。
                nx = ny = nz = 0.0
            record = struct.pack("<12fH", nx, ny, nz, ax, ay, az, bx, by, bz, cx, cy, cz, 0)
            body += record
            rounded.append(struct.unpack_from("<9f", record, 12))
            count += 1
    if count == 0:
        raise ValueError("cannot write an empty mesh to STL")
    problems = validate_float32(rounded)
    if problems:
        raise ValueError("cannot write STL: float32 に丸めた三角形が不正 " + json.dumps(problems, ensure_ascii=False))
    filename = os.fspath(path)
    try:
        with open(filename, "wb") as handle:
            handle.write(_STL_HEADER.ljust(80, b"\0"))
            handle.write(struct.pack("<I", count))
            handle.write(body)
    except OSError:
        _unlink_quietly(filename)
        raise


def validate_float32(rounded: list) -> dict:
    """float32 に丸めた三角形(9値の並び)の不正を数える。空の辞書なら合格。

    有限でない値・縮退(面積が厳密に0。座標が一致する頂点を含む)を数え、最初の数枚の番号を添える。
    縮退の判定は `boundary_check.degenerate_mask`(固定εを使わない厳密な向き判定)。
    """
    import numpy as np

    from .boundary_check import degenerate_mask  # 素の numpy 実装。OCCT は使わない

    tri = np.asarray(rounded, dtype=np.float64).reshape(-1, 3, 3)
    problems = {}
    finite = np.isfinite(tri).all(axis=(1, 2))
    if not finite.all():
        problems["non_finite"] = dict(count=int((~finite).sum()), first=np.flatnonzero(~finite)[:10].tolist())
    zero = degenerate_mask(tri) & finite  # 有限でない三角形は縮退に数えない
    if zero.any():
        problems["degenerate"] = dict(count=int(zero.sum()), first=np.flatnonzero(zero)[:10].tolist())
    return problems


def _unlink_quietly(filename: str) -> None:
    try:
        os.remove(filename)
    except OSError:
        pass


def from_tessellation(result: dict) -> Mesh:
    """`kernel.tessellate` / `tessellate_selective` の戻り値を1つのメッシュへ畳む。

    面ごとの `indices` は面の中で0始まりなので、頂点のオフセットを足して連結する。
    """
    positions: list = []
    indices: list = []
    for face in result.get("faces", []):
        face_positions = face.get("positions") or []
        face_indices = face.get("indices") or []
        if not face_positions or not face_indices:
            continue
        offset = len(positions) // 3
        positions.extend(float(value) for value in face_positions)
        indices.extend(int(value) + offset for value in face_indices)
    if not indices:
        raise ValueError("tessellation produced no triangle")
    return make_mesh(positions, indices)


def reject(body, what: str) -> NoReturn:
    """メッシュボディへの編集要求を断る。できることを必ず並べる(文言を変えるときも後半を落とさないこと)。"""
    raise ValueError(
        f"body {body.id!r} is a mesh body: {what} できない"
        "(三角形メッシュには面・辺・領域の同一性が無い)。"
        "できるのは 表示 / STLへの書き出し(export_stl) / 削除(delete) / "
        "表示の切り替え(set_visible)"
    )
