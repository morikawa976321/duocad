"""hanger を題材にする測定スクリプトが共有する部品(製品と同じ作り方の条件・形ごとの `fem.Model`・1回の求解の計測)。

`import _bootstrap` を済ませたスクリプトから `from _hanger_setup import ...` で読む。
"""
import ctypes
import time
from pathlib import Path

import numpy as np

from duocad import optimize_run
from duocad.session import Session
from duocad.topopt import acceptance, design_space, fem

MODELS_DIR = str(Path(__file__).resolve().parent.parent / 'models')
MODEL_NAME = 'hanger_bar_attachment.json'
PENAL = 3.0


class _MemoryStatus(ctypes.Structure):
    _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]


def free_gb():
    """空き物理メモリ(GB)。2.0mm は過去にここで機械を止めかけているので毎回見る。"""
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return status.ullAvailPhys / 1e9


def _setup(spacing_request):
    """`(space, conditions, min_width)`。探索に渡す目標は実際の目標 2.0 のまま(係数 1.0)。"""
    session = Session(None, 0.2, models_dir=MODELS_DIR)
    session.open_document(MODEL_NAME)
    bundle = optimize_run.build_conditions(session.document)
    limits = optimize_run.resolution_limits(session.document)
    spacing = optimize_run._snap_to_grid(limits['bbox_min'], limits['bbox_max'],
                                         optimize_run._resolve_element_size(limits, spacing_request))
    space = design_space.derive(bundle.conditions, spacing=spacing)
    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    if mapping['status'] != 'pass':
        raise SystemExit(f"確定表が不成立: {mapping['status']}")
    options = optimize_run.OptimizeOptions(target_safety_factor=2.0, search_target_factor=1.0)
    conditions = optimize_run._build_search_conditions(session.document, space, bundle, options,
                                                       table=table, mapping=mapping)
    min_width = optimize_run._resolve_min_width(spacing, None)
    return space, conditions, min_width


def build_model(material, fixed, load):
    live = acceptance.active_nodes(material).ravel()
    active = np.repeat(live, 3)
    return fem.Model(*material.shape, fixed_dofs=fixed, load=load,
                     extra_fixed_dofs=np.flatnonzero(~active), active_mask=material)


def model_for(mask, conditions):
    """形ごとに条件節点(拘束・荷重)を取り直す。探索(`acceptance.safety_check`)と同じ。"""
    _, fixed, load = acceptance.condition_check(mask, conditions.table, conditions.fixed_faces,
                                                conditions.load_faces, conditions.forces)
    return build_model(mask, fixed, load)


def solve_once(model, density, solver):
    with fem.collect_solve_stats() as stats:
        started = time.perf_counter()
        displacement = model.solve(density, PENAL, solver=solver)
        seconds = time.perf_counter() - started
    return displacement, dict(iterations=int(stats['cg_iterations']), seconds=seconds,
                              solve_seconds=float(stats['solve_seconds']),
                              fallbacks=int(stats['fallbacks']))
