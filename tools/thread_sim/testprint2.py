"""2回目の試し刷り TP2 の一式(STL・配置図・結果を返すページ・事前の予測)を作る。

    .venv/Scripts/python.exe -X utf8 -m tools.thread_sim.testprint2

出力は output/thread_sim/testprint2/(git 管理外)。TP1(testprint.py)の部品・ページ・配置図の関数を使い回す。
目的(THREAD_SIM.md 6.3):
- 嵌合: TP1 に合わせた係数(printer.TP1_FIT)で上位になった半円・30° と、対照の 45° を、D6 P1.5 と D3 P1.2 で比べる。
  ナットの隙間は「はまらない」「軽く回りガタ無し」「ガタが出る」の境目をまたぐように選んだ(事前の予測を predictions.json に書く)。
- 強さ: 吊り板に通したボルトにナットを座らせ、ボルトの頭に重りを吊って、壊れる重さと場所を見る(模型の FEA と同じ荷重の掛け方)。
"""
from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

from . import profiles as pf
from . import stl_export as sx
from .testprint import (FLAGS_THREAD, OUTCOMES_THREAD, Piece, _sizes, layout_svg, make_bolt, make_nut, make_plate,
                        profile_svg, write_page)

OUT = Path(__file__).resolve().parents[2] / "output" / "thread_sim" / "testprint2"
TEST_ID = "TP2"  # 部品や隙間を変えたら上げる(結果の1行に入る)

# (記号, 表示名, 呼び径, 山形, 隙間の付け方, 頭とナットの角の数, ナットの隙間(雌雄の和・半径。ボルトは呼び寸法のまま))
# 角の数は TP1 と同じ対応(半円 = 八角、30° = 五角、45° = 四角)。隙間は、TP1 に合わせた係数で隙間を 0.01 刻みに振って出した
# 「はまらない/軽く回る/ガタ/山飛び」の境目(THREAD_SIM.md 6.3)をまたぐように、TP1 のねじの誤差の幅の両端で判定が揃う所から選んだ
SERIES = [
    ("S", "半円 D6 P1.5 ρ0.6(法線の隙間)", 6, lambda: pf.coil(6, 1.5, 0.6), "normal", 8, [0.12, 0.20, 0.25, 0.32]),
    ("T", "30°台形 D6 P1.5 深さ0.6", 6, lambda: pf.trapezoid(6, 1.5, 0.6, 30, 30), "radial", 5, [0.20, 0.32, 0.45, 0.58]),
    ("B", "45°台形 D6 P1.5 深さ0.6", 6, lambda: pf.trapezoid(6, 1.5, 0.6, 45, 45), "radial", 4, [0.08, 0.20, 0.40, 0.58]),
    ("U", "半円 D3 P1.2 ρ0.48(法線の隙間)", 3, lambda: pf.coil(3, 1.2, 0.48), "normal", 8, [0.10, 0.15, 0.19, 0.25]),
    ("V", "30°台形 D3 P1.2 深さ0.48(法線の隙間)", 3, lambda: pf.trapezoid(3, 1.2, 0.48, 30, 30), "normal", 5, [0.06, 0.17, 0.26, 0.33]),
    ("D", "45°台形 D3 P1.2 深さ0.48", 3, lambda: pf.trapezoid(3, 1.2, 0.48, 45, 45), "radial", 4, [0.05, 0.18, 0.31, 0.42]),
]
# 引き抜き: (記号, 表示名, 呼び径, 山形, 付け方, 角の数, 隙間, ナットの高さ, 本数)
PULL = [
    ("P", "半円 D3 P1.2 ρ0.48", 3, lambda: pf.coil(3, 1.2, 0.48), "normal", 8, 0.15, 2.4, 2),
    ("Q", "45°台形 D3 P1.2 深さ0.48", 3, lambda: pf.trapezoid(3, 1.2, 0.48, 45, 45), "radial", 4, 0.20, 3.0, 2),
]
E_THREAD = {6: (0.04, 0.06), 3: (0.0, 0.045)}  # TP1 のねじから読んだ寸法誤差(雌雄の和・半径)の幅(THREAD_SIM.md 6.1)
PLAY_MAX = 0.3  # ガタの境目の軸方向の遊び(TP1、report.PLAY_MAX と同じ)
E_REQ = 0.1  # 掛かりの下限(山飛び)

