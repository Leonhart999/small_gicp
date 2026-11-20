"""Registration and map metrics of the map_eval plugin (numpy + small_gicp; no console code).

Clouds are (N, 3) float64 arrays. The transform T (4x4) maps estimate points into the reference frame:
p_ref = T @ p_est, i.e. small_gicp's T_target_source with the reference as target and the estimate as source.

Metric definitions follow MapEval / Cloud_Map_Evaluation (X. Hu et al., "MapEval: Towards Unified, Robust and
Efficient SLAM Map Evaluation Framework", IEEE RA-L 2025). This is an independent implementation from the
definitions in the paper; no code of that project is used.
"""
import math
import os

import numpy as np
import small_gicp

TAUS = (0.05, 0.1, 0.2, 0.5)      # thresholds of the tau table (m); the chosen tau is added
NN_CHUNK = 1_000_000              # queries per batch_nearest_neighbor_search call (it returns Python lists)
HIST_BINS = 50
# Colour ramp of the error image (0 -> max): "turbo"-like stops, also written to result.json for the page legend
RAMP = ['#30123b', '#4662d7', '#36aaf9', '#1ae4b6', '#72fe5e', '#c8ef34', '#faba39', '#f66b19', '#ca2a04',
        '#7a0403']


def num_threads(n):
    return int(n) if n and int(n) > 0 else (os.cpu_count() or 1)


# ------------------------------------------------------------------ geometry helpers

def transform(points, T):
    return points @ T[:3, :3].T + T[:3, 3]


def rot_z(yaw):
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def pose_matrix(x=0.0, y=0.0, z=0.0, yaw_deg=0.0):
    T = np.eye(4)
    T[:3, :3] = rot_z(math.radians(yaw_deg))
    T[:3, 3] = (x, y, z)
    return T


def decompose(T):
    """x y z (m) and roll pitch yaw (deg, ZYX convention) of a rigid transform."""
    R = T[:3, :3]
    pitch = math.asin(max(-1.0, min(1.0, -R[2, 0])))
    roll = math.atan2(R[2, 1], R[2, 2])
    yaw = math.atan2(R[1, 0], R[0, 0])
    return {'x': float(T[0, 3]), 'y': float(T[1, 3]), 'z': float(T[2, 3]),
            'roll': math.degrees(roll), 'pitch': math.degrees(pitch), 'yaw': math.degrees(yaw)}


def downsample(chunks, voxel, threads):
    """Voxel-grid downsampling (centroid per voxel, small_gicp) of a cloud given as an iterable of (n, 3) chunks.
    Each chunk is downsampled on its own, then the concatenation once more, so memory stays bounded by a chunk.
    Returns (points (M, 3) float64, number of input points)."""
    parts, total = [], 0
    for c in chunks:
        total += len(c)
        if len(c):
            parts.append(small_gicp.voxelgrid_sampling(c, voxel, threads).points()[:, :3].copy())
    if not parts:
        return np.zeros((0, 3)), total
    pts = parts[0] if len(parts) == 1 else np.vstack(parts)
    if len(parts) > 1:
        del parts
        pts = small_gicp.voxelgrid_sampling(pts, voxel, threads).points()[:, :3].copy()
    return pts, total


def nn_distances(tree, points, threads):
    """Distance from every point to its nearest neighbour in the KdTree (inf when none was found)."""
    out = np.empty(len(points))
    for i in range(0, len(points), NN_CHUNK):
        _, sq = tree.batch_nearest_neighbor_search(points[i:i + NN_CHUNK], num_threads=threads)
        out[i:i + NN_CHUNK] = np.sqrt(np.asarray(sq, dtype=np.float64))
    out[~np.isfinite(out)] = np.inf
    return out


# ------------------------------------------------------------------ registration

class Prepared:
    """A cloud downsampled to `voxel` with covariances and a KdTree, ready for GICP / VGICP."""

    def __init__(self, points, voxel, threads, neighbors=10):
        self.voxel = voxel
        self.cloud, self.tree = small_gicp.preprocess_points(points, downsampling_resolution=voxel,
                                                             num_neighbors=neighbors, num_threads=threads)
        self.points = self.cloud.points()[:, :3].copy()
        self._voxelmap = None

    def voxelmap(self):
        if self._voxelmap is None:
            self._voxelmap = small_gicp.GaussianVoxelMap(max(1.0, 4 * self.voxel))
            self._voxelmap.insert(self.cloud)
        return self._voxelmap


def align(target, source, init, method, max_corr, threads, max_iterations):
    """One GICP / VGICP run of `source` onto `target` (both Prepared) from `init`."""
    if method == 'VGICP':
        return small_gicp.align(target.voxelmap(), source.cloud, init, max_correspondence_distance=max_corr,
                                num_threads=threads, max_iterations=max_iterations)
    return small_gicp.align(target.cloud, source.cloud, target.tree, init, registration_type='GICP',
                            max_correspondence_distance=max_corr, num_threads=threads,
                            max_iterations=max_iterations)


