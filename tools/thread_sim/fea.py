"""Small-strain axisymmetric T6 elasticity and fixed-normal frictionless contact.

Use consistent length/force units (e.g. mm, N, MPa). Prescribed displacements
and surface tractions multiply the solve amplitude. Edge IDs refer to Mesh's
original polygon edges. Every body needs sufficient Dirichlet support for its
own free stiffness to be nonsingular; floating bodies are not supported.
"""

from dataclasses import dataclass
from time import perf_counter

import numpy as np
from scipy import sparse
from scipy.linalg import lstsq
from scipy.optimize import minimize
from scipy.sparse.linalg import splu

try:
    from .mesh import Mesh
except ImportError:
    from mesh import Mesh


@dataclass(frozen=True)
class Material:
    E_p: float
    E_z: float
    nu_p: float
    nu_zp: float  # minus transverse strain / axial strain in uniaxial z loading
    G_zp: float

    @classmethod
    def isotropic(cls, E, nu):
        return cls(E, E, nu, nu, E / (2 * (1 + nu)))

    def stiffness(self):
        """Engineering shear convention; stress order r,z,theta,rz."""
        if min(self.E_p, self.E_z, self.G_zp) <= 0:
            raise ValueError("Elastic moduli must be positive")
        s = np.diag([1 / self.E_p, 1 / self.E_z, 1 / self.E_p, 1 / self.G_zp])
        s[0, 2] = s[2, 0] = -self.nu_p / self.E_p
        s[0, 1] = s[1, 0] = s[1, 2] = s[2, 1] = -self.nu_zp / self.E_z
        if not np.isfinite(s).all() or np.linalg.eigvalsh(s).min() <= 0:
            raise ValueError("Material compliance must be positive definite")
        return np.linalg.inv(s)


@dataclass(frozen=True)
class Dirichlet:
    edges: object
    ur: float | None = None
    uz: float | None = None


@dataclass(frozen=True)
class SurfaceLoad:
    edges: object
    traction: object = None  # (tr,tz), or callable(points, outward_normals)
    pressure: float = 0.    # positive pressure acts into material


def shape(points):
    """T6 values and natural derivatives; nodes 0,1,2,01,12,20."""
    x, y = np.asarray(points, dtype=float).T
    l = np.column_stack((1 - x - y, x, y))
    dl = np.array([[-1., -1.], [1., 0.], [0., 1.]])
    n = np.column_stack((l * (2 * l - 1), 4 * l[:, 0] * l[:, 1],
                         4 * l[:, 1] * l[:, 2], 4 * l[:, 2] * l[:, 0]))
    d = np.empty((len(l), 6, 2))
    d[:, :3] = (4 * l - 1)[:, :, None] * dl
    for k, (a, b) in enumerate(((0, 1), (1, 2), (2, 0)), 3):
        d[:, k] = 4 * (l[:, a, None] * dl[b] + l[:, b, None] * dl[a])
    return n, d


# Seven-point Dunavant, area of reference triangle = 1/2.
_a, _b = .470142064105115, .101286507323456
GAUSS = np.array([[1/3, 1/3], [_a, _a], [1-2*_a, _a], [_a, 1-2*_a],
                  [_b, _b], [1-2*_b, _b], [_b, 1-2*_b]])
WEIGHTS = np.array([.225] + [.132394152788506] * 3 + [.125939180544827] * 3) / 2


