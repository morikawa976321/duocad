"""試し刷り TP5 の一式: ねじ verb(duocad の `thread`)で作ったボルトとナット(STL・配置図・結果を返すページ・事前の予測)。

    .venv/Scripts/python.exe -X utf8 -m tools.thread_sim.testprint5

出力は output/thread_sim/testprint5/(git 管理外)。部品は duocad の Session を通して verb で作る(スケッチ → 押し出し → `thread`
→ ナットの凹み)。作った文書は同じフォルダの models/tp5.json に残る(duocad で開ける)。ページ・配置図・予測は TP4(testprint2)と同じ部品を使い回す。

確かめること(本人の指示 2026-10-23、decisions.md「ねじ verb を実装し…」): (1) verb が評価した形を作るか、(2) 小径 D3、
(3) 0.12mm Fine での刷るたびの再現(D6 半円の 0.11・0.14 は TP4 の S3・S2 と同じ隙間)、(4) 大径 D10。強さは確かめない(本人の決定)。
ピッチは verb の既定(D3 → 1.2、D6 → 1.5、D10 → 2.5)、ボルトは隙間 0、ナットは verb の推奨の両端と既定値。
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

from duocad import io_step, kernel, meshbody
from duocad.session import Session

from . import profiles as pf
from .testprint import FLAGS_THREAD, OUTCOMES_THREAD, Piece, _sizes, layout_svg, make_plate, profile_svg, write_page
from .testprint2 import predict

OUT = Path(__file__).resolve().parents[2] / "output" / "thread_sim" / "testprint5"
TEST_ID = "TP5"
DIMPLE_D, DIMPLE_DEPTH = 1.2, 0.6  # TP1〜TP4 の凹みと同じ


def _series(sid, label, D, profile, sides, cs):
    from duocad import kernel_thread as kt
    P = kt.default_pitch(D)
    if profile == "semicircle":
        mk, mode = (lambda: pf.coil(D, P, 0.4 * P)), "normal"
    else:
        beta = kt.PROFILES[profile]["beta"]
        mk, mode = (lambda: pf.trapezoid(D, P, 0.4 * P, beta, beta)), "radial"
    return (sid, f"{label} D{D} P{P:g}(ねじ verb)", D, mk, mode, sides, cs, profile)


# (記号, 山形, 呼び径, verb の山形, 角の数, ナットの隙間)。角の数は TP1〜TP4 と同じ対応(半円 = 八角、30° = 五角、45° = 四角)。
# 記号の頭文字が1列(make_plate)。D6 は TP4 と同じ S・T・B。
SERIES_TP5 = [
    _series("F", "半円", 3, "semicircle", 8, [0.11, 0.12, 0.14]),
    _series("G", "30°台形", 3, "trapezoid30", 5, [0.18, 0.20, 0.22]),
    _series("H", "45°台形", 3, "trapezoid45", 4, [0.11, 0.12, 0.13]),
    _series("S", "半円", 6, "semicircle", 8, [0.11, 0.12, 0.14]),
    _series("T", "30°台形", 6, "trapezoid30", 5, [0.18, 0.20, 0.22]),
    _series("B", "45°台形", 6, "trapezoid45", 4, [0.11, 0.12, 0.13]),
    _series("X", "半円", 10, "semicircle", 8, [0.12]),
    _series("Y", "30°台形", 10, "trapezoid30", 5, [0.20]),
    _series("Z", "45°台形", 10, "trapezoid45", 4, [0.12]),
]

INTRO_TP5 = (
    '<div class="intro"><p><b>刷り方</b>: <code>plate.stl</code> を ElegooSlicer で開き、プリンター「Elegoo Centauri Carbon 0.4 nozzle」・'
    '<b>プロセス「0.12mm Fine @Elegoo CC 0.4 nozzle」</b>・PLA のまま、ほかは何も変えずに刷る(回転しない・サポート不要)。</p>'
    '<p><b>部品はすべて duocad のねじ機能で作った</b>(穴の入口とボルトの先の面取りも機能が付けたもの)。</p>'
    '<p><b>試し方</b>: TP4 と同じ。冷めてから、工具なしで、凹み1個のナットから順に、初回のねじ込みで判定する。'
    '<b>「ガタが大きい」は、ナットをボルトの途中で止めて軸の向きに押し引きしたとき、カタカタ動くのが分かる</b>ときに付ける。'
    '刷れなかった・欠けた部品があれば、その行で「部品が欠けた・刷れなかった」に印を付ける。</p>'
    '<p><b>部品は捨てない</b>: 結果を返した後も、次の試し刷りの結果が出るまで全部の部品を袋などにまとめて取っておく(確かめ直しに使うことがある)。</p>'
    '<p><b>返し方</b>: 各行を1つクリック(点線の枠は当てはまるときだけ追加)。下の「コピー」を押し、出てきた1行を次のメッセージに貼る。途中で閉じても選択は残る。</p></div>'
)


# --------------------------------------------------------------------------------------
# duocad で部品を作る
# --------------------------------------------------------------------------------------
def _polygon(session: Session, sketch_id: str, cx: float, cy: float, af: float, sides: int) -> None:
    """二面幅 af の正多角形(辺の中点が +y の向き)を線分で描く。"""
    R = af / 2 / math.cos(math.pi / sides)
    pts = [(cx + R * math.cos(math.pi / 2 + math.pi / sides + 2 * math.pi * k / sides),
            cy + R * math.sin(math.pi / 2 + math.pi / sides + 2 * math.pi * k / sides)) for k in range(sides)]
    for a, b in zip(pts, pts[1:] + pts[:1]):
        session.add_line(sketch_id, list(a), list(b))


def _select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(refs, [])


def _region_by_area(session: Session, sketch_id: str, area: float) -> int:
    """面積が `area` に最も近い領域の番号。"""
    best = None
    for index in range(1, 64):
        try:
            _select(session, [{"sketch": sketch_id, "index": index}])
            got = session.measure()["total_area"]
        except Exception:  # noqa: BLE001 — 番号が尽きた
            break
        if best is None or abs(got - area) < best[1]:
            best = (index, abs(got - area))
    return best[0]


def _cylinder_face(session: Session, body_id: str) -> int:
    face_map = kernel._face_map(session.document.get_body(body_id).shape)
    found = [i for i in range(1, face_map.Extent() + 1) if kernel.classify_face(face_map.FindKey(i)) == "cylinder_full"]
    assert len(found) == 1, found
    return found[0]


def _polygon_area(af: float, sides: int) -> float:
    return sides * (af / 2) ** 2 * math.tan(math.pi / sides)


def _thread(session: Session, body_id: str, profile: str, clearance: float) -> None:
    _select(session, [{"body": body_id, "kind": "face", "index": _cylinder_face(session, body_id)}])
    session.thread(profile, clearance)


def make_bolt(session: Session, cx: float, cy: float, D: float, profile: str, sides: int) -> str:
    sz = _sizes(D)
    sk = session.sketch_create("XY")["sketch_id"]
    _polygon(session, sk, cx, cy, sz["af"], sides)
    _select(session, [{"sketch": sk, "index": 1}])
    body_id = session.extrude(sz["head_h"], mode="new")["body_id"]
    sk = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sk, [cx, cy], float(D))
    _select(session, [{"sketch": sk, "index": 1}])
    session.extrude(sz["head_h"] + sz["length"], mode="join", body_id=body_id)
    _thread(session, body_id, profile, 0.0)
    return body_id


def make_nut(session: Session, cx: float, cy: float, D: float, profile: str, sides: int, clearance: float, mark: int) -> str:
    from duocad import kernel_thread as kt
    sz = _sizes(D)
    sk = session.sketch_create("XY")["sketch_id"]
    _polygon(session, sk, cx, cy, sz["af"], sides)
    session.add_circle(sk, [cx, cy], float(D))
    _select(session, [{"sketch": sk, "index": _region_by_area(session, sk, _polygon_area(sz["af"], sides) - math.pi * D * D / 4)}])
    body_id = session.extrude(sz["nut_h"], mode="new")["body_id"]
    _thread(session, body_id, profile, clearance)
    # 凹み(上面)。入口の面取り(深さ 0.4P)の外と、多角形の辺の内側の間の輪に並べる
    ring = 0.5 * (D / 2 + clearance + kt.DEPTH_PER_PITCH * kt.default_pitch(D) + sz["af"] / 2)
    sk = session.sketch_create_offset(base_plane="XY", distance=sz["nut_h"])["sketch_id"]
    for k in range(mark):
        a = math.radians(90 + 40 * (k - (mark - 1) / 2))
        session.add_circle(sk, [cx + ring * math.cos(a), cy + ring * math.sin(a)], DIMPLE_D)
    _select(session, [{"sketch": sk, "index": i} for i in range(1, mark + 1)])
    session.extrude(-DIMPLE_DEPTH, mode="cut", body_id=body_id)
    return body_id


def _write_body_stl(session: Session, body_id: str, path: Path) -> None:
    meshbody.write_stl(path, [io_step._triangles_of(session.document.get_body(body_id))])


def build(out: Path = OUT, series=SERIES_TP5) -> list:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    session = Session(io_step.STL_DEVIATION, models_dir=out / "models")
    session.new_document()
    pieces, groups, x = [], [], 0.0
    for sid, name, D, mk, mode, sides, cs, profile in series:
        step = _sizes(D)["af"] + 6.0
        body = make_bolt(session, x, 0.0, D, profile, sides)
        f = f"{sid}0_bolt.stl"
        _write_body_stl(session, body, out / f)
        pieces.append(Piece(f"{sid}0", "bolt", f"{name} のボルト", f, dict(D=D, mode=mode, series=sid, profile=profile)))
        items = []
        for i, c in enumerate(sorted(cs, reverse=True), 1):
            body = make_nut(session, x, step * i, D, profile, sides, c, i)
            f = f"{sid}{i}_nut_c{c:g}.stl"
            _write_body_stl(session, body, out / f)
            pieces.append(Piece(f"{sid}{i}", "nut", f"{name} のナット{i}(凹み{i}個)", f,
                                dict(D=D, mode=mode, clearance=c, series=sid, profile=profile)))
            items.append((f"{sid}{i}", f"ナット{i}(上面の凹み{i}個)", OUTCOMES_THREAD, FLAGS_THREAD + ["部品が欠けた・刷れなかった"]))
        x += step
        shape_ja = {4: "四角", 5: "五角", 6: "六角", 8: "八角"}[sides]
        prof = mk()
        mid = sorted(cs)[len(cs) // 2]
        groups.append((f"{sid}: {name}", f"頭とナットが{shape_ja}の組。凹み1個のナットから順に試す。1つ試したら外してから次へ。工具は使わない。",
                       items, profile_svg(prof, pf.nut_of(prof, mid, mode))))
    session.save_document("tp5.json")
    groups.insert(0, ("刷った設定", "この板を刷ったときのプロセス(層の厚み)を選ぶ。",
                      [("L", "刷ったプロセス", ["0.12mm Fine", "0.20mm Standard", "その他", "未回答"], [])]))
    plate = make_plate(out, pieces)
    (out / "manifest.json").write_text(json.dumps(dict(test_id=TEST_ID, pieces=[p.__dict__ for p in pieces], plate=plate),
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    plain = [s[:7] for s in series]
    layout = layout_svg(out, pieces, plate["placed"], series=plain, extra_rows=[],
                        notes=("記号の数字 0 はボルト。", "ナットの番号 = 上面の凹みの数(1 が最も緩い)。", "実線の白丸はナットの穴、点線はボルトの軸。"))
    (out / "layout.svg").write_text(layout, encoding="utf-8")
    figure = ('<div class="intro"><p><b>配置図</b>(plate.stl を上から見た図。下が手前。系列は頭やナットの角の数と大きさで、'
              'ナットの番号は上面の凹みの数で分かる)</p>' + layout + '</div>')
    write_page(groups, out / "index.html", f"ねじ試し刷り {TEST_ID} の結果", intro=INTRO_TP5 + figure, test_id=TEST_ID)
    from .printer import FINE
    predict(out, series=plain, base=FINE, e_range={3: (0.0, 0.05), 6: (0.0, 0.05), 10: (0.0, 0.05)}, play_range=(0.15, 0.18))
    return pieces


if __name__ == "__main__":
    ps = build()
    print(len(ps), "部品を", OUT, "へ")
