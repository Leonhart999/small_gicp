"""Map quality evaluation plugin backend (plugin interface v1).

Routes under /api/ext/map_eval/:
  GET    maps                         folder maps with a point cloud (estimate or reference)
  GET    references                   reference clouds uploaded to this plugin (PCD / PLY)
  POST   references?name=             upload a reference cloud, streamed straight into <data_dir>/references
                                      (size limit: setting max_upload_mb): multipart/form-data with one file
                                      field `file` (name optional, default the file name), or the raw file as
                                      the body (name required), e.g. curl --data-binary @ref.pcd
  DELETE references/{name}            delete an uploaded reference cloud
  POST   run {estimate, reference: {kind: map|file, name}, params}
                                      start an evaluation job (worker.py in the plugin's venv)
Results, images and clouds come from the generic job routes (/jobs, /jobs/{id}/files/...).

small_gicp and NumPy are used only by the worker; this module runs inside the console and needs neither.
"""
import asyncio
import os
import re
import shutil
import time
import uuid

from fastapi import Body, Request

MAP_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
REF_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.(pcd|ply)$')
MIB = 1024 * 1024
MIN_FREE = 512 * MIB            # bytes left on the disk after an upload
HEADER_BYTES = 65536            # a PCD/PLY header must fit into this many bytes
# key: (default, min, max)
LIMITS = {
    'reg_voxel': (0.25, 0.05, 2.0), 'max_corr': (1.0, 0.05, 10.0), 'eval_voxel': (0.05, 0.01, 0.5),
    'tau': (0.1, 0.01, 2.0), 'max_dist': (1.0, 0.05, 10.0), 'color_max': (0.0, 0.0, 10.0),
    'x': (0.0, -10000.0, 10000.0), 'y': (0.0, -10000.0, 10000.0), 'z': (0.0, -10000.0, 10000.0),
    'yaw': (0.0, -180.0, 180.0),
}
INT_LIMITS = {'yaw_step': (30, 5, 90), 'max_iterations': (50, 5, 200)}
CHOICES = {'init': ('identity', 'manual', 'search'), 'method': ('GICP', 'VGICP')}
BOOLS = {'refine': True, 'crop_overlap': False}
PLY_SIZES = {'char': 1, 'int8': 1, 'uchar': 1, 'uint8': 1, 'short': 2, 'int16': 2, 'ushort': 2, 'uint16': 2,
             'int': 4, 'int32': 4, 'uint': 4, 'uint32': 4, 'float': 4, 'float32': 4, 'double': 8, 'float64': 8}
CLOUD_FILES = ('map.pcd', 'map_voxel.pcd')     # cloud of a console map, densest first


