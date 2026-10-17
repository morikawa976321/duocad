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
                           initial_penetration=r.detail["initial_penetration"])
            else:
                row["error"] = ee.extra.get("strength_error")
        else:
            row["F"] = None
        rows.append(row)
    out["rows"] = rows
    out["seconds"] = time.perf_counter() - t0
    return out


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
    """感度: 垂れ・ビードの丸み(印刷の模擬)と、材料・臨界距離(強度)。代表の設計だけ。"""
    base = []
    for D in (4, 6):
        P = round(0.4 * D / 0.05) * 0.05
        dep = round(0.4 * P, 4)
        b = dict(D=D, L_nut=float(D), R_out=0.9 * D, P=P, depth=dep, pitch_ratio=0.4)
        base += [dict(b, name=f"台形30 D{D} normal", family="trap", b_lo=30, b_up=30, mode="normal"),
                 dict(b, name=f"台形45 D{D} radial", family="trap", b_lo=45, b_up=45, mode="radial"),
                 dict(b, name=f"台形45 D{D} normal", family="trap", b_lo=45, b_up=45, mode="normal"),
                 dict(b, name=f"半円 D{D} normal", family="coil", rho=dep, mode="normal"),
                 dict(b, name=f"バットレス5/45 D{D} normal", family="trap", b_lo=5, b_up=45, mode="normal", bolt_flipped=True)]
        base.append(dict(D=D, L_nut=float(D), R_out=0.9 * D, name=f"ISO M{D} radial", family="iso", P=ISO_COARSE[D], mode="radial"))
    variants = [("基準", {}, {}),
                ("垂れ無し", dict(sag_amp=0.0), {}), ("垂れ半分", dict(sag_amp=0.5), {}), ("垂れ2倍", dict(sag_amp=2.0), {}),
                ("垂れ始めφ0.3", dict(sag_phi_ok=0.3), {}), ("垂れ始めφ0.7", dict(sag_phi_ok=0.7), {}),
                ("ビード角立ち", dict(bead_round=0.0), {}), ("ビード丸み半分", dict(bead_round=0.5), {}),
                ("層間弱 Zt15 S12", {}, dict(Z_t=15.0, S_il=12.0)), ("層間強 Zt35 S25", {}, dict(Z_t=35.0, S_il=25.0)),
                ("せん断だけ弱 S10", {}, dict(S_il=10.0)), ("L0.25", {}, dict(L=0.25)), ("L1.0", {}, dict(L=1.0))]
    return [(dict(s, name=f"{s['name']} | {vn}", variant=vn), pk, mk) for s in base for vn, pk, mk in variants], [0.1], None


STUDIES = {"study1": study1, "study2": study2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("study")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", default=None, help="名前にこの文字列を含む設計だけ")
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
        futs = {pool.submit(run_one, s, pk, e_list, mk): s for s, pk, mk in todo}
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