OUTCOMES_PULL = ["ボルトの軸が切れた", "ボルトのねじ山が潰れて抜けた", "ナットのねじ山が潰れて抜けた", "頭か吊り板が壊れた",
                 "壊れなかった", "試していない"]

INTRO = (
    '<div class="intro"><p><b>刷り方</b>: TP1 と同じ。<code>plate.stl</code> を ElegooSlicer で開き、プリンター「Elegoo Centauri Carbon 0.4 nozzle」・'
    'プロセス「0.20mm Standard @Elegoo CC 0.4 nozzle」・PLA のまま、設定は何も変えずに刷る(回転しない・サポート不要)。</p>'
    '<p><b>嵌合の試し方</b>: TP1 と同じ(冷めてから、工具なしで、凹み1個のナットから順に、初回のねじ込みで判定)。'
    '<b>「ガタが大きい」は、ナットをボルトの途中で止めて軸の向きに押し引きしたとき、カタカタ動くのが分かる</b>ときに付ける。</p>'
    '<p><b>引き抜きの試し方</b>(やらなくてもよい): 吊り板の両端の穴に紐を通して、丈夫な所から水平に吊る。ボルトを吊り板の真ん中の穴に下から通し、'
    '上からナットを手で回して吊り板に当たるまで締める(工具は使わない)。ボルトの頭の横穴に紐を通して袋を吊り、水の入ったペットボトルを1本ずつ足す。'
    '壊れたら、そのときの袋の重さ(はかりで量るか、ボトルの数 × 1本の重さ)を kg で書き、壊れた場所を選ぶ。'
    '<b>落ちる袋の下にはクッションを置き、足や手を下に入れない。</b></p>'
    '<p><b>返し方</b>: 各行を選び、数は入力欄に書く。下の「コピー」を押し、出てきた1行を次のメッセージに貼る。途中で閉じても入力は残る。</p></div>'
)


def make_pull_bolt(prof: pf.Profile, mark: int, length: float, af: float, head_h: float, sides: int, hole_d: float = 3.0):
    """頭に紐を通す横穴のあるボルト(頭を下に刷る)。横穴は頭の高さの中央、穴の上は橋渡しになる。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
    shape = make_bolt(prof, mark, length, af, head_h, sides)
    cyl = sx.tp.cylinder(hole_d / 2, -af, 2 * af)  # z 軸向きの円柱を作って x 軸向きに倒す
    rot = gp_Trsf()
    rot.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 1, 0)), math.pi / 2)
    mv = gp_Trsf()
    mv.SetTranslation(gp_Vec(0, 0, -head_h / 2))
    cyl = BRepBuilderAPI_Transform(BRepBuilderAPI_Transform(cyl, rot, True).Shape(), mv, True).Shape()
    return BRepAlgoAPI_Cut(shape, cyl).Shape()


def make_bracket(D: float, length: float = 50.0, width: float = 16.0, thickness: float = 5.0):
    """吊り板: 真ん中にボルトを通す穴(直径 D+1)、両端に紐の穴(直径 3.5)。平らに刷る。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec
    plate = BRepPrimAPI_MakeBox(gp_Pnt(-length / 2, -width / 2, 0), length, width, thickness).Shape()
    for x, d in ((0.0, D + 1.0), (-length / 2 + 5, 3.5), (length / 2 - 5, 3.5)):
        tr = gp_Trsf()
        tr.SetTranslation(gp_Vec(x, 0, 0))
        plate = BRepAlgoAPI_Cut(plate, BRepBuilderAPI_Transform(sx.tp.cylinder(d / 2, -0.5, thickness + 1), tr, True).Shape()).Shape()
    return plate


