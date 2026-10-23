"""別プロセスで計算するときの費用と、BRep の往復で面の添字が保たれるか(docs/FACE_EDIT.md「別プロセス」)。

    .venv/Scripts/python.exe -m tools.face_edit_probe.isolation [file.step ...]
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from OCP.BRep import BRep_Builder
from OCP.BRepTools import BRepTools
from OCP.TopoDS import TopoDS_Shape

from . import geometry as g
from .provenance import signature
from .realworld import _read
from .shapes import all_cases

ROOT = Path(__file__).resolve().parents[2]


def roundtrip(shape):
    text = BRepTools.WriteToString_s(shape) if hasattr(BRepTools, "WriteToString_s") else None
    if text is None:
        import os
        import tempfile

        handle, path = tempfile.mkstemp(suffix=".brep")
        os.close(handle)
        BRepTools.Write_s(shape, path)
        text = Path(path).read_text()
        os.unlink(path)
    return text


def read_back(text):
    import os
    import tempfile

    handle, path = tempfile.mkstemp(suffix=".brep")
    os.close(handle)
    Path(path).write_text(text)
    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, path, BRep_Builder())
    os.unlink(path)
    return shape


def main(paths):
    started = time.perf_counter()
    subprocess.run([sys.executable, "-c", "import OCP.BRepAlgoAPI, OCP.BOPAlgo, OCP.BRepOffset"], cwd=ROOT, check=True)
    print(f"child process start + OCP import: {1000 * (time.perf_counter() - started):.0f} ms")
    shapes = [(c.name, c.shape) for c in all_cases()][:12]
    for path in paths:
        shape = _read(path)
        shapes += [(f"{Path(path).name}#{i}", s) for i, s in enumerate(g.solids_of(shape))][:3]
    print("shape                                   faces  bytes      write_ms read_ms  same_face_order")
    for name, shape in shapes:
        t0 = time.perf_counter()
        text = roundtrip(shape)
        t1 = time.perf_counter()
        back = read_back(text)
        t2 = time.perf_counter()
        same = signature(back) == signature(shape)
        print(f"{name[:38]:38s} {len(g.faces_of(shape)):6d} {len(text):9d}  {1000*(t1-t0):8.1f} {1000*(t2-t1):7.1f}  {same}")


if __name__ == "__main__":
    main(sys.argv[1:])
