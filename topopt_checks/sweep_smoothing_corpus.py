"""平滑化の検証材料 64 個を作る: hanger_bar_attachment を 荷重4 × 要素サイズ4 × 最小幅4 で最適化し、
マーチングキューブへ渡る直前の二値マスクをボクセルSTLと npz で `output/smoothing_corpus/` へ保存する。

    .\\.venv\\Scripts\\python.exe topopt_checks\\sweep_smoothing_corpus.py --detach   # 切り離して全条件を回す(再開可)
    .\\.venv\\Scripts\\python.exe topopt_checks\\sweep_smoothing_corpus.py --one F0200_h2.4_w2
    .\\.venv\\Scripts\\python.exe topopt_checks\\sweep_smoothing_corpus.py --verify F0200_h2.4_w2
    .\\.venv\\Scripts\\python.exe topopt_checks\\sweep_smoothing_corpus.py --manifest       # manifest.json を作り直して数える

出力(`output/smoothing_corpus/`。gitignore 済み):
  voxel/<条件名>.stl   マスクの材料セルの境界面(バイナリSTL・ワールドmm・外向き)
  voxel/<条件名>.npz   mask(bool) / origin / spacing / shape
  records/<条件名>.json  1条件の記録(**最後に書く**ので、これがあれば条件は確定済み)
  manifest.json        records/ を条件の順に並べた64行
  sweep.lock / sweep.log / progress.json

**マスクは製品の実行 `optimize_run.run` の中で捕まえる**: `mesh.build_surface_from_mask` を差し替え、渡された
マスクを控えて例外で降りる(平滑化・finalize は回さない)。探索・島除去はそのまま製品のもの。
1条件 = 1子プロセス(メモリを持ち越さず、ピークメモリをその条件だけで測る)。
`ok` と `no_solution` の条件は再実行で飛ばす。`error` は次の実行で再挑戦する。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import _bootstrap  # noqa: F401
import _record

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "models" / "hanger_bar_attachment.json"
OUT = REPO_ROOT / "output" / "smoothing_corpus"
LOADS_N = (200, 500, 1000, 2000)
SIZES_MM = (2.4, 2.2, 2.0, 1.8)  # 粗い順(途中でも粗い条件から使えるように)
WIDTH_CELLS = (2, 3, 4, 5)
LOAD_DIRECTION = [0.0, 0.0, -1.0]
LOAD_BODY = "body-48c2ba06938d"
LOAD_FACE_INDEX = 7
MAX_PARALLEL = 10
EARLY_SECONDS = 10 * 60
START_FRACTION = 0.85  # 空きメモリが実測ピークのこの割合を超えたら起動する(ピークは短く、他のアプリの分は Windows が押し出す)
#: 要素サイズごとの1条件のピークメモリ[GB](実測。docs/TOPOPT.md)。
PEAK_GB = {2.4: 7.5, 2.2: 9.5, 2.0: 12.5, 1.8: 16.7}
#: 探索の結果が「目標の安全率を満たす形が無い」を意味する状態(`SearchResult.status`)。
NO_SOLUTION_STATES = ("no_feasible_found", "input_contradiction", "resolution_insufficient")


def condition_name(load: int, size: float, cells: int) -> str:
    return f"F{load:04d}_h{size:.1f}_w{cells}"


def all_conditions() -> list[dict]:
    return [dict(name=condition_name(f, h, w), load_n=f, element_size_mm=h, cells=w)
            for h in SIZES_MM for f in LOADS_N for w in WIDTH_CELLS]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _git_state() -> dict:
    def git(*args):
        return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"), "dirty_product_code": bool(git("status", "--porcelain", "--", "duocad"))}


def _write_atomic(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8")
    else:
        tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------
# 1条件(子プロセス)
# ---------------------------------------------------------------------------------------------
class _Captured(BaseException):
    """平滑化の直前で降りる合図(`except Exception` に飲まれないよう BaseException)。"""


def _set_load(doc, newtons: int) -> None:
    """文書の荷重面(`LOAD_BODY` の面 `LOAD_FACE_INDEX`)の大きさだけを差し替える。向き・面は変えない。"""
    from duocad.verbs.optimize_setup import set_loads

    ref = {"kind": "face", "body": LOAD_BODY, "index": LOAD_FACE_INDEX}
    set_loads(doc, [ref], float(newtons), LOAD_DIRECTION)
    loads = doc.optimize.loads
    assert len(loads) == 1 and loads[0]["body"] == LOAD_BODY and int(loads[0]["index"]) == LOAD_FACE_INDEX, loads
    assert loads[0]["magnitude"] == float(newtons) and loads[0]["vector"] == LOAD_DIRECTION, loads[0]


def _peak_memory_mb() -> dict:
    import psutil

    info = psutil.Process().memory_info()
    return {"peak_working_set_mb": round(info.peak_wset / 2**20, 1), "peak_commit_mb": round(info.peak_pagefile / 2**20, 1)}


def run_condition(spec: dict) -> dict:
    """1条件を製品経路で回し、記録(dict)を返す。成功なら voxel/ に stl・npz を確定させる。"""
    import numpy as np
    from unittest.mock import patch

    from duocad import optimize_run
    from duocad.topopt import binary_search, design_space as ds, mesh, trace
    from _documents import open_session
    from measure_min_width_floor import voxel_surface

    started = time.perf_counter()
    record: dict = dict(name=spec["name"], load_n=spec["load_n"], element_size_requested_mm=spec["element_size_mm"],
                        cells_requested=spec["cells"], started_at=_now(), **_git_state())
    captured: dict = {}

    def fake_surface(mask, *args, **kwargs):
        captured["mask"] = np.array(mask, dtype=bool, copy=True)
        raise _Captured()

    real_derive = ds.derive
    real_search = binary_search.find_best_feasible

    def derive(*args, **kwargs):
        space = real_derive(*args, **kwargs)
        captured["space"] = space
        return space

    def search(*args, **kwargs):
        t0 = time.perf_counter()
        result = real_search(*args, **kwargs)
        captured["search"] = result
        captured["search_seconds"] = time.perf_counter() - t0
        return result

    with tempfile.TemporaryDirectory(prefix="corpus-trace-") as trace_root, \
            patch.object(trace, "DEFAULT_ROOT", Path(trace_root)), \
            patch.object(mesh, "build_surface_from_mask", fake_surface), \
            patch.object(ds, "derive", derive), \
            patch.object(binary_search, "find_best_feasible", search):
        session = open_session(MODEL)
        doc = session.document
        _set_load(doc, spec["load_n"])
        limits = optimize_run.resolution_limits(doc)
        spacing = optimize_run._snap_to_grid(limits["bbox_min"], limits["bbox_max"],
                                             optimize_run._resolve_element_size(limits, spec["element_size_mm"]))
        min_width_mm = spec["cells"] * spacing
        min_width = optimize_run._resolve_min_width(
            spacing, min_width_mm, max_cells=min(optimize_run._grid_shape(limits["bbox_min"], limits["bbox_max"], spacing)))
        record.update(element_size_resolved_mm=spacing, min_width=min_width, min_width_request_mm=min_width_mm,
                      min_width_as_intended=min_width["cells"] == spec["cells"])
        if min_width["cells"] != spec["cells"]:
            raise RuntimeError(f"最小幅が意図したセル数に解決されなかった(意図 {spec['cells']} / 実 {min_width['cells']})")
        options = optimize_run.OptimizeOptions(element_size_mm=spec["element_size_mm"], min_width_mm=min_width_mm)
        record["options"] = dict(target_safety_factor=options.target_safety_factor,
                                 search_target_factor=options.search_target_factor,
                                 max_evaluations=options.max_evaluations)
        result = None
        try:
            result = optimize_run.run(doc, options)
        except _Captured:
            pass
        record["total_seconds_in_run"] = time.perf_counter() - started

    space = captured.get("space")
    if space is not None:
        record.update(grid=dict(origin=[float(v) for v in space.grid.origin], spacing=float(space.grid.spacing),
                                shape=[int(v) for v in space.grid.shape]),
                      design_elements=int(space.element_count))
    search_result = captured.get("search")
    if search_result is not None:
        best = search_result.best_feasible
        record.update(search_status=search_result.status, termination=search_result.termination,
                      evaluations=int(search_result.evaluations), search_seconds=captured["search_seconds"],
                      material_mm3=None if best is None else float(best.material_mm3),
                      safety_factor=None if best is None else float(best.safety_factor))

    mask = captured.get("mask")
    if mask is not None:
        assert search_result.status == "feasible" and tuple(mask.shape) == tuple(space.grid.shape)
        assert np.array_equal(mask, search_result.best_feasible.mask), "捕まえたマスクが探索の採用マスクと違う"
        name = spec["name"]
        verts, faces = voxel_surface(mask, space.grid.origin, space.grid.spacing)
        voxel = OUT / "voxel"
        voxel.mkdir(parents=True, exist_ok=True)
        stl_tmp, npz_tmp = voxel / f"{name}.stl.tmp", voxel / f"{name}.npz.tmp"
        mesh.write_stl(stl_tmp, verts, faces)
        with open(npz_tmp, "wb") as handle:
            np.savez_compressed(handle, mask=mask, origin=np.asarray(space.grid.origin, dtype=float),
                                spacing=float(space.grid.spacing), shape=np.asarray(mask.shape, dtype=np.int64))
        os.replace(stl_tmp, voxel / f"{name}.stl")
        os.replace(npz_tmp, voxel / f"{name}.npz")
        record.update(status="ok", stl=f"voxel/{name}.stl", npz=f"voxel/{name}.npz", stl_triangles=int(len(faces)),
                      stl_bytes=(voxel / f"{name}.stl").stat().st_size, mask_cells=int(mask.sum()))
    elif result is not None:
        state = result.get("state")
        reason = result.get("warning") or state
        record.update(status="no_solution" if state in NO_SOLUTION_STATES else "error", state=state, reason=reason,
                      termination=result.get("termination"), evaluations=result.get("evaluations"))
        if state not in NO_SOLUTION_STATES:
            record["error"] = f"探索に入る前に製品が断った: {state}"
    else:
        record.update(status="error", error="マスクも結果も得られなかった")
    record.update(total_seconds=time.perf_counter() - started, finished_at=_now(), **_peak_memory_mb())
    return record


def one(name: str) -> int:
    spec = next(s for s in all_conditions() if s["name"] == name)
    try:
        record = run_condition(spec)
    except Exception as error:  # noqa: BLE001 - 1条件の例外で全体を止めない
        import traceback
        record = dict(name=spec["name"], load_n=spec["load_n"], element_size_requested_mm=spec["element_size_mm"],
                      cells_requested=spec["cells"], status="error",
                      error=f"{type(error).__name__}: {error}", traceback=traceback.format_exc(),
                      finished_at=_now(), **_git_state())
        try:
            record.update(_peak_memory_mb())
        except Exception:  # noqa: BLE001
            pass
    _write_atomic(OUT / "records" / f"{name}.json", _record.dumps(record, default=float) + "\n")
    print(f"[{_now()}] {name}: {record['status']}  {record.get('total_seconds', 0):.0f}s  "
          f"peak {record.get('peak_working_set_mb')}MB", flush=True)
    return 0 if record["status"] in ("ok", "no_solution") else 1


# ---------------------------------------------------------------------------------------------
# manifest・検証
# ---------------------------------------------------------------------------------------------
def load_records() -> dict:
    records = {}
    for path in sorted((OUT / "records").glob("*.json")):
        records[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return records


def is_settled(record: dict | None) -> bool:
    """出力と記録が揃った確定済みの条件か(ok はファイルの実在まで見る)。"""
    if not record:
        return False
    if record["status"] == "no_solution":
        return bool(record.get("reason"))
    return record["status"] == "ok" and all((OUT / record[key]).is_file() for key in ("stl", "npz"))


def write_manifest() -> dict:
    records = load_records()
    rows = [records.get(spec["name"]) or dict(name=spec["name"], status="pending") for spec in all_conditions()]
    counts: dict = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    manifest = dict(generated_at=_now(), model=str(MODEL.relative_to(REPO_ROOT)), conditions_total=len(rows),
                    counts=counts, settled=sum(is_settled(r) for r in rows), rows=rows)
    _write_atomic(OUT / "manifest.json", _record.dumps(manifest, default=float) + "\n")
    return manifest


def check_manifest() -> int:
    manifest = write_manifest()
    problems = []
    if len(manifest["rows"]) != 64:
        problems.append(f"行数 {len(manifest['rows'])} != 64")
    for row in manifest["rows"]:
        if not is_settled(row if row["status"] != "pending" else None):
            problems.append(f"{row['name']}: 未確定({row['status']})")
        elif not row.get("min_width_as_intended"):
            problems.append(f"{row['name']}: 最小幅が意図したセル数でない")
    print(json.dumps(dict(counts=manifest["counts"], settled=manifest["settled"], problems=problems),
                     ensure_ascii=False, indent=1))
    return 0 if not problems else 1


def verify(name: str) -> int:
    """製品のマーチングキューブで同じマスクから作った面が、ボクセルSTLの境界から半セル以内にあるか。"""
    import numpy as np
    from scipy.spatial import cKDTree

    from duocad import optimize_run
    from duocad.topopt import mesh

    data = np.load(OUT / "voxel" / f"{name}.npz")
    mask, origin, spacing = data["mask"], data["origin"], float(data["spacing"])
    raw = np.fromfile(OUT / "voxel" / f"{name}.stl", dtype=np.uint8)
    triangles = int(np.frombuffer(raw[80:84].tobytes(), dtype="<u4")[0])
    body = np.frombuffer(raw[84:].tobytes(), dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
    assert len(body) == triangles
    tri = body["v"].astype(float)
    # STL の三角形 → 元のセル座標(ワールド → 要素添字)。境界面を密にサンプルして最近点を引く。
    tri_cells = (tri - origin) / spacing
    weights = [(a, b, 1.0 - a - b) for a in np.linspace(0, 1, 9) for b in np.linspace(0, 1, 9) if a + b <= 1.0 + 1e-9]
    samples = np.concatenate([tri_cells[:, 0] * c + tri_cells[:, 1] * a + tri_cells[:, 2] * b for a, b, c in weights])
    tree = cKDTree(samples)
    surface = mesh.build_surface_from_mask(mask)
    # 法線: 向きは巻き順から。外向きかを、三角形の重心から法線方向へ半セル出た点が空セルにあるかで確かめる。
    normal = np.cross(tri_cells[:, 1] - tri_cells[:, 0], tri_cells[:, 2] - tri_cells[:, 0])
    normal /= np.linalg.norm(normal, axis=1, keepdims=True)
    centre = tri_cells.mean(axis=1)
    inside = np.floor(centre - 0.25 * normal).astype(int)
    outside = np.floor(centre + 0.25 * normal).astype(int)
    pad = np.pad(mask, 1, constant_values=False)
    inside_ok = pad[tuple((inside + 1).T)].all()
    outside_ok = (~pad[tuple((outside + 1).T)]).all()
    out = {"name": name, "stl_triangles": triangles, "winding_outward": bool(inside_ok and outside_ok)}
    for label, verts in (("marching_cubes_raw", surface.verts_raw), ("taubin_smoothed", surface.verts_smooth)):
        distance, _ = tree.query(np.asarray(verts, dtype=float))
        out[label] = dict(max_distance_cells=float(distance.max()), p99_cells=float(np.percentile(distance, 99)),
                          within_half_cell=bool(distance.max() <= 0.5 + 0.09))
    # 製品が世界座標へ直す式とSTLの座標が同じ原点・刻みであること。
    world = np.asarray(optimize_run._mesh_to_world(surface.verts_raw, type("G", (), dict(origin=origin, spacing=spacing))()),
                       dtype=float).reshape(-1, 3)
    out["stl_bbox_mm"] = [tri.reshape(-1, 3).min(axis=0).round(4).tolist(), tri.reshape(-1, 3).max(axis=0).round(4).tolist()]
    out["mc_raw_bbox_mm"] = [world.min(axis=0).round(4).tolist(), world.max(axis=0).round(4).tolist()]
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["winding_outward"] and out["marching_cubes_raw"]["within_half_cell"] else 1


# ---------------------------------------------------------------------------------------------
# ドライバ(ロック・再開・切り離し)
# ---------------------------------------------------------------------------------------------
LOCK = OUT / "sweep.lock"
LOG = OUT / "sweep.log"


def _pid_is_sweep(pid: int) -> bool:
    import psutil

    try:
        return "sweep_smoothing_corpus" in " ".join(psutil.Process(pid).cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _acquire_lock() -> bool:
    OUT.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                pid = int(LOCK.read_text().split()[0])
            except (ValueError, IndexError):
                pid = -1  # 作成直後で pid が未記入かもしれない: 十分古くなければ触らない
                if time.time() - LOCK.stat().st_mtime < 5:
                    return False
            except OSError:
                continue
            if pid > 0 and _pid_is_sweep(pid):
                print(f"別のスイープが走っている(pid {pid})。何もしない", flush=True)
                return False
            # 死んだ持ち主の錠。名前を付け替えて奪う(同時に回収しようとした片方だけが成功する)。
            grave = LOCK.with_name(f"sweep.lock.stale.{os.getpid()}")
            try:
                os.replace(LOCK, grave)
            except OSError:
                continue
            grave.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(f"{os.getpid()} {_now()}\n")
        return True
    return False


def _running_children() -> dict:
    """走っている `--one` 子プロセスの {条件名: 起動からの秒数}(前のドライバが残した子も数える)。"""
    import psutil

    found = {}
    for proc in psutil.process_iter(["cmdline", "create_time"]):
        cmd = proc.info["cmdline"] or []
        if "--one" in cmd and any("sweep_smoothing_corpus" in part for part in cmd):
            found[cmd[cmd.index("--one") + 1]] = time.time() - proc.info["create_time"]
    return found


def drive() -> int:
    """粗い順に、子プロセスを**メモリの許す範囲で並べて**回す。

    実測(docs/TOPOPT.md): 1条件のピークメモリは探索の最初の評価(全材料)で立ち、その後の長い削減は 1GB 未満・1コア。
    そこで、起動から `EARLY_SECONDS` 以内の子が居ない間だけ次を起動し(ピーク同士を重ねない)、空きメモリが
    その要素サイズの実測ピークの `START_FRACTION` 倍を超えるときだけ起動する。
    """
    import psutil

    if not _acquire_lock():
        return 3
    attempted: set = set()
    try:
        while True:
            records = load_records()
            running = _running_children()
            pending = [s for s in all_conditions()
                       if not is_settled(records.get(s["name"])) and s["name"] not in running and s["name"] not in attempted]
            if not pending and not running:
                break
            if pending and len(running) < MAX_PARALLEL and all(age > EARLY_SECONDS for age in running.values()):
                spec = pending[0]
                need = PEAK_GB[spec["element_size_mm"]] * START_FRACTION
                if psutil.virtual_memory().available / 2**30 > need:
                    attempted.add(spec["name"])
                    print(f"[{_now()}] start {spec['name']}", flush=True)
                    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--one", spec["name"]],
                                     cwd=REPO_ROOT, stdin=subprocess.DEVNULL)
                    time.sleep(20)
                    continue
            manifest = write_manifest()
            _write_atomic(OUT / "progress.json", json.dumps(dict(
                updated_at=_now(), counts=manifest["counts"], settled=manifest["settled"], running=sorted(running)),
                ensure_ascii=False, indent=1))
            time.sleep(30)
        manifest = write_manifest()
        print(f"[{_now()}] sweep finished: {manifest['counts']}", flush=True)
        return 0
    finally:
        LOCK.unlink(missing_ok=True)


def detach() -> int:
    """自分を親から切り離して起動する(親が終わっても道連れにならない)。"""
    OUT.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().split()[0])
        except (ValueError, IndexError, OSError):
            pid = -1
        if pid > 0 and _pid_is_sweep(pid):
            print(f"already running: pid {pid}")
            return 0
    log = open(LOG, "ab")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    command = [sys.executable, str(Path(__file__).resolve()), "--drive"]
    try:
        proc = subprocess.Popen(command, cwd=REPO_ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, close_fds=True)
    except OSError:  # ジョブが breakaway を許さない環境
        proc = subprocess.Popen(command, cwd=REPO_ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                creationflags=flags, close_fds=True)
    print(f"started pid {proc.pid}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--detach", action="store_true", help="切り離したプロセスとして全条件を回す(再開可)")
    group.add_argument("--drive", action="store_true", help="前面で全条件を回す(--detach が内部で使う)")
    group.add_argument("--one", metavar="NAME", help="1条件だけ回す(記録も書く)")
    group.add_argument("--verify", metavar="NAME", help="ボクセルSTLが製品のマーチングキューブの面と半セル以内で揃うか")
    group.add_argument("--manifest", action="store_true", help="manifest.json を作り直し、64条件の確定を検査する")
    args = parser.parse_args()
    if args.detach:
        return detach()
    if args.drive:
        return drive()
    if args.one:
        return one(args.one)
    if args.verify:
        return verify(args.verify)
    return check_manifest()


if __name__ == "__main__":
    raise SystemExit(main())
