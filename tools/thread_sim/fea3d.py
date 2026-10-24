"""Periodic, small-strain 3-D thread elasticity (mm, N, MPa).

H8 with nine statically condensed incompatible displacement-gradient modes,
2x2x2 integration. The enhanced gradients use the centre Jacobian and the
det(J0)/det(J) correction: their volume integral vanishes even in distorted
elements, preserving the constant-stress patch test. They reproduce quadratic
bending on rectangular elements without the parasitic H8 shear energy.

Stress/engineering-strain order: xx, yy, zz, xy, yz, xz. Wall thickness is
RADIAL, not normal to the helical surface. Each element has one material.
Only numpy/scipy and the standard library are needed; no workers are started.
"""

from dataclasses import dataclass
from itertools import product
from time import perf_counter

import numpy as np
from scipy import sparse
from scipy.sparse.linalg import LinearOperator, cg, splu
from scipy.spatial import cKDTree


SIGNS = np.array([[-1, -1, -1], [1, -1, -1], [1, 1, -1],
                  [-1, 1, -1], [-1, -1, 1], [1, -1, 1],
                  [1, 1, 1], [-1, 1, 1]], float)
GAUSS = np.array(list(product(*([[-1 / np.sqrt(3), 1 / np.sqrt(3)]] * 3))))


def stiffness(material):
    """Same five transverse-isotropic constants and nu_zp convention as fea."""
    material.stiffness()  # includes positivity/reciprocity validation
    m = material
    s = np.diag([1/m.E_p, 1/m.E_p, 1/m.E_z,
                 2*(1+m.nu_p)/m.E_p, 1/m.G_zp, 1/m.G_zp])
    s[0, 1] = s[1, 0] = -m.nu_p/m.E_p
    s[0, 2] = s[2, 0] = s[1, 2] = s[2, 1] = -m.nu_zp/m.E_z
    return np.linalg.inv(s)


def profile_function(profile, pitch=None):
    """Accept Profile, callable plus pitch, or an (N,2) periodic (s,r) polyline."""
    if callable(profile):
        pitch = getattr(profile, 'pitch', pitch)
        f = profile
    else:
        p = np.asarray(profile, float)
        if (p.ndim != 2 or p.shape[1] != 2 or len(p) < 2
                or not np.isfinite(p).all() or p[0, 0] != 0
                or np.any(np.diff(p[:, 0]) <= 0)
                or not np.isclose(p[0, 1], p[-1, 1])):
            raise ValueError('Require closed, finite, increasing (s,r) polyline')
        pitch = p[-1, 0] if pitch is None else pitch
        if not np.isclose(p[-1, 0], pitch):
            raise ValueError('Polyline endpoint must equal pitch')
        f = lambda s: np.interp(np.mod(s, pitch), p[:, 0], p[:, 1])
    if pitch is None or not np.isfinite(pitch) or pitch <= 0:
        raise ValueError('A finite positive pitch is required')
    return lambda s: np.asarray(f(np.mod(s, pitch)), float), float(pitch)


@dataclass
class Mesh3D:
    nodes: np.ndarray
    hexes: np.ndarray
    wall: np.ndarray
    bottom: np.ndarray
    top: np.ndarray
    pitch: float
    n: int
    nz: int