def fitness(target, source, T, max_corr, threads):
    """(inlier fraction, inlier RMSE) of the source points moved by T against the target, inliers = points
    whose nearest target point is within max_corr."""
    d = nn_distances(target.tree, transform(source.points, T), threads)
    inl = d <= max_corr
    frac = float(inl.mean()) if len(d) else 0.0
    rmse = float(np.sqrt(np.mean(d[inl] ** 2))) if inl.any() else float('inf')
    return frac, rmse


def search_initial(target, source, yaw_step, max_corr, method, threads, max_iterations, progress=None):
    """Coarse global search over the yaw angle: from every yaw in steps of `yaw_step` degrees, with the
    translation that matches the centroids and with zero translation, run two registrations with shrinking
    correspondence distances (max(5, 4 max_corr) m, then max(2, 2 max_corr) m), score each result by its inlier
    fraction at max_corr (ties within 0.01: smaller inlier RMSE) and keep the best. `target` / `source` are
    coarse Prepared clouds. Returns (best candidate, all candidates)."""
    ct, cs = target.points.mean(axis=0), source.points.mean(axis=0)
    coarse_corr = (max(5.0, 4 * max_corr), max(2.0, 2 * max_corr))
    yaws = list(range(0, 360, int(yaw_step)))
    candidates = []
    for k, yaw in enumerate(yaws):
        R = rot_z(math.radians(yaw))
        for mode, t in (('centroid', ct - R @ cs), ('origin', np.zeros(3))):
            T = np.eye(4)
            T[:3, :3], T[:3, 3] = R, t
            for corr in coarse_corr:
                T = align(target, source, T, method, corr, threads, max_iterations).T_target_source
            frac, rmse = fitness(target, source, T, max_corr, threads)
            candidates.append({'yaw0': yaw if yaw <= 180 else yaw - 360, 'start': mode, 'fitness': frac,
                               'rmse': rmse, 'T': T})
        if progress:
            progress((k + 1) / len(yaws))
    best_fit = max(c['fitness'] for c in candidates)
    # among the candidates whose fitness is within 0.01 of the best, the smallest RMSE wins
    best = min((c for c in candidates if c['fitness'] >= best_fit - 0.01), key=lambda c: c['rmse'])
    return best, candidates


# ------------------------------------------------------------------ metrics

def overlap_masks(est, ref, cell):
    """Points of each cloud that lie in XY cells (size `cell`) occupied by both clouds, after growing each
    cloud's footprint by one cell. Used for the optional "evaluate the overlap only" mode."""
    lo = np.minimum(est[:, :2].min(axis=0), ref[:, :2].min(axis=0))
    ce = np.floor((est[:, :2] - lo) / cell).astype(np.int64)
    cr = np.floor((ref[:, :2] - lo) / cell).astype(np.int64)
    shape = tuple(np.maximum(ce.max(axis=0), cr.max(axis=0)) + 1)

    def footprint(c):
        occ = np.zeros(shape, bool)
        occ[c[:, 0], c[:, 1]] = True
        grown = occ.copy()
        grown[1:, :] |= occ[:-1, :]
        grown[:-1, :] |= occ[1:, :]
        grown[:, 1:] |= occ[:, :-1]
        grown[:, :-1] |= occ[:, 1:]
        return grown

    both = footprint(ce) & footprint(cr)
    return both[ce[:, 0], ce[:, 1]], both[cr[:, 0], cr[:, 1]]


def stats(d):
    if not len(d):
        return {'mean': None, 'median': None, 'rmse': None, 'p95': None}
    return {'mean': float(d.mean()), 'median': float(np.median(d)), 'rmse': float(np.sqrt(np.mean(d ** 2))),
            'p95': float(np.percentile(d, 95))}


def fscore(p, c):
    return 2 * p * c / (p + c) if p + c > 0 else 0.0


def metrics(d_est, d_ref, tau, max_dist, taus=TAUS):
    """Map metrics from the nearest-neighbour distances est->ref (d_est) and ref->est (d_ref).

    accuracy      statistics of d_est over the estimate points with d_est <= max_dist (points farther away are
                  outside the overlap and counted in `beyond`)
    precision(t)  share of estimate points with d_est < t
    completeness(t) share of reference points with d_ref < t
    F(t)          harmonic mean of precision and completeness
    inlier_rmse(t) RMSE of d_est over the estimate points with d_est < t (MapEval's AC at threshold t)
    chamfer       mean of the two directions' mean distances, each distance truncated at max_dist
    """
    d_est, d_ref = np.asarray(d_est, float), np.asarray(d_ref, float)
    matched = d_est[d_est <= max_dist]
    acc = stats(matched)
    acc.update(matched=int(len(matched)), beyond=int(len(d_est) - len(matched)))
    rows = []
    for t in sorted(set(taus) | {tau}):
        p = float(np.mean(d_est < t)) if len(d_est) else 0.0
        c = float(np.mean(d_ref < t)) if len(d_ref) else 0.0
        inl = d_est[d_est < t]
        rows.append({'tau': t, 'precision': p, 'completeness': c, 'fscore': fscore(p, c),
                     'inlier_rmse': float(np.sqrt(np.mean(inl ** 2))) if len(inl) else None})
    main = next(r for r in rows if r['tau'] == tau)
    e2r = float(np.minimum(d_est, max_dist).mean()) if len(d_est) else None
    r2e = float(np.minimum(d_ref, max_dist).mean()) if len(d_ref) else None
    chamfer = {'value': 0.5 * (e2r + r2e) if e2r is not None and r2e is not None else None,
               'est_to_ref': e2r, 'ref_to_est': r2e,
               'truncated_est': int(np.sum(d_est > max_dist)), 'truncated_ref': int(np.sum(d_ref > max_dist))}
    return {'tau': tau, 'max_dist': max_dist, 'accuracy': acc, 'precision': main['precision'],
            'completeness': main['completeness'], 'fscore': main['fscore'], 'chamfer': chamfer, 'table': rows,
            'points': {'estimate': int(len(d_est)), 'reference': int(len(d_ref))}}


