"""Boundary-conforming straight-sided T6 meshes (length units are caller-defined)."""

from dataclasses import dataclass

import numpy as np
from scipy.spatial import Delaunay, cKDTree


@dataclass
class Mesh:
    nodes: np.ndarray
    tris: np.ndarray
    boundary_edges: np.ndarray  # endpoint, midpoint, endpoint; material on left
    edge_src: np.ndarray

    def boundary_nodes(self, sources):
        """Nodes on original polygon edge IDs, including midside nodes."""
        return np.unique(self.boundary_edges[np.isin(self.edge_src, list(sources))])

    def min_angles(self):
        """Minimum corner angle of each triangle, in degrees."""
        p = self.nodes[self.tris[:, :3]]
        v = np.roll(p, -1, axis=1) - p
        lengths = np.linalg.norm(v, axis=2)
        cosines = (lengths**2 + np.roll(lengths, 1, axis=1)**2
                   - np.roll(lengths, -1, axis=1)**2)
        cosines /= 2 * lengths * np.roll(lengths, 1, axis=1)
        return np.degrees(np.arccos(np.clip(cosines, -1, 1))).min(axis=1)


def _ring(poly):
    p = np.asarray(poly, dtype=float)
    if p.ndim != 2 or p.shape[1] != 2 or not np.isfinite(p).all():
        raise ValueError("A polygon must be a finite (N,2) array")
    if len(p) > 1 and np.array_equal(p[0], p[-1]):
        p = p[:-1]
    if len(p) < 3 or np.any(np.linalg.norm(np.roll(p, -1, axis=0) - p, axis=1) == 0):
        raise ValueError("At least three vertices and no zero-length edges required")
    return p.copy()


def _area(p):
    q = p - p[0]
    return .5 * np.sum(q[:, 0] * np.roll(q[:, 1], -1)
                        - q[:, 1] * np.roll(q[:, 0], -1))


