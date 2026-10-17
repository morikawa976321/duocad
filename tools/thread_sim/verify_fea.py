"""Run: .venv/Scripts/python.exe -B -m tools.thread_sim.verify_fea [--case all].

Only NumPy/SciPy are required. JSON/CSV reports go to output/thread_sim/fea_verify.
Benchmarks and meshes run sequentially; no worker processes are launched.
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
import traceback

# Bound BLAS threading without modifying the persistent environment.
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
import numpy as np
from scipy import sparse
from scipy.sparse.linalg import spsolve

try:
    from .mesh import mesh_polygon
    from .fea import Body, ContactPair, ContactSystem, Dirichlet, Material, SurfaceLoad
except ImportError:
    from mesh import mesh_polygon
    from fea import Body, ContactPair, ContactSystem, Dirichlet, Material, SurfaceLoad


OUTPUT = Path(__file__).resolve().parents[2] / 'output/thread_sim/fea_verify'
E, NU = 2000., .3
ISO = Material.isotropic(E, NU)
QUALITY = []
SOURCE = 'https://doi.org/10.1016/j.ijfatigue.2005.04.015'
CORRECTION = 'https://www.researchgate.net/publication/379346942_Code_of_Formula_for_Corrected_IJF2006Vol28pp151-163_dated_20240328'


def mesh(poly, h, label, size_fn=None, h_max=None, holes=()):
    m = mesh_polygon(poly, size_fn or (lambda p: np.full(len(p), h)), h, h_max or h, holes)
    a = m.min_angles()
    QUALITY.append(dict(mesh=label, nodes=len(m.nodes), elements=len(m.tris),
                        min_deg=float(a.min()), p05_deg=float(np.percentile(a, 5)),
                        median_deg=float(np.median(a)), below15=int(np.sum(a < 15)),
                        below30=int(np.sum(a < 30))))
    return m


def force_on(body, result, edges):
    return float(result.reaction[body.mesh.boundary_nodes(edges), 1].sum())


def cylinder():
    m = mesh([[0, 0], [1, 0], [1, 2], [0, 2]], .18, 'cylinder')
    b = Body(m, ISO, [Dirichlet({0}, uz=0), Dirichlet({2}, uz=.002)])
    r = b.result()
    err = float(np.max(np.abs(r.stress - [0, 2, 0, 0])) / 2)
    f = force_on(b, r, {2})
    return dict(stress_relative_error=err, axial_force=f, exact_force=2*np.pi,
                pass_=err < 1e-6)


def lame():
    rows = []
    for h in (.25, .125, .0625):
        m = mesh([[1, 0], [2, 0], [2, 1], [1, 1]], h, f'lame_{h}')
        b = Body(m, ISO, loads=[SurfaceLoad({3}, pressure=1)], plane_strain=True)
        r = b.result()
        radius = r.position[:, :, 0]
        sr, st = (1-4/radius**2)/3, (1+4/radius**2)/3
        # Componentwise absolute errors normalized by each exact component peak.
        er = float(np.max(np.abs(r.stress[:, :, 0]-sr)))
        et = float(np.max(np.abs(r.stress[:, :, 2]-st)) / (5/3))
        l2 = float(np.sqrt(np.sum(((r.stress[:, :, 0]-sr)**2
                                  + (r.stress[:, :, 2]-st)**2)*r.volume)
                           / np.sum((sr**2+st**2)*r.volume)))
        rows.append(dict(h=h, nodes=len(m.nodes), radial_relative_error=er,
                          hoop_relative_error=et, relative_l2=l2))
    return dict(levels=rows, pass_=max(rows[-1]['radial_relative_error'],
                                      rows[-1]['hoop_relative_error']) < .005
                and all(rows[i+1]['relative_l2'] < rows[i]['relative_l2'] for i in range(2)))


def noda_takase(R, t, rho):
    """Tension, nu=.3; 2006 Eq.12 with authors' 2024 corrected coefficients.

    Equations describe circular-arc/V grooves with 60-degree opening. Here
    t/rho=1 circular semicircular U grooves are used, where the paper finds
    negligible opening-angle dependence. Nominal stress uses minimum section.
    """
    a = R-t
    xi, eta = np.sqrt(t/rho), np.sqrt(rho/t)
    kts = (1+2*xi) * (1-.127*xi+.2908*xi**2-.1420*xi**3 if xi <= 1
                      else 1.148-.160*eta-.0345*eta**2+.0693*eta**3)
    ap = a/rho
    x = ap if ap <= 1 else 2-1/ap
    s = np.sqrt(ap+1)
    kth = (ap*s + .8*ap + 1.3*(s+1)) / (ap+.6*s+2)
    ktd = kth * (1.0011-.025485*x+.015727*x**2+.006131*x**3)
    kn = 1 + ((kts-1)*(ktd-1))/((kts-1)**2.8+(ktd-1)**2.8)**(1/2.8)
    coeff = np.array([[1.0001, .0036, -.0065, .0021],
                      [.0116, 1.404, -1.285, .1799],
                      [-.1311, -8.165, 9.687, -2.124],
                      [.4240, 16.94, -22.77, 5.618],
                      [-.5156, -15.07, 21.71, -5.571],
                      [.2112, 4.890, -7.332, 1.896]])
    return float(kn * (np.array([(t/R)**i for i in range(6)]) @ coeff @ x**np.arange(4)))


def notch():
    rows = []
    for rho in (.5, 1., 2.):
        R, L = 5., 20.
        angle = np.linspace(-np.pi/2, np.pi/2, 97)
        arc = np.column_stack((R-rho*np.cos(angle), rho*np.sin(angle)))
        poly = np.vstack(([0, -L], [R, -L], arc, [R, L], [0, L]))
        def size(p):
            return .018 + .22*np.linalg.norm(p-[R-rho, 0], axis=1)
        m = mesh(poly, .018, f'notch_{rho}', size, .65)
        b = Body(m, ISO, [Dirichlet({0}, uz=0), Dirichlet({len(poly)-2}, uz=.02)])
        r = b.result()
        root = np.argmin(np.linalg.norm(m.nodes-[R-rho, 0], axis=1))
        ids = np.flatnonzero(np.any(m.tris[:, :3] == root, axis=1))
        stress, _ = b.stress_at(r.displacement, ids, [[0, 0], [1, 0], [0, 1]])
        corner = np.argmax(m.tris[ids, :3] == root, axis=1)
        peak = float(np.mean(stress[np.arange(len(ids)), corner, 1]))
        f = force_on(b, r, {len(poly)-2})
        kt = peak / (f / (np.pi*(R-rho)**2))
        ref = noda_takase(R, rho, rho)
        rows.append(dict(radius=R, depth=rho, root_radius=rho, nodes=len(m.nodes),
                          Kt=kt, reference=ref, relative_error=abs(kt/ref-1)))
    return dict(shapes=rows, source=SOURCE, corrected_formula=CORRECTION,
                reference_scope='nu=0.3, t/rho=1; opening-angle approximation',
                peak_method='mean of T6 element-corner sigma_z at notch root',
                pass_=all(r['relative_error'] < .03 for r in rows))


def boundary_pressure(body, contact_forces, sources):
    """Consistent surface mass projection of nodal ring forces to pressure."""
    m = body.mesh
    edges = m.boundary_edges[np.isin(m.edge_src, list(sources))]
    nodes = np.unique(edges)
    lookup = np.full(len(m.nodes), -1, dtype=int)
    lookup[nodes] = np.arange(len(nodes))
    xi, w = np.polynomial.legendre.leggauss(4)
    xi, w = (xi+1)/2, w/2
    n = np.array([(1-xi)*(1-2*xi), 4*xi*(1-xi), xi*(2*xi-1)]).T
    p = m.nodes[edges]
    pos = np.einsum('qn,eni->eqi', n, p)
    length = np.linalg.norm(p[:, 2]-p[:, 0], axis=1)
    mass = np.einsum('qi,qj,eq->eij', n, n, length[:, None]*w*2*np.pi*pos[:, :, 0])
    ids = lookup[edges]
    M = sparse.coo_matrix((mass.ravel(), (np.broadcast_to(ids[:, :, None], mass.shape).ravel(),
                                         np.broadcast_to(ids[:, None, :], mass.shape).ravel())),
                          shape=(len(nodes), len(nodes))).tocsc()
    pressure = spsolve(M, np.asarray(contact_forces).reshape(-1, 2)[nodes, 1])
    return m.nodes[nodes, 0], pressure


def patch():
    lower = mesh([[0, -1], [1, -1], [1, 0], [0, 0]], .13, 'patch_lower')
    upper = mesh([[0, .01], [1, .01], [1, 1.01], [0, 1.01]], .18, 'patch_upper')
    a = Body(lower, ISO, [Dirichlet({0}, uz=0)])
    b = Body(upper, ISO, [Dirichlet({2}, uz=-1)])
    system = ContactSystem([a, b], [ContactPair(0, {2}, 1, {0})], .03)
    result = system.solve(.012)
    F = -force_on(b, result.bodies[1], {2})
    exact = E*np.pi*.002/2
    force = system.B[0].T @ result.multipliers
    _, pressure = boundary_pressure(a, force, {2})
    variation = float(np.max(np.abs(pressure+2))/2)
    reopened = system.solve(0.)
    # A single bar of the combined material height, at the post-closure shortening.
    single_m = mesh([[0, 0], [1, 0], [1, 2], [0, 2]], .18, 'patch_single')
    single = Body(single_m, ISO, [Dirichlet({0}, uz=0), Dirichlet({2}, uz=-.002)])
    sf = -force_on(single, single.result(), {2})
    return dict(force=F, exact_force=exact, stiffness_relative_error=abs(F/sf-1),
                pressure_relative_max_error=variation,
                min_gap=float(result.gaps.min()), candidates=len(system.g0),
                reopen_max_force=float(np.max(reopened.multipliers)), lcp=result.info,
                pass_=abs(F/exact-1)<1e-6 and variation<1e-5
                and np.max(reopened.multipliers)==0)


def hertz():
    rows = []
    for h in (.02, .01, .005):
        R, extent = 10., 4.
        # Many original vertices also exercise retention on a curved boundary.
        rs = np.concatenate((np.linspace(0, 1., round(1/h)+1), np.linspace(1., extent, 51)[1:]))
        arc = np.column_stack((rs, R-np.sqrt(R*R-rs*rs)))
        poly = np.vstack((arc, [extent, 4.], [0, 4.]))
        def size(p):
            return h+.22*np.maximum(np.linalg.norm(p, axis=1)-.32, 0)
        flat_poly = np.vstack(([0, -4], [extent, -4], np.column_stack((rs[::-1], np.zeros(len(rs))))))
        bottom = mesh(flat_poly, h,
                      f'hertz_flat_{h}', size, .5)
        top = mesh(poly, h, f'hertz_cap_{h}', size, .5)
        a = Body(bottom, ISO, [Dirichlet({0}, uz=0)])
        b = Body(top, ISO, [Dirichlet({len(poly)-2}, uz=-1)])
        flat_edges = set(range(2, len(flat_poly)-1))
        system = ContactSystem([a, b], [ContactPair(0, flat_edges, 1, set(range(len(arc)-1)))], .04)
        result = system.solve(.006)
        F = -force_on(b, result.bodies[1], {len(poly)-2})
        effective = E/(2*(1-NU**2))
        exact_a = (3*F*R/(4*effective))**(1/3)
        exact_p = 3*F/(2*np.pi*exact_a**2)
        active = result.multipliers > result.multipliers.max()*1e-5
        radius = float(result.points[active, 0].max())
        _, p = boundary_pressure(a, system.B[0].T @ result.multipliers, flat_edges)
        peak = float(np.max(-p))
        rows.append(dict(h_min=h, nodes=len(top.nodes)+len(bottom.nodes), force=F,
                          contact_radius=radius, hertz_radius=exact_a,
                          radius_relative_error=abs(radius/exact_a-1),
                          p0=peak, hertz_p0=exact_p, p0_relative_error=abs(peak/exact_p-1),
                          candidates=len(system.g0), lcp=result.info))
    return dict(levels=rows, radius_measurement='outermost active slave radius (no fitted radius)',
                pressure_measurement='maximum consistent boundary-mass-projected pressure',
                reference='Hertz: a=(3FR/(4E*))^(1/3), p0=3F/(2*pi*a^2)',
                criteria='radius <5% interprets requested few percent; pressure <8% is an additional diagnostic tolerance',
                pass_=rows[-1]['radius_relative_error'] < .05
                and rows[-1]['p0_relative_error'] < .08)


def transverse():
    rows = []
    m = mesh([[0, 0], [1, 0], [1, 2], [0, 2]], .2, 'transverse')
    for ez in (1000., 2000., 4000.):
        mat = Material(2000., ez, .3, .22, 650.)
        b = Body(m, mat, [Dirichlet({0}, uz=0)], [SurfaceLoad({2}, traction=(0, 1))])
        r = b.result()
        uz = r.displacement[m.boundary_nodes({2}), 1].mean()
        strain = float(uz/2)
        err = abs(strain*ez-1)
        rows.append(dict(E_z=ez, strain=strain, exact_strain=1/ez, relative_error=err))
    return dict(levels=rows, pass_=all(r['relative_error']<1e-8 for r in rows))


def peak_memory_mb():
    if sys.platform == 'win32':
        class Counters(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                        ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
        c = Counters()
        c.cb = ctypes.sizeof(c)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        get_info = ctypes.WinDLL('psapi').GetProcessMemoryInfo
        get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
        get_info.restype = ctypes.c_int
        ok = get_info(kernel.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return float(c.PeakWorkingSetSize/1024**2) if ok else None
    return None


def benchmark():
    # 2*~15000 T6 nodes, one contact interface, sequential assembly and LU.
    started = perf_counter()
    m1 = mesh([[0, -5], [5, -5], [5, 0], [0, 0]], .085, 'benchmark_lower')
    m2 = mesh([[0, 0], [5, 0], [5, 5], [0, 5]], .085, 'benchmark_upper')
    mesh_seconds = perf_counter()-started
    a = Body(m1, ISO, [Dirichlet({0}, uz=0)])
    b = Body(m2, ISO, [Dirichlet({2}, uz=-1)])
    system = ContactSystem([a, b], [ContactPair(0, {2}, 1, {0})], .02)
    result = system.solve(.001)
    F = -force_on(b, result.bodies[1], {2})
    exact = E*np.pi*25*.001/10
    peak = peak_memory_mb()
    return dict(nodes=len(m1.nodes)+len(m2.nodes), elements=len(m1.tris)+len(m2.tris),
                candidates=len(system.g0), meshing_seconds=mesh_seconds,
                assembly_seconds=a.assembly_seconds+b.assembly_seconds,
                factor_seconds=a.factor_seconds+b.factor_seconds,
                reduction_seconds=system.reduction_seconds, lcp_seconds=result.info['seconds'],
                total_seconds=perf_counter()-started, peak_working_set_mb=peak,
                force_relative_error=abs(F/exact-1), lcp=result.info,
                pass_=abs(F/exact-1)<1e-6 and (peak is None or peak < 2048))


def geometry():
    rows = []
    # 0.2-mm square steps, >1000 vertices, no rounding, clockwise input.
    side = [[4., 0.]]
    for j in range(500):
        z = (j+1)*.2
        r = 4.+(.2 if j % 2 == 0 else 0.)
        side.extend([[side[-1][0], z], [r, z]])
    poly = np.array([[0, 0]]+side+[[0, 100.]])[::-1]
    m = mesh(poly, .12, '1003_vertex_steps', lambda p: .12+.18*(4.2-p[:, 0]), .65)
    # Original vertex retention, boundary length per original edge, and area.
    from scipy.spatial import cKDTree
    retained = float(cKDTree(m.nodes).query(poly)[0].max())
    length = np.linalg.norm(m.nodes[m.boundary_edges[:, 2]]-m.nodes[m.boundary_edges[:, 0]], axis=1)
    recovered = np.bincount(m.edge_src, weights=length, minlength=len(poly))
    expected = np.linalg.norm(np.roll(poly, -1, axis=0)-poly, axis=1)
    length_error = float(np.max(np.abs(recovered-expected)))
    p = m.nodes[m.tris[:, :3]]
    area = .5*np.sum(np.abs((p[:, 1, 0]-p[:, 0, 0])*(p[:, 2, 1]-p[:, 0, 1])
                            -(p[:, 1, 1]-p[:, 0, 1])*(p[:, 2, 0]-p[:, 0, 0])))
    rows.append(dict(name='steps', vertices=len(poly), nodes=len(m.nodes), retained_error=retained,
                      edge_length_error=length_error, area=float(area)))
    del m
    hole = [[1, 1], [2, 1], [2, 2], [1, 2]]
    m = mesh([[.2, 0], [3, 0], [3, 3], [.2, 3]], .22, 'hole', holes=(hole,))
    p = m.nodes[m.tris[:, :3]]
    hole_area = .5*np.sum((p[:, 1, 0]-p[:, 0, 0])*(p[:, 2, 1]-p[:, 0, 1])
                          -(p[:, 1, 1]-p[:, 0, 1])*(p[:, 2, 0]-p[:, 0, 0]))
    rows.append(dict(name='hole', area=float(hole_area), exact_area=7.4,
                      hole_source_ids=sorted(set(m.edge_src[m.edge_src>=4].tolist()))))
    # Penetration rejection and immutable normal/gap for a pair of sloping faces.
    lower = mesh([[0, -1], [1, -1], [1, .2], [0, 0]], .2, 'inclined_lower')
    upper = mesh([[0, -.001], [1, .199], [1, 1], [0, 1]], .2, 'inclined_upper')
    a = Body(lower, ISO, [Dirichlet({0}, uz=0)])
    penetration = []
    for shift in (0., .2):
        shifted = replace(upper, nodes=upper.nodes-[0, shift])
        b = Body(shifted, ISO, [Dirichlet({2}, uz=0)])
        detected, message = False, ''
        try:
            ContactSystem([a, b], [ContactPair(0, {2}, 1, {0})], .1)
        except ValueError as ex:
            message = str(ex)
            detected = 'Initial penetration' in message
        penetration.append(dict(axial_overlap=.001+shift, search_distance=.1,
                                 detected=detected, message=message))
    return dict(meshes=rows, penetration=penetration,
                pass_=retained<1e-12 and length_error<1e-10
                and abs(hole_area-7.4)<1e-10 and all(p['detected'] for p in penetration))


CASES = dict(cylinder=cylinder, lame=lame, notch=notch, hertz=hertz, patch=patch,
             transverse=transverse, geometry=geometry, benchmark=benchmark)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=['all']+list(CASES), default='all')
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = dict(python=sys.version, numpy=np.__version__, cases={})
    started = perf_counter()
    for name, fn in CASES.items():
        if args.case not in ('all', name):
            continue
        tick = perf_counter()
        try:
            result = fn()
        except Exception as exc:
            result = dict(pass_=False, error=str(exc), traceback=traceback.format_exc())
        result['elapsed_seconds'] = perf_counter()-tick
        report['cases'][name] = result
        result = json.loads(json.dumps(result, default=lambda v: v.item(), ensure_ascii=False))
        report['cases'][name] = result
        print(name, json.dumps(result, ensure_ascii=False), flush=True)
        gc.collect()
    report['quality'] = QUALITY
    report['quality_passed'] = all(q['min_deg']>15 and q['median_deg']>30 for q in QUALITY)
    report['total_seconds'] = perf_counter()-started
    report['peak_working_set_mb'] = peak_memory_mb()
    report['passed'] = all(c['pass_'] for c in report['cases'].values()) and report['quality_passed']
    (OUTPUT / f'{args.case}.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    if QUALITY:
        with (OUTPUT / f'{args.case}_quality.csv').open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=list(QUALITY[0]))
            writer.writeheader()
            writer.writerows(QUALITY)
    print('report:', OUTPUT / f'{args.case}.json', 'passed:', report['passed'],
          'seconds:', report['total_seconds'], 'peak MB:', report['peak_working_set_mb'], flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
