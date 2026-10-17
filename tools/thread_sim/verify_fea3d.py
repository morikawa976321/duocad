"""Sequential verification/comparison; numpy/scipy only, no new dependencies.

Run from repo root with .venv/Scripts/python.exe -B -m
tools.thread_sim.verify_fea3d [--case all|analytic|comparison]
[--profile all|trap15|trap24|iso] [--levels 8,12,16].
For the finer reported comparison, run each profile separately with
--case comparison --profile NAME --levels 12,18,24.
Outputs: output/thread_sim/fea3d/*.json, *.csv and fine-mesh Gauss stresses.
Each profile can be run separately to keep individual runs short. The Windows
process gets a 1800 MiB commit limit and reports peak working set AND commit.
All tolerances below are explicit verification criteria, not measured facts.
"""

import argparse
import csv
import ctypes
from dataclasses import replace
import gc
import json
import os
from pathlib import Path
import sys
from time import perf_counter

# Process-local only. Single-threaded BLAS avoids oversubscription/extra stacks.
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
sys.dont_write_bytecode = True

import numpy as np
from scipy.spatial import cKDTree

try:
    from . import profiles as pf
    from .fea import Body, Dirichlet, Material
    from .mesh import Mesh
    from .fea3d import (PeriodicBody, WallAverager, element_matrices,
                        kinematics, make_mesh, stiffness)
except ImportError:
    import profiles as pf
    from fea import Body, Dirichlet, Material
    from mesh import Mesh
    from fea3d import (PeriodicBody, WallAverager, element_matrices,
                       kinematics, make_mesh, stiffness)


OUTPUT = Path(__file__).resolve().parents[2]/'output/thread_sim/fea3d'
WALL = Material(3200., 2900., .33, .33, 3200/(2*1.33))
INFILL = replace(WALL, E_p=.06*WALL.E_p, E_z=.06*WALL.E_z, G_zp=.06*WALL.G_zp)
EPS = 1e-3
TWIST = 1e-3  # rad/mm
_JOB = None


def memory_limit():
    """Limit this process's private committed memory, without creating workers."""
    global _JOB
    if os.name != 'nt':
        return 'not Windows; mesh/averaging allocation guards remain active'
    from ctypes import wintypes as w
    size = ctypes.c_size_t

    class Basic(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                    ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', w.DWORD),
                    ('MinimumWorkingSetSize', size), ('MaximumWorkingSetSize', size),
                    ('ActiveProcessLimit', w.DWORD), ('Affinity', size),
                    ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(a, ctypes.c_uint64) for a in
                    ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                     'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class Extended(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', IO),
                    ('ProcessMemoryLimit', size), ('JobMemoryLimit', size),
                    ('PeakProcessMemoryUsed', size), ('PeakJobMemoryUsed', size)]

    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateJobObjectW.restype = w.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    k.GetCurrentProcess.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    _JOB = k.CreateJobObjectW(None, None)
    limit = Extended()
    limit.BasicLimitInformation.LimitFlags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    limit.ProcessMemoryLimit = 1800*1024**2
    if not _JOB or not k.SetInformationJobObject(_JOB, 9, ctypes.byref(limit), ctypes.sizeof(limit)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not k.AssignProcessToJobObject(_JOB, k.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return 'Windows Job Object: process commit <= 1800 MiB'


def peak_memory():
    if os.name != 'nt':
        import resource
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return dict(peak_working_set_MiB=v/(1024**2 if sys.platform == 'darwin' else 1024))
    from ctypes import wintypes as w

    class Counters(ctypes.Structure):
        _fields_ = [('cb', w.DWORD), ('PageFaultCount', w.DWORD)] + [
            (n, ctypes.c_size_t) for n in ('PeakWorkingSetSize', 'WorkingSetSize',
             'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage',
             'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage',
             'PagefileUsage', 'PeakPagefileUsage')]

    c = Counters()
    c.cb = ctypes.sizeof(c)
    k = ctypes.WinDLL('kernel32')
    k.GetCurrentProcess.restype = w.HANDLE
    psapi = ctypes.WinDLL('psapi')
    psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD]
    if not psapi.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(c), c.cb):
        raise ctypes.WinError()
    return dict(peak_working_set_MiB=c.PeakWorkingSetSize/1024**2,
                peak_commit_MiB=c.PeakPagefileUsage/1024**2)


