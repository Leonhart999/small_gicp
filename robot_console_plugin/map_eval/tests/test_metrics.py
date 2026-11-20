"""Synthetic checks of the map_eval registration and metrics (needs small_gicp and NumPy: run with the plugin's venv,
e.g. <data_root>/plugin_envs/map_eval/bin/python -m pytest tests).

The scene is a closed room with a few boxes and a pillar, sampled on its surfaces: enough structure in every
direction for GICP and no symmetry that a yaw search could confuse.
"""
import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
small_gicp = pytest.importorskip('small_gicp')
import evaluation as ev  # noqa: E402
from mapio import cloud_chunks, write_pcd_xyz, write_pcd_xyzi  # noqa: E402


def box_surface(rng, lo, hi, density):
    """Points on the six faces of an axis-aligned box."""
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    out = []
    for axis in range(3):
        a, b = [i for i in range(3) if i != axis]
        area = (hi[a] - lo[a]) * (hi[b] - lo[b])
        n = max(10, int(area * density))
        for side in (lo[axis], hi[axis]):
            p = np.empty((n, 3))
            p[:, axis] = side
            p[:, a] = rng.uniform(lo[a], hi[a], n)
            p[:, b] = rng.uniform(lo[b], hi[b], n)
            out.append(p)
    return np.vstack(out)


def scene(seed=0, density=400):
    rng = np.random.default_rng(seed)
    parts = [box_surface(rng, (0, 0, 0), (24, 14, 3), density),            # room
             box_surface(rng, (3, 2, 0), (5, 3, 1.2), density),             # boxes
             box_surface(rng, (15, 9, 0), (16.5, 12, 2.0), density),
             box_surface(rng, (9, 5, 0), (9.4, 5.4, 3), density),           # pillar
             box_surface(rng, (19, 2, 0), (22, 2.6, 0.8), density)]
    return np.vstack(parts)


def eval_cloud(points, voxel=0.05):
    pts, _ = ev.downsample([points], voxel, 4)
    return pts


def test_identical_clouds_are_perfect():
    pts = eval_cloud(scene())
    tree = small_gicp.KdTree(pts, num_threads=4)
    d = ev.nn_distances(tree, pts, 4)
    m = ev.metrics(d, d, tau=0.1, max_dist=1.0)
    assert m['accuracy']['mean'] == pytest.approx(0.0, abs=1e-9)
    assert m['accuracy']['rmse'] == pytest.approx(0.0, abs=1e-9)
    assert m['completeness'] == 1.0 and m['precision'] == 1.0 and m['fscore'] == 1.0
    assert m['chamfer']['value'] == pytest.approx(0.0, abs=1e-9)
    assert m['chamfer']['truncated_est'] == 0 and m['chamfer']['truncated_ref'] == 0


def test_metric_definitions_on_known_distances():
    d_est = np.array([0.0, 0.04, 0.08, 0.3, 2.0])
    d_ref = np.array([0.02, 0.06, 0.5, 5.0])
    m = ev.metrics(d_est, d_ref, tau=0.1, max_dist=1.0)
    assert m['precision'] == pytest.approx(3 / 5)
    assert m['completeness'] == pytest.approx(2 / 4)
    assert m['fscore'] == pytest.approx(2 * 0.6 * 0.5 / 1.1)
    assert m['accuracy']['matched'] == 4 and m['accuracy']['beyond'] == 1
    assert m['accuracy']['mean'] == pytest.approx(np.mean([0, 0.04, 0.08, 0.3]))
    assert m['chamfer']['est_to_ref'] == pytest.approx(np.mean([0, 0.04, 0.08, 0.3, 1.0]))
    assert m['chamfer']['ref_to_est'] == pytest.approx(np.mean([0.02, 0.06, 0.5, 1.0]))
    assert m['chamfer']['truncated_est'] == 1 and m['chamfer']['truncated_ref'] == 1
    row = next(r for r in m['table'] if r['tau'] == 0.1)
    assert row['inlier_rmse'] == pytest.approx(math.sqrt(np.mean([0, 0.04 ** 2, 0.08 ** 2])))
    assert [r['tau'] for r in m['table']] == [0.05, 0.1, 0.2, 0.5]


def known_pair(yaw_deg=20.0, t=(3.0, -2.0, 0.5), noise=0.01, seed=1):
    est = scene(seed=seed)
    T_true = ev.pose_matrix(*t, yaw_deg)
    rng = np.random.default_rng(seed + 10)
    ref = ev.transform(scene(seed=seed + 5), T_true)
    ref = ref + rng.normal(0, noise, ref.shape)
    return eval_cloud(est), eval_cloud(ref), T_true


