"""Map, scan and trajectory files of the console, read with numpy only (runs in a plugin's own venv).

The same module is shipped in the console's ground_seg and dynamic_removal plugins so each plugin folder stands
alone; map_eval adds the functions at the end (chunked reading of large PCD / PLY clouds, x y z intensity output).

  read_pcd_xyz / write_pcd_xyz   PCD (ascii, binary) <-> (N, 3) float32
  cloud_chunks / cloud_points    PCD (ascii, binary) or PLY (ascii, binary) -> (n, 3) float64 chunks, memory-mapped
  write_pcd_xyzi                 (N, 3) + (N,) -> binary PCD with x y z intensity
  read_tum                       TUM trajectory: stamps (N,), poses (N, 7) = x y z qx qy qz qw
  ScanRecording                  scans.bin + index.csv written by the console's scan recorder
                                 (fastlio_loop_record.py: float32 x y z intensity per point, body frame)
  read_grid                      map.yaml + image -> geometry and the 0/205/254 cells as map_server reads them
  rotations                      quaternions (x y z w) -> rotation matrices
"""
import json
import os
import sys

import numpy as np
import yaml

OCCUPIED, UNKNOWN, FREE = 0, 205, 254


def emit(**msg):
    """A progress line for the console's job runner."""
    print(json.dumps(msg), flush=True)


def fail(job, plugin, code, **params):
    """Stop the job with a coded error, translated by the plugin's messages (errors.<code>)."""
    with open(os.path.join(job, 'error.json'), 'w', encoding='utf-8') as f:
        json.dump({'code': f'plugin.{plugin}.{code}', 'params': params}, f)
    sys.exit(1)


def read_pcd_xyz(path):
    header = {}
    with open(path, 'rb') as f:
        for _ in range(64):
            line = f.readline()
            if not line:
                break
            parts = line.decode('ascii', 'replace').strip().split()
            if not parts or parts[0].startswith('#'):
                continue
            header[parts[0].upper()] = parts[1:]
            if parts[0].upper() == 'DATA':
                break
        fields = header['FIELDS']
        sizes = [int(v) for v in header['SIZE']]
        types = header['TYPE']
        counts = [int(v) for v in header.get('COUNT', ['1'] * len(fields))]
        n = int(header['POINTS'][0])
        kind = header['DATA'][0].lower()
        if kind == 'binary':
            names = {}
            descr = []
            for i, (name, size, typ, count) in enumerate(zip(fields, sizes, types, counts)):
                key = name if name not in names and name != '_' else f'_pad{i}'
                names[key] = True
                code = {'F': 'f', 'I': 'i', 'U': 'u'}[typ] + str(size)
                descr.append((key, '<' + code, (count,)) if count > 1 else (key, '<' + code))
            dtype = np.dtype(descr)
            data = np.frombuffer(f.read(n * dtype.itemsize), dtype=dtype, count=n)
            xyz = np.column_stack([data['x'], data['y'], data['z']]).astype(np.float32)
        elif kind == 'ascii':
            cols = np.cumsum([0] + counts[:-1])
            use = [int(cols[fields.index(k)]) for k in ('x', 'y', 'z')]
            xyz = np.loadtxt(f, dtype=np.float32, usecols=use, ndmin=2, max_rows=n)
        else:
            raise ValueError(f'PCD DATA {kind} is not supported (only ascii and binary)')
    return xyz[np.isfinite(xyz).all(axis=1)]


def write_pcd_xyz(path, xyz):
    xyz = np.ascontiguousarray(xyz, dtype='<f4')
    n = len(xyz)
    header = (f'# .PCD v0.7 - Point Cloud Data file format\nVERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\n'
              f'COUNT 1 1 1\nWIDTH {n}\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS {n}\nDATA binary\n')
    with open(path, 'wb') as f:
        f.write(header.encode('ascii'))
        f.write(xyz.tobytes())


def read_tum(path):
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 8 and not parts[0].startswith('#'):
                try:
                    rows.append([float(v) for v in parts[:8]])
                except ValueError:
                    continue
    a = np.array(rows, dtype=np.float64).reshape(-1, 8)
    return a[:, 0], a[:, 1:8]


def rotations(q):
    """(N, 4) quaternions x y z w -> (N, 3, 3) rotation matrices (normalised first)."""
    q = np.asarray(q, dtype=np.float64)
    q = q / np.linalg.norm(q, axis=1, keepdims=True)
    x, y, z, w = q[:, 0], q[:, 1], q[:, 2], q[:, 3]
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], axis=1),
        np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], axis=1),
        np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], axis=1),
    ], axis=1)


