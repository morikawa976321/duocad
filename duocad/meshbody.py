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

したがってメッシュボディにできるのは **表示 / STLへの書き出し / 削除 / 表示の切り替え**
だけで、編集verbは `reject()` で拒否する。

**名前の変更は「できること」に入れていない** —— duocadには**どの種別のボディにも**改名の
verbが無い。メッシュの名前は `add_mesh_body(name=...)` で作るときに決める。
"""
from __future__ import annotations

import math
import os
import struct
from typing import Iterable, Iterator, NoReturn

from .document import Mesh, make_mesh

#: バイナリSTLの80バイトヘッダ。**先頭5文字を "solid" にしない** —— ASCII STLと
#: 誤判定するスライサがある。
_STL_HEADER = b"duocad binary STL"


def triangles(mesh: Mesh) -> Iterator[tuple]:
    """三角形を「頂点3つの組」で1枚ずつ返す。"""
    positions = mesh.positions
    indices = mesh.indices
    for i in range(0, len(indices), 3):
        yield tuple(
            (positions[3 * n], positions[3 * n + 1], positions[3 * n + 2])
            for n in indices[i : i + 3]
        )


def bbox(mesh: Mesh) -> dict:
    """外接直方体。**キーは `kernel.tessellate` の `bbox` と同じ**(画面側が同じ形で読む)。"""
    positions = mesh.positions
    xs = positions[0::3]
    ys = positions[1::3]
    zs = positions[2::3]
    low = [min(xs), min(ys), min(zs)]
    high = [max(xs), max(ys), max(zs)]
    return {
        "min": low,
        "max": high,
        "center": [(low[i] + high[i]) / 2.0 for i in range(3)],
        "diagonal": math.dist(low, high),
    }


def volume(mesh: Mesh) -> float:
    """体積[mm^3]。符号付き四面体の総和(発散定理)の**絶対値**。

    絶対値にするのは、巻き順が内向きに揃ったメッシュでも負の値を画面へ出さないため
    (B-repの `query.volume` は常に正を返すので、そちらへ揃える)。閉じていないメッシュでは
    値に意味が無いが、**開いているかどうかをここで判定はしない** —— 判定には辺の多重度の
    走査が要り、体積を1つ出すためだけに払う費用としては重い。
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
    """画面へ配る payload。**`kernel.tessellate_selective` と同じキー構成**にする。

    **三角形群を「面1枚」として配る**(三角形1枚を1面にしない) —— 画面側
    (`web/src/viewport.ts` の `FaceGeometry`)は面ごとに1つのTHREE.Meshを作るので、
    三角形の数だけ面を作るとドローコールが数万になる。面添字を持たせるのは
    payload の形を合わせるためだけで、**この添字で面を選べるわけではない**
    (`selection.select_face` はメッシュボディを拒否する)。
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
        # **偏差は 0.0**。持っている三角形をそのまま配るので「どれくらい細かく測ったか」が
        # 無い(画面の可変解像度はメッシュボディには効かない)。
        "deviation": 0.0,
        "coarse_deviation": 0.0,
        "fine_face_count": 1,
        "face_count": 1,
        "closed": False,
        "untriangulated": [],
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
    """バイナリSTLを1本書く。`sources` は**「三角形の並び」の並び**(複数ボディを1本へ連結する)。

    三角形が1枚も無ければ ValueError で落とし、**ファイルを作らない**(0バイトの成果物を
    残さない。`kernel.write_stl` と同じ規律)。
    """
    body = bytearray()
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
            body += struct.pack(
                "<12fH", nx, ny, nz, ax, ay, az, bx, by, bz, cx, cy, cz, 0
            )
            count += 1
    if count == 0:
        raise ValueError("cannot write an empty mesh to STL")
    filename = os.fspath(path)
    try:
        with open(filename, "wb") as handle:
            handle.write(_STL_HEADER.ljust(80, b"\0"))
            handle.write(struct.pack("<I", count))
            handle.write(body)
    except OSError:
        _unlink_quietly(filename)
        raise


def _unlink_quietly(filename: str) -> None:
    """書き出し失敗時に半端なファイルを残さない(`kernel._unlink_quietly` と同じ)。"""
    try:
        os.remove(filename)
    except OSError:
        pass


def from_tessellation(result: dict) -> Mesh:
    """`kernel.tessellate` / `tessellate_selective` の戻り値を1つのメッシュへ畳む。

    **面をまたぐときに頂点のオフセットを足す**(面ごとの `indices` は面の中で0始まりなので、
    そのまま連結すると全部の面が1枚目の頂点を指す)。
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
    """メッシュボディへの編集要求を断る。**できることを必ず並べる**。

    「できない」だけで終えると利用者は次に何をすればよいか分からない。
    文言を変えるときもこの後半を落とさないこと。
    """
    raise ValueError(
        f"body {body.id!r} is a mesh body: {what} できない"
        "(三角形メッシュには面・辺・領域の同一性が無い)。"
        "できるのは 表示 / STLへの書き出し(export_stl) / 削除(delete) / "
        "表示の切り替え(set_visible)"
    )