def make_mesh(profile, n=12, nz=24, *, pitch=None, t_wall=.83, solid=False):
    """Noncollapsed concentric square-to-disk map, uniform z steps.

    Iso-squares map to circles (four regular sectors, no polar-axis collapsed
    elements). Piecewise radial grading fits r=f(s)-t_wall to an iso-square
    grid layer. Thus material interfaces do not staircase through elements.
    Surface nodes lie on the exact profile; H8 faces approximate the surface.
    """
    if n < 4 or n % 2 or nz < 2 or not np.isfinite(t_wall) or t_wall <= 0:
        raise ValueError('Require even n>=4, nz>=2 and positive wall thickness')
    if n*n*nz > 30000 or 3*(n+1)**2*nz > 100000:
        raise MemoryError('Mesh exceeds conservative single-process budget')
    f, pitch = profile_function(profile, pitch)
    uv = np.linspace(-1, 1, n+1)
    u, v = np.meshgrid(uv, uv, indexing='ij')
    q = np.maximum(abs(u), abs(v))
    # Signed radius form of the concentric square/disk mapping.
    a = np.abs(u) > np.abs(v)
    angle = np.zeros_like(u)
    np.divide(v, u, out=angle, where=a)
    angle *= np.pi/4
    other = np.zeros_like(v)
    np.divide(u, v, out=other, where=(~a) & (v != 0))
    angle = np.where(a, angle, np.pi/2 - np.pi/4*other)
    signed_r = np.where(a, u, v)
    theta = np.arctan2(signed_r*np.sin(angle), signed_r*np.cos(angle))
    sample = np.broadcast_to(f(np.linspace(0, pitch, 1024, endpoint=False)), (1024,))
    if not np.isfinite(sample).all() or sample.min() <= 0:
        raise ValueError('Profile radii must be finite and positive')
    all_wall = solid or t_wall >= sample.max()
    if not all_wall and t_wall >= sample.min():
        raise ValueError('Wall must be thinner than minimum profile radius, or solid=True')
    layers = max(1, min(n//2-1, round(n/2 * t_wall/sample.mean())))
    interface = 1 - 2*layers/n
    nodes = []
    for z in np.linspace(0, pitch, nz+1):
        R = np.broadcast_to(f(z-pitch*theta/(2*np.pi)), q.shape)
        if not np.isfinite(R).all() or R.min() <= 0:
            raise ValueError('Invalid profile at a mesh node')
        radius = q*R if all_wall else np.where(
            q <= interface, q/interface*(R-t_wall),
            R-t_wall+(q-interface)/(1-interface)*t_wall)
        nodes.append(np.stack((radius*np.cos(theta), radius*np.sin(theta),
                               np.full_like(q, z)), axis=-1).reshape(-1, 3))
    nodes = np.concatenate(nodes)
    plane = (n+1)**2
    # Identical end geometry, including floating point phase wrapping.
    nodes[-plane:, :2] = nodes[:plane, :2]
    k, i, j = np.meshgrid(np.arange(nz), np.arange(n), np.arange(n), indexing='ij')
    b = (k*plane+i*(n+1)+j).ravel()
    offsets = np.array([0, n+1, n+2, 1, plane, plane+n+1, plane+n+2, plane+1])
    hexes = (b[:, None]+offsets).astype(np.int32)
    qc = np.maximum(abs((uv[:-1]+uv[1:])/2)[:, None],
                    abs((uv[:-1]+uv[1:])/2)[None, :])
    wall = np.ones(len(hexes), bool) if all_wall else np.tile((qc > interface).ravel(), nz)
    return Mesh3D(nodes, hexes, wall, np.arange(plane),
                  np.arange(nz*plane, (nz+1)*plane), pitch, n, nz)


def shape(points=GAUSS):
    factors = 1+np.asarray(points)[:, None, :]*SIGNS[None]
    N = factors.prod(axis=-1)/8
    dN = np.empty((len(points), 8, 3))
    for a in range(3):
        dN[:, :, a] = SIGNS[:, a]*np.prod(np.delete(factors, a, axis=2), axis=2)/8
    return N, dN


def strain_matrix(gradient):
    b = np.zeros((*gradient.shape[:-2], 6, 3*gradient.shape[-2]))
    b[..., 0, 0::3] = gradient[..., 0]
    b[..., 1, 1::3] = gradient[..., 1]
    b[..., 2, 2::3] = gradient[..., 2]
    b[..., 3, 0::3], b[..., 3, 1::3] = gradient[..., 1], gradient[..., 0]
    b[..., 4, 1::3], b[..., 4, 2::3] = gradient[..., 2], gradient[..., 1]
    b[..., 5, 0::3], b[..., 5, 2::3] = gradient[..., 2], gradient[..., 0]
    return b


def kinematics(coordinates):
    N, dN = shape()
    J = np.einsum('eni,qna->eqia', coordinates, dN)
    det = np.linalg.det(J)
    if not np.isfinite(det).all() or np.any(det <= 0):
        raise ValueError('Inverted/degenerate hexahedron; refine mesh')
    grad = np.einsum('qna,eqai->eqni', dN, np.linalg.inv(J))
    _, d0 = shape(np.zeros((1, 3)))
    J0 = np.einsum('eni,na->eia', coordinates, d0[0])
    # Three scalar incompatible modes, each with three displacement components.
    enhanced = (-2*GAUSS[None, :, :, None]*np.linalg.inv(J0)[:, None])
    enhanced *= (np.linalg.det(J0)[:, None]/det)[:, :, None, None]
    return (strain_matrix(grad), strain_matrix(enhanced),
            np.einsum('qn,eni->eqi', N, coordinates), det)


def element_matrices(coordinates, D):
    b, h, pos, w = kinematics(coordinates)
    kuu = np.einsum('eqai,eab,eqbj,eq->eij', b, D, b, w, optimize=True)
    kua = np.einsum('eqai,eab,eqbj,eq->eij', b, D, h, w, optimize=True)
    kaa = np.einsum('eqai,eab,eqbj,eq->eij', h, D, h, w, optimize=True)
    recovery = -np.linalg.solve(kaa, kua.transpose(0, 2, 1))
    ke = kuu + kua@recovery
    return (ke+ke.transpose(0, 2, 1))/2, recovery


@dataclass
class Result3D:
    displacement: np.ndarray
    reaction: np.ndarray  # K*u, including equal/opposite periodic-face reactions
    stress: np.ndarray  # element, Gauss point, xx yy zz xy yz xz
    position: np.ndarray
    volume: np.ndarray
    F: float  # positive tension: minus bottom-face axial reaction
    T: float  # positive twist: minus bottom-face moment
    energy: float
    equilibrium_residual: float
    seconds: float
    iterations: int

    def save(self, path):
        np.savez_compressed(path, displacement=self.displacement, reaction=self.reaction,
                            stress=self.stress, position=self.position, volume=self.volume,
                            F=self.F, T=self.T, energy=self.energy)


class PeriodicBody:
    """One pitch, prescribed axial strain and infinitesimal twist/length.

    u(top)-u(bottom) = eps*P*ez + twist*P*(-y,x,0). Four gauge constraints
    remove three translations and rotation about z; no physical end clamps.
    Component-block preconditioner is shared by tension/torsion. The coupled
    SPD system is solved by CG rather than memory-intensive vector sparse LU.
    Above 40000 reduced DOFs, an additive two-level Jacobi/Galerkin coarse
    correction bounds factorization size. Both preconditioners are SPD.
    """
    def __init__(self, mesh, wall_material, infill_material=None):
        started = perf_counter()
        self.mesh = mesh
        mats = [wall_material, wall_material if infill_material is None else infill_material]
        self.De = np.stack([stiffness(m) for m in mats])[(~mesh.wall).astype(int)]
        ndof = 3*len(mesh.nodes)
        ne = len(mesh.hexes)
        rows = np.empty(ne*576, np.int32)
        cols = np.empty_like(rows)
        vals = np.empty(ne*576)
        self.recovery = np.empty((ne, 9, 24))
        for start in range(0, ne, 64):
            stop = min(start+64, ne)
            elements = mesh.hexes[start:stop]
            ke, rec = element_matrices(mesh.nodes[elements], self.De[start:stop])
            self.recovery[start:stop] = rec
            dofs = (3*elements[:, :, None]+np.arange(3)).reshape(-1, 24)
            sl = slice(start*576, stop*576)
            rows[sl] = np.broadcast_to(dofs[:, :, None], ke.shape).ravel()
            cols[sl] = np.broadcast_to(dofs[:, None, :], ke.shape).ravel()
            vals[sl] = ke.ravel()
        self.K = sparse.coo_matrix((vals, (rows, cols)), shape=(ndof, ndof)).tocsr()
        del rows, cols, vals
        plane = len(mesh.bottom)
        master = np.arange(len(mesh.nodes), dtype=np.int32)
        master[mesh.top] = mesh.bottom
        mdof = (3*master[:, None]+np.arange(3)).ravel()
        nr = 3*(len(mesh.nodes)-plane)
        self.P = sparse.csr_matrix((np.ones(ndof), (np.arange(ndof), mdof)), shape=(ndof, nr))
        center = (mesh.n//2)*(mesh.n+1)+mesh.n//2
        anchor = mesh.n*(mesh.n+1)+mesh.n//2
        fixed = np.array([3*center, 3*center+1, 3*center+2, 3*anchor+1])
        self.free = np.setdiff1d(np.arange(nr), fixed)
        self.Q = self.P[:, self.free]
        reduced = (self.Q.T@self.K@self.Q).tocsc()
        self.assembly_seconds = perf_counter()-started
        started = perf_counter()
        self.reduced = reduced.tocsr()
        if len(self.free) > 40000:
            prolong = self._coarse_prolongation()
            coarse = (prolong.T@reduced@prolong).tocsc()
            self.coarse_lu = splu(coarse, permc_spec='MMD_AT_PLUS_A')
            self.prolong = prolong
            self.inverse_diagonal = 1/reduced.diagonal()

            def precondition(v):
                return (self.inverse_diagonal*v
                        + prolong@self.coarse_lu.solve(prolong.T@v))

            self.preconditioner = LinearOperator(reduced.shape, matvec=precondition)
        else:
            self.components = [np.flatnonzero(self.free % 3 == i) for i in range(3)]
            self.factors = [splu(reduced[ids][:, ids], permc_spec='MMD_AT_PLUS_A')
                            for ids in self.components]

            def precondition(v):
                out = np.empty_like(v)
                for ids, factor in zip(self.components, self.factors):
                    out[ids] = factor.solve(v[ids])
                return out

            self.preconditioner = LinearOperator(reduced.shape, matvec=precondition)
        self.factor_seconds = perf_counter()-started

    def _coarse_prolongation(self):
        """Trilinear interpolation in computational space; periodic axial index.

        This is only a preconditioner: the solved operator is still the full
        fine-grid element stiffness. Coarse geometry is never substituted.
        """
        n, nz = self.mesh.n, self.mesh.nz
        nc, nzc = max(2, n//2), max(2, nz//2)
        k, i, j = np.meshgrid(np.arange(nz), np.arange(n+1), np.arange(n+1), indexing='ij')
        location = np.column_stack((k.ravel()*nzc/nz, i.ravel()*nc/n, j.ravel()*nc/n))
        low = np.floor(location).astype(int)
        fraction = location-low
        row = np.arange(len(location))
        rows, cols, values = [], [], []
        for offset in product((0, 1), repeat=3):
            offset = np.array(offset)
            neighbor = low+offset
            neighbor[:, 0] %= nzc
            neighbor[:, 1:] = np.minimum(neighbor[:, 1:], nc)
            weight = np.prod(np.where(offset, fraction, 1-fraction), axis=1)
            col = neighbor[:, 0]*(nc+1)**2+neighbor[:, 1]*(nc+1)+neighbor[:, 2]
            keep = weight > 0
            rows.append(row[keep])
            cols.append(col[keep])
            values.append(weight[keep])
        scalar = sparse.coo_matrix((np.concatenate(values), (np.concatenate(rows), np.concatenate(cols))),
                                   shape=(len(location), nzc*(nc+1)**2)).tocsr()
        prolong = sparse.kron(scalar, sparse.eye(3, format='csr'), format='csr')[self.free]
        if nc % 2 == 0:
            center = nc//2*(nc+1)+nc//2
            anchor = nc*(nc+1)+nc//2
            fixed = [3*center, 3*center+1, 3*center+2, 3*anchor+1]
            prolong = prolong[:, np.setdiff1d(np.arange(prolong.shape[1]), fixed)]
        return prolong

    def solve(self, eps=0., twist=0.):
        if not np.isfinite([eps, twist]).all():
            raise ValueError('Finite load amplitudes required')
        started = perf_counter()
        m = self.mesh
        offset = np.zeros_like(m.nodes)
        x, y = m.nodes[m.top, :2].T
        offset[m.top] = m.pitch*np.column_stack((-twist*y, twist*x, np.full(len(x), eps)))
        offset = offset.ravel()
        rhs = -(self.Q.T@(self.K@offset))
        self.iterations = 0

        def count(_):
            self.iterations += 1

        fluctuation, info = cg(self.reduced, rhs, M=self.preconditioner,
                               rtol=2e-11, atol=0., maxiter=3000, callback=count)
        if info != 0:
            raise RuntimeError(f'Periodic iterative solve did not converge: {info}')
        u = offset+self.Q@fluctuation
        reaction = (self.K@u).reshape(-1, 3)
        stress, positions, volumes = [], [], []
        for start in range(0, len(m.hexes), 128):
            stop = min(start+128, len(m.hexes))
            elements = m.hexes[start:stop]
            b, h, pos, w = kinematics(m.nodes[elements])
            ue = u[(3*elements[:, :, None]+np.arange(3)).reshape(-1, 24)]
            alpha = np.einsum('eai,ei->ea', self.recovery[start:stop], ue)
            strain = (np.einsum('eqai,ei->eqa', b, ue)
                      + np.einsum('eqai,ei->eqa', h, alpha))
            stress.append(np.einsum('eab,eqb->eqa', self.De[start:stop], strain))
            positions.append(pos)
            volumes.append(w)
        bottom_force = reaction[m.bottom]
        F = -bottom_force[:, 2].sum()
        T = -np.sum(m.nodes[m.bottom, 0]*bottom_force[:, 1]
                    - m.nodes[m.bottom, 1]*bottom_force[:, 0])
        residual = np.max(abs(self.Q.T@reaction.ravel()))/max(np.max(abs(reaction)), 1e-30)
        return Result3D(u.reshape(-1, 3), reaction, np.concatenate(stress),
                        np.concatenate(positions), np.concatenate(volumes),
                        float(F), float(T), float(u@(self.K@u)/2),
                        float(residual), perf_counter()-started, self.iterations)


def failure_index(stress):
    """Tensor average first; cylindrical shear norm equals xz^2+yz^2."""
    stress = np.asarray(stress)
    return (np.maximum(stress[..., 2], 0)/30)**2 + (
        stress[..., 4]**2+stress[..., 5]**2)/25**2


class WallAverager:
    """Volume-weighted spherical wall-only averages, periodic in z.

    Retains all periodic images within radius (also when 2*radius>pitch).
    Search centres are wall-element centroids AND their nodes, including the
    exterior and wall/infill interface. The maximum is a sampled maximum;
    its discretization error must be checked by mesh refinement.
    Neighbours and sums are evaluated in batches; no global averaging matrix.
    """
    def __init__(self, mesh, result, radius=.66, centers=None):
        if not np.isfinite(radius) or radius <= 0:
            raise ValueError('Positive averaging radius required')
        wall = mesh.wall
        if not wall.any():
            raise ValueError('No wall elements')
        self.mask = np.repeat(wall, result.stress.shape[1])
        points = result.position.reshape(-1, 3)[self.mask]
        weights = result.volume.ravel()[self.mask]
        if centers is None:
            nodes = np.unique(mesh.hexes[wall])
            centers = np.vstack((mesh.nodes[nodes], mesh.nodes[mesh.hexes[wall]].mean(axis=1)))
        centers = np.asarray(centers, float).copy()
        centers[:, 2] %= mesh.pitch
        self.centers = np.unique(centers, axis=0)
        images = int(np.ceil(radius/mesh.pitch))
        shifts = np.arange(-images, images+1)
        cloud = np.concatenate([points+[0, 0, s*mesh.pitch] for s in shifts])
        ids = np.tile(np.arange(len(points)), len(shifts))
        self.tree, self.ids, self.weights, self.radius = cKDTree(cloud), ids, weights, radius

    def average_stresses(self, results):
        """Accept one result or a list; construct each averaging block only once."""
        single = isinstance(results, Result3D)
        results = [results] if single else results
        stresses = np.concatenate([r.stress.reshape(-1, 6)[self.mask] for r in results], axis=1)
        averages = np.empty((len(self.centers), 6*len(results)))
        for start in range(0, len(self.centers), 64):
            cs = self.centers[start:start+64]
            neighbors = self.tree.query_ball_point(cs, self.radius)
            lengths = np.array([len(a) for a in neighbors])
            if np.any(lengths == 0):
                raise ValueError('Empty wall-average neighbourhood; refine mesh')
            columns = self.ids[np.concatenate(neighbors)]
            rows = np.repeat(np.arange(len(cs)), lengths)
            values = self.weights[columns]
            sums = np.bincount(rows, weights=values, minlength=len(cs))
            values = values/sums[rows]
            block = sparse.csr_matrix((values, (rows, columns)), shape=(len(cs), len(stresses)))
            averages[start:start+len(cs)] = block@stresses
        return averages if single else np.stack(np.split(averages, len(results), axis=1))

    def evaluate(self, result):
        return self.evaluate_many([result])[0]

    def evaluate_many(self, results):
        out = []
        for average in self.average_stresses(results):
            fi = failure_index(average)
            i = int(np.argmax(fi))
            out.append(dict(FI=float(fi[i]), center=self.centers[i].tolist(),
                            average_stress=average[i].tolist(), centers=len(fi)))
        return out
