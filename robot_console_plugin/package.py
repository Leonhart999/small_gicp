#!/usr/bin/env python3
"""Package a SLAM & AutoNav console plugin of this folder for the plugin market.

    python3 package.py <plugin id> [--release-base URL]

Writes into dist/:
  <id>-<version>.zip         the plugin folder <id>/ (without web/node_modules, caches and test files),
                             byte-for-byte reproducible: fixed time stamps, sorted entries
  <id>-<version>.entry.json  the entry for index.json of github.com/Leonhart999/slam-console-plugins
                             (id, version, api, texts, requirements, url, sha256, size)

The release base is the URL the zip is attached to, by default
https://github.com/<owner>/<repo>/releases/download/console-<id>-v<version> of this repository's origin.
Needs PyYAML.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {'node_modules', '__pycache__', '.pytest_cache', '.vite', 'dist'}
SKIP_SUFFIXES = ('.pyc', '.pyo', '.log')
STAMP = (2020, 1, 1, 0, 0, 0)


def origin_repo():
    try:
        url = subprocess.run(['git', '-C', HERE, 'remote', 'get-url', 'origin'], capture_output=True, text=True,
                             check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    m = re.search(r'github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$', url)
    return f'{m.group(1)}/{m.group(2)}' if m else None


def files_of(folder):
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith('.'))
        for name in sorted(files):
            if name.endswith(SKIP_SUFFIXES) or name.startswith('.'):
                continue
            full = os.path.join(root, name)
            if os.path.islink(full):
                sys.exit(f'symbolic links are not allowed in a plugin: {full}')
            out.append(full)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('id')
    ap.add_argument('--release-base', default='')
    args = ap.parse_args()
    folder = os.path.join(HERE, args.id)
    with open(os.path.join(folder, 'plugin.yaml'), encoding='utf-8') as f:
        m = yaml.safe_load(f)
    if m.get('id') != args.id:
        sys.exit(f'plugin.yaml id {m.get("id")!r} differs from the folder name {args.id!r}')
    fe = m.get('frontend') or {}
    if fe and not os.path.isfile(os.path.join(folder, fe.get('dir', 'ui'), fe.get('script', 'plugin.js'))):
        sys.exit('the frontend is not built: run npm run build in web/ first')
    version = str(m['version'])
    dist = os.path.join(HERE, 'dist')
    os.makedirs(dist, exist_ok=True)
    zpath = os.path.join(dist, f'{args.id}-{version}.zip')
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for full in files_of(folder):
            rel = os.path.relpath(full, HERE).replace(os.sep, '/')
            info = zipfile.ZipInfo(rel, STAMP)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(full, 'rb') as f:
                z.writestr(info, f.read())
    data = open(zpath, 'rb').read()
    sha = hashlib.sha256(data).hexdigest()
    base = args.release_base
    if not base:
        repo = origin_repo()
        if not repo:
            sys.exit('no GitHub origin: pass --release-base')
        base = f'https://github.com/{repo}/releases/download/console-{args.id}-v{version}'
    repo = origin_repo()
    entry = {
        'id': args.id, 'version': version, 'api': m['api'],
        'name': m['name'], 'description': m['description'],
        'author': m.get('author', ''), 'license': m.get('license', ''), 'homepage': m.get('homepage', ''),
        'source': f'https://github.com/{repo}/tree/HEAD/robot_console_plugin/{args.id}' if repo else '',
        'upstream': m.get('upstream', ''),
        'requirements': list((m.get('python') or {}).get('requirements') or []),
        'url': f'{base.rstrip("/")}/{os.path.basename(zpath)}',
        'sha256': sha, 'size': len(data),
        'published': datetime.date.today().isoformat(),
    }
    with open(os.path.join(dist, f'{args.id}-{version}.entry.json'), 'w', encoding='utf-8') as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(f'{zpath}\n  {len(data)} bytes  sha256 {sha}\n  {entry["url"]}')


if __name__ == '__main__':
    main()
