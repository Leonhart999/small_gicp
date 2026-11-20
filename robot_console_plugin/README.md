# Console plugin built on this fork

This folder holds a plugin for the SLAM & AutoNav console (robot_console, a FastAPI + Vue 3 web console for ROS 2
Humble SLAM and navigation) that is built on small_gicp:

- [`map_eval/`](map_eval/README.md): **map quality evaluation**. Registers a SLAM map onto a reference cloud
  (another map or an uploaded PCD/PLY) with small_gicp and reports accuracy, completeness, precision, F-score and
  Chamfer distance, with an error image and histogram.

The plugin uses the `small_gicp` Python package from PyPI (`small_gicp==1.0.1`), installed by the console into
the plugin's own venv; it does not need this repository's C++ build.

## Install

- From the console's **plugin market** (Plugins page), or
- copy `map_eval/` into a console plugin folder (default `~/robot_console_data/plugins/`), press *Rescan* on the
  *Plugins* page, enable the plugin, press **Install packages** and restart the console.

## Develop

- The page is a standalone Vite project in `map_eval/web/` (`src/shared/` is a copy of the console's shared
  plugin kit). Build it into `map_eval/ui/` (committed, so users need no Node.js):

  ```bash
  cd map_eval/web && npm install && npm run build
  ```

- Tests (in the plugin's venv, with pytest installed there):
  `<data_root>/plugin_envs/map_eval/bin/python -m pytest map_eval/tests`

## Package for the market

```bash
python3 package.py map_eval            # needs PyYAML; the frontend must be built
```

writes `dist/map_eval-<version>.zip` (reproducible, without `web/node_modules` and caches) and
`dist/map_eval-<version>.entry.json`, the entry for the market index; the zip is attached to the release
`console-map_eval-v<version>` of this repository.