def _write(name, data):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT/f'{name}.json').write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def element_tests():
    # Distorted element: arbitrary affine displacement must produce D*strain.
    from_signs = np.array([[-1,-1,-1], [1,-1,-1], [1,1,-1], [-1,1,-1],
                           [-1,-1,1], [1,-1,1], [1,1,1], [-1,1,1]], float)
    p = from_signs.copy()
    p[6] += [.31, -.13, .22]
    p[3] += [-.12, .07, .14]
    D = stiffness(WALL)[None]
    ke, recovery = element_matrices(p[None], D)
    b, h, _, w = kinematics(p[None])
    A = np.array([[.003, .002, -.001], [.004, -.002, .001], [-.003, .001, .005]])
    u = (p@A.T + [.2, -.1, .4]).ravel()
    alpha = recovery[0]@u
    strain = np.einsum('qai,i->qa', b[0], u)+np.einsum('qai,i->qa', h[0], alpha)
    exact = np.array([A[0,0], A[1,1], A[2,2], A[0,1]+A[1,0],
                      A[1,2]+A[2,1], A[0,2]+A[2,0]])
    patch_error = float(np.max(abs(strain-exact)))
    eigen = np.linalg.eigvalsh(ke[0])
    # Slender rectangular element, pure bending about y, length/thickness=100.
    E, nu, length, width, thick = 3200., .33, 10., 1., .1
    p = from_signs*np.array([length, width, thick])/2
    x, y, z = p.T
    u = np.column_stack((-x*z, nu*y*z, (x*x-nu*y*y+nu*z*z)/2)).ravel()
    iso = stiffness(Material.isotropic(E, nu))[None]
    ke, _ = element_matrices(p[None], iso)
    b, _, _, w = kinematics(p[None])
    standard = np.einsum('eqai,eab,eqbj,eq->eij', b, iso, b, w, optimize=True)[0]
    exact_energy = .5*E*width*thick**3/12*length
    ratio = float(.5*u@ke[0]@u/exact_energy)
    standard_ratio = float(.5*u@standard@u/exact_energy)
    return dict(distorted_patch_max_strain_error=patch_error,
                rigid_modes=int(np.sum(abs(eigen) < eigen.max()*1e-10)),
                bending_energy_ratio=ratio, standard_H8_bending_energy_ratio=standard_ratio,
                pass_=bool(patch_error < 1e-12 and abs(ratio-1) < 1e-7
                           and np.sum(abs(eigen) < eigen.max()*1e-10) == 6))


