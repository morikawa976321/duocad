"""荷重の性格が違う合成の設計空間(S1 曲げ・S2 引張・S3 ねじり+曲げ)を製品の探索に通し、採用形状を k×k×k に割って解き直す
(`docs/TOPOPT_THICKNESS.md` 節O)。知りたいのは「k=1 の安全率 ÷ 細分後の安全率」が荷重の性格でどれだけ振れるか。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_inner_target_synthetic.py calibrate
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_inner_target_synthetic.py search --subject S1 --cells 2 --target 4.2 --budget 60 --out-dir D
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_inner_target_synthetic.py refine --subject S1 --npz D\\S1-m2-t4.2.npz --k 1 2 3 4 --result D\\S1-m2-t4.2-refine.json
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_inner_target_synthetic.py chain --out-dir D --cells 2 3

設計空間は 80×20×20 セルの直方体(要素サイズ 1・許容応力 40MPa・単位は N と mm)。x 最小面の中央(y・z とも 5〜15)を固定し、荷重面は題材ごと。
固定面・荷重面には、その面に接する 4 セル厚(x 方向)の保持ブロックを置く(節点は探索の確定表と同じく保持ブロックに接する)。
確定表は `tests/test_binary_search.py` の `Table` と同じ面/節点インタフェースの偽物で、荷重の配り方だけ製品の `AnchorTable.load_vector` に揃えて
節点の担当面積の比にする(k を変えても荷重の合力は同じ)。`search` は `binary_search.find_best_feasible` をそのまま呼ぶ(差し替えなし)。
`refine` は採用マスクを形は変えず k×k×k に割り、同じ物理的な面に対して確定表を作り直して `acceptance.inspect` で解き直す。
**判定はしない**(数字を書き出すだけ)。`chain` は全ジョブを直列に回し(結果があるジョブは飛ばす)、起動ごとの空きメモリ・HEAD・未コミットの変更を `chain.log` に残す。
空きメモリの確認(10GB 以上・足りなければ 10 分おきに最大 3 回)をするのは、要素数 10 万以上の細分だけ。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  (topopt_checks の import 経路を通す)
import _record
import numpy as np
from build123d import Box, Pos
from measure_min_width_floor import _opening_removed, _search_summary, voxel_surface

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "output" / "min_width" / "inner-target-synthetic-20260921"
SHAPE = (80, 20, 20)
#: 固定側の保持ブロック(セル添字の [下, 上) を x・y・z の順に)。固定面はこのブロックの x=0 側の面。
FIXED_BLOCK = ((0, 4), (5, 15), (5, 15))
FIXED_FACE, LOAD_FACE = 1, 2
#: 題材ごとの荷重面の保持ブロックと荷重(N)。荷重面はブロックの x=80 側の面。力の大きさは `calibrate` の全材料の安全率から決めた(節O)。
SUBJECTS = {
    "S1": dict(name="曲げ支配", load_block=((76, 80), (8, 12), (8, 12)), force=(0.0, 0.0, -2.0)),
    "S2": dict(name="引張支配", load_block=((76, 80), (8, 12), (8, 12)), force=(20.0, 0.0, 0.0)),
    "S3": dict(name="ねじり+曲げ", load_block=((76, 80), (16, 20), (8, 12)), force=(0.0, 0.0, -2.0)),
}
MEMORY_FREE_MIN_BYTES = 10 * 2**30
HEAVY_ELEMENTS = 100_000
MAX_ELEMENTS = 300_000
TARGET_FOR_PASS = 2.0


class PatchTable:
    """面 → (節点番号, 荷重の重み) の偽の確定表。`tests/test_binary_search.py` の `Table` と同じ面/節点インタフェース。"""

    def __init__(self, grid, faces: dict, dropped: dict | None = None):
        from duocad.topopt import anchor

        self.node_grid = anchor.NodeGrid(grid)
        self.faces = faces
        self.dropped = dropped or {}

    def node_ids(self, faces):
        return np.asarray(self.faces[faces[0]][0], dtype=np.int64)

    def fixed_dofs(self, faces):
        ids = self.node_ids(faces)
        return (ids[:, None] * 3 + np.arange(3)).ravel()

    def load_vector(self, faces, force):
        ids, weights = self.faces[faces[0]]
        out = np.zeros(self.node_grid.ndof)
        out.reshape(-1, 3)[ids] = np.outer(weights, np.asarray(force, dtype=float))
        return out

    def empty_faces(self):
        return []


def _patch(shape, x_node: int, y_nodes, z_nodes, k: int):
    """x=一定の面にある長方形の節点の(格子, 番号, 節点添字, 担当面積の比)。比は縁が半分・角が 1/4 で合計 1。引数の範囲は k=1 の節点単位。"""
    from duocad.topopt import anchor, voxelize

    grid = voxelize.Grid((0.0, 0.0, 0.0), 1.0 / k, tuple(int(n) * k for n in shape))
    ys = np.arange(y_nodes[0] * k, y_nodes[1] * k + 1)
    zs = np.arange(z_nodes[0] * k, z_nodes[1] * k + 1)
    wy = np.ones(len(ys)); wy[[0, -1]] = 0.5
    wz = np.ones(len(zs)); wz[[0, -1]] = 0.5
    yy, zz = np.meshgrid(ys, zs, indexing="ij")
    indices = np.stack([np.full(yy.size, x_node * k), yy.ravel(), zz.ravel()], axis=1)
    ids = anchor.NodeGrid(grid).ids_of(indices)
    weights = np.outer(wy, wz).ravel()
    return grid, ids, indices, weights / weights.sum()


def block_mask(block, k: int = 1) -> np.ndarray:
    mask = np.zeros(tuple(n * k for n in SHAPE), dtype=bool)
    mask[tuple(slice(lo * k, hi * k) for lo, hi in block)] = True
    return mask


def _block_shape(block):
    lo = np.array([b[0] for b in block], dtype=float)
    hi = np.array([b[1] for b in block], dtype=float)
    return (Pos(*((lo + hi) / 2)) * Box(*(hi - lo))).wrapped


def _live_only(ids: np.ndarray, indices: np.ndarray, weights: np.ndarray, live: np.ndarray | None, rule: str):
    """`live`(材料に接する節点の真偽。節点番号順)があれば、材料に接さない節点を面から外して重みを配り直す。合力は変わらない。

    製品の確定表(`anchor.build_anchors(allowed_nodes=...)`)も材料に接する節点だけへ対応付けるが、面の標本ごとに球内の許可節点へ配る。
    ここは同じではない単純化: `renormalize` は残った節点の重みの比のまま合計 1 に取り直し、`nearest` は外れた節点の重みをその面で最も近い残りの節点へ足す。
    """
    if live is None:
        return ids, weights, 0, 0.0
    keep = live[ids]
    dropped_weight = float(weights[~keep].sum())
    if rule == "nearest":
        alive = np.flatnonzero(keep)
        moved = weights[keep].copy()
        for i in np.flatnonzero(~keep):
            moved[np.linalg.norm(indices[alive] - indices[i], axis=1).argmin()] += weights[i]
        return ids[keep], moved, int((~keep).sum()), dropped_weight
    return ids[keep], weights[keep] / weights[keep].sum(), int((~keep).sum()), dropped_weight


def build_conditions(subject: str, k: int, target: float, mask: np.ndarray | None = None, load_rule: str = "renormalize"):
    """題材の探索条件(格子は k 細分後。物理的な面・保持ブロックは k に依らず同じ)。

    `mask`(k 細分後の材料)を渡すと、確定表は材料に接する節点だけにする(細分の解き直し用。探索の入口の表は全節点)。
    `load_rule` は外れた荷重節点の重みの配り直し(`_live_only`)。
    """
    from duocad.topopt import binary_search
    from duocad.topopt.acceptance import active_nodes

    spec = SUBJECTS[subject]
    force = tuple(float(v) for v in spec["force"])
    fixed_y, fixed_z = FIXED_BLOCK[1], FIXED_BLOCK[2]
    load_block = spec["load_block"]
    grid, fixed_ids, fixed_idx, fixed_w = _patch(SHAPE, FIXED_BLOCK[0][0], fixed_y, fixed_z, k)
    _, load_ids, load_idx, load_w = _patch(SHAPE, load_block[0][1], load_block[1], load_block[2], k)
    live = None if mask is None else active_nodes(mask).ravel()
    fixed_ids, fixed_w, fixed_dropped, _ = _live_only(fixed_ids, fixed_idx, fixed_w, live, "renormalize")
    load_ids, load_w, load_dropped, load_dropped_weight = _live_only(load_ids, load_idx, load_w, live, load_rule)
    table = PatchTable(grid, {FIXED_FACE: (fixed_ids, fixed_w), LOAD_FACE: (load_ids, load_w)},
                       dropped=dict(fixed_nodes=fixed_dropped, load_nodes=load_dropped, load_weight=load_dropped_weight))
    keep = block_mask(FIXED_BLOCK, 1) | block_mask(load_block, 1)
    return binary_search.SearchConditions(
        grid=grid, table=table, fixed_faces=(FIXED_FACE,), load_faces=(LOAD_FACE,), forces=(force,),
        preserves=(("keep_fixed", _block_shape(FIXED_BLOCK)), ("keep_load", _block_shape(load_block))),
        excludes=(), target_safety_factor=float(target), keep_cells=keep if k == 1 else None)


def _safety_row(report: dict) -> dict:
    safety = report.get("checks", {}).get("safety") or {}
    return {key: safety.get(key) for key in safety if not isinstance(safety.get(key), (dict, list))}


def _load_check(conditions) -> dict:
    """荷重ベクトルの合力(N)と節点数。k に依らず力の合力が同じであることを確かめる記録。"""
    vector = conditions.table.load_vector([LOAD_FACE], conditions.forces[0]).reshape(-1, 3)
    return dict(resultant_n=vector.sum(axis=0).tolist(), requested_n=list(conditions.forces[0]),
                load_nodes=int(len(conditions.table.node_ids([LOAD_FACE]))),
                fixed_nodes=int(len(conditions.table.node_ids([FIXED_FACE]))), dropped_not_touching_material=conditions.table.dropped,
                moment_about_fixed_patch_centre_nmm=_moment(conditions, vector).tolist())


def _moment(conditions, vector: np.ndarray) -> np.ndarray:
    """荷重の、固定面の中央まわりのモーメント(N·mm)。ねじり成分(x 軸まわり)が題材で違うことの確認用。"""
    ids = np.flatnonzero(np.abs(vector).sum(axis=1))
    indices = conditions.table.node_grid.indices_of_ids(ids)
    positions = conditions.table.node_grid.positions_of(indices)
    centre = np.array([0.0, sum(FIXED_BLOCK[1]) / 2, sum(FIXED_BLOCK[2]) / 2])
    return np.cross(positions - centre, vector[ids]).sum(axis=0)


def _inspect(mask: np.ndarray, conditions) -> dict:
    from duocad.topopt import acceptance

    return acceptance.inspect(mask, mask, conditions.grid, **conditions.inspect_kwargs(bbox_cache=None))


def _code_state() -> dict:
    def git(*args):
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, encoding="utf-8").stdout.strip()

    return dict(head=git("rev-parse", "HEAD"), status_duocad_topopt_checks=git("status", "--short", "--", "duocad", "topopt_checks"))


def calibrate(args) -> int:
    """全材料(設計空間の全セル)の安全率を、`SUBJECTS` の力で出す。"""
    rows = {}
    for subject in SUBJECTS:
        conditions = build_conditions(subject, 1, args.target)
        mask = np.ones(SHAPE, dtype=bool)
        started = time.perf_counter()
        report = _inspect(mask, conditions)
        row = dict(subject=subject, full_material_safety=_safety_row(report), status=report["status"],
                   failed=[n for n, c in report["checks"].items() if c.get("status") != "pass"],
                   load=_load_check(conditions), seconds=time.perf_counter() - started)
        rows[subject] = row
        print(json.dumps(row, ensure_ascii=False, default=float), flush=True)
    return 0


def search(args) -> int:
    from duocad.topopt import binary_search, mesh, trace

    subject, m, target = args.subject, args.cells, args.target
    spec = SUBJECTS[subject]
    tag = args.tag or f"{subject}-m{m}-t{target:g}"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conditions = build_conditions(subject, 1, target)
    design = np.ones(SHAPE, dtype=bool)
    out: dict = dict(tag=tag, subject=subject, subject_name=spec["name"], grid_shape=list(SHAPE), design_space_elements=int(design.sum()),
                     min_width_cells=m, target_safety_factor=target, max_evaluations=args.budget,
                     force_n=list(conditions.forces[0]), load_block=spec["load_block"], fixed_block=FIXED_BLOCK,
                     load=_load_check(conditions), code_state=_code_state())
    with tempfile.TemporaryDirectory(prefix="topopt-trace-") as trace_root, patch.object(trace, "DEFAULT_ROOT", Path(trace_root)):
        started = time.perf_counter()
        full = _inspect(design, conditions)
        out["full_material"] = dict(_safety_row(full), status=full["status"], seconds=time.perf_counter() - started)
        started = time.perf_counter()
        result = binary_search.find_best_feasible(design, conditions, max_evaluations=args.budget, min_width_cells=m)
        out["seconds"] = time.perf_counter() - started
    out.update(search_status=result.status, termination=result.termination, evaluations=int(result.evaluations))
    diagnostics = result.diagnostics or {}
    out["min_width_diagnostics"] = {key: value for key, value in (diagnostics.get("min_width") or {}).items()
                                    if isinstance(value, (int, float, str, bool, type(None)))}
    best = result.best_feasible
    if best is None:
        out["error"] = f"best_feasible が無い(status={result.status})"
        _record.write(out_dir / f"{tag}.json", out, default=float)
        print(_record.dumps(out, default=float), flush=True)
        return 0
    reduction = diagnostics.get("reduction") or {}
    out.update(
        accepted_elements=int(best.mask.sum()),
        material_fraction_of_design_space=float(best.mask.sum() / design.sum()),
        coarse_safety_factor=float(best.safety_factor),
        reduction_stop_reason=reduction.get("stop_reason"),
        opening_2x2x2=_opening_removed(best.mask, 2), opening_3x3x3=_opening_removed(best.mask, 3),
        opening_4x4x4=_opening_removed(best.mask, 4),
        **_search_summary(result, best.mask, np.asarray(conditions.keep_cells, dtype=bool), m))
    np.savez_compressed(out_dir / f"{tag}.npz", mask=best.mask, origin=np.zeros(3), spacing=1.0)
    verts, faces = voxel_surface(best.mask, np.zeros(3), 1.0)
    stl_path = out_dir / f"{tag}-voxel.stl"
    mesh.write_stl(stl_path, verts, faces)
    out.update(stl_path=str(stl_path), stl_triangles=int(len(faces)))
    _record.write(out_dir / f"{tag}.json", out, default=float)
    print(_record.dumps(out, default=float), flush=True)
    return 0


def _available_bytes() -> int:
    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total_phys", ctypes.c_ulonglong),
                    ("avail_phys", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong), ("avail_ext", ctypes.c_ulonglong)]

    status = Status()
    status.length = ctypes.sizeof(Status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return int(status.avail_phys)


def wait_for_memory(need: int = MEMORY_FREE_MIN_BYTES, tries: int = 3, wait_seconds: int = 600) -> tuple[bool, list[float]]:
    """空き物理メモリ(GB)が `need` 以上になるまで、最大 `tries` 回(間隔 `wait_seconds`)確かめる。"""
    seen: list[float] = []
    for attempt in range(tries):
        free = _available_bytes()
        seen.append(round(free / 2**30, 2))
        if free >= need:
            return True, seen
        if attempt < tries - 1:
            time.sleep(wait_seconds)
    return False, seen


def refine(args) -> int:
    from duocad.topopt import trace

    data = np.load(args.npz)
    mask = data["mask"].astype(bool)
    if mask.shape != SHAPE:
        raise SystemExit(f"マスクの形が違う: {mask.shape} != {SHAPE}")
    result_path = Path(args.result) if args.result else None
    rows: list[dict] = json.loads(result_path.read_text(encoding="utf-8")) if result_path and result_path.exists() else []
    done = {row["k"] for row in rows}
    with tempfile.TemporaryDirectory(prefix="topopt-trace-") as trace_root, patch.object(trace, "DEFAULT_ROOT", Path(trace_root)):
        for k in args.k:
            if k in done:
                continue
            fine = mask
            for axis in range(3):
                fine = np.repeat(fine, k, axis=axis)
            elements = int(fine.sum())
            row: dict = dict(k=k, spacing=1.0 / k, elements=elements, grid_shape=[n * k for n in SHAPE])
            if elements > MAX_ELEMENTS:
                row.update(status="not_measured", reason=f"要素数が {MAX_ELEMENTS:,} を超える")
            else:
                ok, free_seen = (True, [round(_available_bytes() / 2**30, 2)]) if elements < HEAVY_ELEMENTS else wait_for_memory()
                row["free_memory_gb_before"] = free_seen
                if not ok:
                    row.update(status="not_measured", reason="空きメモリ不足(10GB 未満のまま 3 回確かめた)")
                else:
                    conditions = build_conditions(args.subject, k, TARGET_FOR_PASS, fine, args.load_rule)
                    row["load_rule"] = args.load_rule
                    row["load"] = _load_check(conditions)
                    started = time.perf_counter()
                    report = _inspect(fine, conditions)
                    row.update(status=report.get("status"), seconds=time.perf_counter() - started,
                               failed=[n for n, c in report.get("checks", {}).items() if c.get("status") != "pass"],
                               safety=_safety_row(report))
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False, default=float), flush=True)
            if result_path:
                _record.write(result_path, sorted(rows, key=lambda r: r["k"]), default=float)
    return 0


def _python_processes() -> list[str]:
    """物理メモリ 300MB 以上の python.exe(自分と親〈venv の起動用プロセス〉は除く)。他セッションの重い Python が並走していたかの記録用。"""
    text = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    rows = []
    for line in text.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 5 and parts[0].lower().startswith("python") and parts[1] not in (str(os.getpid()), str(os.getppid())):
            kilobytes = int(parts[4].replace(",", "").replace(" K", "").replace("\xa0", "").strip() or 0) if parts[4] else 0
            if kilobytes >= 300 * 1024:
                rows.append(f"pid {parts[1]}: {kilobytes // 1024}MB")
    return rows


def chain(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / "chain.log"
    script = str(Path(__file__).resolve())

    def note(text: str) -> None:
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {text}\n")

    def run(arguments: list[str], stdout: Path) -> None:
        note(f"start {' '.join(arguments)} free={_available_bytes() / 2**30:.1f}GB python={_python_processes()} code={_code_state()}")
        with stdout.open("a", encoding="utf-8") as handle:
            subprocess.run([sys.executable, script, *arguments], stdout=handle, stderr=subprocess.STDOUT, check=False)
        note(f"end {arguments[0]} {arguments[-1]} free={_available_bytes() / 2**30:.1f}GB")

    for m in args.cells:
        for subject in args.subjects:
            for target in args.targets:
                tag = f"{subject}-m{m}-t{target:g}"
                final = tag
                if not (out_dir / f"{tag}.json").exists():
                    run(["search", "--subject", subject, "--cells", str(m), "--target", str(target), "--budget", "60",
                         "--out-dir", str(out_dir)], out_dir / f"{tag}.stdout.log")
                first = json.loads((out_dir / f"{tag}.json").read_text(encoding="utf-8"))
                unfinished = (first.get("termination") == "evaluation_limit"
                              and (first.get("coarse_safety_factor") or 0.0) >= 1.1 * target)
                if unfinished:
                    final = f"{tag}-b120"
                    if not (out_dir / f"{final}.json").exists():
                        run(["search", "--subject", subject, "--cells", str(m), "--target", str(target), "--budget", "120",
                             "--tag", final, "--out-dir", str(out_dir)], out_dir / f"{final}.stdout.log")
                if (out_dir / f"{final}.npz").exists():
                    run(["refine", "--subject", subject, "--npz", str(out_dir / f"{final}.npz"), "--k", "1", "2", "3", "4",
                         "--result", str(out_dir / f"{final}-refine.json")], out_dir / f"{final}-refine.stdout.log")
    note("chain done")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("calibrate", help="全材料の安全率を出す")
    p.add_argument("--target", type=float, default=4.2)
    p.set_defaults(func=calibrate)
    p = sub.add_parser("search", help="探索を1本回して採用マスク・STL・結果 JSON を書く")
    p.add_argument("--subject", choices=sorted(SUBJECTS), required=True)
    p.add_argument("--cells", type=int, required=True, help="最小幅のセル数 m")
    p.add_argument("--target", type=float, required=True, help="探索に渡す目標安全率")
    p.add_argument("--budget", type=int, default=60)
    p.add_argument("--tag", default=None)
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.set_defaults(func=search)
    p = sub.add_parser("refine", help="採用マスクを k×k×k に割って解き直す")
    p.add_argument("--subject", choices=sorted(SUBJECTS), required=True)
    p.add_argument("--npz", required=True)
    p.add_argument("--k", type=int, nargs="+", default=[1, 2, 3, 4])
    p.add_argument("--result", default=None)
    p.add_argument("--load-rule", choices=["renormalize", "nearest"], default="renormalize",
                   help="細分で材料に接さない荷重節点の重みの配り直し(既定 renormalize)")
    p.set_defaults(func=refine)
    p = sub.add_parser("chain", help="全ジョブを直列に回す(結果があるジョブは飛ばす)")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--cells", type=int, nargs="+", default=[2, 3])
    p.add_argument("--subjects", nargs="+", default=["S1", "S2", "S3"])
    p.add_argument("--targets", type=float, nargs="+", default=[2.0, 4.2])
    p.set_defaults(func=chain)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