def _kinematics(mesh, ids, natural=GAUSS):
    n, dn = shape(natural)
    p = mesh.nodes[mesh.tris[ids]]
    # Straight geometry: Jacobian is constant on each element.
    j = np.stack((p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=2)
    det = np.linalg.det(j)
    if np.any(det <= 0):
        raise ValueError("Inverted/degenerate triangle")
    grad = np.einsum('qni,eij->eqnj', dn, np.linalg.inv(j))
    pos = np.einsum('qn,eni->eqi', n, p)
    r = pos[:, :, 0]
    if np.any(r < -1e-13):
        raise ValueError("Negative integration-point radius")
    hoop = np.broadcast_to(n, (len(p), *n.shape)).copy()
    np.divide(hoop, r[:, :, None], out=hoop, where=r[:, :, None] > 1e-14)
    hoop = np.where(r[:, :, None] > 1e-14, hoop, grad[:, :, :, 0])
    b = np.zeros((len(p), len(n), 4, 12))
    b[:, :, 0, 0::2] = grad[:, :, :, 0]
    b[:, :, 1, 1::2] = grad[:, :, :, 1]
    b[:, :, 2, 0::2] = hoop
    b[:, :, 3, 0::2] = grad[:, :, :, 1]
    b[:, :, 3, 1::2] = grad[:, :, :, 0]
    return b, pos, det


def _dofs(tris):
    return (2 * tris[:, :, None] + np.arange(2)).reshape(len(tris), 12)


@dataclass
class BodyResult:
    displacement: np.ndarray
    reaction: np.ndarray  # support reactions only, nodal r,z force
    stress: np.ndarray  # element, gauss point, 4
    position: np.ndarray
    area: np.ndarray  # meridional integration weights
    volume: np.ndarray  # area * 2*pi*r

    @property
    def reaction_sum(self):
        return self.reaction.sum(axis=0)


class Body:
    def __init__(self, mesh: Mesh, material, conditions=(), loads=(), *, plane_strain=False, groups=None):
        """material は Material 1つか、その列。列のときは groups(要素ごとの添字)で要素の材料を選ぶ
        (印刷の壁の帯と疎な充填を分けるため)。"""
        self.mesh, self.material = mesh, material
        mats = list(material) if isinstance(material, (list, tuple)) else [material]
        Ds = np.stack([m.stiffness() for m in mats])
        g = np.zeros(len(mesh.tris), int) if groups is None else np.asarray(groups, int)
        if len(g) != len(mesh.tris) or g.min() < 0 or g.max() >= len(mats):
            raise ValueError("groups must index the material list, one per element")
        self.D = Ds[0]
        self.De = Ds[g]  # (要素, 4, 4)
        ndof = 2 * len(mesh.nodes)
        started = perf_counter()
        rows, cols, values = [], [], []
        for start in range(0, len(mesh.tris), 512):
            ids = np.arange(start, min(start + 512, len(mesh.tris)))
            b, pos, det = _kinematics(mesh, ids)
            w = det[:, None] * WEIGHTS * 2 * np.pi * pos[:, :, 0]
            ke = np.einsum('eqai,eab,eqbj,eq->eij', b, self.De[ids], b, w, optimize=True)
            dofs = _dofs(mesh.tris[ids])
            rows.append(np.broadcast_to(dofs[:, :, None], ke.shape).ravel().copy())
            cols.append(np.broadcast_to(dofs[:, None, :], ke.shape).ravel().copy())
            values.append(ke.ravel())
        self.K = sparse.coo_matrix((np.concatenate(values),
                                     (np.concatenate(rows), np.concatenate(cols))),
                                    shape=(ndof, ndof)).tocsc()
        self.assembly_seconds = perf_counter() - started
        del rows, cols, values
        fixed = {}
        for bc in conditions:
            nodes = mesh.boundary_nodes(bc.edges)
            if len(nodes) == 0:
                raise ValueError("Dirichlet edge selection is empty")
            for component, value in enumerate((bc.ur, bc.uz)):
                if value is None:
                    continue
                for i in 2 * nodes + component:
                    if i in fixed and not np.isclose(fixed[i], value, rtol=0, atol=1e-14):
                        raise ValueError("Conflicting prescribed displacements")
                    fixed[int(i)] = float(value)
        for i in 2 * np.flatnonzero(mesh.nodes[:, 0] == 0):
            if fixed.get(int(i), 0.) != 0:
                raise ValueError("Nonzero radial displacement on axis")
            fixed[int(i)] = 0.
        if plane_strain:
            for i in range(1, ndof, 2):
                if fixed.get(i, 0.) != 0:
                    raise ValueError("plane_strain requires zero axial displacement")
                fixed[i] = 0.
        if not any(i % 2 for i in fixed):
            raise ValueError("Each body needs an axial support before condensation")
        self.fixed = np.array(sorted(fixed), dtype=int)
        self.free = np.setdiff1d(np.arange(ndof), self.fixed)
        self.u_ref = np.zeros(ndof)
        self.u_ref[self.fixed] = [fixed[int(i)] for i in self.fixed]
        self.f_ref = self._surface_loads(loads)
        started = perf_counter()
        self.lu = splu(self.K[self.free][:, self.free])
        self.factor_seconds = perf_counter() - started
        rhs = self.f_ref - self.K @ self.u_ref
        self.u_ref[self.free] = self.lu.solve(rhs[self.free])

    def _surface_loads(self, loads):
        f = np.zeros(2 * len(self.mesh.nodes))
        xi, w = np.polynomial.legendre.leggauss(3)
        xi, w = (xi + 1) / 2, w / 2
        n = np.array([(1-xi)*(1-2*xi), 4*xi*(1-xi), xi*(2*xi-1)]).T
        for load in loads:
            edges = self.mesh.boundary_edges[np.isin(self.mesh.edge_src, list(load.edges))]
            if not len(edges):
                raise ValueError("Traction edge selection is empty")
            p = self.mesh.nodes[edges]
            tangent = p[:, 2] - p[:, 0]
            length = np.linalg.norm(tangent, axis=1)
            normal = np.column_stack((tangent[:, 1], -tangent[:, 0])) / length[:, None]
            pos = np.einsum('qn,eni->eqi', n, p)
            tr = np.zeros_like(pos) - load.pressure * normal[:, None, :]
            if load.traction is not None:
                val = load.traction(pos.reshape(-1, 2),
                                    np.repeat(normal, len(xi), axis=0)) if callable(load.traction) else load.traction
                tr += np.broadcast_to(np.asarray(val).reshape(-1, 2), (len(edges)*len(xi), 2)).reshape(tr.shape)
            fe = np.einsum('qn,eqi,eq->eni', n, tr,
                           length[:, None] * w * 2 * np.pi * pos[:, :, 0])
            for component in range(2):
                np.add.at(f, (2 * edges + component).ravel(), fe[:, :, component].ravel())
        return f

    def result(self, amplitude=1., contact_force=None):
        force = np.zeros_like(self.u_ref) if contact_force is None else np.asarray(contact_force)
        u = amplitude * self.u_ref.copy()
        u[self.free] += self.lu.solve(force[self.free])
        residual = self.K @ u - amplitude * self.f_ref - force
        reaction = np.zeros_like(u)
        reaction[self.fixed] = residual[self.fixed]
        stress, positions, areas = [], [], []
        for start in range(0, len(self.mesh.tris), 512):
            ids = np.arange(start, min(start + 512, len(self.mesh.tris)))
            b, pos, det = _kinematics(self.mesh, ids)
            strain = np.einsum('eqai,ei->eqa', b, u[_dofs(self.mesh.tris[ids])])
            stress.append(np.einsum('eab,eqb->eqa', self.De[ids], strain))
            positions.append(pos)
            areas.append(det[:, None] * WEIGHTS)
        pos, area = np.concatenate(positions), np.concatenate(areas)
        return BodyResult(u.reshape(-1, 2), reaction.reshape(-1, 2),
                          np.concatenate(stress), pos, area, area * 2 * np.pi * pos[:, :, 0])

    def stress_at(self, displacement, elements, natural):
        """Evaluate stresses at arbitrary natural points in selected elements."""
        ids = np.asarray(elements, dtype=int)
        b, pos, _ = _kinematics(self.mesh, ids, np.asarray(natural))
        strain = np.einsum('eqai,ei->eqa', b, np.asarray(displacement).ravel()[_dofs(self.mesh.tris[ids])])
        return np.einsum('eab,eqb->eqa', self.De[ids], strain), pos


@dataclass(frozen=True)
class ContactPair:
    a: int
    edges_a: object
    b: int
    edges_b: object


@dataclass
class ContactResult:
    bodies: list
    multipliers: np.ndarray  # ring forces, not pressure
    gaps: np.ndarray
    points: np.ndarray  # reference slave points
    normals: np.ndarray
    info: dict


def solve_lcp(A, q, warm_start=None, rtol=1e-9, maxiter=4000):
    """Convex bound QP with diagonal scaling and active-set polishing."""
    started = perf_counter()
    if len(q) == 0:
        return np.zeros(0), dict(seconds=perf_counter()-started, iterations=0,
                                  relative_residual=0., converged=True)
    diagonal = np.diag(A)
    if np.any(diagonal <= 0):
        raise ValueError("Contact constraints with zero compliance must be removed")
    scale = np.sqrt(diagonal)
    H = A / scale[:, None] / scale[None, :]
    qs = q / scale
    qscale = max(np.max(np.abs(qs)), 1e-30)
    qs /= qscale
    x0 = np.zeros(len(q)) if warm_start is None else np.maximum(warm_start * scale / qscale, 0)

    def objective(x):
        hx = H @ x
        return .5 * x @ hx + qs @ x, hx + qs

    opt = minimize(objective, x0, jac=True, bounds=[(0., None)] * len(q),
                   method='L-BFGS-B', options=dict(gtol=rtol*.1, ftol=1e-15,
                                                  maxiter=maxiter, maxls=50, maxcor=20))
    x = opt.x
    # Least-squares handles the semidefinite matrix from two-pass duplicates.
    # Add violated constraints and drop negative multipliers until KKT holds.
    active = x > max(np.max(x)*1e-9, 1e-13)
    polish = 0
    for polish in range(min(100, len(q) + 5)):
        ids = np.flatnonzero(active)
        trial = np.zeros(len(q))
        if len(ids):
            trial[ids] = lstsq(H[np.ix_(ids, ids)], -qs[ids], cond=1e-12,
                              lapack_driver='gelsy')[0]
        if np.any(trial[ids] < -rtol):
            active[ids[np.argmin(trial[ids])]] = False
            continue
        trial = np.maximum(trial, 0)
        grad = H @ trial + qs
        if np.min(grad) < -rtol:
            active[np.argmin(grad)] = True
            continue
        x = trial
        break
    grad = H @ x + qs
    projected = x - np.maximum(0, x - grad)
    relative = float(np.max(np.abs(projected)))
    lam = x * qscale / scale
    gaps = q + A @ lam
    info = dict(seconds=perf_counter()-started, iterations=int(opt.nit),
                polish_iterations=polish + 1, relative_residual=relative,
                min_gap=float(gaps.min()), min_lambda=float(lam.min()),
                max_complementarity=float(np.max(np.abs(lam * gaps))),
                converged=bool(relative < 10 * rtol), optimizer_message=str(opt.message))
    if not info['converged']:
        raise RuntimeError(f"LCP did not reach requested accuracy: {info}")
    return lam, info


class ContactSystem:
    """Two-pass nearest-facet constraints; A and the LU factors are reused.

    B maps each body's full displacement to signed normal gaps. g_slope is
    the gap change per unit amplitude; records identify slave/master nodes.
    memory_mb bounds dense contact work, not the separate sparse LU factors.
    """
    def __init__(self, bodies, pairs, search_distance, *, allow_penetration=False,
                 max_candidates=2400, memory_mb=768):
        self.bodies = list(bodies)
        if not np.isfinite(search_distance) or search_distance <= 0:
            raise ValueError("search_distance must be positive")
        rows = [[] for _ in bodies]
        cols = [[] for _ in bodies]
        vals = [[] for _ in bodies]
        gaps, points, normals, records = [], [], [], []
        for pair in pairs:
            for si, se, mi, me in ((pair.a, pair.edges_a, pair.b, pair.edges_b),
                                    (pair.b, pair.edges_b, pair.a, pair.edges_a)):
                if si == mi:
                    raise ValueError("Self-contact is not supported")
                sm, mm = bodies[si].mesh, bodies[mi].mesh
                sedges = sm.boundary_edges[np.isin(sm.edge_src, list(se))]
                medges = mm.boundary_edges[np.isin(mm.edge_src, list(me))]
                if not len(sedges) or not len(medges):
                    raise ValueError("Contact edge selection is empty")
                v = mm.nodes[medges[:, 2]] - mm.nodes[medges[:, 0]]
                vv = np.sum(v*v, axis=1)
                mn = np.column_stack((v[:, 1], -v[:, 0])) / np.sqrt(vv[:, None])
                sn = np.zeros_like(sm.nodes)
                sv = sm.nodes[sedges[:, 2]] - sm.nodes[sedges[:, 0]]
                sout = np.column_stack((sv[:, 1], -sv[:, 0])) / np.linalg.norm(sv, axis=1)[:, None]
                for j in range(3):
                    np.add.at(sn, sedges[:, j], sout)
                for node in np.unique(sedges):
                    p = sm.nodes[node]
                    raw = np.sum((p-mm.nodes[medges[:, 0]])*v, axis=1)/vv
                    t = np.clip(raw, 0, 1)
                    projections = mm.nodes[medges[:, 0]] + t[:, None] * v
                    distance2 = np.sum((p-projections)**2, axis=1)
                    # Reject back-facing facets before choosing nearest projection.
                    distance2[mn @ sn[node] >= -1e-10] = np.inf
                    # 射影が辺の外へはみ出して端点に丸められた組は、その辺の法線で測ると符号が逆になりうる
                    # (実測: ナットの上端より上のボルトの節点が穴の端の頂点へ射影され、重なり0なのに g=-0.4mm)。
                    # 両方向から見る接触なので、凸の角はもう一方の向きの制約が受け持つ
                    distance2[(raw < -1e-9) | (raw > 1 + 1e-9)] = np.inf
                    j = int(np.argmin(distance2))
                    if not np.isfinite(distance2[j]):
                        continue
                    normal, xi = mn[j], t[j]
                    g = float((p-projections[j]) @ normal)
                    tangential = np.linalg.norm(p-projections[j]-g*normal)
                    if g >= search_distance or tangential >= search_distance:
                        continue
                    row = len(gaps)
                    gaps.append(g)
                    points.append(p)
                    normals.append(normal)
                    records.append((si, int(node), mi, medges[j].copy(), float(xi)))
                    weights = [(1-xi)*(1-2*xi), 4*xi*(1-xi), xi*(2*xi-1)]
                    for c in range(2):
                        rows[si].append(row)
                        cols[si].append(2*node+c)
                        vals[si].append(normal[c])
                        for master, w in zip(medges[j], weights):
                            rows[mi].append(row)
                            cols[mi].append(2*int(master)+c)
                            vals[mi].append(-w*normal[c])
                    if len(gaps) > max_candidates:
                        raise MemoryError("Too many contact candidates; narrow the edge sets/search")
        self.g0 = np.asarray(gaps)
        self.points = np.asarray(points).reshape(-1, 2)
        self.normals = np.asarray(normals).reshape(-1, 2)
        self.records = records
        self.penetration_indices = np.flatnonzero(self.g0 < -1e-10)
        if len(self.penetration_indices) and not allow_penetration:
            raise ValueError(f"Initial penetration: {len(self.penetration_indices)} candidates, "
                             f"minimum gap {self.g0.min():.9g}")
        count = len(gaps)
        self.B = [sparse.coo_matrix((v, (r, c)), shape=(count, 2*len(b.mesh.nodes))).tocsr()
                  for b, r, c, v in zip(bodies, rows, cols, vals)]
        # Zero free compliance is a prescribed/prescribed inequality: check at solve.
        compliant = np.zeros(count, bool)
        for b, mat in zip(bodies, self.B):
            compliant |= np.asarray(abs(mat[:, b.free]).sum(axis=1)).ravel() > 1e-13
        self.compliant = np.flatnonzero(compliant)
        self.prescribed_only = np.flatnonzero(~compliant)
        n = len(self.compliant)
        if n*n*8*4 > memory_mb*1024**2:
            raise MemoryError("Dense contact reduction exceeds configured memory budget")
        self.A = np.zeros((n, n))
        self.g_slope = sum((m @ b.u_ref for b, m in zip(bodies, self.B)), np.zeros(count))
        started = perf_counter()
        for body, mat in zip(bodies, self.B):
            bf = mat[self.compliant][:, body.free]
            # Two temporary dense blocks, bounded independently of total node count.
            batch = max(1, min(48, int(24*1024**2 / max(16*len(body.free), 1))))
            for start in range(0, n, batch):
                stop = min(start+batch, n)
                rhs = bf[start:stop].T.toarray(order='F')
                compliance = body.lu.solve(rhs)
                self.A[:, start:stop] += bf @ compliance
        self.A = (self.A + self.A.T) / 2
        self.reduction_seconds = perf_counter() - started

    def free_gaps(self, amplitude):
        return self.g0 + amplitude * self.g_slope

    def solve(self, amplitude, warm_start=None, rtol=1e-9):
        """Reuse factorizations and contact flexibility for a new amplitude."""
        q = self.free_gaps(amplitude)
        if len(self.prescribed_only) and np.min(q[self.prescribed_only]) < -1e-12:
            raise ValueError("Incompatible prescribed displacements at contact")
        warm = None if warm_start is None else np.asarray(warm_start)[self.compliant]
        reduced, info = solve_lcp(self.A, q[self.compliant], warm, rtol)
        lam = np.zeros(len(q))
        lam[self.compliant] = reduced
        results = [b.result(amplitude, mat.T @ lam) for b, mat in zip(self.bodies, self.B)]
        gaps = self.g0 + sum((m @ r.displacement.ravel() for m, r in zip(self.B, results)),
                              np.zeros(len(q)))
        info['initial_penetration_count'] = len(self.penetration_indices)
        return ContactResult(results, lam, gaps, self.points, self.normals, info)