def analytic():
    rows = []
    for label, material in [('isotropic', Material.isotropic(3200, .33)), ('TI', WALL)]:
        for n in (8, 24, 48):
            started = perf_counter()
            m = make_mesh(lambda s: np.zeros_like(s)+3, n, 4, pitch=1.5, solid=True)
            b = PeriodicBody(m, material)
            tension, torsion = b.solve(eps=EPS), b.solve(twist=TWIST)
            A, J = np.pi*3**2, np.pi*3**4/2
            F, T = material.E_z*A*EPS, material.G_zp*J*TWIST
            exact_stress = np.zeros_like(torsion.stress)
            exact_stress[..., 4] = material.G_zp*TWIST*torsion.position[..., 0]
            exact_stress[..., 5] = -material.G_zp*TWIST*torsion.position[..., 1]
            l2 = np.sqrt(np.sum((torsion.stress-exact_stress)**2*torsion.volume[..., None])
                         /np.sum(exact_stress**2*torsion.volume[..., None]))
            expected = np.array([0, 0, material.E_z*EPS, 0, 0, 0])
            row = dict(material=label, n=n, nz=4, F=tension.F, exact_F=F,
                       F_error_pct=100*(tension.F/F-1), T=torsion.T, exact_T=T,
                       T_error_pct=100*(torsion.T/T-1), torsion_stress_L2_pct=100*float(l2),
                       torsion_stress_max_relative=float(np.max(abs(torsion.stress-exact_stress))
                                                        /(material.G_zp*TWIST*3)),
                       tension_stress_max_relative=float(np.max(abs(tension.stress-expected))/(material.E_z*EPS)),
                       equilibrium=max(tension.equilibrium_residual, torsion.equilibrium_residual),
                       seconds=perf_counter()-started)
            row.update(peak_memory())
            rows.append(row)
            print(json.dumps(row), flush=True)
            del b, tension, torsion
            gc.collect()
    concentric = []
    for n in (8, 12, 16):
        m = make_mesh(lambda s: 3+np.zeros_like(s), n, 4, pitch=1.5)
        b = PeriodicBody(m, WALL, INFILL)
        r = b.solve(eps=EPS)
        exact = EPS*np.pi*(WALL.E_z*(3**2-2.17**2)+INFILL.E_z*2.17**2)
        # With matching Poisson ratios the analytic common Poisson contraction
        # satisfies interface compatibility/traction, so area weighting is exact.
        mesh_exact = EPS*np.sum(np.where(m.wall[:, None], WALL.E_z, INFILL.E_z)*r.volume)/m.pitch
        row = dict(n=n, F=r.F, exact_F=exact, error_pct=100*(r.F/exact-1),
                   mesh_area_force=mesh_exact, mesh_area_error_pct=100*(r.F/mesh_exact-1))
        concentric.append(row)
        print(json.dumps(row), flush=True)
        del b, r
        gc.collect()
    # A radius greater than half the period must count both neighbouring images.
    # Nonconstant manufactured stresses + enormous infill stresses independently
    # exercise wrap-around, image multiplicities, and wall-only selection.
    m = make_mesh(lambda s: 3+np.zeros_like(s), 8, 4, pitch=1.)
    b = PeriodicBody(m, WALL, INFILL)
    r = b.solve(eps=EPS)
    avg = WallAverager(m, r, centers=[[2.7, 0, .01], [2.7, 0, .99]])
    vals = avg.average_stresses(r)
    avg_error = float(np.max(abs(vals-[0, 0, WALL.E_z*EPS, 0, 0, 0])))
    manufactured = np.zeros_like(r.stress)
    manufactured[..., 2] = 7+np.sin(2*np.pi*r.position[..., 2])+r.position[..., 0]/10
    manufactured[~m.wall] = 1e12
    modified = replace(r, stress=manufactured)
    computed = avg.average_stresses(modified)
    points = r.position[m.wall].reshape(-1, 3)
    stresses = manufactured[m.wall].reshape(-1, 6)
    weights = r.volume[m.wall].ravel()
    brute = []
    for center in avg.centers:
        numerator, denominator = np.zeros(6), 0.
        for shift in range(-2, 3):
            inside = np.linalg.norm(points+[0, 0, shift]-center, axis=1) <= .66
            numerator += np.sum(stresses[inside]*weights[inside, None], axis=0)
            denominator += weights[inside].sum()
        brute.append(numerator/denominator)
    manufactured_error = float(np.max(abs(computed-brute)))
    e = element_tests()
    fine = [x for x in rows if x['n'] == 48]
    passed = (e['pass_'] and avg_error < 1e-9 and manufactured_error < 1e-12
              and all(abs(x['F_error_pct']) < .25 and abs(x['T_error_pct']) < .5
                      and x['torsion_stress_L2_pct'] < 1
                      and x['tension_stress_max_relative'] < 1e-8
                      and x['equilibrium'] < 1e-7 for x in fine)
              and abs(concentric[-1]['error_pct']) < .25
              and abs(concentric[-1]['mesh_area_error_pct']) < 1e-7)
    return dict(cylinders=rows, concentric=concentric, element=e,
                periodic_average_absolute_error=avg_error,
                manufactured_wall_periodic_average_error=manufactured_error, pass_=bool(passed))