def histogram(d_est, max_dist, bins=HIST_BINS):
    counts, edges = np.histogram(d_est[d_est <= max_dist], bins=bins, range=(0.0, max_dist))
    return {'bin_width': float(edges[1] - edges[0]), 'max': float(max_dist), 'counts': counts.tolist(),
            'overflow': int(np.sum(d_est > max_dist))}


# ------------------------------------------------------------------ image

def ramp_colours(values, vmax):
    """RGB uint8 of values in [0, vmax] on the RAMP colour scale."""
    stops = np.array([[int(h[i:i + 2], 16) for i in (1, 3, 5)] for h in RAMP], dtype=np.float64)
    x = np.clip(np.asarray(values, float) / max(vmax, 1e-9), 0, 1) * (len(RAMP) - 1)
    i = np.minimum(np.floor(x).astype(int), len(RAMP) - 2)
    f = (x - i)[:, None]
    return (stops[i] * (1 - f) + stops[i + 1] * f).round().astype(np.uint8)


def render_top(est, d_est, ref, vmax, path, size=1600, min_pixel=0.02):
    """Top view: reference points light grey, estimate points coloured by the mean est->ref distance of the
    points around each pixel, a colour bar below. The pixel is at least `min_pixel` m (the worker passes twice
    the evaluation voxel, so pixels hold several points). Returns the image geometry for result.json."""
    from PIL import Image, ImageDraw
    both = np.vstack([est[:, :2], ref[:, :2]])
    lo, hi = both.min(axis=0), both.max(axis=0)
    res = max(float((hi - lo).max()) / size, min_pixel)
    w, h = (np.floor((hi - lo) / res).astype(int) + 1)
    img = np.full((h, w, 3), 255, np.uint8)

    def pixels(pts):
        c = np.floor((pts[:, :2] - lo) / res).astype(np.int64)
        return (h - 1 - c[:, 1]) * w + c[:, 0]

    ref_px = np.unique(pixels(ref))
    img.reshape(-1, 3)[ref_px] = (205, 205, 205)
    px = pixels(est)
    n = np.bincount(px, minlength=w * h).astype(np.float64).reshape(h, w)
    s = np.bincount(px, weights=np.minimum(d_est, vmax * 10), minlength=w * h).reshape(h, w)
    hit = n > 0
    # mean over the 3x3 neighbourhood of every pixel that has points: smooths scan-line speckle without
    # spreading colour into empty pixels
    ns, ss = n.copy(), s.copy()
    for dy, dx in ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)):
        ys, yd = (slice(max(dy, 0), h + min(dy, 0)), slice(max(-dy, 0), h + min(-dy, 0)))
        xs, xd = (slice(max(dx, 0), w + min(dx, 0)), slice(max(-dx, 0), w + min(-dx, 0)))
        ns[yd, xd] += n[ys, xs]
        ss[yd, xd] += s[ys, xs]
    img[hit] = ramp_colours(ss[hit] / ns[hit], vmax)
    bar_h = 46
    canvas = Image.new('RGB', (max(int(w), 360), int(h) + bar_h), (255, 255, 255))
    canvas.paste(Image.fromarray(img), (0, 0))
    draw = ImageDraw.Draw(canvas)
    bw, x0, y0 = 240, 12, int(h) + 8
    ramp = ramp_colours(np.linspace(0, vmax, bw), vmax)
    for i in range(bw):
        draw.line([(x0 + i, y0), (x0 + i, y0 + 12)], fill=tuple(int(v) for v in ramp[i]))
    for frac in (0.0, 0.5, 1.0):
        x = x0 + int(frac * (bw - 1))
        draw.line([(x, y0 + 12), (x, y0 + 16)], fill=(60, 60, 60))
        label = f'{frac * vmax:.2f}' + (' m+' if frac == 1.0 else '')
        draw.text((x - 8, y0 + 18), label, fill=(40, 40, 40))
    draw.text((x0 + bw + 14, y0), 'est -> ref distance; grey: reference', fill=(40, 40, 40))
    canvas.save(path)
    return {'file': os.path.basename(path), 'resolution': res, 'origin': [float(lo[0]), float(lo[1])],
            'width': int(w), 'height': int(h), 'vmin': 0.0, 'vmax': float(vmax), 'ramp': RAMP,
            'pixel': 'mean est->ref distance of the estimate points in the pixel'}
