"""触れる模型(docs/FACE_EDIT.md の説明用ページ)のデータを、本物の計算から作る。

    .venv/Scripts/python.exe -m tools.face_edit_probe.viz out.json

合成形状ごとに、削除は「面を1〜数枚選んだ全組み合わせ」、オフセットは「各面 × 各距離」を厳しい版(strict.py: SA / SD)で
計算し、三角形の網・面の色分け(動いた面・延びた面・作り直した丸め)・断った理由を書き出す。関門が断ったときは、
比較用に関門なしの OCCT の答え(A / B をそのまま)と、それのどこが悪いか(残った面・ずれた面)も書き出す。
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from itertools import combinations

from OCP.BOPAlgo import BOPAlgo_RemoveFeatures
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GeomAbs import GeomAbs_CurveType
from OCP.Message import Message_Gravity
from OCP.TopAbs import TopAbs_Orientation
from OCP.TopLoc import TopLoc_Location
from OCP.TopTools import TopTools_ListOfShape

from . import geometry as g
from . import methods, shapes, strict
from .shapes import W, D, H, box, chamfer, cut, cylinder, fillet, fuse, prism_yz
from .worker import as_solid

KIND_JA = {"plane": "平面", "cylinder": "円筒", "cone": "円錐", "sphere": "球", "torus": "トーラス", "bspline": "自由曲面"}
DISTANCES = [-8, -6, -4, -3, -2, -1, 1, 2, 3, 4, 6, 8, 12, 20]
SCALE = 100  # 座標は 0.01 mm 単位の整数で持つ


# --------------------------------------------------------------------------------------
# 網
# --------------------------------------------------------------------------------------
class Meshes:
    """同じ形は1つにまとめる(多くの組み合わせが同じ結果になる)。"""

    def __init__(self):
        self.items, self.by_hash = {}, {}

    def add(self, shape):
        data = _mesh(shape)
        key = hashlib.sha1(json.dumps(data, separators=(",", ":")).encode()).hexdigest()[:16]
        if key not in self.by_hash:
            self.by_hash[key] = f"m{len(self.items)}"
            self.items[self.by_hash[key]] = data
        return self.by_hash[key]


def _mesh(shape):
    BRepMesh_IncrementalMesh(shape, 0.15, False, 0.25, True)
    positions, faces = [], []
    for face in g.faces_of(shape):
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:  # 幅0の面は三角形を持たない(段の蹴上げを端まで動かした OCCT の答え)
            faces.append({"k": g.surface_type(face), "t": []})
            continue
        trsf = location.Transformation()
        base = len(positions) // 3
        for i in range(1, triangulation.NbNodes() + 1):
            p = triangulation.Node(i).Transformed(trsf)
            positions += [round(p.X() * SCALE), round(p.Y() * SCALE), round(p.Z() * SCALE)]
        flip = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
        indices = []
        for t in range(1, triangulation.NbTriangles() + 1):
            a, b, c = triangulation.Triangle(t).Get()
            if flip:
                b, c = c, b
            indices += [base + a - 1, base + b - 1, base + c - 1]
        faces.append({"k": g.surface_type(face), "t": indices})
    edges = []
    for edge in g.edges_of(shape):
        if BRep_Tool.Degenerated_s(edge):
            continue
        curve = BRepAdaptor_Curve(edge)
        count = 2 if curve.GetType() == GeomAbs_CurveType.GeomAbs_Line else 33
        u0, u1 = curve.FirstParameter(), curve.LastParameter()
        line = []
        for i in range(count):
            p = curve.Value(u0 + (u1 - u0) * i / (count - 1))
            line += [round(p.X() * SCALE), round(p.Y() * SCALE), round(p.Z() * SCALE)]
        edges.append(line)
    return {"p": positions, "f": faces, "e": edges}


def face_labels(shape):
    """面ごとの短い名前(種類と向き)。"""
    labels = []
    for face in g.faces_of(shape):
        kind = g.surface_type(face)
        name = KIND_JA.get(kind, kind)
        if kind == "plane":
            n = g.face_normal(face)
            axis = max(range(3), key=lambda k: abs(n[k]))
            if abs(abs(n[axis]) - 1) < 1e-6:
                name += "(" + {(0, 1): "右", (0, -1): "左", (1, 1): "奥", (1, -1): "手前", (2, 1): "上", (2, -1): "下"}[(axis, 1 if n[axis] > 0 else -1)] + ")"
            else:
                name += "(斜め)"
        elif kind == "cylinder":
            definition = strict.describe_surface(face)
            name += f" r{definition[3]:g}" + ("(穴)" if strict._convexity(face, definition) < 0 else "")
        labels.append(name)
    return labels


# --------------------------------------------------------------------------------------
# 断った理由を人の言葉に
# --------------------------------------------------------------------------------------
def explain(message: str) -> str:
    rules = [
        ("RemoveFeatures warned", "周りの面を延ばしても閉じない(OCCT が「消せない」と警告した)"),
        ("MakeOffset: empty result", "この距離では形が作れない(OCCT が空の結果を返した)"),
        ("MakeOffset: IsDone", "OCCT が計算に失敗した"),
        ("BRepCheck invalid", "結果の形が壊れている(BRepCheck が不合格)"),
        ("area <", "幅が0になった面が残る(隣の面が消える移動)"),
        ("is not where it should be (kept", "選んでいない面までずれる(接線でつながる面ごと動く)"),
        ("is not where it should be (moved", "選んだ面が指定の距離だけ動いていない"),
        ("topology changed", "面が消える・割れる(形のつながりが変わる)"),
        ("not one-to-one", "面が消える・増える(形のつながりが変わる)"),
        ("volume moved the wrong way", "体積が距離の向きと逆に変わった"),
        ("tangent neighbour", "接線でつながる隣の面が、付け直せる丸めではない"),
        ("rests on another blend", "丸めの上に丸めが乗っている"),
        ("re-blend", "丸めを付け直せなかった"),
        ("result has", "結果が1つの閉じたソリッドにならない"),
        ("raw result", "結果が1つの閉じたソリッドにならない"),
        ("raw shell", "結果が閉じていない"),
        ("lost part of its original region", "残した面の元の範囲が消え、形が別の閉じ方につぶれた"),
        ("left its own surface", "残した面の曲面が変わった"),
        ("do not come from", "どこから来たか分からない面ができた"),
        ("is not removed", "選んだ面が消えていない"),
        ("lost", "残すはずの面が消えた"),
        ("distance must not be 0", "距離が0"),
    ]
    for key, text in rules:
        if key in message:
            return text
    return message


# --------------------------------------------------------------------------------------
# 1回の計算
# --------------------------------------------------------------------------------------
def _positions(after_shape, shapes_):
    final = strict._face_map(after_shape)
    return sorted({final.FindIndex(s) for s in shapes_} - {0})


def do_delete(shape, indices, meshes):
    all_faces = g.faces_of(shape)
    faces = [all_faces[i - 1] for i in indices]
    started = time.perf_counter()
    try:
        result, table, _ = strict.strict_delete(shape, faces)
    except Exception as error:  # noqa: BLE001
        out = {"ok": False, "why": explain(str(error)), "raw_error": str(error)[:160], "ms": _ms(started)}
        raw = _raw_delete(shape, faces, meshes)
        if raw:
            out["raw"] = raw
        return out
    neighbours = [g.face_index(shape, n) for n in g.neighbours(shape, faces)]
    extended = sorted({p for i in neighbours for p in table.get(i, [])})
    return {
        "ok": True, "mesh": meshes.add(result), "ms": _ms(started),
        "v0": round(g.volume(shape), 2), "v1": round(g.volume(result), 2), "n1": len(g.faces_of(result)),
        "hl": {"extended": extended},
    }


def _raw_delete(shape, faces, meshes):
    """関門なしの A。警告を無視して OCCT が返した形と、残ってしまった選択面。"""
    algo = BOPAlgo_RemoveFeatures()
    algo.SetShape(shape)
    selected = TopTools_ListOfShape()
    for face in faces:
        selected.Append(face)
    algo.AddFacesToRemove(selected)
    algo.SetToFillHistory(True)
    algo.Perform()
    if algo.HasErrors():
        return None
    result = algo.Shape()
    if result is None or result.IsNull() or not g.solids_of(result):
        return None
    warnings = [a.GetMessageKey() for a in algo.GetReport().GetAlerts(Message_Gravity.Message_Warning)]
    left = _positions(result, faces)
    same = abs(g.volume(result) - g.volume(shape)) < 1e-6 * max(1.0, g.volume(shape))
    note = "OCCT は警告を1つ出しただけで、"
    note += "元の形をそのまま返した" if same else "消せた面だけ消した形を返した"
    if not warnings:
        note = "OCCT は警告を出さずに形を返した"
    return {"mesh": meshes.add(result), "bad": left, "note": note, "warn": warnings}


def do_offset(shape, index, distance, meshes):
    all_faces = g.faces_of(shape)
    face = all_faces[index - 1]
    started = time.perf_counter()
    try:
        result, table, regenerated = strict.strict_follow(shape, [face], float(distance))
    except Exception as error:  # noqa: BLE001
        out = {"ok": False, "why": explain(str(error)), "raw_error": str(error)[:160], "ms": _ms(started)}
        raw = _raw_offset(shape, face, distance, meshes)
        if raw:
            out["raw"] = raw
        return out
    neighbours = [g.face_index(shape, n) for n in g.neighbours(shape, [face])]
    moved = table.get(index, [])
    extended = sorted({p for i in neighbours for p in table.get(i, [])} - set(moved) - set(regenerated))
    out = {
        "ok": True, "mesh": meshes.add(result), "ms": _ms(started),
        "v0": round(g.volume(shape), 2), "v1": round(g.volume(result), 2), "n1": len(g.faces_of(result)),
        "hl": {"moved": moved, "extended": extended, "regenerated": list(regenerated)},
    }
    if regenerated:
        raw = _raw_offset(shape, face, distance, meshes)
        if raw:
            out["raw"] = raw
    return out


def _raw_offset(shape, face, distance, meshes):
    """関門も丸めの付け直しも無しの B。選んでいないのに元の曲面を離れた面。"""
    try:
        raw, algo = methods.offset_faces(shape, [face], float(distance))
    except Exception:  # noqa: BLE001
        return None
    result = as_solid(raw)
    if not g.solids_of(result):
        return None
    bad = []
    for i, old in enumerate(g.faces_of(shape), start=1):
        positions = _positions(result, list(algo.Generated(old)))
        for p in positions:
            new = g.faces_of(result)[p - 1]
            try:
                fine = strict.lies_on(new, old, float(distance) if old.IsSame(face) else 0.0)
            except Exception:  # noqa: BLE001
                fine = False
            if not fine:
                bad.append(p)
    tiny = [k for k, f in enumerate(g.faces_of(result), start=1) if g.face_area(f) < 1e-6]
    if not bad and not tiny:
        note = "OCCT の答えに、この関門で見つかる問題は無い"
    elif tiny and not bad:
        note = "OCCT は幅0の面を残したまま、形として通る答えを返した"
    else:
        note = "OCCT は、接線でつながる面(赤)まで同じ距離ずらした形を返した"
    return {"mesh": meshes.add(result), "bad": sorted(set(bad + tiny)), "note": note,
            "v1": round(g.volume(result), 2)}


def _ms(started):
    return round(1000 * (time.perf_counter() - started), 1)


# --------------------------------------------------------------------------------------
# 場面
# --------------------------------------------------------------------------------------
def _top_front(m):
    return abs(m[1]) < 1e-6 and abs(m[2] - H) < 1e-6


def scenes():
    base = box(W, D, H)
    out = []
    s = fillet(base, _top_front, 5)
    out.append(("del_fillet", "delete", "フィレット1辺の箱", "丸め(円筒)を選んで消すと、元の角に戻る。平らな面を消すと閉じないので断る。", s, 3))
    s = chamfer(base, _top_front, 5)
    out.append(("del_chamfer", "delete", "面取り1辺の箱", "斜めの面を消すと元の角に戻る。", s, 2))
    small = cylinder(3, H + 2, (W / 2, D / 2, -1))
    s = cut(base, small, cylinder(5.5, 6, (W / 2, D / 2, 15)))
    out.append(("del_cbore", "delete", "座ぐり穴の箱", "穴の3面を全部消すと穴が埋まる。座ぐりの2面だけ消すと細い穴が残る。", s, 3))
    s = fuse(base, cylinder(5, 8, (W / 2, D / 2, H)))
    s = fillet(s, lambda m: abs(m[2] - H) < 1e-6 and math.hypot(m[0] - W / 2, m[1] - D / 2) < 5.5, 2)
    out.append(("del_boss", "delete", "根元が丸いボス", "側面・上面・根元の丸めの3面を消すとボスが消える。", s, 3))
    s = fillet(base, lambda m: abs(m[2] - H) < 1e-6, 4)
    out.append(("del_chain", "delete", "上面4辺のフィレット", "4枚全部消すと箱に戻る。1枚だけ消すと、残りの丸めが新しい角まで延びる。", s, 4))
    lblock = fuse(box(40, 30, 10), box(20, 30, 10, 0, 0, 10))
    out.append(("del_step", "delete", "段(L字)", "段の面を消すと閉じ方が2通りあって決まらないので断る。", lblock, 2))
    out.append(("off_box", "offset", "箱", "どの面も、隣の4面が伸び縮みして箱のまま寸法が変わる。底を越える距離は断る。", base, 1))
    s = cut(base, cylinder(5, H + 2, (W / 2, D / 2, -1)))
    out.append(("off_hole", "offset", "穴あき箱", "穴の面を正へ動かすと穴が細る(法線は穴の中心を向く)。壁を破る・半径0は断る。", s, 1))
    wedge = prism_yz([(0, 0), (30, 0), (30, 20), (10, 20), (0, 8)], W)
    out.append(("off_wedge", "offset", "斜面のある形", "上面を動かすと斜面が同じ傾きのまま延びる・縮む。", wedge, 1))
    s = fillet(base, _top_front, 5)
    out.append(("off_fillet", "offset", "フィレット1辺の箱", "上面・手前の面を動かすと丸めが付いてくる。「関門なしの OCCT の答え」を見ると、丸めの先の面まで動いている。", s, 1))
    s = fillet(fillet(base, _top_front, 5), lambda m: abs(m[1] - D) < 1e-6 and abs(m[2] - H) < 1e-6, 3)
    out.append(("off_two", "offset", "前 r5・後 r3 のフィレット", "上面を動かすと、半径の違う2つの丸めがそれぞれの半径で付いてくる。", s, 1))
    out.append(("off_step", "offset", "段(L字)", "蹴上げを右へ 20 動かすと踏み面の幅が0になるので断る。", lblock, 1))
    round_top = fuse(box(40, 10, 15), cylinder(5, 40, (0, 5, 15), (1, 0, 0)))
    out.append(("off_round", "offset", "上が全丸めの板", "側面に接する半円筒は外せない丸めなので、側面は動かせず断る。", round_top, 1))
    return out


def suggestions(scene):
    """ページの「試してみる」: 通る操作と、断る操作のうち見どころのあるもの(関門なしの答えが別の形になるもの)。"""
    labels = scene["labels"]
    out = []
    if scene["kind"] == "delete":
        oks = sorted((k for k, r in scene["results"].items() if r["ok"]), key=lambda k: (k.count(","), k))
        for key in oks[:4]:
            out.append({"faces": [int(i) for i in key.split(",")], "ok": True})
        refused = [k for k, r in scene["results"].items() if not r["ok"] and r.get("raw")]
        interesting = [k for k in refused if "つぶれ" in scene["results"][k]["why"]]
        interesting += [k for k in refused if k not in interesting and "," not in k]
        for key in interesting[:3]:
            out.append({"faces": [int(i) for i in key.split(",")], "ok": False})
    else:
        seen = set()
        for key, r in scene["results"].items():
            index, d = (int(x) for x in key.split(":"))
            follow = r["ok"] and r["hl"].get("regenerated")
            if follow and index not in seen and d == 3:
                out.append({"faces": [index], "d": d, "ok": True})
                seen.add(index)
        for key, r in scene["results"].items():
            index, d = (int(x) for x in key.split(":"))
            if not r["ok"] and index not in seen:
                out.append({"faces": [index], "d": d, "ok": False})
                seen.add(index)
        for key, r in scene["results"].items():
            index, d = (int(x) for x in key.split(":"))
            if r["ok"] and index not in seen and d == 3 and len(out) < 6:
                out.append({"faces": [index], "d": d, "ok": True})
                seen.add(index)
    for item in out:
        names = "・".join(labels[i - 1] for i in item["faces"])
        item["label"] = f"{names} を消す" if scene["kind"] == "delete" else f"{names} を {item['d']:+d}"
    return out[:7]


def build():
    meshes = Meshes()
    data = {"scenes": [], "distances": DISTANCES, "meshes": None, "made": time.strftime("%Y-%m-%d")}
    for key, kind, title, note, shape, size in scenes():
        started = time.perf_counter()
        faces = g.faces_of(shape)
        scene = {
            "id": key, "kind": kind, "title": title, "note": note,
            "before": meshes.add(shape), "labels": face_labels(shape), "v0": round(g.volume(shape), 2),
        }
        if kind == "delete":
            results = {}
            for k in range(1, size + 1):
                for combo in combinations(range(1, len(faces) + 1), k):
                    results[",".join(map(str, combo))] = do_delete(shape, list(combo), meshes)
            scene["results"] = results
            scene["max_pick"] = size
        else:
            results = {}
            for index in range(1, len(faces) + 1):
                for d in DISTANCES:
                    results[f"{index}:{d}"] = do_offset(shape, index, d, meshes)
            scene["results"] = results
            scene["max_pick"] = 1
        scene["try"] = suggestions(scene)
        data["scenes"].append(scene)
        ok = sum(1 for r in scene["results"].values() if r["ok"])
        print(f"{key:12s} faces={len(faces):3d} results={len(scene['results']):4d} ok={ok:4d} {time.perf_counter() - started:5.1f}s", file=sys.stderr)
    data["meshes"] = meshes.items
    return data


if __name__ == "__main__":
    data = build()
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    open(sys.argv[1], "w", encoding="utf-8").write(text)
    print(f"meshes={len(data['meshes'])} bytes={len(text.encode())}", file=sys.stderr)