def ring_mesh(profile, n, *, solid=False, periods=5, t_wall=.83):
    """Structured meridional T6 mesh, exact profile corners and wall interface."""
    P = profile.pitch
    z = np.unique(np.concatenate([np.linspace(0, periods*P, periods*2*n+1)]
                                +[profile.s+j*P for j in range(periods)]))
    # Merge near-identical breakpoints without zero-height elements.
    z = z[np.r_[True, np.diff(z) > 1e-10*P]]
    nr = n
    layers = max(1, min(nr-1, round(nr*t_wall/np.mean(profile.r))))
    interface = 1-layers/nr
    q = np.linspace(0, 1, nr+1)
    R = profile(z)
    radius = (q[None]*R[:, None] if solid else np.where(
        q[None] <= interface, q[None]/interface*(R[:, None]-t_wall),
        R[:, None]-t_wall+(q[None]-interface)/(1-interface)*t_wall))
    corners = np.stack((radius, np.broadcast_to(z[:, None], radius.shape)), axis=-1).reshape(-1, 2)
    k, i = np.meshgrid(np.arange(len(z)-1), np.arange(nr), indexing='ij')
    a = (k*(nr+1)+i).ravel()
    b, c, d = a+1, a+nr+2, a+nr+1
    triangles = np.concatenate((np.column_stack((a, b, c)), np.column_stack((a, c, d))))
    groups = np.tile((np.arange(nr) >= nr-layers), len(z)-1)
    wall = np.ones(len(triangles), bool) if solid else np.tile(groups, 2)
    edges, inverse, count = np.unique(np.sort(triangles[:, [[0,1], [1,2], [2,0]]].reshape(-1, 2), axis=1),
                                     axis=0, return_inverse=True, return_counts=True)
    mids = len(corners)+np.arange(len(edges))
    nodes = np.vstack((corners, corners[edges].mean(axis=1)))
    tris = np.column_stack((triangles, mids[inverse].reshape(-1, 3)))
    boundary_ids = np.flatnonzero(count == 1)
    boundary = np.column_stack((edges[boundary_ids, 0], mids[boundary_ids], edges[boundary_ids, 1]))
    zz = nodes[boundary][:, :, 1]
    sources = np.where(np.all(abs(zz) < 1e-12, axis=1), 0,
                       np.where(np.all(abs(zz-periods*P) < 1e-12, axis=1), 1, 2))
    return Mesh(nodes, tris, boundary, sources), wall


def ring_average(mesh, wall, r, profile, radius=.66, periods=5):
    mask = np.repeat(wall, r.stress.shape[1])
    points = r.position.reshape(-1, 2)[mask]
    weights = r.volume.ravel()[mask]  # volume, including 2*pi*r, NOT area
    stresses = r.stress.reshape(-1, 4)[mask]
    wn = np.unique(mesh.tris[wall])
    centers = np.vstack((mesh.nodes[wn], mesh.nodes[mesh.tris[wall, :3]].mean(axis=1)))
    P = profile.pitch
    lo, hi = (periods-1)/2*P, (periods+1)/2*P
    centers = np.unique(centers[(centers[:, 1] >= lo) & (centers[:, 1] <= hi)], axis=0)
    tree = cKDTree(points)
    best = dict(FI=-1.)
    for start in range(0, len(centers), 64):
        for center, ids in zip(centers[start:start+64], tree.query_ball_point(centers[start:start+64], radius)):
            ids = np.asarray(ids, int)
            if not len(ids):
                raise ValueError('Empty ring wall-average neighbourhood')
            avg = np.average(stresses[ids], axis=0, weights=weights[ids])
            fi = (max(avg[1], 0)/30)**2+(avg[3]/25)**2
            if fi > best['FI']:
                best = dict(FI=float(fi), center=center.tolist(), average_stress=avg.tolist(), centers=len(centers))
    return best