def assert_transform_close(T, T_true, rot_deg=0.3, trans=0.03):
    dR = T[:3, :3] @ T_true[:3, :3].T
    angle = math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(dR) - 1) / 2))))
    assert angle < rot_deg, angle
    assert np.linalg.norm(T[:3, 3] - T_true[:3, 3]) < trans, T[:3, 3] - T_true[:3, 3]


@pytest.mark.parametrize('method', ['GICP', 'VGICP'])
def test_search_and_refine_recover_known_transform(method):
    est, ref, T_true = known_pair()
    tgt_c, src_c = ev.Prepared(ref, 0.5, 4), ev.Prepared(est, 0.5, 4)
    best, cands = ev.search_initial(tgt_c, src_c, 30, 1.0, method, 4, 50)
    assert len(cands) == 24
    tgt, src = ev.Prepared(ref, 0.15, 4), ev.Prepared(est, 0.15, 4)
    r = ev.align(tgt, src, best['T'], method, 1.0, 4, 50)
    T = np.asarray(r.T_target_source)
    assert_transform_close(T, T_true)
    frac, rmse = ev.fitness(tgt, src, T, 1.0, 4)
    assert frac > 0.95


def test_metrics_after_alignment_are_near_ideal():
    est, ref, T_true = known_pair(noise=0.01)
    est_t = ev.transform(est, T_true)
    d_est = ev.nn_distances(small_gicp.KdTree(ref, num_threads=4), est_t, 4)
    d_ref = ev.nn_distances(small_gicp.KdTree(est_t, num_threads=4), ref, 4)
    m = ev.metrics(d_est, d_ref, tau=0.1, max_dist=1.0)
    assert m['accuracy']['mean'] < 0.04
    assert m['fscore'] > 0.97
    assert m['chamfer']['truncated_est'] == 0


def test_manual_pose_and_decompose_round_trip():
    T = ev.pose_matrix(1.5, -2.0, 0.25, 135.0)
    p = ev.decompose(T)
    assert p['x'] == pytest.approx(1.5) and p['y'] == pytest.approx(-2.0) and p['z'] == pytest.approx(0.25)
    assert p['yaw'] == pytest.approx(135.0) and p['roll'] == pytest.approx(0.0) and p['pitch'] == pytest.approx(0.0)


def test_overlap_masks_keep_shared_footprint():
    a = np.column_stack([np.linspace(0, 20, 2001), np.zeros(2001), np.zeros(2001)])
    b = np.column_stack([np.linspace(10, 30, 2001), np.zeros(2001), np.zeros(2001)])
    ma, mb = ev.overlap_masks(a, b, 1.0)
    # footprints grow by one 1 m cell: a keeps x >= 9, b keeps x < 22
    assert a[ma, 0].min() >= 9.0 - 1e-9 and a[ma, 0].max() == pytest.approx(20)
    assert b[mb, 0].max() < 22.0 and b[mb, 0].min() == pytest.approx(10)
    assert not ma[a[:, 0] < 8.9].any() and not mb[b[:, 0] > 22.1].any()


def test_cloud_io_round_trip(tmp_path):
    pts = scene(density=50).astype(np.float32)
    write_pcd_xyz(str(tmp_path / 'a.pcd'), pts)
    back = np.vstack(list(cloud_chunks(str(tmp_path / 'a.pcd'), chunk=1000)))
    assert np.allclose(back, pts)
    write_pcd_xyzi(str(tmp_path / 'b.pcd'), pts, np.arange(len(pts), dtype=np.float32))
    assert np.allclose(np.vstack(list(cloud_chunks(str(tmp_path / 'b.pcd')))), pts)
    # binary PLY with an extra property and ascii PLY
    data = np.zeros(len(pts), dtype=[('x', '<f4'), ('y', '<f4'), ('z', '<f4'), ('intensity', '<f4')])
    data['x'], data['y'], data['z'] = pts.T
    with open(tmp_path / 'c.ply', 'wb') as f:
        f.write(f'ply\nformat binary_little_endian 1.0\nelement vertex {len(pts)}\nproperty float x\n'
                f'property float y\nproperty float z\nproperty float intensity\nend_header\n'.encode())
        f.write(data.tobytes())
    assert np.allclose(np.vstack(list(cloud_chunks(str(tmp_path / 'c.ply')))), pts)
    with open(tmp_path / 'd.ply', 'w') as f:
        f.write(f'ply\nformat ascii 1.0\nelement vertex 3\nproperty float x\nproperty float y\nproperty float z\n'
                f'end_header\n1 2 3\n4 5 6\n7 8 9\n')
    assert np.allclose(np.vstack(list(cloud_chunks(str(tmp_path / 'd.ply')))), [[1, 2, 3], [4, 5, 6], [7, 8, 9]])
