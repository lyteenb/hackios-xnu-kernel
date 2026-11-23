#!/usr/bin/env python3
"""Build the small /redwood boot image on Windows.

The host has no POSIX symlink privilege and does not need one: mkapfs.py takes
the mode/owner/symlink metadata from a manifest.  This script stages the
production init, the Redwood libraries and a minimal set of iOS launchd jobs,
then builds root.apfs with mkapfs.py.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MKA = ROOT / 'tools' / 'apfs' / 'mkapfs.py'
JOBS = [
    'com.apple.notifyd', 'com.apple.cfprefsd.xpc.daemon',
    'com.apple.cfprefsd.xpc.daemon.system', 'com.apple.logd',
    'com.apple.syslogd', 'com.apple.distnoted.xpc.daemon',
    'com.apple.diagnosticd', 'com.apple.fseventsd',
    'com.apple.containermanagerd', 'com.apple.containermanagerd.system',
    'com.apple.runningboardd', 'com.apple.backboardd',
    'com.apple.mobile.usermanagerd', 'com.apple.mobilegestalt.xpc',
    'com.apple.lsd', 'com.apple.powerd', 'com.apple.analyticsd',
    'com.apple.logd_helper', 'com.apple.iconservices.iconservicesagent',
    'com.apple.pluginkit.pkd', 'com.apple.SpringBoard',
]


def copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def tree_entries(base: Path, manifest: Path) -> None:
    lines = []
    for dirpath, dirnames, filenames in os.walk(base):
        d = Path(dirpath)
        rel = d.relative_to(base)
        if rel != Path('.'):
            lines.append(('40755', 0, 0, rel.as_posix(), ''))
        for name in filenames:
            p = d / name
            relp = p.relative_to(base).as_posix()
            if relp.startswith(('bin/', 'sbin/')) or relp.endswith('.dylib') or \
                    relp.endswith('.bundle/RedwoodMetal'):
                mode = '100755'
            else:
                mode = '100644'
            uid = gid = 501 if relp.startswith('var/mobile/') else 0
            lines.append((mode, uid, gid, relp, ''))
    seen = set()
    with manifest.open('w', encoding='utf-8', newline='\n') as f:
        for mode, uid, gid, path, target in lines:
            if path in seen:
                continue
            seen.add(path)
            f.write(f'{mode}\t{uid}\t{gid}\t{path}\t{target}\n')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, default=ROOT / 'out/root-staging')
    ap.add_argument('--manifest', type=Path, default=ROOT / 'out/root-staging.manifest')
    ap.add_argument('--output', type=Path, default=ROOT / 'userland/root.apfs')
    ap.add_argument('--init', type=Path, default=ROOT / 'out/lemonade-base-runtime/init')
    ap.add_argument('--metal-enable', type=Path, default=ROOT / 'out/lemonade-base-runtime/metal-enable')
    ap.add_argument('--base-prebuilt', type=Path, default=ROOT / 'prebuilt/userland')
    ap.add_argument('--metal', type=Path, default=ROOT / 'out/lemonade-metal')
    ap.add_argument('--gpu', type=Path, default=ROOT / 'fw-gpu')
    ap.add_argument('--wifi', type=Path, default=ROOT / 'local/lemonade/wifi/firmware')
    ap.add_argument('--jobs', type=Path, default=ROOT / 'sysimage/rootfs')
    args = ap.parse_args()

    root = args.root.resolve()
    if root == ROOT.resolve() or ROOT.resolve() not in root.parents:
        raise SystemExit(f'unsafe root path: {root}')
    if root.exists():
        for dp, dn, fn in os.walk(root, topdown=False):
            for n in fn:
                try: os.chmod(Path(dp) / n, 0o600)
                except OSError: pass
            try: os.chmod(dp, 0o700)
            except OSError: pass
        shutil.rmtree(root)
    for d in ('sbin', 'bin', 'etc', 'lib', 'LaunchDaemons', 'dev', 'private/var',
              'share', 'empty', 'xpc', 'hal', 'fw', 'var'):
        (root / d).mkdir(parents=True, exist_ok=True)

    copy(args.init, root / 'sbin/init')
    copy(args.metal_enable, root / 'bin/metal-enable')
    for name in ('libredwood.dylib', 'libredwood-sb.dylib', 'libredwood-media.dylib',
                 'libredwood-app.dylib', 'libredwood-tweaks.dylib'):
        src = args.base_prebuilt / name
        if src.is_file():
            copy(src, root / 'lib' / name)
    for name in ('libredwood.dylib', 'libredwoodair.dylib', 'libredwoodmetal.dylib'):
        src = args.metal / name
        if src.is_file():
            copy(src, root / 'lib' / name)
    bundle = args.metal / 'RedwoodMetal.bundle'
    if bundle.is_dir():
        for p in bundle.rglob('*'):
            if p.is_file():
                copy(p, root / 'lib/RedwoodMetal.bundle' / p.relative_to(bundle))
    for name in ('libOSMesa.8.dylib', 'libredwood-gl.dylib'):
        src = ROOT / 'userland/obj' / name
        if src.is_file():
            copy(src, root / 'lib' / name)

    (root / 'etc/rc').write_text('exec /sbin/launchd.ios\n', encoding='ascii')
    (root / 'etc/motd').write_text('Darwin on redwood: image built on Windows\n', encoding='ascii')
    injections = [
        ('/redwood/lib/libredwood.dylib', 'backboardd containermanagerd containermanagerd_system'),
        ('/redwood/lib/libredwood-sb.dylib', 'SpringBoard'),
        ('/redwood/lib/libredwood-media.dylib', 'mediaserverd'),
    ]
    (root / 'etc/inject.conf').write_text(
        ''.join(f'{lib} {names}\n' for lib, names in injections
                if (root / 'lib' / Path(lib).name).is_file()), encoding='utf-8')
    skel = (ROOT / 'sysimage/var.skel').read_text(encoding='utf-8')
    skel = skel.replace('l\t/usr/share/zoneinfo\t/private/var/db/timezone/zoneinfo',
                        'l\t/usr/share/zoneinfo.default\t/private/var/db/timezone/zoneinfo')
    skel = skel.replace('l\t/var/db/timezone/zoneinfo/US/Pacific\t/private/var/db/timezone/localtime',
                        'l\t/var/db/timezone/zoneinfo/Asia/Shanghai\t/private/var/db/timezone/localtime')
    skel += 'd\t700\t501\t501\t/private/var/mobile/tmp\n'
    vardir = ROOT / 'userland/var'
    if vardir.is_dir():
        for p in vardir.rglob('*'):
            if p.is_file():
                rel = p.relative_to(vardir)
                copy(p, root / 'var' / rel)
                owner = 501 if rel.as_posix().startswith('mobile/') else 0
                skel += f'f\t644\t{owner}\t{owner}\t/private/var/{rel.as_posix()}\t/var/{rel.as_posix()}\n'
    (root / 'etc/var.skel').write_text(skel, encoding='utf-8')

    job_cmd = [sys.executable, str(ROOT / 'userland/mkjobs.py'), str(args.jobs),
               str(root / 'LaunchDaemons'), *JOBS]
    env = dict(os.environ, OSLOG='com.apple.backboardd')
    subprocess.run(job_cmd, check=True, env=env)

    for d in (args.gpu, args.wifi):
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file() and p.name not in ('manifest.json',):
                    copy(p, root / 'fw' / p.name)

    tree_entries(root, args.manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(MKA), '-z', '--plain', 'fw', '-o', str(args.output),
                    f'redwood={root},manifest={args.manifest}'], check=True)
    print(f'built root image: {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
