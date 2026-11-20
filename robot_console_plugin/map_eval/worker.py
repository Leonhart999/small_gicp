"""Map evaluation job (runs in the plugin's venv with small_gicp and NumPy).

1. Both clouds are read in chunks (memory-mapped) and voxel-downsampled to the evaluation voxel.
2. Initial guess: identity, a manual x y z yaw, or a coarse yaw search (evaluation.search_initial).
3. Fine registration of the estimate onto the reference with small_gicp GICP / VGICP at the registration voxel
   (unless `refine` is off: then the initial guess is used as it is).
4. Nearest-neighbour distances est->ref and ref->est on the evaluation clouds (small_gicp KdTree), metrics,
   histogram, error image.

Outputs in the job folder: result.json, error_top.png (top view coloured by the est->ref distance),
aligned.pcd (estimate evaluation cloud in the reference frame, intensity = est->ref distance in m).
"""
import json
import os
import sys
import time

import numpy as np

from mapio import cloud_chunks, cloud_points, emit, fail, write_pcd_xyzi

PLUGIN = 'map_eval'
MAX_EVAL_POINTS = 25_000_000      # per cloud after downsampling to the evaluation voxel
MIN_POINTS = 100


def r6(v):
    return None if v is None else round(float(v), 6)


def main():
    job = sys.argv[1]
    with open(os.path.join(job, 'input.json'), encoding='utf-8') as f:
        p = json.load(f)['params']
    t_start = time.time()
    try:
        import small_gicp
        import evaluation as ev
    except ImportError as e:
        fail(job, PLUGIN, 'run_failed', reason=f'small_gicp: {e}')
    threads = ev.num_threads(p.get('threads'))
    timing = {}

    # ---- 1. read and downsample
    clouds = {}
    for k, (role, lo, hi) in enumerate((('estimate', 0.0, 0.2), ('reference', 0.2, 0.4))):
        src = p[role]
        emit(phase=f'loading_{role}', progress=lo)
        t0 = time.time()
        try:
            total = cloud_points(src['path'])
            done = [0]

            def chunks():
                for c in cloud_chunks(src['path']):
                    done[0] += len(c)
                    emit(phase=f'loading_{role}', progress=round(lo + (hi - lo) * min(1.0, done[0] / max(total, 1)), 4))
                    yield c
            pts, n_in = ev.downsample(chunks(), p['eval_voxel'], threads)
        except (OSError, ValueError, KeyError) as e:
            fail(job, PLUGIN, 'cloud_unreadable', name=src['name'], reason=str(e))
        if len(pts) < MIN_POINTS:
            fail(job, PLUGIN, 'empty_cloud', name=src['name'], points=int(len(pts)))
        if len(pts) > MAX_EVAL_POINTS:
            fail(job, PLUGIN, 'too_many_points', name=src['name'], points=int(len(pts)), limit=MAX_EVAL_POINTS)
        clouds[role] = pts
        timing[f'load_{role}'] = round(time.time() - t0, 2)
        print(f'{role}: {n_in} points -> {len(pts)} at {p["eval_voxel"]} m ({timing[f"load_{role}"]} s)', flush=True)
        src.update(points=int(n_in), eval_points=int(len(pts)))
    est, ref = clouds['estimate'], clouds['reference']

    # ---- 2. initial guess
    t0 = time.time()
    reg = {'init': p['init'], 'method': p['method'], 'refine': p['refine'], 'search': None}
    if p['init'] == 'manual':
        T = ev.pose_matrix(p['x'], p['y'], p['z'], p['yaw'])
    elif p['init'] == 'search':
        emit(phase='searching', progress=0.42)
        coarse_voxel = max(1.0, 2 * p['reg_voxel'])
        tgt_c, src_c = ev.Prepared(ref, coarse_voxel, threads), ev.Prepared(est, coarse_voxel, threads)
        best, cands = ev.search_initial(
            tgt_c, src_c, p['yaw_step'], p['max_corr'], p['method'], threads, p['max_iterations'],
            progress=lambda f: emit(phase='searching', progress=round(0.42 + 0.2 * f, 4)))
        T = best['T']
        cands.sort(key=lambda c: (-c['fitness'], c['rmse']))
        reg['search'] = {
            'coarse_voxel': coarse_voxel, 'tried': len(cands),
            'best': {'yaw0': best['yaw0'], 'start': best['start'], 'fitness': r6(best['fitness']),
                     'rmse': r6(best['rmse'])},
            'top': [{'yaw0': c['yaw0'], 'start': c['start'], 'fitness': r6(c['fitness']),
                     'rmse': r6(c['rmse'] if np.isfinite(c['rmse']) else None),
                     **{k: round(v, 3) for k, v in ev.decompose(c['T']).items() if k in ('x', 'y', 'z', 'yaw')}}
                    for c in cands[:8]],
        }
        print(f'search: {len(cands)} candidates, best yaw0 {best["yaw0"]} ({best["start"]}) fitness '
              f'{best["fitness"]:.3f} rmse {best["rmse"]:.3f} ({time.time() - t0:.1f} s)', flush=True)
    else:
        T = np.eye(4)
    reg['initial'] = T.tolist()

    # ---- 3. fine registration
    emit(phase='registering', progress=0.63)
    tgt, src = ev.Prepared(ref, p['reg_voxel'], threads), ev.Prepared(est, p['reg_voxel'], threads)
    if p['refine']:
        r = ev.align(tgt, src, T, p['method'], p['max_corr'], threads, p['max_iterations'])
        T = np.asarray(r.T_target_source)
        reg.update(converged=bool(r.converged), iterations=int(r.iterations), num_inliers=int(r.num_inliers),
                   error=r6(r.error))
    else:
        reg.update(converged=None, iterations=0, num_inliers=None, error=None)
    frac, rmse = ev.fitness(tgt, src, T, p['max_corr'], threads)
    reg.update(transform=[[r6(v) for v in row] for row in T], pose={k: round(v, 6) for k, v in ev.decompose(T).items()},
               inlier_fraction=r6(frac), rmse=r6(rmse) if np.isfinite(rmse) else None,
               reg_points={'reference': int(len(tgt.points)), 'estimate': int(len(src.points))},
               max_corr=p['max_corr'], reg_voxel=p['reg_voxel'])
    del tgt, src
    timing['register'] = round(time.time() - t0, 2)
    print(f'registration: inliers {frac:.3f} rmse {rmse:.4f} pose {reg["pose"]} ({timing["register"]} s)', flush=True)

    # ---- 4. distances and metrics
    t0 = time.time()
    emit(phase='evaluating', progress=0.75)
    est_t = ev.transform(est, T)
    d_est = ev.nn_distances(small_gicp.KdTree(ref, num_threads=threads), est_t, threads)
    emit(phase='evaluating', progress=0.83)
    d_ref = ev.nn_distances(small_gicp.KdTree(est_t, num_threads=threads), ref, threads)
    crop = None
    if p['crop_overlap']:
        cell = max(1.0, p['max_dist'])
        m_est, m_ref = ev.overlap_masks(est_t, ref, cell)
        crop = {'cell': cell, 'estimate_kept': int(m_est.sum()), 'reference_kept': int(m_ref.sum())}
        de, dr = d_est[m_est], d_ref[m_ref]
    else:
        de, dr = d_est, d_ref
    if not len(de) or not len(dr):
        fail(job, PLUGIN, 'no_overlap')
    met = ev.metrics(de, dr, p['tau'], p['max_dist'])
    met['crop'] = crop
    hist = ev.histogram(de, p['max_dist'])
    timing['evaluate'] = round(time.time() - t0, 2)

    # ---- 5. outputs
    t0 = time.time()
    emit(phase='rendering', progress=0.9)
    vmax = p['color_max'] or float(min(p['max_dist'], max(2 * p['tau'], met['accuracy']['p95'] or p['tau'])))
    image = ev.render_top(est_t, d_est, ref, vmax, os.path.join(job, 'error_top.png'), min_pixel=2 * p['eval_voxel'])
    emit(phase='writing', progress=0.95)
    write_pcd_xyzi(os.path.join(job, 'aligned.pcd'), est_t, np.minimum(d_est, 1e6))
    timing['outputs'] = round(time.time() - t0, 2)
    timing['total'] = round(time.time() - t_start, 2)

    warnings = []
    if frac < 0.5:
        warnings.append('low_inliers')
    if p['refine'] and not reg['converged']:
        warnings.append('not_converged')
    if p['tau'] < 2 * p['eval_voxel']:
        warnings.append('tau_small')
    if met['accuracy']['beyond'] > 0.3 * len(de):
        warnings.append('partial_overlap')

    def rounded(o):
        if isinstance(o, float):
            return round(o, 6)
        if isinstance(o, dict):
            return {k: rounded(v) for k, v in o.items()}
        if isinstance(o, list):
            return [rounded(v) for v in o]
        return o

    result = {
        'estimate': {k: p['estimate'][k] for k in ('kind', 'name', 'points', 'eval_points')},
        'reference': {k: p['reference'][k] for k in ('kind', 'name', 'points', 'eval_points')},
        'params': {k: p[k] for k in ('init', 'method', 'refine', 'reg_voxel', 'max_corr', 'max_iterations', 'yaw_step',
                                     'eval_voxel', 'tau', 'max_dist', 'crop_overlap', 'color_max', 'x', 'y', 'z',
                                     'yaw')},
        'threads': threads,
        'registration': reg,
        'metrics': met,
        'histogram': hist,
        'image': image,
        'files': {'image': 'error_top.png', 'aligned': 'aligned.pcd'},
        'warnings': warnings,
        'seconds': timing,
    }
    with open(os.path.join(job, 'result.json'), 'w', encoding='utf-8') as f:
        json.dump(rounded(result), f)
    print(f'done: F({p["tau"]}) {met["fscore"]:.4f} accuracy mean {met["accuracy"]["mean"]} '
          f'chamfer {met["chamfer"]["value"]} in {timing["total"]} s', flush=True)


if __name__ == '__main__':
    main()
