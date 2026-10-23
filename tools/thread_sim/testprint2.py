"""2回目の試し刷り TP2 の一式(STL・配置図・結果を返すページ・事前の予測)を作る。

    .venv/Scripts/python.exe -X utf8 -m tools.thread_sim.testprint2

出力は output/thread_sim/testprint2/(git 管理外)。TP1(testprint.py)の部品・ページ・配置図の関数を使い回す。
目的(THREAD_SIM.md 6.3): TP1 に合わせた係数(printer.TP1_FIT)で上位になった半円・30° と、対照の 45° を、同じ D6 P1.5 で比べる。
ナットの隙間は「はまらない/使える幅の締まり側/使える幅の緩み側/使えない(ガタか山飛び)」の4点(事前の予測を predictions.json に書く)。
引き抜き(PULL)は、家での吊り試験の安全と支持の確かめが済むまで別の試験に回す(astra の7回目の意見)。コードは残し、PULL が空なら作らない。
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
    ("S", "半円 D6 P1.5 ρ0.6(法線の隙間)", 6, lambda: pf.coil(6, 1.5, 0.6), "normal", 8, [0.13, 0.17, 0.22, 0.27]),
    ("T", "30°台形 D6 P1.5 深さ0.6", 6, lambda: pf.trapezoid(6, 1.5, 0.6, 30, 30), "radial", 5, [0.22, 0.30, 0.47, 0.55]),
    ("B", "45°台形 D6 P1.5 深さ0.6", 6, lambda: pf.trapezoid(6, 1.5, 0.6, 45, 45), "radial", 4, [0.10, 0.17, 0.26, 0.34]),
]
# TP3: 本人のプリントの 99% が使う 0.12mm Fine(層 0.12・壁3周)で刷る。同じ D6 P1.5 の3系列と、45° の複製(E、六角)で同じ板の中の判定の揺れを見る。
# 隙間は 0.12mm Fine の模型で、誤差 0〜0.05 とガタの境目 0.15〜0.28 のどの組でも判定が変わらない点(B3・E3 の 0.18 だけはガタの境目で分かれる点)
SERIES_TP3 = [
    ("S", "半円 D6 P1.5 ρ0.6(法線の隙間)", 6, lambda: pf.coil(6, 1.5, 0.6), "normal", 8, [0.07, 0.11, 0.14, 0.24]),
    ("T", "30°台形 D6 P1.5 深さ0.6", 6, lambda: pf.trapezoid(6, 1.5, 0.6, 30, 30), "radial", 5, [0.10, 0.20, 0.24, 0.44]),
    ("B", "45°台形 D6 P1.5 深さ0.6", 6, lambda: pf.trapezoid(6, 1.5, 0.6, 45, 45), "radial", 4, [0.04, 0.12, 0.18, 0.28]),
    ("E", "45°台形 D6 P1.5 深さ0.6(B の複製)", 6, lambda: pf.trapezoid(6, 1.5, 0.6, 45, 45), "radial", 6, [0.04, 0.12, 0.18, 0.28]),
]
# 引き抜き: (記号, 表示名, 呼び径, 山形, 付け方, 角の数, 隙間, ナットの高さ, 本数)
# 例(次の試験の候補): ("P", "半円 D3 P1.2 ρ0.48", 3, lambda: pf.coil(3, 1.2, 0.48), "normal", 8, 0.15, 2.4, 2)
PULL = []
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
    '刷れなかった・欠けた部品があれば、その行で「部品が欠けた・刷れなかった」に印を付ける(印刷のしやすさの記録になる)。</p>'
    '<p><b>部品は捨てない</b>: 結果を返した後も、次の試し刷りの結果が出るまで全部の部品を袋などにまとめて取っておく(確かめ直しに使うことがある)。</p>'
    '<p><b>返し方</b>: 各行を1つクリック(点線の枠は当てはまるときだけ追加)。下の「コピー」を押し、出てきた1行を次のメッセージに貼る。途中で閉じても選択は残る。</p></div>'
)


INTRO_TP3 = (
    '<div class="intro"><p><b>刷り方</b>: <code>plate.stl</code> を ElegooSlicer で開き、プリンター「Elegoo Centauri Carbon 0.4 nozzle」・'
    '<b>プロセス「0.12mm Fine @Elegoo CC 0.4 nozzle」</b>・PLA のまま、ほかは何も変えずに刷る(回転しない・サポート不要)。</p>'
    '<p><b>試し方</b>: TP2 と同じ。冷めてから、工具なしで、凹み1個のナットから順に、初回のねじ込みで判定する。'
    '<b>「ガタが大きい」は、ナットをボルトの途中で止めて軸の向きに押し引きしたとき、カタカタ動くのが分かる</b>ときに付ける。'
    'B(四角)と E(六角)は同じ設計なので、それぞれ別に判定する(見比べて揃えない)。'
    '刷れなかった・欠けた部品があれば、その行で「部品が欠けた・刷れなかった」に印を付ける。</p>'
    '<p><b>部品は捨てない</b>: 結果を返した後も、次の試し刷りの結果が出るまで全部の部品を袋などにまとめて取っておく(確かめ直しに使うことがある)。</p>'
    '<p><b>返し方</b>: 各行を1つクリック(点線の枠は当てはまるときだけ追加)。下の「コピー」を押し、出てきた1行を次のメッセージに貼る。途中で閉じても選択は残る。</p></div>'
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


def _states(sid_or_spec, c, e, kw=None, base=None):
    """TP1 に合わせた係数で、誤差 e(雌雄の和・半径)のもとで刷ったときの余り・掛かり代・軸方向の遊び。ボルトは呼び寸法(split=0)。
    base は印刷の設定(既定は 0.20mm Standard の DEFAULT、0.12mm Fine なら printer.FINE)。"""
    from . import evaluate as ev
    from .printer import DEFAULT, TP1_FIT
    name, D, mk, mode = sid_or_spec
    pr = (base or DEFAULT).with_(**{**TP1_FIT, **(kw or {})})
    pr = pr.with_(contour_bias=pr.contour_bias + e / 2, hole_bias=pr.hole_bias - e / 2)
    r = ev.evaluate(ev.Design(name, mk(), c, mode=mode, L_nut=float(D), R_out=0.9 * D, split=0.0), pr, with_strength=False)
    return r.margin, r.engagement, r.play


def classify(margin, engagement, play, play_max=PLAY_MAX):
    if margin < -0.005:
        return "はまらない"
    if engagement + 1e-9 < E_REQ:
        return "山飛び"
    if play > play_max:
        return "ガタ"
    return "軽く回りガタ無し"


def predict(out: Path = OUT, series=None, base=None, e_range=None, play_range=(PLAY_MAX,)) -> dict:
    """事前の予測(結果を見る前に固定する)。ナットごとに、誤差の幅の両端 × ガタの境目の候補で予測した判定。引き抜きは保持の荷重と壊れる所。"""
    from . import evaluate as ev
    from .printer import DEFAULT, TP1_FIT
    res = dict(series={}, pull={})
    for sid, name, D, mk, mode, _, cs in (series or SERIES):
        nuts = []
        for c in sorted(cs, reverse=True):
            calls = {}
            for e in (e_range or E_THREAD)[D]:
                st = _states((name, D, mk, mode), c, e, base=base)
                for pm in play_range:
                    calls[f"e{e:g} ガタ境目{pm:g}"] = classify(*st, play_max=pm)
            nuts.append(dict(c=c, predicted=calls))
        res["series"][sid] = dict(name=name, nuts=nuts)
    pr = DEFAULT.with_(**TP1_FIT)
    for sid, name, D, mk, mode, _, c, H, n in PULL:
        prof = mk()
        # ナットの両端の面取りは雌の山形の深さで切る(stl_export.nut)ので、ねじの掛かる長さもそれで測る
        nprof = sx.simplify_profile(pf.nut_of(prof, c, mode))
        engaged = H - 2 * (max(r for _, r in nprof) - min(r for _, r in nprof))
        des = ev.Design(name, prof, c, mode=mode, L_nut=engaged, R_out=_pull_af(D) / 2, split=0.0)
        s = ev.evaluate(des, pr, with_strength=True).strength
        hold = min(s.F[k] for k in ("tcd", "strip_bolt", "strip_nut")) if s else None
        mode_ja = {"tcd": "ボルトの軸(芯の層間)", "strip_bolt": "ボルトのねじ山", "strip_nut": "ナットのねじ山"}
        res["pull"][sid] = dict(name=name, clearance=c, nut_height=H, engaged=round(engaged, 3),
                                hold_N=round(hold, 1) if hold else None, hold_kg=round(hold / 9.81, 1) if hold else None,
                                where=mode_ja.get(min(("tcd", "strip_bolt", "strip_nut"), key=s.F.get)) if s else None,
                                F={k: round(v, 1) for k, v in s.F.items()} if s else None)
    (out / "predictions.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def _pull_af(D):
    return max(1.6 * D, D + 4.0) + 2.0


def build(out: Path = OUT, test_id: str = TEST_ID, series=None, base=None, intro=None, e_range=None, play_range=(PLAY_MAX,)):
    """series・base(印刷の設定)・intro を変えると別の回の試し刷りになる(TP3 = 0.12mm Fine の版)。"""
    out.mkdir(parents=True, exist_ok=True)
    series = series or SERIES
    pieces, groups = [], []
    for sid, name, D, mk, mode, sides, cs in series:
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
            items.append((f"{sid}{i}", f"ナット{i}(上面の凹み{i}個)", OUTCOMES_THREAD, FLAGS_THREAD + ["部品が欠けた・刷れなかった"]))
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
    if PULL:
        f = "R_bracket.stl"
        sx.write_stl(make_bracket(PULL[0][2]), out / f)
        pieces.append(Piece("R", "bracket", "吊り板", f, dict(D=None)))
        groups.append(("引き抜き", "吊り板 R にボルトを下から通し、上からナットを手で締めて、頭の横穴に重りを吊る。"
                       "同じ組の2本は、同じ条件のくり返し。", pitems))
    plate = make_plate(out, pieces)
    (out / "manifest.json").write_text(json.dumps(dict(test_id=test_id, pieces=[p.__dict__ for p in pieces], plate=plate),
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    layout = layout_svg(out, pieces, plate["placed"], series=series,
                        extra_rows=[(s[0], f"{s[0]}: 引き抜き {s[1]}(ボルトの頭に横穴、ナットは低い)", "R は吊り板") for s in PULL],
                        notes=("記号の数字 0 はボルト。", "ナットの番号 = 上面の凹みの数(1 が最も緩い)。", "実線の白丸はナットの穴、点線はボルトの軸。"))
    (out / "layout.svg").write_text(layout, encoding="utf-8")
    figure = ('<div class="intro"><p><b>配置図</b>(plate.stl を上から見た図。下が手前。系列は頭やナットの角の数で、'
              'ナットの番号は上面の凹みの数で分かる)</p>' + layout + '</div>')
    write_page(groups, out / "index.html", f"ねじ試し刷り {test_id} の結果", intro=(intro or INTRO) + figure, test_id=test_id)
    predict(out, series=series, base=base, e_range=e_range, play_range=play_range)
    return pieces


# TP1 で「ガタ」を付けずに回ったナット(id, 表示名)。TP2 と同じ押し引きで確かめ直し、ガタの判定が確かめ方で変わるかを見る(astra の8回目)
RECHECK_TP1 = [
    ("A2", "A: ISO M6(六角)のナット2(凹み2個)と六角のボルト"),
    ("A3", "A: ISO M6(六角)のナット3(凹み3個)と六角のボルト"),
    ("B3", "B: 45° D6(四角)のナット3(凹み3個)と四角のボルト"),
    ("K2", "K: 30° D6(五角)のナット2(凹み2個)と五角のボルト"),
    ("D2", "D: 45° D3(小さい四角)のナット2(凹み2個)と小さい四角のボルト"),
    ("F1", "F: 半円 D3(小さい八角)のナット1(凹み1個)と小さい八角のボルト"),
    ("F2", "F: 半円 D3(小さい八角)のナット2(凹み2個)と小さい八角のボルト"),
]


def write_recheck_tp1(path: Path = OUT.parent / "testprint" / "recheck_tp1.html"):
    """TP1 の部品を、TP2 のガタの確かめ方で見直すページ(新しく刷らない)。結果は 'TP1R' の1行。"""
    intro = ('<div class="intro"><p><b>新しく刷らない。</b>TP1 の部品が手元に残っていれば、下の組だけを確かめ直す(配置図は TP1 の index.html)。</p>'
             '<p><b>確かめ方</b>(TP2 と同じ): ナットをボルトの真ん中あたりまで手で回して止め、ボルトの頭を持ってナットを軸の向きに押し引きする。'
             'カタカタ動くのが指か耳で分かれば「ガタが分かる」、分からなければ「ガタ無し」。部品が無い・回らない組は「試していない」のままでよい。</p>'
             '<p><b>返し方</b>: 各行を1つクリックし、下の「コピー」で出る1行を次のメッセージに貼る。</p></div>')
    items = [(iid, name, ["ガタ無し", "ガタが分かる", "試していない"], []) for iid, name in RECHECK_TP1]
    write_page([("TP1 の部品の確かめ直し", "TP1 で「ガタ」を付けなかった組。", items)], path, "TP1 の部品のガタの確かめ直し",
               intro=intro, test_id="TP1R")
    return path


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "recheck":
        print(write_recheck_tp1())
    elif len(sys.argv) > 1 and sys.argv[1] == "tp3":  # 0.12mm Fine の版(THREAD_SIM.md 6.5)
        from .printer import FINE
        ps = build(OUT.parent / "testprint3", "TP3", series=SERIES_TP3, base=FINE, intro=INTRO_TP3,
                   e_range={6: (0.0, 0.05)}, play_range=(0.15, 0.28))
        print(len(ps), "部品を", OUT.parent / "testprint3", "へ")
    else:
        ps = build()
        print(len(ps), "部品を", OUT, "へ")