def _inside(points, rings):
    inside = np.zeros(len(points), dtype=bool)
    x, y = points.T
    for ring in rings:
        for a, b in zip(ring, np.roll(ring, -1, axis=0)):
            if a[1] == b[1]:
                continue
            ids = np.flatnonzero((a[1] > y) != (b[1] > y))
            cross = a[0] + (y[ids] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            inside[ids] ^= x[ids] < cross
    return inside


def _edges(t):
    return np.unique(np.sort(t[:, [[0, 1], [1, 2], [2, 0]]].reshape(-1, 2), axis=1), axis=0)


def mesh_polygon(poly, size_fn, h_min, h_max, holes=()):
    """Mesh a simple polygon; holes are polygon rings, with consecutive edge IDs.

    Original vertices survive exactly. Outer edge IDs are 0..N-1 (a repeated
    closing vertex is ignored); hole edge IDs follow in input order. Boundary
    recovery may split below h_min to retain small geometric features. Input
    rings must be disjoint, non-self-intersecting, with holes inside the outer.
    """
    if not 0 < h_min <= h_max:
        raise ValueError("Require 0 < h_min <= h_max")
    rings = [_ring(poly)] + [_ring(h) for h in holes]
    if any(abs(_area(r)) < np.finfo(float).eps for r in rings):
        raise ValueError("Zero-area polygon")
    if np.min(np.concatenate(rings)[:, 0]) < 0:
        raise ValueError("Axisymmetric radii must be nonnegative")

    def sizes(p):
        h = np.broadcast_to(np.asarray(size_fn(p), float), (len(p),))
        if not np.isfinite(h).all():
            raise ValueError("size_fn returned nonfinite values")
        return np.clip(h, h_min, h_max)

    pts, segments, sources = [], [], []
    offset = 0
    for k, ring in enumerate(rings):
        base = len(pts)
        pts.extend(ring)
        forward = (_area(ring) > 0) == (k == 0)
        for j, (a, b) in enumerate(zip(ring, np.roll(ring, -1, axis=0))):
            length = np.linalg.norm(b - a)
            # Adaptive bisection samples endpoints and midpoint, not just centroid.
            stack = [(0., 1., base + j, base + (j + 1) % len(ring))]
            while stack:
                lo, hi, ia, ib = stack.pop()
                sample = a + np.array([lo, (lo + hi) / 2, hi])[:, None] * (b - a)
                if (hi - lo) * length > 1.15 * sizes(sample).min():
                    mid = len(pts)
                    pts.append((sample[0] + sample[2]) / 2)
                    stack.extend([((lo + hi) / 2, hi, mid, ib),
                                  (lo, (lo + hi) / 2, ia, mid)])
                else:
                    segments.append((ia, ib) if forward else (ib, ia))
                    sources.append(offset + j)
        offset += len(ring)
    points = np.asarray(pts)
    segments = np.asarray(segments, dtype=np.int64)
    sources = np.asarray(sources, dtype=np.int64)
    n_boundary = len(points)
    segment_tree = cKDTree(points[segments].mean(axis=1))
    segment_lengths = np.linalg.norm(points[segments[:, 1]]-points[segments[:, 0]], axis=1)

    def interior_sizes(p):
        # Dense original vertices impose a geometric size even if size_fn is
        # coarse there. Grade into the interior rather than making flat slivers.
        distance, ids = segment_tree.query(p, k=min(8, len(segments)))
        if distance.ndim == 1:
            distance, ids = distance[:, None], ids[:, None]
        geometric = np.min(1.3*segment_lengths[ids]+.35*distance, axis=1)
        return np.minimum(sizes(p), geometric)

    lo, hi = rings[0].min(axis=0), rings[0].max(axis=0)
    width = np.max(hi - lo)
    centers = ((lo + hi) / 2)[None, :]
    cell_width = width
    seeds = []
    boundary_tree = cKDTree(points)
    # Quadtree seeding avoids a uniform h_min grid over the entire bounding box.
    for _ in range(40):
        h = interior_sizes(centers)
        distance = boundary_tree.query(centers)[0]
        useful = _inside(centers, rings) | (distance < cell_width * .8 + h_max)
        centers, h = centers[useful], h[useful]
        split = cell_width > h
        if np.any(~split):
            p = centers[~split]
            keep = _inside(p, rings)
            keep &= boundary_tree.query(p)[0] > .38 * interior_sizes(p)
            seeds.extend(p[keep])
        if not np.any(split):
            break
        shifts = np.array([[-1, -1], [1, -1], [-1, 1], [1, 1]]) * cell_width / 4
        centers = (centers[split, None, :] + shifts).reshape(-1, 2)
        cell_width /= 2
        if len(centers) + len(seeds) > 180_000:
            raise MemoryError("Mesh seeding exceeds 180000 linear vertices")
    if seeds:
        points = np.vstack((points, seeds))

    def triangulate(p, seg, src):
        for _ in range(48):
            dt = Delaunay(p)
            # Qhull can triangulate nominally collinear boundary subdivisions
            # into roundoff-area triangles. Their centroids have no reliable
            # inside/outside classification and can create a spurious exterior
            # chord. Exclude them from both recovery and domain classification.
            v = p[dt.simplices]
            a, b = v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]
            cross = a[:, 0]*b[:, 1] - a[:, 1]*b[:, 0]
            edge2 = np.sum((np.roll(v, -1, axis=1) - v)**2, axis=2).max(axis=1)
            # Rounding of coordinates (including interpolated boundary points)
            # scales with their absolute magnitude, not just local edge length.
            # A cutoff using edge2 alone misses short, translated flat triangles
            # both along one input edge and between nearly aligned thread crests.
            scale = np.maximum(edge2, np.abs(v).max(axis=(1, 2))*np.sqrt(edge2))
            simplices = dt.simplices[cross > 32*np.finfo(float).eps*scale]
            all_edges = _edges(simplices)
            keys = all_edges[:, 0] * len(p) + all_edges[:, 1]
            s = np.sort(seg, axis=1)
            missing = ~np.isin(s[:, 0] * len(p) + s[:, 1], keys)
            if not np.any(missing):
                t = simplices[_inside(p[simplices].mean(axis=1), rings)]
                # Every recovered boundary must occur exactly once in the domain.
                te = np.sort(t[:, [[0, 1], [1, 2], [2, 0]]].reshape(-1, 2), axis=1)
                u, counts = np.unique(te, axis=0, return_counts=True)
                exterior = u[counts == 1]
                if set(map(tuple, exterior)) != set(map(tuple, s)):
                    raise ValueError("Invalid/intersecting polygon or unresolved boundary")
                return p, seg, src, t
            ids = np.flatnonzero(missing)
            new_ids = np.arange(len(p), len(p) + len(ids))
            p = np.vstack((p, p[seg[ids]].mean(axis=1)))
            old = seg[ids].copy()
            seg[ids, 1] = new_ids
            seg = np.vstack((seg, np.column_stack((new_ids, old[:, 1]))))
            src = np.concatenate((src, src[ids]))
        raise RuntimeError("Boundary recovery did not converge")

    points, segments, sources, tris = triangulate(points, segments, sources)
    def relax(points, segments, sources, tris, iterations):
        # Fixed boundary, damped Laplacian relaxation of interior vertices.
        for _ in range(iterations):
            fixed = np.zeros(len(points), bool)
            fixed[np.unique(segments)] = True
            e = _edges(tris)
            sums = np.zeros_like(points)
            counts = np.zeros(len(points))
            np.add.at(sums, e[:, 0], points[e[:, 1]])
            np.add.at(sums, e[:, 1], points[e[:, 0]])
            np.add.at(counts, e.ravel(), 1)
            movable = (~fixed) & (counts > 0)
            trial = points.copy()
            trial[movable] = .5 * (points[movable] + sums[movable] / counts[movable, None])
            valid = _inside(trial[movable], rings)
            trial[np.flatnonzero(movable)[~valid]] = points[movable][~valid]
            points, segments, sources, tris = triangulate(trial, segments, sources)
        return points, segments, sources, tris

    points, segments, sources, tris = relax(points, segments, sources, tris, 8)
    # Bounded Delaunay refinement for the remaining boundary-transition slivers.
    # Acute prescribed corners can prevent the angle target; never discard them.
    for _ in range(8):
        angles = Mesh(points, tris, segments, sources).min_angles()
        bad = tris[angles < 18]
        if not len(bad):
            break
        boundary_map = {tuple(sorted(e)): i for i, e in enumerate(segments)}
        mids = points[segments].mean(axis=1)
        lengths = np.linalg.norm(points[segments[:, 1]]-points[segments[:, 0]], axis=1)
        mid_tree = cKDTree(mids)
        point_tree = cKDTree(points)
        split, additions = set(), []
        for t in bad:
            boundary = [boundary_map[tuple(sorted((t[a], t[b])))]
                        for a, b in ((0, 1), (1, 2), (2, 0))
                        if tuple(sorted((t[a], t[b]))) in boundary_map]
            if len(boundary) >= 2:
                continue  # preserve acute input corners without endless subdivision
            if boundary:
                split.add(max(boundary, key=lambda k: lengths[k]))
                continue
            p = points[t]
            v = p[1:]-p[0]
            center = p[0]+np.linalg.solve(2*v, np.sum(v*v, axis=1))
            candidates = mid_tree.query_ball_point(center, lengths.max()/2)
            encroached = [i for i in candidates if np.linalg.norm(center-mids[i]) < .49*lengths[i]]
            if encroached:
                split.add(max(encroached, key=lambda k: lengths[k]))
            elif _inside(center[None], rings)[0] and point_tree.query(center)[0] > 1e-10*width:
                if not additions or np.min(np.linalg.norm(np.asarray(additions)-center, axis=1)) > .2*point_tree.query(center)[0]:
                    additions.append(center)
        if not split and not additions:
            break
        ids = np.array(sorted(split), dtype=int)
        new_ids = np.arange(len(points), len(points)+len(ids))
        points = np.vstack((points, mids[ids], np.asarray(additions).reshape(-1, 2)))
        old = segments[ids].copy()
        segments[ids, 1] = new_ids
        segments = np.vstack((segments, np.column_stack((new_ids, old[:, 1]))))
        sources = np.concatenate((sources, sources[ids]))
        points, segments, sources, tris = triangulate(points, segments, sources)
        points, segments, sources, tris = relax(points, segments, sources, tris, 4)
    used = np.unique(tris)
    if not np.isin(np.arange(n_boundary), used).all():
        raise ValueError("Polygon vertices missing from triangulation")
    remap = np.full(len(points), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    points, tris, segments = points[used], remap[tris], remap[segments]
    v = points[tris]
    cross = ((v[:, 1, 0] - v[:, 0, 0]) * (v[:, 2, 1] - v[:, 0, 1])
             - (v[:, 1, 1] - v[:, 0, 1]) * (v[:, 2, 0] - v[:, 0, 0]))
    flip = cross < 0
    tris[flip] = tris[flip][:, [0, 2, 1]]
    edges = _edges(tris)
    edge_id = {tuple(e): i + len(points) for i, e in enumerate(edges)}
    mids = np.array([[edge_id[tuple(sorted((t[a], t[b])))]
                      for a, b in ((0, 1), (1, 2), (2, 0))] for t in tris])
    boundary = np.array([[a, edge_id[tuple(sorted((a, b)))], b] for a, b in segments])
    return Mesh(np.vstack((points, points[edges].mean(axis=1))),
                np.column_stack((tris, mids)), boundary, sources)
