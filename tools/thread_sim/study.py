"""比較の計算を並列で回し、1設計1行の JSONL に書く(途中で止めても、書いた行は飛ばして続きから回る)。

    .venv/Scripts/python.exe -X utf8 -m tools.thread_sim.study study1 --workers 4

結果は output/thread_sim/<study>.jsonl(git 管理外)。表にするのは report.py。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from . import evaluate as ev
from . import profiles as pf
from . import strength as st
from .printer import DEFAULT

OUT = Path(__file__).resolve().parents[2] / "output" / "thread_sim"
ISO_COARSE = {3: 0.5, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5, 12: 1.75}


def make_profile(s: dict) -> pf.Profile:
    f = s["family"]
    if f == "iso":
        return pf.iso(s["D"], s["P"])
    if f == "trap":
        return pf.trapezoid(s["D"], s["P"], s["depth"], s["b_lo"], s["b_up"], s.get("crest_share", 0.5))
    if f == "coil":
        return pf.coil(s["D"], s["P"], s["rho"])
    if f == "trap_round":
        base = pf.trapezoid(s["D"], s["P"], s["depth"], s["b_lo"], s["b_up"], s.get("crest_share", 0.5))
        return pf.fillet(base, s["rc"], s["rr"])
    raise ValueError(f)


def run_one(spec: dict, printer_kw: dict, e_list, mat_kw: dict) -> dict:
    t0 = time.perf_counter()
    pr = DEFAULT.with_(**printer_kw)
    mat = st.PrintedPLA(**mat_kw)
    prof = make_profile(spec)
    des = ev.Design(spec["name"], prof, 0.3, mode=spec["mode"], L_nut=spec["L_nut"], R_out=spec["R_out"],
                    bolt_flipped=spec.get("bolt_flipped", False), nut_flipped=spec.get("nut_flipped", False))
    out = dict(spec, printer=printer_kw, mat=mat_kw, depth=prof.depth, flank=prof.flank_angles())
    c_geo = ev.c_required(des, pr) if des.mode == "normal" else None
    if c_geo is None:
        c_geo = ev.evaluate(des, pr, with_strength=False).c_req
    out["c_geo"] = c_geo
    if not math.isfinite(c_geo):
        out["error"] = "どの隙間でも包絡が通らない"
        return out
    e0 = ev.evaluate(replace(des, clearance=c_geo), pr, with_strength=False)
    out.update(over_bolt=e0.over_bolt, over_nut=e0.over_nut, depth_kept=e0.depth_kept, rms_dev=e0.rms_dev,
               engagement_at_cgeo=e0.engagement)
    rows = []
    for e in e_list:
        c = c_geo + e
        try:
            ee = ev.evaluate(replace(des, clearance=c), pr, with_strength=False)
        except ValueError as ex:  # 法線の隙間で山形が潰れるなど
            rows.append(dict(e=e, c=c, error=str(ex)))
            continue
        row = dict(e=e, c=c, engagement=ee.engagement, play=ee.play, margin=ee.margin)
        if ee.engagement > 0.02:
            ee.strength = ev.strength_of(ee, mat)
            if ee.strength is not None:
                r = ee.strength
                row.update(F=r.F, mode=r.mode, tcd_mode=r.detail["tcd_mode"], tcd_where=r.detail["tcd_where"],
                           share=r.detail["share"], F_yield_contact=r.detail["F_yield_contact"],
                           initial_penetration=r.detail["initial_penetration"], beta_eff=r.detail["beta_eff"],
                           jump_ratio=r.detail["jump_ratio"])
            else:
                row["error"] = ee.extra.get("strength_error")
        else:
            row["F"] = None
        rows.append(row)
    out["rows"] = rows
    out["seconds"] = time.perf_counter() - t0
    return out


def _sim(des, pr):
    """誤差なしの印刷で、余り(はまる余裕、半径)と掛かり代。"""
    e = ev.evaluate(des, pr, with_strength=False)
    return e


def _strength_row(des, pr, mat, eps, split=0.5):
    """実際の誤差 eps(雌雄の和・半径。+で雄が太り穴が痩せる)で刷ったときの掛かり代と強さ。split は雄が受け持つ割合。"""
    prb = pr.with_(contour_bias=pr.contour_bias + eps * split, hole_bias=pr.hole_bias - eps * (1 - split))
    e = ev.evaluate(des, prb, with_strength=False)
    row = dict(eps=eps, split=split, engagement=e.engagement, margin=e.margin, play=e.play)
    if e.margin < -0.005:
        row["error"] = "はまらない"
        return row
    if e.engagement <= 0.02:
        row["F"] = None
        return row
    s = ev.strength_of(e, mat)
    if s is None:
        row["error"] = e.extra.get("strength_error")
        return row
    row.update(F=s.F, mode=s.mode, tcd_where=s.detail["tcd_where"], share=s.detail["share"],
               beta_eff=s.detail["beta_eff"], jump_ratio=s.detail["jump_ratio"], F_yield_contact=s.detail["F_yield_contact"])
    return row


def _window_center(des, pr, e_req):
    """窓の中心(誤差 0 が窓の真ん中に来る隙間)と W。半径方向の隙間なら W は隙間によらない。
    返り値 (必要な隙間 c_geo, 中心の隙間, W, 走査した (c, 余り, 掛かり代), 中心が見つかったか)。"""
    found = True
    if des.mode == "radial":
        e1 = _sim(des, pr)
        c_geo = des.clearance - e1.margin
        e_g = _sim(replace(des, clearance=c_geo), pr)
        W = e_g.margin + e_g.engagement - e_req
        c_center = c_geo + W / 2
        scan = [(c_geo, e_g.margin, e_g.engagement)]
    else:
        c_geo = ev.c_required(des, pr)
        if not math.isfinite(c_geo):
            raise ValueError("どの隙間でも包絡が通らない")
        # 窓の中心 z(c) = (余り − 掛かり代 + 下限)/2 は c とともに増える。z が 0 をまたぐまで c を広げ、
        # 線形補間した c で W を測り直す
        scan, cen = [], []
        found = False
        for dc in (0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.45, 0.6, 0.8, 1.0, 1.3, 1.7):
            try:
                e_c = _sim(replace(des, clearance=c_geo + dc), pr)
            except ValueError:
                break
            m_, g_ = e_c.margin, e_c.engagement
            scan.append((c_geo + dc, m_, g_))
            cen.append((c_geo + dc, (m_ - g_ + e_req) / 2))
            if len(cen) >= 2 and cen[-2][1] <= 0 <= cen[-1][1]:
                found = True
                break
            if len(cen) == 1 and cen[0][1] >= 0:  # c_geo で既に中心より緩い(掛かり代 < 下限 + 余り): 窓が開かない
                break
        if found:
            (c0, z0), (c1, z1) = cen[-2], cen[-1]
            c_center = c0 + (0 - z0) / (z1 - z0) * (c1 - c0) if z1 != z0 else c0
            e_c = _sim(replace(des, clearance=c_center), pr)
            W = e_c.margin + e_c.engagement - e_req
        else:
            c_center, m_, g_ = scan[0] if cen and cen[0][1] >= 0 else scan[-1]
            W = m_ + g_ - e_req
    return c_geo, c_center, W, scan, found


def run_window(spec: dict, printer_kw: dict, opts, mat_kw: dict) -> dict:
    """設計の隙間を1つに決め、印刷の誤差を外形と穴の系統誤差として与えて評価する(astra の指摘)。

    誤差 ε は外形と穴の半径を一様にずらすので、余りも掛かり代もちょうど ε だけ動く。だから
    誤差を吸収できる幅 W = 余り + 掛かり代 − 掛かりの下限 は誤差なしの模擬1回から出る。
    半径方向の隙間なら W は隙間によらず一定、法線方向なら隙間を振って W の窓の中心が ε=0 に来る隙間を選ぶ。
    強さは窓の中心(ε=0)と緩い端(掛かり代 = 下限)と固い端(余り 0)で見る。"""
    t0 = time.perf_counter()
    e_req = opts.get("e_req", 0.1) if isinstance(opts, dict) else 0.1
    pr = DEFAULT.with_(**printer_kw)
    mat = st.PrintedPLA(**mat_kw)
    prof = make_profile(spec)
    des = ev.Design(spec["name"], prof, 0.3, mode=spec["mode"], L_nut=spec["L_nut"], R_out=spec["R_out"],
                    bolt_flipped=spec.get("bolt_flipped", False), nut_flipped=spec.get("nut_flipped", False))
    out = dict(spec, printer=printer_kw, mat=mat_kw, e_req=e_req, depth=prof.depth, flank=prof.flank_angles())
    try:
        c_geo, c_center, W, scan, found = _window_center(des, pr, e_req)
    except ValueError as ex:
        out["error"] = str(ex)
        return out
    if not found:
        out["center_not_found"] = True
    e_g0 = _sim(replace(des, clearance=c_geo), pr)
    out.update(c_geo=c_geo, c_center=c_center, W=W, scan=scan, over_bolt=e_g0.over_bolt, over_nut=e_g0.over_nut,
               depth_kept=e_g0.depth_kept, rms_dev=e_g0.rms_dev, engagement_at_cgeo=e_g0.engagement)
    if W <= 0:
        out["rows"] = []
        out["seconds"] = time.perf_counter() - t0
        return out
    dc = replace(des, clearance=c_center)
    out["rows"] = [_strength_row(dc, pr, mat, eps) for eps in (0.0, -W / 2, +W / 2 - 1e-3)]
    out["seconds"] = time.perf_counter() - t0
    return out


def _hold(row):
    """強さの行の、引張で保持できる荷重(リングの FEA と山のせん断の小さい方)。"""
    F = row.get("F")
    return min(F[k] for k in ("tcd", "strip_bolt", "strip_nut")) if F else None


def run_robust(spec: dict, printer_kw: dict, opts, mat_kw: dict) -> dict:
    """隙間を基準の係数での窓の中心に固定し、印刷の模擬の係数を振った各場面でも はまる・掛かる 誤差の範囲の共通部分
    (誤差を吸収できる幅 W_rob)と、その範囲の両端での強さを見る(astra の指摘: 係数ごとに隙間を選び直すと、
    刷った同じ部品の頑健さにならない)。緩い端は誤差を雄だけ・雌だけ・半々に受け持たせる(嵌合は同じで強さが変わる)。"""
    t0 = time.perf_counter()
    e_req = opts["e_req"]
    pr = DEFAULT.with_(**printer_kw)
    mat = st.PrintedPLA(**mat_kw)
    prof = make_profile(spec)
    des = ev.Design(spec["name"], prof, 0.3, mode=spec["mode"], L_nut=spec["L_nut"], R_out=spec["R_out"],
                    bolt_flipped=spec.get("bolt_flipped", False), nut_flipped=spec.get("nut_flipped", False))
    out = dict(spec, printer=printer_kw, mat=mat_kw, e_req=e_req, depth=prof.depth, flank=prof.flank_angles())
    try:
        c_geo, c_center, W, _, found = _window_center(des, pr, e_req)
    except ValueError as ex:
        out["error"] = str(ex)
        return out
    out.update(c_geo=c_geo, c_center=c_center, W=W, center_not_found=not found)
    dc = replace(des, clearance=c_center)
    scen = []
    for name, kw in opts["scenarios"]:
        prs = pr.with_(**kw)
        try:
            e = _sim(dc, prs)
        except ValueError as ex:
            scen.append(dict(name=name, error=str(ex)))
            continue
        # 系統誤差 ε(+で雄が太り穴が痩せる)は余りを ε だけ減らし掛かり代を ε だけ増やす: はまる ε ≤ 余り、掛かる ε ≥ 下限 − 掛かり代
        scen.append(dict(name=name, margin=e.margin, engagement=e.engagement, lo=e_req - e.engagement, hi=e.margin))
    ok = [s for s in scen if "error" not in s]
    out["scenarios"] = scen
    if len(ok) < len(scen) or not ok:
        out["error"] = "印刷の模擬が失敗した場面がある"
        return out
    s_lo = max(ok, key=lambda s: s["lo"])
    s_hi = min(ok, key=lambda s: s["hi"])
    eps_lo, eps_hi = s_lo["lo"], s_hi["hi"]
    out.update(eps_lo=eps_lo, eps_hi=eps_hi, W_rob=eps_hi - eps_lo, worst_lo=s_lo["name"], worst_hi=s_hi["name"],
               W_scen_min=min(s["hi"] - s["lo"] for s in ok))
    if eps_hi - eps_lo <= 0:
        out["rows"] = []
        out["seconds"] = time.perf_counter() - t0
        return out
    kw_of = dict(opts["scenarios"])
    rows = []
    mid = 0.0 if eps_lo <= 0 <= eps_hi else 0.5 * (eps_lo + eps_hi)
    rows.append(dict(_strength_row(dc, pr, mat, mid), at="中心", scenario=opts["scenarios"][0][0]))
    for split, who in ((0.5, "半々"), (1.0, "雄だけ"), (0.0, "雌だけ")):
        rows.append(dict(_strength_row(dc, pr.with_(**kw_of[s_lo["name"]]), mat, eps_lo + 1e-3, split),
                         at=f"緩い端 {who}", scenario=s_lo["name"]))
    rows.append(dict(_strength_row(dc, pr.with_(**kw_of[s_hi["name"]]), mat, eps_hi - 1e-3), at="固い端", scenario=s_hi["name"]))
    out["rows"] = rows
    holds = [_hold(r) for r in rows]
    out["F_rob"] = min(h for h in holds if h is not None) if any(h is not None for h in holds) else None
    out["seconds"] = time.perf_counter() - t0
    return out


def study4():
    """study1 と同じ設計を、窓の幅 W と、窓の中心・緩い端・固い端での強さ(壁と充填のモデル)で評価し直す。"""
    specs, _, _, _ = study1()
    return specs, {}, dict(e_req=0.1, runner="window"), {}


def study1():
    """同じ径・ピッチ・深さでの山形の比較(astra の指摘: 交絡を外す)+ ISO 並目。ナットは外径 1.8d・はめあい長 1d に固定。"""
    specs = []
    for D in (3, 4, 5, 6, 8, 10):
        base = dict(D=D, L_nut=float(D), R_out=0.9 * D)
        for mode in ("radial", "normal"):
            specs.append(dict(base, name=f"ISO M{D} {mode}", family="iso", P=ISO_COARSE[D], mode=mode))
        for pr_ in (0.25, 0.4):
            P = round(pr_ * D / 0.05) * 0.05
            dep = round(0.4 * P, 4)
            b = dict(base, P=P, depth=dep, pitch_ratio=pr_)
            for beta in (30, 40, 45, 50):
                for mode in ("radial", "normal"):
                    specs.append(dict(b, name=f"台形{beta} D{D} P{P:g} {mode}", family="trap", b_lo=beta, b_up=beta, mode=mode))
            for mode in ("radial", "normal"):
                specs.append(dict(b, name=f"半円 D{D} P{P:g} {mode}", family="coil", rho=dep, mode=mode))
            specs.append(dict(b, name=f"台形45丸め D{D} P{P:g} normal", family="trap_round", b_lo=45, b_up=45,
                              rc=round(0.1 * P, 4), rr=round(0.1 * P, 4), mode="normal"))
            for mode in ("radial", "normal"):
                # 荷重面(ボルトの下向きの面)を棚にし、ボルトを逆さに刷る
                specs.append(dict(b, name=f"バットレス5/45 D{D} P{P:g} {mode}", family="trap", b_lo=5, b_up=45,
                                  mode=mode, bolt_flipped=True))
    return specs, {}, [0.0, 0.1, 0.2], {}


def study2():
    """感度(窓の評価): 垂れ・ビードの丸み(W に効く)と、材料・充填・臨界距離(F に効く)。代表の設計だけ。"""
    base = []
    for D in (4, 6):
        P = round(0.25 * D / 0.05) * 0.05
        dep = round(0.4 * P, 4)
        b = dict(D=D, L_nut=float(D), R_out=0.9 * D, P=P, depth=dep, pitch_ratio=0.25)
        for beta in (30, 40, 45, 50):
            base.append(dict(b, name=f"台形{beta} D{D} radial", family="trap", b_lo=beta, b_up=beta, mode="radial"))
        base += [dict(b, name=f"半円 D{D} normal", family="coil", rho=dep, mode="normal"),
                 dict(b, name=f"バットレス5/45 D{D} radial", family="trap", b_lo=5, b_up=45, mode="radial", bolt_flipped=True)]
        base.append(dict(D=D, L_nut=float(D), R_out=0.9 * D, name=f"ISO M{D} radial", family="iso", P=ISO_COARSE[D], mode="radial"))
    variants = [("基準", {}, {}),
                ("垂れ無し", dict(sag_amp=0.0), {}), ("垂れ半分", dict(sag_amp=0.5), {}), ("垂れ2倍", dict(sag_amp=2.0), {}),
                ("垂れ始めφ0.3", dict(sag_phi_ok=0.3), {}), ("垂れ始めφ0.7", dict(sag_phi_ok=0.7), {}),
                ("ビード角立ち", dict(bead_round=0.0), {}), ("ビード丸み半分", dict(bead_round=0.5), {}),
                ("層間弱 Zt15 S12", {}, dict(Z_t=15.0, S_il=12.0)), ("層間強 Zt35 S25", {}, dict(Z_t=35.0, S_il=25.0)),
                ("充填ほぼ無し", {}, dict(infill_E=0.02, infill_Z=0.0)), ("充填強め", {}, dict(infill_E=0.15, infill_Z=0.1)),
                ("中実とみなす", {}, dict(walls=False)), ("摩擦0.12", {}, dict(mu=0.12)), ("摩擦0.38", {}, dict(mu=0.38)),
                ("L0.25", {}, dict(L=0.25)), ("L1.0", {}, dict(L=1.0))]
    items = [(dict(s, name=f"{s['name']} | {vn}", variant=vn), pk, mk) for s in base for vn, pk, mk in variants]
    return items, None, dict(e_req=0.1, runner="window"), None


def flats_trapezoid(D, beta, depth, crest, root):
    """斜面の角・深さ・山頂の平坦・谷の平坦からピッチが決まる対称台形の仕様。"""
    P = round(2 * depth * math.tan(math.radians(beta)) + crest + root, 4)
    return dict(family="trap", D=D, P=P, depth=depth, b_lo=beta, b_up=beta, crest_share=crest / (crest + root))


def study3():
    """改良の探索(格子、窓の評価): 対称台形の角 β・ピッチ P・深さ・平坦の配分(山頂の取り分)。隙間は半径方向。
    目的は (誤差を吸収できる幅 W, 窓の中心での強さ) のパレート。"""
    specs = []
    for D in (3, 4, 5, 6, 8):
        base = dict(L_nut=float(D), R_out=0.9 * D, mode="radial", family="trap", D=D)
        for pr_ in (0.2, 0.3, 0.4, 0.5):
            P = round(pr_ * D / 0.05) * 0.05
            for beta in (40, 45, 50):
                for depth in (0.2, 0.3, 0.45, 0.65, 0.9):
                    if depth > 0.25 * D:
                        continue
                    flats = P - 2 * depth * math.tan(math.radians(beta))
                    if flats < 0.2:
                        continue
                    for share in (0.35, 0.65):
                        s = dict(base, P=P, depth=depth, b_lo=beta, b_up=beta, crest_share=share, pitch_ratio=pr_,
                                 name=f"台形{beta} D{D} P{P:g} h{depth} 山{share}")
                        specs.append(s)
    return specs, {}, dict(e_req=0.1, runner="window"), {}


def study5():
    """改良の第2段(小径に絞る): 斜面の角(対称・非対称)・山頂の取り分・ピッチ/径・深さ/ピッチ、隙間は法線方向と半径方向。
    D2 まで下げて、W ≥ 0.2 が残る最小の径を探す。"""
    specs = []
    for D in (2.0, 2.5, 3.0, 4.0, 5.0, 6.0):
        base = dict(L_nut=float(D), R_out=0.9 * D, family="trap", D=D)
        for pr_ in (0.3, 0.4, 0.5):
            P = round(pr_ * D / 0.05) * 0.05
            for dr in (0.3, 0.4):
                depth = round(dr * P, 3)
                for b_lo, b_up in ((40, 40), (45, 45), (45, 40), (40, 45)):
                    flats = P - depth * (math.tan(math.radians(b_lo)) + math.tan(math.radians(b_up)))
                    if flats < 0.15:
                        continue
                    for share in (0.3, 0.5, 0.7):
                        for mode in ("normal", "radial"):
                            specs.append(dict(base, P=P, depth=depth, b_lo=b_lo, b_up=b_up, crest_share=share, mode=mode,
                                              pitch_ratio=pr_, name=f"台形{b_lo}/{b_up} D{D:g} P{P:g} h{depth:g} 山{share} {mode}"))
    return specs, {}, dict(e_req=0.1, runner="window"), {}


# 印刷の模擬の係数の場面(study2 の印刷の側と同じ振り幅 + 悪い組・良い組)。先頭は基準
ROBUST_SCENARIOS = [("基準", {}),
                    ("垂れ無し", dict(sag_amp=0.0)), ("垂れ半分", dict(sag_amp=0.5)), ("垂れ2倍", dict(sag_amp=2.0)),
                    ("垂れ始めφ0.3", dict(sag_phi_ok=0.3)), ("垂れ始めφ0.7", dict(sag_phi_ok=0.7)),
                    ("ビード角立ち", dict(bead_round=0.0)), ("ビード丸み半分", dict(bead_round=0.5)),
                    ("垂れ2倍+φ0.7", dict(sag_amp=2.0, sag_phi_ok=0.7)), ("垂れ無し+角立ち", dict(sag_amp=0.0, bead_round=0.0))]

# study3(直す前のコード。半径方向の隙間の W は修正の影響を受けない)の径ごとのパレート (W, 窓の中心での強さ) から、
# 山頂の取り分 0.65 の側を採った (β, P, 深さ)
ROBUST_PARETO = {3: [(45, 1.5, 0.65), (50, 1.5, 0.45)],
                 4: [(45, 2.0, 0.9), (50, 2.0, 0.65), (45, 1.6, 0.65), (50, 1.6, 0.45)],
                 5: [(50, 2.5, 0.9), (45, 2.0, 0.9), (50, 2.0, 0.65), (45, 1.5, 0.65), (50, 1.5, 0.45)],
                 6: [(50, 2.4, 0.9), (40, 1.8, 0.9), (50, 1.8, 0.65), (50, 1.8, 0.45), (45, 1.2, 0.45)],
                 8: [(50, 2.4, 0.9), (50, 2.4, 0.65), (45, 1.6, 0.65), (40, 1.6, 0.65), (50, 1.6, 0.45)]}


def study6():
    """固定した設計の頑健さ: 改良の候補(study3 のパレート)と対照(ISO・30°・半円・バットレス・法線方向の 45°)の隙間を
    基準の係数での窓の中心に固定し、係数の場面すべてで はまる・掛かる 誤差の範囲の共通部分 W_rob と、その端での強さ。"""
    specs = []
    for D, picks in ROBUST_PARETO.items():
        base = dict(D=D, L_nut=float(D), R_out=0.9 * D)
        for beta, P, depth in picks:
            specs.append(dict(base, family="trap", P=P, depth=depth, b_lo=beta, b_up=beta, crest_share=0.65, mode="radial",
                              name=f"候補 台形{beta} D{D} P{P:g} h{depth:g} 山0.65 radial"))
        P = round(0.25 * D / 0.05) * 0.05
        dep = round(0.4 * P, 4)
        b = dict(base, P=P, depth=dep)
        specs += [dict(base, family="iso", P=ISO_COARSE[D], mode="radial", name=f"対照 ISO M{D} radial"),
                  dict(base, family="iso", P=ISO_COARSE[D], mode="normal", name=f"対照 ISO M{D} normal"),
                  dict(b, family="trap", b_lo=30, b_up=30, mode="radial", name=f"対照 台形30 D{D} P{P:g} radial"),
                  dict(b, family="trap", b_lo=45, b_up=45, mode="normal", name=f"対照 台形45 D{D} P{P:g} normal"),
                  dict(b, family="coil", rho=dep, mode="normal", name=f"対照 半円 D{D} P{P:g} normal"),
                  dict(b, family="trap", b_lo=5, b_up=45, mode="normal", bolt_flipped=True,
                       name=f"対照 バットレス5/45 D{D} P{P:g} normal")]
    return specs, {}, dict(e_req=0.1, runner="robust", scenarios=ROBUST_SCENARIOS), {}


STUDIES = {"study1": study1, "study2": study2, "study3": study3, "study4": study4, "study5": study5, "study6": study6}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("study")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default=None, help="名前にこの文字列を含む設計だけ")
    ap.add_argument("--redo-errors", action="store_true",
                    help="計算の失敗(メッシュ・接触・例外)を含む行を消して回し直す。「はまらない」は失敗ではないので残す")
    a = ap.parse_args()
    got = STUDIES[a.study]()
    if got[3] is None:  # 設計ごとに (spec, printer_kw, mat_kw)
        items, e_list = got[0], got[2]
    else:
        specs, printer_kw, e_list, mat_kw = got
        items = [(s, printer_kw, mat_kw) for s in specs]
    specs = [s for s, _, _ in items]
    if a.only:
        specs = [s for s in specs if a.only in s["name"]]
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{a.study}.jsonl"
    done = set()
    if path.exists() and a.redo_errors:
        keep, dropped = [], 0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            bad = "error" in d and "c_geo" not in d and "包絡が通らない" not in str(d.get("error"))
            bad |= any("error" in r and r["error"] != "はまらない" for r in d.get("rows", []))
            if bad:
                dropped += 1
            else:
                keep.append(line)
        path.write_text("".join(x + "\n" for x in keep), encoding="utf-8")
        print(f"失敗を含む {dropped} 行を消した", flush=True)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["name"])
            except (json.JSONDecodeError, KeyError):
                pass
    todo = [(s, pk, mk) for s, pk, mk in items if s["name"] not in done and (not a.only or a.only in s["name"])]
    print(f"{a.study}: {len(specs)} 設計、済み {len(done)}、残り {len(todo)}", flush=True)
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=a.workers) as pool, path.open("a", encoding="utf-8") as fh:
        runner = {"window": run_window, "robust": run_robust}.get(e_list.get("runner") if isinstance(e_list, dict) else None, run_one)
        futs = {pool.submit(runner, s, pk, e_list, mk): s for s, pk, mk in todo}
        for i, fu in enumerate(as_completed(futs), 1):
            s = futs[fu]
            try:
                res = fu.result()
            except Exception:
                res = dict(s, error=traceback.format_exc(limit=3))
            fh.write(json.dumps(res, ensure_ascii=False) + "\n")
            fh.flush()
            print(f"[{i}/{len(todo)}] {s['name']} {res.get('seconds', 0):.0f}s {'ERR' if 'error' in res else ''}"
                  f"  経過 {time.perf_counter() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
