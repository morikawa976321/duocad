r"""らせんのねじ山(B-spline の線織面)どうしで、方式ごとの精度・時間を比べる。

    .\.venv\Scripts\python.exe -m tools.interference_probe.threads [--timeout 300]

ねじ山は tools/thread_prototype.py の `threaded_segment`(ねじ verb の採用方式)で作る。雄と雌の山形を
半径方向に ±off ずらすと、60° の山形では斜面どうしの隙間は約 2·off·sin30° = off、山頂・谷は 2·off になる。
off < 0 は雄が太い(食い込む)。

方式ごとに子プロセスで走らせ、--timeout 秒で打ち切る(OCCT の呼び出しは Python から中断できない。
1回目はプロセス1本で全部を回し、最初の組の途中で 20 分の打ち切りに掛かった)。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

OUT = Path("output/interference_probe/threads")


def build(d, pitch, off, length=10.0):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec

    from tools.thread_prototype import iso_profile, threaded_segment

    from .cases import box, cut
    male = threaded_segment(iso_profile(d / 2 - off, pitch), pitch, length)
    void = threaded_segment(iso_profile(d / 2 + off, pitch), pitch, length + 4 * pitch)
    shift = gp_Trsf()
    shift.SetTranslation(gp_Vec(0, 0, -2 * pitch))  # 1ピッチの整数倍だけずらすので位相は揃ったまま
    void = BRepBuilderAPI_Transform(void, shift, True).Shape()
    a = d * 2
    nut = cut(box(-a / 2, -a / 2, 1.0, a, a, length - 2.0), void)
    return male, nut


def run_one(method, male_path, nut_path):
    """子プロセス側: 1方式だけ測って JSON を1行出す。"""
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    from . import methods as M

    def read(p):
        s = TopoDS_Shape()
        BRepTools.Read_s(s, str(p), BRep_Builder())
        return s
    male, nut = read(male_path), read(nut_path)
    t0 = time.perf_counter()
    if method == "dist":
        r = M.dist(male, nut, parallel=True)
        out = dict(distance=r.get("distance"), inner=r.get("inner"))
    elif method == "gf":
        r = M.gf_relations([male, nut])
        rel = r["relations"].get((0, 1), {})
        out = dict(kind=rel.get("kind"), volume=rel.get("volume"), thickness=rel.get("thickness"),
                   errors=r["errors"], warnings=r["warnings"])
    elif method.startswith("mf"):
        dev = float(method[2:])
        ma, mb = M.to_manifold(*M.tessellated(male, dev)), M.to_manifold(*M.tessellated(nut, dev))
        t_tess = time.perf_counter() - t0
        r = M.manifold_pair(ma, mb, search=1.0)
        out = dict(gap=r["min_gap"], volume=r["volume"], tris=ma.num_tri() + mb.num_tri(), tess_s=t_tess)
    else:
        raise ValueError(method)
    out["seconds"] = time.perf_counter() - t0
    print(json.dumps(out))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--one", nargs=3, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.one:
        run_one(*args.one)
        return 0
    from OCP.BRepTools import BRepTools

    OUT.mkdir(parents=True, exist_ok=True)
    for d, pitch in [(8.0, 1.25), (20.0, 2.5)]:
        for off in (0.1, 0.05, 0.0, -0.05):
            t0 = time.perf_counter()
            male, nut = build(d, pitch, off)
            t_build = time.perf_counter() - t0
            tag = f"M{d:g}x{pitch:g}_off{off:+.2f}"
            mp, np_ = OUT / f"{tag}_male.brep", OUT / f"{tag}_nut.brep"
            BRepTools.Write_s(male, str(mp))
            BRepTools.Write_s(nut, str(np_))
            line = f"{tag} build={t_build:.1f}s"
            for method in ("gf", "dist", "mf0.2", "mf0.05", "mf0.01"):
                try:
                    proc = subprocess.run([sys.executable, "-X", "utf8", "-m", "tools.interference_probe.threads",
                                           "--one", method, str(mp), str(np_)],
                                          capture_output=True, text=True, timeout=args.timeout)
                    last = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else f"rc={proc.returncode}"
                    line += f" | {method} {last}"
                except subprocess.TimeoutExpired:
                    line += f" | {method} timeout>{args.timeout:g}s"
            print(line, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
