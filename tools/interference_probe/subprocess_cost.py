r"""干渉チェックを子プロセスで走らせる費用: 起動と import、形状の受け渡し(BRep の文字列)。

    .\.venv\Scripts\python.exe -m tools.interference_probe.subprocess_cost <PC ケースの STEP>

OCP の `Message_ProgressIndicator` は Python から継承できない(構築子が無い)ので、OCCT の計算を途中で止めるには
プロセスごと止めるしかない。その代価を測る。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

CHILD = r"""
import sys, time
t0 = time.perf_counter()
import OCP.BOPAlgo, OCP.BRepTools, OCP.BRepExtrema
from duocad import kernel_brep
t_import = time.perf_counter() - t0
data = sys.stdin.read().split('\x00')
t1 = time.perf_counter()
shapes = [kernel_brep.decode_brep(text) for text in data if text]
print(f"{t_import:.3f} {time.perf_counter() - t1:.3f} {len(shapes)}")
"""


def main():
    from duocad import kernel_brep
    from .real_docs import load_step
    solids, _ = load_step(Path(sys.argv[1]))
    t0 = time.perf_counter()
    texts = [kernel_brep.encode_brep(s.shape) for s in solids]
    t_enc = time.perf_counter() - t0
    payload = "\x00".join(texts)
    t0 = time.perf_counter()
    proc = subprocess.run([sys.executable, "-X", "utf8", "-c", CHILD], input=payload, capture_output=True, text=True)
    t_total = time.perf_counter() - t0
    t_import, t_dec, n = proc.stdout.split()
    print(f"bodies={len(texts)} brep_text={len(payload) / 1e6:.2f}MB encode={t_enc:.3f}s | child total={t_total:.2f}s "
          f"(import {t_import}s, decode {t_dec}s, {n} shapes) rc={proc.returncode} {proc.stderr[-300:]}")


if __name__ == "__main__":
    main()
