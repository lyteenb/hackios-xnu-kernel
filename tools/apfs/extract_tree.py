#!/usr/bin/env python3
"""Extract an APFS volume to a directory and a mkapfs-compatible manifest.

Works on Windows as well as POSIX: APFS contents are read through tools/apfs/apfs.py,
so LZBITMAP files, symlinks and metadata do not depend on 7-Zip's host filesystem
behavior. Symlinks are recorded in the manifest but are not created on the host.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import apfs  # noqa: E402


def contents(ino, unzbm, tempdir):
    hdr = ino.decmpfs
    if hdr and len(hdr) >= 16 and struct.unpack_from('<I', hdr, 0)[0] == apfs.DECMPFS_MAGIC:
        ctype = struct.unpack_from('<I', hdr, 4)[0]
        if ctype == 14:
            rsrc = ino.vol.xattr_bytes(ino.obj_id, apfs.RSRC_FORK_XATTR_NAME, ino.xattrs)
            if rsrc is None:
                raise apfs.ApfsError(f'inode {ino.obj_id}: LZBITMAP without resource fork')
            base = Path(tempdir) / 'item'
            if base.exists():
                base.unlink()
            base.write_bytes(b'')
            with open(str(base) + ':com.apple.decmpfs', 'wb') as stream:
                stream.write(hdr)
            with open(str(base) + ':com.apple.ResourceFork', 'wb') as stream:
                stream.write(rsrc)
            result = subprocess.run([str(unzbm), str(base)], capture_output=True)
            if result.returncode:
                raise apfs.ApfsError(f'inode {ino.obj_id}: unzbm failed: ' +
                                     result.stderr.decode('utf-8', 'replace'))
            return base.read_bytes()
    return ino.contents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    parser.add_argument('output')
    parser.add_argument('--manifest')
    parser.add_argument('--include-prefix', action='append', default=[])
    parser.add_argument('--exclude-prefix', action='append', default=[])
    parser.add_argument('--exclude-suffix', action='append', default=[])
    parser.add_argument('--unzbm', default=str(Path('.local-tools/bin/unzbm.exe')))
    parser.add_argument('--volume')
    args = parser.parse_args()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = Path(args.manifest) if args.manifest else None
    if manifest:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_bytes(b'')

    unzbm = Path(args.unzbm)
    if not unzbm.is_file():
        raise SystemExit(f'unzbm not found: {unzbm}')

    container = apfs.Container(args.image)
    volume = apfs.pick_volume(container, args.volume)
    included = tuple(p.rstrip('/') for p in args.include_prefix)
    excluded = tuple(p.rstrip('/') for p in args.exclude_prefix)
    suffixes = tuple(args.exclude_suffix)
    files = dirs = links = 0
    try:
        with tempfile.TemporaryDirectory(prefix='lemonade-unzbm-') as tempdir, \
                (open(manifest, 'wb') if manifest else open(os.devnull, 'wb')) as mf:
            for path, name, fid, dtype in apfs.walk(volume, apfs.ROOT_DIR_INO_NUM):
                rel = path.lstrip('/')
                if included and not any(rel == p or rel.startswith(p + '/') for p in included):
                    continue
                if any(rel == p or rel.startswith(p + '/') for p in excluded):
                    continue
                ino = volume.inode(fid)
                if ino is None:
                    continue
                mode = ino.mode
                uid, gid = ino.owner, ino.group
                target = b''
                if stat.S_ISLNK(mode):
                    target = ino.symlink_target_bytes() or b''
                    links += 1
                elif stat.S_ISDIR(mode):
                    (output / rel).mkdir(parents=True, exist_ok=True)
                    dirs += 1
                elif stat.S_ISREG(mode):
                    if any(rel.endswith(s) for s in suffixes):
                        continue
                    dest = output / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(contents(ino, unzbm, tempdir))
                    files += 1
                else:
                    continue
                mf.write(f'{mode:o}\t{uid}\t{gid}\t{rel}\t'.encode('utf-8', 'surrogateescape'))
                if target:
                    mf.write(target)
                mf.write(b'\n')
    finally:
        container.close()
    print(f'extracted files={files} dirs={dirs} symlinks={links} -> {output}')


if __name__ == '__main__':
    main()