class ScanRecording:
    """Scans of a recording with the poses of the map's trajectory (matched by time stamp)."""

    def __init__(self, recording, trajectory, max_dt=0.005):
        idx = np.loadtxt(os.path.join(recording, 'index.csv'), dtype=np.float64, ndmin=2)
        if idx.size == 0:
            raise ValueError('the scan recording is empty')
        size = os.path.getsize(os.path.join(recording, 'scans.bin')) // 16
        idx = idx[(idx[:, 1] + idx[:, 2]) <= size]           # a scan cut off at the end of the file is skipped
        self.points = np.memmap(os.path.join(recording, 'scans.bin'), dtype='<f4', mode='r', shape=(size, 4))
        stamps, poses = read_tum(trajectory)
        if not len(stamps):
            raise ValueError('the trajectory is empty')
        order = np.argsort(stamps)
        stamps, poses = stamps[order], poses[order]
        pos = np.clip(np.searchsorted(stamps, idx[:, 0]), 1, len(stamps) - 1) if len(stamps) > 1 else \
            np.zeros(len(idx), dtype=int)
        if len(stamps) > 1:
            left = np.abs(stamps[pos - 1] - idx[:, 0]) <= np.abs(stamps[pos] - idx[:, 0])
            pos = np.where(left, pos - 1, pos)
        ok = np.abs(stamps[pos] - idx[:, 0]) <= max_dt
        self.index = idx[ok]
        self.poses = poses[pos[ok]]
        self.unmatched = int((~ok).sum())

    def __len__(self):
        return len(self.index)

    def scan(self, i):
        """(points (n, 4) float32 in the body frame, pose x y z qx qy qz qw)"""
        off, n = int(self.index[i, 1]), int(self.index[i, 2])
        return np.asarray(self.points[off:off + n]), self.poses[i]


def read_grid(map_yaml):
    """Geometry and cells of a map: dict(res, origin, width, height) and the (h, w) uint8 array of
    0 / 205 / 254 as nav2 map_server reads the image with this map.yaml (row 0 = top = largest y)."""
    from PIL import Image
    with open(map_yaml, encoding='utf-8') as f:
        info = yaml.safe_load(f)
    image = os.path.join(os.path.dirname(map_yaml), info['image'])
    with Image.open(image) as im:
        raw = np.asarray(im.convert('L')).astype(np.float32)
    p = raw / 255.0 if int(info.get('negate', 0)) else (255.0 - raw) / 255.0
    cells = np.full(raw.shape, UNKNOWN, dtype=np.uint8)
    cells[p > float(info.get('occupied_thresh', 0.65))] = OCCUPIED
    cells[p < float(info.get('free_thresh', 0.25))] = FREE
    h, w = cells.shape
    return {'res': float(info['resolution']), 'origin': [float(v) for v in info['origin'][:2]],
            'width': w, 'height': h}, cells


def cell_index(xy, grid):
    """Flat cell index (row 0 = bottom = smallest y) of every point and a mask of the points inside the grid."""
    cx = np.floor((xy[:, 0] - grid['origin'][0]) / grid['res']).astype(np.int64)
    cy = np.floor((xy[:, 1] - grid['origin'][1]) / grid['res']).astype(np.int64)
    inside = (cx >= 0) & (cx < grid['width']) & (cy >= 0) & (cy < grid['height'])
    return cy * grid['width'] + cx, inside


def save_png(path, array):
    from PIL import Image
    Image.fromarray(np.ascontiguousarray(array)).save(path)

# ---------------------------------------------------------------- map_eval additions

PLY_TYPES = {'char': 'i1', 'int8': 'i1', 'uchar': 'u1', 'uint8': 'u1', 'short': 'i2', 'int16': 'i2',
             'ushort': 'u2', 'uint16': 'u2', 'int': 'i4', 'int32': 'i4', 'uint': 'u4', 'uint32': 'u4',
             'float': 'f4', 'float32': 'f4', 'double': 'f8', 'float64': 'f8'}


def _pcd_layout(path):
    """(kind, point count, numpy dtype of a binary row or None, columns of x y z for ascii, data offset)"""
    header = {}
    with open(path, 'rb') as f:
        for _ in range(64):
            line = f.readline()
            if not line:
                break
            parts = line.decode('ascii', 'replace').strip().split()
            if not parts or parts[0].startswith('#'):
                continue
            header[parts[0].upper()] = parts[1:]
            if parts[0].upper() == 'DATA':
                break
        offset = f.tell()
    if 'DATA' not in header:
        raise ValueError('not a PCD file (no DATA line)')
    fields = header.get('FIELDS', [])
    if not {'x', 'y', 'z'} <= set(fields):
        raise ValueError('the PCD file has no x y z fields')
    sizes = [int(v) for v in header['SIZE']]
    types = header['TYPE']
    counts = [int(v) for v in header.get('COUNT', ['1'] * len(fields))]
    n = int(header['POINTS'][0])
    kind = (header['DATA'] or ['?'])[0].lower()
    if kind == 'binary':
        descr, seen = [], set()
        for i, (name, size, typ, count) in enumerate(zip(fields, sizes, types, counts)):
            key = name if name not in seen and name != '_' else f'_pad{i}'
            seen.add(key)
            code = '<' + {'F': 'f', 'I': 'i', 'U': 'u'}[typ] + str(size)
            descr.append((key, code, (count,)) if count > 1 else (key, code))
        dtype = np.dtype(descr)
        need = offset + n * dtype.itemsize
        if os.path.getsize(path) < need:
            raise ValueError(f'the PCD file is truncated ({os.path.getsize(path)} of {need} bytes)')
        return kind, n, dtype, None, offset
    if kind == 'ascii':
        cols = np.cumsum([0] + counts[:-1])
        return kind, n, None, [int(cols[fields.index(k)]) for k in ('x', 'y', 'z')], offset
    raise ValueError(f'PCD DATA {kind} is not supported (save the cloud as binary or ascii)')