def cloud_header(path, kind=None):
    """Format and point count of a PCD or PLY file (kind 'pcd' / 'ply', default: from the file name) from its
    header (pure Python). Binary files are checked to be as long as the header says. ValueError when the file
    cannot be evaluated."""
    with open(path, 'rb') as f:
        head = f.read(HEADER_BYTES)
    size = os.path.getsize(path)
    if (kind or os.path.splitext(path)[1].lower().lstrip('.')) == 'ply':
        if not head.startswith(b'ply'):
            raise ValueError('not a PLY file')
        end = head.find(b'end_header')
        if end < 0:
            raise ValueError('no end_header in the first 64 KiB')
        offset = head.index(b'\n', end) + 1 if b'\n' in head[end:] else len(head)
        fmt, elements = None, []
        for raw in head[:end].split(b'\n')[1:]:
            parts = raw.decode('ascii', 'replace').split()
            if not parts:
                continue
            if parts[0] == 'format' and len(parts) > 1:
                fmt = parts[1]
            elif parts[0] == 'element' and len(parts) > 2:
                elements.append([parts[1], int(parts[2]), []])
            elif parts[0] == 'property' and elements:
                elements[-1][2].append(parts[1:])
        if fmt not in ('ascii', 'binary_little_endian', 'binary_big_endian'):
            raise ValueError(f'PLY format {fmt} is not supported')
        skip = 0
        for name, count, props in elements:
            fixed = all(p[0] in PLY_SIZES for p in props)
            if name == 'vertex':
                if not {'x', 'y', 'z'} <= {p[-1] for p in props}:
                    raise ValueError('the PLY vertex element has no x y z')
                if not fixed:
                    raise ValueError('PLY vertex elements with list properties are not supported')
                if fmt != 'ascii':
                    need = offset + skip + count * sum(PLY_SIZES[p[0]] for p in props)
                    if size < need:
                        raise ValueError(f'the file is truncated ({size} of {need} bytes)')
                return {'format': f'ply {fmt}', 'points': count}
            if not fixed:
                raise ValueError('PLY elements with list properties before the vertices are not supported')
            skip += count * sum(PLY_SIZES[p[0]] for p in props)
        raise ValueError('the PLY file has no vertex element')
    header, offset = {}, 0
    for raw in head.split(b'\n'):
        offset += len(raw) + 1
        parts = raw.decode('ascii', 'replace').split()
        if not parts or parts[0].startswith('#'):
            continue
        header[parts[0].upper()] = parts[1:]
        if parts[0].upper() == 'DATA':
            break
    else:
        raise ValueError('not a PCD file (no DATA line)')
    if not {'x', 'y', 'z'} <= set(header.get('FIELDS', [])):
        raise ValueError('the PCD file has no x y z fields')
    data = (header['DATA'] or ['?'])[0].lower()
    if data not in ('ascii', 'binary'):
        raise ValueError(f'PCD DATA {data} is not supported (save the cloud as binary or ascii)')
    try:
        points = int(header['POINTS'][0])
    except (KeyError, IndexError, ValueError):
        raise ValueError('the PCD file has no POINTS line')
    if data == 'binary':
        try:
            sizes = [int(v) for v in header['SIZE']]
            counts = [int(v) for v in header.get('COUNT', ['1'] * len(sizes))]
        except (KeyError, ValueError):
            raise ValueError('the PCD file has no valid SIZE / COUNT lines')
        need = offset + points * sum(s * c for s, c in zip(sizes, counts))
        if size < need:
            raise ValueError(f'the file is truncated ({size} of {need} bytes)')
    return {'format': f'pcd {data}', 'points': points}


class UploadError(Exception):
    def __init__(self, status, code, **params):
        super().__init__(code)
        self.status, self.code, self.params = status, code, params


