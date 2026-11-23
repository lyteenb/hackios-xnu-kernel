#!/usr/bin/env python3
"""Stage the iOS system image on Windows and build it with mkapfs.py.

The source tree is Apple's extracted rootfs.  Files are hard-linked where the
host supports it, so the 3 GB tree is not copied.  Manifest-only entries carry
the symlinks and directory modes that Windows cannot create without elevation.
"""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
APFS = ROOT / 'tools' / 'apfs' / 'mkapfs.py'


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for dirpath, dirnames, filenames in os.walk(path, topdown=False):
        for name in filenames:
            try:
                os.chmod(Path(dirpath) / name, stat.S_IWRITE)
            except OSError:
                pass
        try:
            os.chmod(dirpath, stat.S_IWRITE)
        except OSError:
            pass
    shutil.rmtree(path)


def clone_tree(src: Path, dst: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        target = dst / rel
        target.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            source = Path(dirpath) / name
            dest = target / name
            if dest.exists():
                continue
            try:
                os.link(source, dest)
            except OSError:
                shutil.copy2(source, dest)


def fields(raw: bytes):
    return raw.rstrip(b'\r\n').split(b'\t')


def path_of(raw: bytes) -> str:
    f = fields(raw)
    return f[3].decode('utf-8', 'surrogateescape') if len(f) >= 4 else ''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--rootfs', type=Path, default=ROOT / 'sysimage/rootfs')
    ap.add_argument('--manifest', type=Path, default=ROOT / 'sysimage/system.manifest')
    ap.add_argument('--stage0', type=Path, default=ROOT / 'prebuilt/userland/stage0')
    ap.add_argument('--cryptex', type=Path, default=ROOT / 'sysimage/cryptex')
    ap.add_argument('--cryptex-manifest', type=Path, default=ROOT / 'sysimage/cryptex-apfs.manifest')
    ap.add_argument('--cryptex-app', type=Path, default=ROOT / 'sysimage/cryptex-app')
    ap.add_argument('--cryptex-app-manifest', type=Path, default=ROOT / 'sysimage/cryptex-app.manifest')
    ap.add_argument('--cache', type=Path, default=ROOT / 'sysimage/20F66__iPhone10,3_6')
    ap.add_argument('--cache-manifest', type=Path, default=ROOT / 'sysimage/cryptex-cache-staging.manifest')
    ap.add_argument('--overrides', type=Path, default=ROOT / 'sysimage/source-overrides')
    ap.add_argument('--sysroot', type=Path, default=ROOT / 'sysroot')
    ap.add_argument('--out-manifest', type=Path, default=ROOT / 'sysimage/system.windows.manifest')
    ap.add_argument('--output', type=Path, default=ROOT / 'userland/system.apfs')
    ap.add_argument('--keep-sysroot', action='store_true')
    args = ap.parse_args()

    sysroot = args.sysroot.resolve()
    root = ROOT.resolve()
    if sysroot == root or root not in sysroot.parents:
        raise SystemExit(f'unsafe sysroot path: {sysroot}')
    remove_tree(sysroot)
    sysroot.mkdir(parents=True)
    clone_tree(args.rootfs.resolve(), sysroot)

    # staged apps belong in /Applications, and /private/var is created in RAM
    staged = sysroot / 'private/var/staged_system_apps'
    applications = sysroot / 'Applications'
    applications.mkdir(exist_ok=True)
    if staged.is_dir():
        for item in staged.iterdir():
            shutil.move(str(item), str(applications / item.name))
    var = sysroot / 'private/var'
    remove_tree(var)

    # launchd reads our root image's /redwood/LaunchDaemons, not Apple's full set
    for relative in ('System/Library/LaunchDaemons', 'System/Library/NanoLaunchDaemons',
                     'System/Library/LaunchAngels'):
        p = sysroot / relative
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    xpc_job = sysroot / 'System/Library/xpc/launchd.plist'
    if xpc_job.exists():
        xpc_job.unlink()

    # stage0 is the system image's first program; Apple's launchd stays beside it
    launchd = sysroot / 'sbin/launchd'
    launchd_ios = sysroot / 'sbin/launchd.ios'
    if launchd.is_file():
        if launchd_ios.exists():
            launchd_ios.unlink()
        launchd.rename(launchd_ios)
    launchd.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.stage0, launchd)
    os.chmod(launchd, 0o755)

    (sysroot / 'redwood').mkdir(parents=True, exist_ok=True)
    (sysroot / 'System/Volumes/iOS').mkdir(parents=True, exist_ok=True)
    (sysroot / 'System/Cryptexes').mkdir(parents=True, exist_ok=True)
    clone_tree(args.cryptex, sysroot / 'System/Cryptexes/OS')
    clone_tree(args.cryptex_app, sysroot / 'System/Cryptexes/App')
    cache_dst = sysroot / 'System/Cryptexes/OS/System/Library/Caches/com.apple.dyld'
    cache_dst.mkdir(parents=True, exist_ok=True)
    for item in args.cache.iterdir():
        if item.is_file() and not item.name.endswith('.symbols'):
            dest = cache_dst / item.name
            if not dest.exists():
                try:
                    os.link(item, dest)
                except OSError:
                    shutil.copy2(item, dest)

    # Start from the original manifest, but omit entries replaced above.
    skipped = ('System/Library/LaunchDaemons', 'System/Library/NanoLaunchDaemons',
               'System/Library/LaunchAngels', 'System/Library/xpc/launchd.plist')
    lines = []
    for raw in args.manifest.read_bytes().splitlines():
        path = path_of(raw)
        if any(path == s or path.startswith(s + '/') for s in skipped):
            continue
        lines.append(raw)

    def add(mode: int, uid: int, gid: int, path: str, extra: str = '') -> None:
        lines.append(f'{mode:o}\t{uid}\t{gid}\t{path}\t{extra}'.encode('utf-8'))

    add(0o40755, 0, 0, 'redwood')
    add(0o40755, 0, 0, 'System/Volumes/iOS')
    add(0o40755, 0, 0, 'System/Cryptexes')
    add(0o40755, 0, 0, 'System/Cryptexes/OS')
    add(0o40755, 0, 0, 'System/Cryptexes/App')
    add(0o100755, 0, 0, 'sbin/launchd')
    add(0o100755, 0, 0, 'sbin/launchd.ios')
    add(0o120755, 0, 0, 'System/Library/LaunchDaemons', '/redwood/LaunchDaemons')
    add(0o120755, 0, 0, 'System/Library/NanoLaunchDaemons', '/redwood/empty')
    add(0o120755, 0, 0, 'System/Library/LaunchAngels', '/redwood/empty')

    def append_manifest(manifest: Path, prefix: str) -> None:
        if not manifest.is_file():
            return
        for raw in manifest.read_bytes().splitlines():
            f = fields(raw)
            if len(f) < 4:
                continue
            mode = int(f[0], 8)
            path = f[3].decode('utf-8', 'surrogateescape')
            target = f[4].decode('utf-8', 'surrogateescape') if len(f) > 4 else ''
            add(mode, int(f[1]), int(f[2]), prefix + '/' + path, target)

    append_manifest(args.cryptex_manifest, 'System/Cryptexes/OS')
    append_manifest(args.cryptex_app_manifest, 'System/Cryptexes/App')
    append_manifest(args.cache_manifest, 'System/Cryptexes/OS')

    # Two iOS filenames contain '?'; Windows cannot create them as host names.
    for raw in args.manifest.read_bytes().splitlines():
        f = fields(raw)
        if len(f) < 4:
            continue
        path = f[3].decode('utf-8', 'surrogateescape')
        if '?' not in path or 'What’s a shortcut?' not in path:
            continue
        safe = args.overrides / (hashlib.sha256(path.encode()).hexdigest()[:16] + '.shortcut')
        if not safe.is_file():
            raise SystemExit(f'missing source override for {path}: {safe}')
        add(int(f[0], 8), int(f[1]), int(f[2]), path, str(safe.resolve()))

    args.out_manifest.write_bytes(b'\n'.join(lines) + b'\n')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(APFS), '-z', '--plain', 'sbin', '-o', str(args.output),
                    f'System={sysroot},manifest={args.out_manifest}'], check=True)
    if not args.keep_sysroot:
        remove_tree(sysroot)
    print(f'staged system image: {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
