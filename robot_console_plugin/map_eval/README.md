# map_eval: map quality evaluation for the SLAM & AutoNav console

A plugin for the SLAM & AutoNav console (robot_console, plugin interface v1). It compares a point-cloud map built
by SLAM (the *estimate*: a console map's `map.pcd`) with a *reference* cloud (another console map, or an uploaded
PCD/PLY cloud such as a laser-scanner ground truth): it registers the two clouds with
[small_gicp](https://github.com/koide3/small_gicp) and computes accuracy, completeness, precision, F-score and
Chamfer distance, with a top view coloured by the error and an error histogram.

The console already reports trajectory errors (ATE/RPE); this plugin measures the map itself.

## Install

From the console's plugin market, or copy this folder (`map_eval/`) into a plugin folder of the console (default
`~/robot_console_data/plugins/`), press *Rescan* on the *Plugins* page, enable the plugin, press **Install
packages** (creates the plugin's own venv with `small_gicp==1.0.1`, NumPy, Pillow, PyYAML) and restart the
console. The page is *Map evaluation* in the side menu (`#/ext/map_eval/main`).

Settings (Plugins page): `threads` (registration and nearest-neighbour threads, 0 = all cores) and
`max_upload_mb` (upload limit for reference clouds, default 2048).

## How a job runs

1. **Read and downsample.** Both clouds are read in chunks of 4 M points (binary files are memory-mapped) and
   voxel-downsampled (centroid per voxel, `small_gicp.voxelgrid_sampling`) to the **evaluation voxel**
   (default 0.05 m). Every metric is computed on these two clouds, so different point densities of the two maps
   do not bias the result.
2. **Initial guess** of the transform *T* that maps estimate points into the reference frame:
   - `identity`: both maps are already in the same frame (e.g. built from the same bag with the same start);
   - `manual`: x, y, z, yaw given by the user;
   - `search`: the clouds are downsampled to a coarse voxel (max(1 m, 2 × registration voxel)); for every yaw in
     steps of `yaw_step` (default 30°), starting once with the centroids matched and once with zero translation,
     two GICP/VGICP runs with shrinking correspondence distances (max(5, 4 · max_corr), then max(2, 2 · max_corr))
     are made. Each candidate is scored by its inlier fraction (share of estimate points with a reference point
     within `max_corr`); among the candidates within 0.01 of the best, the one with the smallest inlier RMSE wins.
3. **Fine registration** (`refine`, on by default): GICP (or VGICP) of the estimate onto the reference at the
   **registration voxel** (default 0.25 m, 10 neighbours for the covariances) with `max_corr` (default 1 m) and
   `max_iterations` (default 50). With `refine` off the initial guess is used as it is.
4. **Distances.** With small_gicp KdTrees, the nearest-neighbour distance of every (aligned) estimate point to the
   reference, d(e → R), and of every reference point to the estimate, d(r → E).
5. **Metrics, histogram, image, outputs** (below).

## Metrics

Let *E* be the aligned estimate cloud and *R* the reference cloud (both at the evaluation voxel), τ the
threshold (default 0.1 m) and *D* the truncation distance (default 1 m).

| Metric | Definition |
|---|---|
| Accuracy (mean, median, RMSE, 95th percentile) | statistics of d(e → R) over the estimate points with d(e → R) ≤ D. Points farther than D are taken as outside the overlap and counted separately (`beyond`). |
| Precision P(τ) | share of estimate points with d(e → R) < τ |
| Completeness C(τ) | share of reference points with d(r → E) < τ |
| F-score F(τ) | 2 P C / (P + C) |
| Inlier RMSE(τ) | RMSE of d(e → R) over the estimate points with d(e → R) < τ (the accuracy of MapEval at threshold τ) |
| Chamfer distance | ½ (mean over E of min(d(e → R), D) + mean over R of min(d(r → E), D)); the number of truncated points of each direction is reported |

P, C, F and the inlier RMSE are also reported for τ ∈ {0.05, 0.1, 0.2, 0.5} m plus the chosen τ.

The definitions follow MapEval / Cloud_Map_Evaluation:
X. Hu, J. Wu, M. Jia, H. Yan, Y. Jiang, B. Jiang, W. Zhang, W. He, P. Tan, "MapEval: Towards Unified, Robust and
Efficient SLAM Map Evaluation Framework", *IEEE Robotics and Automation Letters*, 2025
([paper](https://ieeexplore.ieee.org/document/10910156),
[code](https://github.com/JokerJohn/Cloud_Map_Evaluation)). This plugin is an independent implementation from
the definitions; it contains no code of that repository (which has no licence file). MapEval's structure-based
metrics (MME, AWD, SCS) are not implemented.

### Choosing the parameters

- Keep τ at least twice the evaluation voxel (the job warns otherwise): two clouds of the same surface,
  downsampled independently, have centroids up to about half a voxel apart even when the map is perfect.
- **Partial overlap.** When the reference covers only a part of the mapped area (or the other way round),
  completeness and precision drop for the parts that have no counterpart, although nothing is wrong there. The
  truncation distance D keeps those parts from dominating the averages: they are left out of the accuracy
  statistics (reported as `beyond`) and contribute at most D each to the Chamfer distance (the truncated counts
  tell how many). P, C and F are still computed over all points; switch on **Overlap only** to compute every
  metric only on the points in horizontal cells (size max(1 m, D), each cloud's footprint grown by one cell)
  that both clouds occupy. The job warns when more than 30 % of the estimate points are beyond D.
- **Deformed maps.** A map with drift cannot be aligned rigidly everywhere at once; the registration then fits
  the parts with most points and the error concentrates elsewhere (visible in the error image). Different
  registrations (GICP vs VGICP, other initial guesses) can then give different but equally valid alignments,
  and the metrics depend on it; report the registration settings with the numbers.
- A registration inlier fraction below 50 % usually means that the registration failed (the job warns): try the
  `search` initial guess, a manual guess or a larger `max_corr`.

## Outputs (job folder, downloadable from the page)

- `result.json`: the estimate and reference (names, input and evaluation point counts), all parameters, the
  registration (`initial` and final 4 × 4 `transform` mapping estimate → reference, x y z roll pitch yaw,
  inlier fraction and RMSE at `max_corr`, small_gicp's converged / iterations / inliers / error, search
  candidates), the metrics, the τ table, a 50-bin histogram of d(e → R) from 0 to D (plus the overflow count), the
  colour scale of the image (0 … `vmax`, colour stops) and timings.
- `error_top.png`: top view; reference points light grey, estimate points coloured by the mean d(e → R) of the
  points around each pixel (3 × 3 pixels; pixel ≥ 2 × the evaluation voxel), colour bar at the bottom. The colour
  scale ends at `color_max`, or automatically at the larger of the 95th percentile of the accuracy and 2τ (at
  most D).
- `aligned.pcd`: the estimate evaluation cloud in the reference frame, binary PCD with x y z intensity, intensity
  = d(e → R) in metres (open it in CloudCompare and colour by the scalar field).

## Parameters (`POST /api/ext/map_eval/run`)

```json
{"estimate": "<map>", "reference": {"kind": "map", "name": "<map>"},
 "params": {"init": "identity", "x": 0, "y": 0, "z": 0, "yaw": 0, "yaw_step": 30, "method": "GICP",
            "refine": true, "reg_voxel": 0.25, "max_corr": 1.0, "max_iterations": 50,
            "eval_voxel": 0.05, "tau": 0.1, "max_dist": 1.0, "color_max": 0, "crop_overlap": false}}
```

`reference.kind` is `map` (a console map) or `file` (an uploaded reference cloud). Other routes:
`GET maps`, `GET references`, `POST references` (multipart field `file`, or the raw file as the body with
`?name=<file>.pcd|.ply`, e.g. `curl --data-binary @gt.pcd ".../references?name=gt.pcd"`),
`DELETE references/{name}`, and the console's generic job routes (`GET jobs`, `GET jobs/{id}/files/{name}`, ...).
Reference clouds are kept in `<data_root>/plugin_data/map_eval/references/`.

Supported clouds: PCD with `DATA ascii` or `binary` (not `binary_compressed`: save it as binary, e.g. with
CloudCompare or `pcl_convert_pcd_ascii_binary`), PLY ascii / binary little / big endian with a `vertex` element
having x y z (no list properties). Up to 25 M points per cloud after downsampling to the evaluation voxel.

## Measured (HEU61 real-robot data and simulation; WSL2, 12 threads)

| Estimate → reference | Points (input → eval) | Time |
|---|---|---|
| HEU61 FAST-LIO ground 33.3 M → loop-closed 11.0 M | 7.3 M / 11.0 M | 13 s (identity), 34 s (search) |
| warehouse sim 2.8 M → 1.1 M | 1.2 M / 0.6 M | 1.7 s |

Peak memory of the 33 M-point job: about 1.7 GB.

## Files

```
plugin.yaml          manifest (interface v1)
backend/__init__.py  routes in the console process (no NumPy / small_gicp there)
worker.py            the job (runs in the plugin's venv)
evaluation.py        registration, metrics, histogram and image (NumPy + small_gicp)
mapio.py             PCD / PLY readers and writers (shared with the console's tool plugins, plus additions)
tests/               pytest: synthetic scene with known transform + noise, metric definitions, file I/O
web/                 page source (Vue 3, built into ui/ with npm run build)
ui/                  built page (plugin.js, plugin.css)
```

Tests: `<data_root>/plugin_envs/map_eval/bin/python -m pip install pytest`, then
`<data_root>/plugin_envs/map_eval/bin/python -m pytest map_eval/tests`.

## Licences

- This plugin: MIT, see `LICENSE` in this folder.
- [small_gicp](https://github.com/koide3/small_gicp) by Kenji Koide: MIT, installed from PyPI (`small_gicp==1.0.1`)
  into the plugin's venv. NumPy (BSD-3), Pillow (MIT-CMU), PyYAML (MIT).
- Metric definitions: MapEval (Hu et al., IEEE RA-L 2025), cited above; no code reused.