def _ply_layout(path):
    """(format, vertex count, dtype of a binary vertex row or None, x y z columns for ascii, offset of the
    vertex data, rows to skip before the vertices for ascii)"""
    with open(path, 'rb') as f:
        if f.readline().strip() != b'ply':
            raise ValueError('not a PLY file')
        fmt, elements = None, []
        for _ in range(4096):
            line = f.readline()
            if not line:
                raise ValueError('no end_header')
            parts = line.decode('ascii', 'replace').split()
            if not parts:
                continue
            if parts[0] == 'format' and len(parts) > 1:
                fmt = parts[1]
            elif parts[0] == 'element' and len(parts) > 2:
                elements.append({'name': parts[1], 'count': int(parts[2]), 'props': []})
            elif parts[0] == 'property' and elements:
                elements[-1]['props'].append(parts[1:])
            elif parts[0] == 'end_header':
                break
        offset = f.tell()
    if fmt not in ('ascii', 'binary_little_endian', 'binary_big_endian'):
        raise ValueError(f'PLY format {fmt} is not supported')
    endian = '<' if fmt == 'binary_little_endian' else '>'
    skip_bytes, skip_rows = 0, 0
    for el in elements:
        lists = [p for p in el['props'] if p[0] == 'list']
        if el['name'] == 'vertex':
            if lists:
                raise ValueError('PLY vertex elements with list properties are not supported')
            names = [p[-1] for p in el['props']]
            if not {'x', 'y', 'z'} <= set(names):
                raise ValueError('the PLY vertex element has no x y z')
            if fmt == 'ascii':
                return fmt, el['count'], None, [names.index(k) for k in ('x', 'y', 'z')], offset, skip_rows
            try:
                dtype = np.dtype([(p[-1], endian + PLY_TYPES[p[0]]) for p in el['props']])
            except KeyError as e:
                raise ValueError(f'PLY property type {e} is not supported')
            need = offset + skip_bytes + el['count'] * dtype.itemsize
            if os.path.getsize(path) < need:
                raise ValueError(f'the PLY file is truncated ({os.path.getsize(path)} of {need} bytes)')
            return fmt, el['count'], dtype, None, offset + skip_bytes, skip_rows
        if lists:
            raise ValueError('PLY elements with list properties before the vertices are not supported')
        skip_rows += el['count']
        skip_bytes += el['count'] * sum(np.dtype(PLY_TYPES[p[0]]).itemsize for p in el['props'])
    raise ValueError('the PLY file has no vertex element')


def cloud_points(path):
    """Point count in the header of a PCD or PLY file (ValueError when the file cannot be read)."""
    return (_ply_layout(path) if path.lower().endswith('.ply') else _pcd_layout(path))[1]


def cloud_chunks(path, chunk=4_000_000):
    """Yield the finite x y z points of a PCD or PLY file as (n, 3) float64 arrays of at most `chunk` points.
    Binary files are memory-mapped, so a cloud of tens of millions of points is never read into memory whole."""
    if path.lower().endswith('.ply'):
        fmt, n, dtype, cols, offset, skip = _ply_layout(path)
        binary = fmt != 'ascii'
    else:
        fmt, n, dtype, cols, offset = _pcd_layout(path)
        binary, skip = fmt == 'binary', 0
    if n <= 0:
        return
    if binary:
        rows = np.memmap(path, dtype=dtype, mode='r', offset=offset, shape=(n,))
        for i in range(0, n, chunk):
            part = rows[i:i + chunk]
            xyz = np.column_stack([part['x'], part['y'], part['z']]).astype(np.float64)
            yield xyz[np.isfinite(xyz).all(axis=1)]
        return
    import itertools
    with open(path, 'rb') as f:
        f.seek(offset)
        for _ in range(skip):
            f.readline()
        left = n
        while left > 0:
            lines = list(itertools.islice(f, min(chunk, left)))
            if not lines:
                break
            left -= len(lines)
            xyz = np.loadtxt(lines, dtype=np.float64, usecols=cols, ndmin=2)
            yield xyz[np.isfinite(xyz).all(axis=1)]


def write_pcd_xyzi(path, xyz, intensity):
    """Binary PCD with float32 x y z intensity."""
    n = len(xyz)
    data = np.empty(n, dtype=[('x', '<f4'), ('y', '<f4'), ('z', '<f4'), ('intensity', '<f4')])
    data['x'], data['y'], data['z'] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    data['intensity'] = intensity
    header = (f'# .PCD v0.7 - Point Cloud Data file format\nVERSION 0.7\nFIELDS x y z intensity\nSIZE 4 4 4 4\n'
              f'TYPE F F F F\nCOUNT 1 1 1 1\nWIDTH {n}\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS {n}\n'
              f'DATA binary\n')
    with open(path, 'wb') as f:
        f.write(header.encode('ascii'))
        f.write(data.tobytes())