async def stream_multipart_file(request, field, dest, limit):
    """Write the multipart/form-data part named `field` of the request body to `dest` while it arrives (no
    temporary copy). Other parts are skipped. Returns (bytes written, filename of the part)."""
    ctype = request.headers.get('content-type', '')
    m = re.search(r'boundary="?([^";]+)"?', ctype)
    if not ctype.lower().startswith('multipart/form-data') or not m:
        raise UploadError(400, 'bad_upload', reason='expected multipart/form-data')
    delim = b'\r\n--' + m.group(1).encode('latin-1')
    buf = bytearray(b'\r\n')         # so that the first boundary looks like every other one
    state, out, written, filename, found = 'preamble', None, 0, '', False
    keep = len(delim) + 4
    try:
        async for chunk in request.stream():
            buf += chunk
            while True:
                if state == 'preamble':
                    i = buf.find(delim)
                    if i < 0:
                        del buf[:max(0, len(buf) - keep)]
                        break
                    del buf[:i + len(delim)]
                    state = 'after_delim'
                elif state == 'after_delim':
                    if len(buf) < 2:
                        break
                    if buf[:2] == b'--':
                        state = 'done'
                        break
                    if buf[:2] != b'\r\n':
                        raise UploadError(400, 'bad_upload', reason='malformed multipart boundary')
                    del buf[:2]
                    state = 'headers'
                elif state == 'headers':
                    i = buf.find(b'\r\n\r\n')
                    if i < 0:
                        if len(buf) > 16384:
                            raise UploadError(400, 'bad_upload', reason='part headers too long')
                        break
                    headers = bytes(buf[:i]).decode('utf-8', 'replace')
                    del buf[:i + 4]
                    disp = next((h for h in headers.split('\r\n') if h.lower().startswith('content-disposition')), '')
                    name = re.search(r'\bname="([^"]*)"', disp)
                    fname = re.search(r'\bfilename="([^"]*)"', disp)
                    if name and name.group(1) == field and not found:
                        found = True
                        filename = fname.group(1) if fname else ''
                        out = open(dest, 'xb')
                        state = 'file'
                    else:
                        state = 'skip'
                else:                                   # 'file' or 'skip': data up to the next boundary
                    i = buf.find(delim)
                    end = i if i >= 0 else max(0, len(buf) - keep)
                    if state == 'file' and end:
                        written += end
                        if written > limit:
                            raise UploadError(413, 'upload_too_large', limit=limit // MIB)
                        await asyncio.to_thread(out.write, bytes(buf[:end]))
                    if i < 0:
                        del buf[:end]
                        break
                    del buf[:i + len(delim)]
                    if state == 'file':
                        out.close()
                        out = None
                    state = 'after_delim'
            if state == 'done':
                break
        if state != 'done':
            raise UploadError(400, 'bad_upload', reason='the request body ended before the last boundary')
        if not found:
            raise UploadError(400, 'bad_upload', reason=f'no file field "{field}"')
        return written, filename
    except BaseException:
        if out is not None:
            out.close()
        if os.path.exists(dest):
            os.remove(dest)
        raise
    finally:
        if out is not None and not out.closed:
            out.close()


async def stream_raw_body(request, dest, limit):
    """Write the raw request body (e.g. curl --data-binary @cloud.pcd) to `dest` while it arrives."""
    written = 0
    try:
        with open(dest, 'xb') as out:
            async for chunk in request.stream():
                written += len(chunk)
                if written > limit:
                    raise UploadError(413, 'upload_too_large', limit=limit // MIB)
                await asyncio.to_thread(out.write, chunk)
        if not written:
            raise UploadError(400, 'bad_upload', reason='the request body is empty')
        return written
    except BaseException:
        if os.path.exists(dest):
            os.remove(dest)
        raise


def setup(ctx):
    refs_dir = os.path.join(ctx.data_dir, 'references')

    def active_jobs():
        return [j for j in ctx.jobs.list() if j['state'] in ('queued', 'running')]

    async def map_cloud(name):
        if not isinstance(name, str) or not MAP_RE.match(name):
            raise ctx.error(400, 'map_unavailable', name=str(name))
        try:
            d = await ctx.api.get(f'/api/maps/{name}')
        except ctx.ApiCallError as e:
            raise ctx.error(e.status if e.status in (400, 404) else 400, 'map_unavailable', name=name)
        for f in CLOUD_FILES:
            path = os.path.join(d['location'], f)
            if os.path.isfile(path):
                return path
        raise ctx.error(400, 'no_cloud', name=name)

    def reference_file(name):
        if not isinstance(name, str) or not REF_RE.match(name):
            raise ctx.error(400, 'reference_unavailable', name=str(name))
        path = os.path.join(refs_dir, name)
        if not os.path.isfile(path):
            raise ctx.error(404, 'reference_unavailable', name=name)
        return path

    @ctx.router.get('/maps')
    async def maps():
        return [{'name': m['name'], 'created': m.get('created'), 'algorithm': m.get('algorithm'),
                 'points': m.get('points'), 'bag': m.get('bag'), 'extent': m.get('extent')}
                for m in await ctx.api.get('/api/maps') if m.get('kind') == 'folder' and m.get('has_cloud')]

    @ctx.router.get('/references')
    def references():
        out = []
        if os.path.isdir(refs_dir):
            for name in sorted(os.listdir(refs_dir)):
                path = os.path.join(refs_dir, name)
                if not REF_RE.match(name) or not os.path.isfile(path):
                    continue
                st = os.stat(path)
                entry = {'name': name, 'size': st.st_size, 'mtime': st.st_mtime, 'format': None, 'points': None,
                         'error': None}
                try:
                    entry.update(cloud_header(path))
                except (OSError, ValueError) as e:
                    entry['error'] = str(e)
                out.append(entry)
        total, used, free = shutil.disk_usage(refs_dir if os.path.isdir(refs_dir) else ctx.data_dir)
        return {'files': out, 'free': free, 'limit_mb': int(ctx.settings().get('max_upload_mb') or 2048)}

    @ctx.router.post('/references')
    async def upload_reference(request: Request, name: str = ''):
        limit = int(ctx.settings().get('max_upload_mb') or 2048) * MIB
        os.makedirs(refs_dir, exist_ok=True)
        length = request.headers.get('content-length')
        free = shutil.disk_usage(refs_dir).free
        if length and length.isdigit():
            if int(length) > limit + MIB:
                raise ctx.error(413, 'upload_too_large', limit=limit // MIB)
            if int(length) + MIN_FREE > free:
                raise ctx.error(507, 'no_space', free=round(free / 2**30, 2))
        part = os.path.join(refs_dir, f'.upload-{uuid.uuid4().hex}.part')
        multipart = request.headers.get('content-type', '').lower().startswith('multipart/')
        if not multipart and not name:
            raise ctx.error(400, 'bad_reference_name', name='')
        try:
            if multipart:
                size, filename = await stream_multipart_file(request, 'file', part, limit)
            else:
                size, filename = await stream_raw_body(request, part, limit), name
        except UploadError as e:
            raise ctx.error(e.status, e.code, **e.params)
        try:
            base = (name or filename).replace('\\', '/').rsplit('/', 1)[-1].strip()
            stem, ext = os.path.splitext(base)
            ext = ext.lower()
            if ext not in ('.pcd', '.ply'):
                raise ctx.error(400, 'bad_reference_name', name=base)
            stem = re.sub(r'[^A-Za-z0-9_.-]+', '_', stem).strip('._-')[:70] or 'reference'
            final = f'{stem}{ext}'
            if not REF_RE.match(final):
                raise ctx.error(400, 'bad_reference_name', name=base)
            try:
                info = cloud_header(part, ext[1:])
            except (OSError, ValueError) as e:
                raise ctx.error(400, 'bad_reference', reason=str(e))
            try:
                os.link(part, os.path.join(refs_dir, final))      # fails instead of replacing an existing file
            except FileExistsError:
                raise ctx.error(409, 'reference_exists', name=final)
        finally:
            if os.path.exists(part):
                os.remove(part)
        ctx.log.info(f'reference cloud {final} uploaded ({size} bytes, {info["points"]} points)')
        return {'name': final, 'size': size, **info}

    @ctx.router.delete('/references/{name}')
    def delete_reference(name: str):
        path = reference_file(name)
        if any(j['params'].get('reference', {}).get('path') == path for j in active_jobs()):
            raise ctx.error(409, 'reference_in_use', name=name)
        os.remove(path)
        return {'ok': True}

    def parameters(body):
        if not isinstance(body, dict):
            raise ctx.error(400, 'bad_params', reason='params is not an object')
        p = {}
        for key, (default, lo, hi) in LIMITS.items():
            v = body.get(key, default)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ctx.error(400, 'bad_params', reason=f'{key} must be between {lo} and {hi}')
            p[key] = float(v)
        for key, (default, lo, hi) in INT_LIMITS.items():
            v = body.get(key, default)
            if type(v) is not int or not lo <= v <= hi:
                raise ctx.error(400, 'bad_params', reason=f'{key} must be an integer between {lo} and {hi}')
            p[key] = v
        for key, options in CHOICES.items():
            v = body.get(key, options[0])
            if v not in options:
                raise ctx.error(400, 'bad_params', reason=f'{key} must be one of {", ".join(options)}')
            p[key] = v
        for key, default in BOOLS.items():
            v = body.get(key, default)
            if not isinstance(v, bool):
                raise ctx.error(400, 'bad_params', reason=f'{key} must be true or false')
            p[key] = v
        if p['eval_voxel'] > p['tau']:
            raise ctx.error(400, 'bad_params', reason='the evaluation voxel must not be larger than τ')
        if p['tau'] > p['max_dist']:
            raise ctx.error(400, 'bad_params', reason='τ must not be larger than the max distance')
        p['threads'] = int(ctx.settings().get('threads') or 0)
        return p

    @ctx.router.post('/run')
    async def run(body: dict = Body(...)):
        est_name = body.get('estimate')
        est_path = await map_cloud(est_name)
        ref = body.get('reference')
        if not isinstance(ref, dict) or ref.get('kind') not in ('map', 'file'):
            raise ctx.error(400, 'bad_params', reason='reference must be {kind: map|file, name}')
        if ref['kind'] == 'map':
            ref_path = await map_cloud(ref.get('name'))
        else:
            ref_path = reference_file(ref.get('name'))
        if os.path.realpath(ref_path) == os.path.realpath(est_path):
            raise ctx.error(400, 'same_cloud')
        params = parameters(body.get('params') or {})
        params.update(estimate={'kind': 'map', 'name': est_name, 'path': est_path},
                      reference={'kind': ref['kind'], 'name': ref['name'], 'path': ref_path},
                      submitted=time.time())
        return ctx.jobs.submit('evaluate', params, title=f'{est_name} → {ref["name"]}', target=est_name)