def comparison(profile_name, levels):
    profiles = dict(trap15=pf.trapezoid(6, 1.5, .6, 45, 45),
                    trap24=pf.trapezoid(6, 2.4, .96, 45, 45), iso=pf.iso(6, 1.0))
    profile = profiles[profile_name]
    rows = []
    for solid in (False, True):
        for n in levels:
            started = perf_counter()
            mesh = make_mesh(profile, n, 2*n, solid=solid)
            body = PeriodicBody(mesh, WALL, INFILL)
            tension, torsion = body.solve(eps=EPS), body.solve(twist=TWIST)
            averager = WallAverager(mesh, tension)
            fa, ta = averager.evaluate_many([tension, torsion])
            row = dict(profile=profile_name, solid=solid, n=n, nz=2*n,
                       elements3d=len(mesh.hexes), dofs3d=3*(len(mesh.nodes)-len(mesh.top)),
                       K_axial_3d=tension.F/EPS, K_torsion_3d=torsion.T/TWIST,
                       FI_tension_3d=fa['FI'], FI_torsion_3d=ta['FI'],
                       F_fail_3d=tension.F/np.sqrt(fa['FI']),
                       T_fail_3d=torsion.T/np.sqrt(ta['FI']),
                       eps_fail_3d=EPS/np.sqrt(fa['FI']),
                       twist_fail_3d=TWIST/np.sqrt(ta['FI']),
                       max_FI_center_tension=fa['center'], max_FI_center_torsion=ta['center'],
                       tension_induced_T=tension.T, torsion_induced_F=torsion.F,
                       reciprocity_error=abs(tension.T/EPS-torsion.F/TWIST)
                           /max(abs(tension.T/EPS), 1.),
                       tension_energy_identity_error=abs(2*tension.energy/(tension.F*EPS*mesh.pitch)-1),
                       torsion_energy_identity_error=abs(2*torsion.energy/(torsion.T*TWIST*mesh.pitch)-1),
                       equilibrium=max(tension.equilibrium_residual, torsion.equilibrium_residual),
                       tension_iterations=tension.iterations, torsion_iterations=torsion.iterations,
                       assembly_seconds=body.assembly_seconds, factor_seconds=body.factor_seconds,
                       seconds3d=perf_counter()-started)
            if n == levels[-1]:
                OUTPUT.mkdir(parents=True, exist_ok=True)
                label = f'{profile_name}_{"solid" if solid else "wall"}'
                tension.save(OUTPUT/f'{label}_tension.npz')
                torsion.save(OUTPUT/f'{label}_torsion.npz')
                np.savez_compressed(OUTPUT/f'{label}_mesh.npz', nodes=mesh.nodes,
                                    hexes=mesh.hexes, wall=mesh.wall)
            del averager, body, tension, torsion, mesh
            gc.collect()
            started_ring = perf_counter()
            rm, rw = ring_mesh(profile, n, solid=solid)
            rb = Body(rm, [WALL, INFILL], [Dirichlet({0}, uz=0),
                       Dirichlet({1}, uz=EPS*5*profile.pitch)], groups=(~rw).astype(int))
            rr = rb.result()
            ravg = ring_average(rm, rw, rr, profile)
            force = -float(rr.reaction[rm.boundary_nodes({0}), 1].sum())
            row.update(K_axial_ring=force/EPS, FI_tension_ring=ravg['FI'],
                       F_fail_ring=force/np.sqrt(ravg['FI']), elements_ring=len(rm.tris),
                       max_FI_center_ring=ravg['center'], seconds_ring=perf_counter()-started_ring,
                       seconds_total=perf_counter()-started)
            row.update(peak_memory())
            row['stiffness_ratio_3d_ring'] = row['K_axial_3d']/row['K_axial_ring']
            row['failure_force_ratio_3d_ring'] = row['F_fail_3d']/row['F_fail_ring']
            previous = next((x for x in reversed(rows) if x['solid'] == solid), None)
            metrics = ('K_axial_3d', 'K_axial_ring', 'FI_tension_3d', 'FI_tension_ring',
                       'F_fail_3d', 'F_fail_ring', 'FI_torsion_3d', 'T_fail_3d')
            if previous:
                row['change_pct'] = {key: 100*(row[key]/previous[key]-1) for key in metrics}
            rows.append(row)
            print(json.dumps(row), flush=True)
            _write(f'comparison_{profile_name}', dict(rows=rows, complete=False))
            del rb, rr, rm
            gc.collect()
    last = [r for r in rows if r['n'] == levels[-1]]
    # Reporting criterion: <=2% stiffness and <=5% predicted failure loads.
    converged = len(levels) >= 3 and all(
        abs(r['change_pct'][key]) < tol for r in last for key, tol in
        [('K_axial_3d', 2), ('K_axial_ring', 2), ('F_fail_3d', 5),
         ('F_fail_ring', 5), ('T_fail_3d', 5)])
    equilibrium_pass = all(r['equilibrium'] < 1e-7
                           and r['reciprocity_error'] < 1e-7
                           and r['tension_energy_identity_error'] < 1e-7
                           and r['torsion_energy_identity_error'] < 1e-7 for r in rows)
    return dict(rows=rows, complete=True, convergence_criteria=dict(stiffness_pct=2, failure_load_pct=5),
                converged=bool(converged), equilibrium_pass=bool(equilibrium_pass))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=['all', 'analytic', 'comparison'], default='all')
    parser.add_argument('--profile', choices=['all', 'trap15', 'trap24', 'iso'], default='all')
    parser.add_argument('--levels', default='8,12,16')
    args = parser.parse_args()
    levels = [int(s) for s in args.levels.split(',')]
    if any(n < 4 or n % 2 for n in levels) or any(b <= a for a, b in zip(levels, levels[1:])):
        parser.error('levels must be increasing even integers >=4')
    started = perf_counter()
    report = dict(memory_limit=memory_limit(), eps0=EPS, twist_per_mm=TWIST,
                  wall_thickness_radial_mm=.83, averaging_radius_mm=.66,
                  stress_order=['xx', 'yy', 'zz', 'xy', 'yz', 'xz'],
                  wall_only_averaging=True, ring_periods=5,
                  averaging_centers='wall nodes and wall element centroids; sampled maximum',
                  torsion_boundary='infinitesimal rotation; linear small-strain elasticity')
    good = True
    if args.case in ('all', 'analytic'):
        report['analytic'] = analytic()
        good &= report['analytic']['pass_']
        _write('analytic', report['analytic'])
    if args.case in ('all', 'comparison'):
        names = ['trap15', 'trap24', 'iso'] if args.profile == 'all' else [args.profile]
        report['comparison'] = {}
        for name in names:
            data = comparison(name, levels)
            _write(f'comparison_{name}', data)
            report['comparison'][name] = data
            good &= data['equilibrium_pass']
            flat = []
            for row in data['rows']:
                flat.append({k: v for k, v in row.items() if not isinstance(v, (dict, list))})
            with (OUTPUT/f'comparison_{name}.csv').open('w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=list(flat[0]))
                writer.writeheader()
                writer.writerows(flat)
    report.update(seconds=perf_counter()-started, **peak_memory(), pass_=bool(good))
    _write(f'report_{args.case}_{args.profile}', report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('analytic', 'comparison')}), flush=True)
    return 0 if good else 1


if __name__ == '__main__':
    raise SystemExit(main())
