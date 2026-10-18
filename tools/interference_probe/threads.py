"""らせんのねじ山(B-spline の線織面)どうしで、厳密経路とメッシュ経路の精度・時間を比べる。

    .\\.venv\\Scripts\\python.exe -m tools.interference_probe.threads

ねじ山は tools/thread_prototype.py の `threaded_segment`(ねじ verb の採用方式)で作る。雄と雌の山形を
半径方向に ±off ずらすと、60° の山形では斜面どうしの隙間は約 2·off·sin30° = off、山頂・谷は 2·off になる。
off < 0 は雄が太い(食い込む)。
"""
from __future__ import annotations

import math
import sys
import time

from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.gp import gp_Trsf, gp_Vec

from tools.thread_prototype import iso_profile, threaded_segment

from . import methods as M
from .cases import box, cut


def pair(d: float, pitch: float, off: float, length: float = 10.0):
    male = threaded_segment(iso_profile(d / 2 - off, pitch), pitch, length)
    void = threaded_segment(iso_profile(d / 2 + off, pitch), pitch, length + 4 * pitch)
    shift = gp_Trsf()
    shift.SetTranslation(gp_Vec(0, 0, -2 * pitch))  # 1ピッチの整数倍だけずらすので位相は揃ったまま
    void = BRepBuilderAPI_Transform(void, shift, True).Shape()
    a = d * 2
    nut = cut(box(-a / 2, -a / 2, 1.0, a, a, length - 2.0), void)
    return male, nut


def main():
    rows = []
    for d, pitch in [(8.0, 1.25), (20.0, 2.5)]:
        for off in (0.1, 0.05, 0.0, -0.05):
            t0 = time.perf_counter()
            male, nut = pair(d, pitch, off)
            t_build = time.perf_counter() - t0
            dd = M.dist(male, nut)
            c = M.common(male, nut)
            g = M.gf_pairs([male, nut])
            line = (f"M{d:g}x{pitch:g} off={off:+.2f} build={t_build:.2f}s | exact d={dd.get('distance', float('nan')):.5f} "
                    f"inner={dd.get('inner')} ({dd['seconds']:.2f}s) common V={c['volume']:.4f} warn={c['warnings']} "
                    f"err={c['errors']} ({c['seconds']:.2f}s) gf V={g['pairs'].get((0, 1), 0.0):.4f} ({g['seconds']:.2f}s)")
            for dev in (0.2, 0.05, 0.01):
                try:
                    t1 = time.perf_counter()
                    ma, mb = M.to_manifold(*M.tessellated(male, dev)), M.to_manifold(*M.tessellated(nut, dev))
                    t_tess = time.perf_counter() - t1
                    mr = M.manifold_pair(ma, mb, search=1.0)
                    line += (f" | mf{dev:g} gap={mr['min_gap']:.5f} V={mr['volume']:.4f} "
                             f"(tess {t_tess:.2f}s + {mr['seconds']:.3f}s, {ma.num_tri() + mb.num_tri()} tris)")
                except Exception as exc:
                    line += f" | mf{dev:g} error {exc!r:.80}"
            print(line, flush=True)
            rows.append(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