def _states(sid_or_spec, c, e, kw=None):
    """TP1 に合わせた係数で、誤差 e(雌雄の和・半径)のもとで刷ったときの余り・掛かり代・軸方向の遊び。ボルトは呼び寸法(split=0)。"""
    from . import evaluate as ev
    from .printer import DEFAULT, TP1_FIT
    name, D, mk, mode = sid_or_spec
    pr = DEFAULT.with_(**{**TP1_FIT, **(kw or {})})
    pr = pr.with_(contour_bias=pr.contour_bias + e / 2, hole_bias=pr.hole_bias - e / 2)
    r = ev.evaluate(ev.Design(name, mk(), c, mode=mode, L_nut=float(D), R_out=0.9 * D, split=0.0), pr, with_strength=False)
    return r.margin, r.engagement, r.play


def classify(margin, engagement, play):
    if margin < -0.005:
        return "はまらない"
    if engagement + 1e-9 < E_REQ:
        return "山飛び"
    if play > PLAY_MAX:
        return "ガタ"
    return "軽く回りガタ無し"


def predict(out: Path = OUT) -> dict:
    """事前の予測(結果を見る前に固定する)。ナットごとに、TP1 のねじの誤差の幅の両端で予測した判定。引き抜きは保持の荷重と壊れる所。"""
    from . import evaluate as ev
    from .printer import DEFAULT, TP1_FIT
    res = dict(series={}, pull={})
    for sid, name, D, mk, mode, _, cs in SERIES:
        nuts = []
        for c in sorted(cs, reverse=True):
            calls = {f"e{e:g}": classify(*_states((name, D, mk, mode), c, e)) for e in E_THREAD[D]}
            nuts.append(dict(c=c, predicted=calls))
        res["series"][sid] = dict(name=name, nuts=nuts)
    pr = DEFAULT.with_(**TP1_FIT)
    for sid, name, D, mk, mode, _, c, H, n in PULL:
        prof = mk()
        des = ev.Design(name, prof, c, mode=mode, L_nut=H - 2 * prof.depth, R_out=_pull_af(D) / 2, split=0.0)
        s = ev.evaluate(des, pr, with_strength=True).strength
        hold = min(s.F[k] for k in ("tcd", "strip_bolt", "strip_nut")) if s else None
        mode_ja = {"tcd": "ボルトの軸(芯の層間)", "strip_bolt": "ボルトのねじ山", "strip_nut": "ナットのねじ山"}
        res["pull"][sid] = dict(name=name, clearance=c, nut_height=H, engaged=round(H - 2 * prof.depth, 3),
                                hold_N=round(hold, 1) if hold else None, hold_kg=round(hold / 9.81, 1) if hold else None,
                                where=mode_ja.get(min(("tcd", "strip_bolt", "strip_nut"), key=s.F.get)) if s else None,
                                F={k: round(v, 1) for k, v in s.F.items()} if s else None)
    (out / "predictions.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def _pull_af(D):
    return max(1.6 * D, D + 4.0) + 2.0


def build(out: Path = OUT):
    out.mkdir(parents=True, exist_ok=True)
    pieces, groups = [], []
    for sid, name, D, mk, mode, sides, cs in SERIES:
        prof = mk()
        sz = _sizes(D)
        f = f"{sid}0_bolt.stl"
        sx.write_stl(make_bolt(prof, 0, sz["length"], sz["af"], sz["head_h"], sides), out / f)
        pieces.append(Piece(f"{sid}0", "bolt", f"{name} のボルト", f, dict(D=D, mode=mode, series=sid)))
        items = []
        for i, c in enumerate(sorted(cs, reverse=True), 1):
            f = f"{sid}{i}_nut_c{c:g}.stl"
            sx.write_stl(make_nut(pf.nut_of(prof, c, mode), i, sz["nut_h"], sz["af"], sides), out / f)
            pieces.append(Piece(f"{sid}{i}", "nut", f"{name} のナット{i}(凹み{i}個)", f, dict(D=D, mode=mode, clearance=c, series=sid)))
            items.append((f"{sid}{i}", f"ナット{i}(上面の凹み{i}個)", OUTCOMES_THREAD, FLAGS_THREAD))
        shape_ja = {4: "四角", 5: "五角", 6: "六角", 8: "八角"}[sides]
        mid = sorted(cs)[len(cs) // 2]
        groups.append((f"{sid}: {name}", f"頭とナットが{shape_ja}の組。凹み1個のナットから順に試す。1つ試したら外してから次へ。工具は使わない。",
                       items, profile_svg(prof, pf.nut_of(prof, mid, mode))))
    pitems = []
    for sid, name, D, mk, mode, sides, c, H, n in PULL:
        prof = mk()
        sz = _sizes(D)
        af = _pull_af(D)
        for k in range(1, n + 1):
            f = f"{sid}{k}_pullbolt.stl"
            sx.write_stl(make_pull_bolt(prof, 0, H + 5.0 + 2 * D, af, 7.0, sides), out / f)
            pieces.append(Piece(f"{sid}{k}", "bolt", f"引き抜き {name} のボルト {k}", f, dict(D=D, mode=mode, series=sid)))
            f = f"{sid}{k}n_pullnut_c{c:g}.stl"
            sx.write_stl(make_nut(pf.nut_of(prof, c, mode), k, H, af, sides), out / f)
            pieces.append(Piece(f"{sid}{k}n", "nut", f"引き抜き {name} のナット {k}(凹み{k}個)", f,
                                dict(D=D, mode=mode, clearance=c, series=sid, height=H)))
            pitems.append((f"{sid}{k}w", f"{sid}{k}({name}): 壊れたときの重さ kg", "number", []))
            pitems.append((f"{sid}{k}m", f"{sid}{k}: 壊れた場所", OUTCOMES_PULL, []))
    f = "R_bracket.stl"
    sx.write_stl(make_bracket(3.0), out / f)
    pieces.append(Piece("R", "bracket", "吊り板(D3 用)", f, dict(D=None)))
    groups.append(("P・Q: 引き抜き(やらなくてもよい)", "吊り板 R にボルトを下から通し、上からナットを手で締めて、頭の横穴に重りを吊る。"
                   "同じ組の2本は、同じ条件のくり返し。", pitems))
    plate = make_plate(out, pieces)
    (out / "manifest.json").write_text(json.dumps(dict(test_id=TEST_ID, pieces=[p.__dict__ for p in pieces], plate=plate),
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    layout = layout_svg(out, pieces, plate["placed"], series=SERIES,
                        extra_rows=[("P", "P・Q: 引き抜き(各2本。ボルトの頭に横穴、ナットは低い)", "P は半円 D3、Q は 45° D3。R は吊り板")],
                        notes=("記号の数字 0 は嵌合を試すボルト。P1・P2・Q1・Q2 は引き抜きのボルト、P1n などはその相手のナット。",
                               "ナットの番号 = 上面の凹みの数(1 が最も緩い)。", "実線の白丸はナットの穴、点線はボルトの軸。"))
    (out / "layout.svg").write_text(layout, encoding="utf-8")
    figure = ('<div class="intro"><p><b>配置図</b>(plate.stl を上から見た図。下が手前。系列は頭やナットの角の数で、'
              'ナットの番号は上面の凹みの数で分かる)</p>' + layout + '</div>')
    write_page(groups, out / "index.html", "ねじ試し刷り TP2 の結果", intro=INTRO + figure, test_id=TEST_ID)
    predict(out)
    return pieces


if __name__ == "__main__":
    ps = build()
    print(len(ps), "部品を", OUT, "へ")
