#!/usr/bin/env python3
"""Prepare the pinned Mu-Silicium tree for the OnePlus 9 UEFI build.

This is the non-private half of devices/oneplus-9/tools/lemonade-prepare.py:
it applies the tracked patch/files and build-time source edits, but never reads
a device DTB or any phone file. CI uses it before build_uefi.py.
"""
from pathlib import Path
import os
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
MU = ROOT / "Mu-Silicium"
PIN = (ROOT / "devices/oneplus-9/uefi/BASE").read_text().strip()


def replace_once(path, old, new):
    text = path.read_text()
    if new in text:
        return
    if text.count(old) != 1:
        raise SystemExit(f"Expected exactly one patch anchor: {path}")
    path.write_text(text.replace(old, new, 1))


if subprocess.check_output(["git", "-C", str(MU), "rev-parse", "HEAD"], text=True).strip() != PIN:
    raise SystemExit("Mu-Silicium is not at devices/oneplus-9/uefi/BASE")
patch = ROOT / "devices/oneplus-9/uefi/mu-silicium.patch"
if subprocess.run(["git", "-C", str(MU), "apply", "--check", str(patch)], capture_output=True).returncode == 0:
    subprocess.run(["git", "-C", str(MU), "apply", "--whitespace=nowarn", str(patch)], check=True)
elif "XnuLoaderMain ();" not in (MU / "Silicon/Silicium/SiliciumPkg/Library/BootManagerLib/BootManager.c").read_text():
    raise SystemExit("UEFI hooks do not apply cleanly")

lib = MU / "Silicon/Silicium/SiliciumPkg/Library/BootManagerLib"
for filename in ("XnuLoader.c", "XnuLoader.h"):
    source = ROOT / "devices/oneplus-9/uefi/files" / lib.relative_to(MU) / filename
    target = lib / filename
    if not target.exists():
        shutil.copy2(source, target)

replace_once(lib / "XnuLoader.c", "  BlockIo = FindPartition (XNU_LOG_PARTITION);",
             "  // Runtime diagnostics stay in RAM/USB.\n"
             "  if (XNU_NO_PERSISTENT_LOGS) {\n    return;\n  }\n\n"
             "  BlockIo = FindPartition (XNU_LOG_PARTITION);")
replace_once(lib / "XnuLoader.h", "#define _XNU_LOADER_H_",
             "#define _XNU_LOADER_H_\n\n#ifndef XNU_NO_PERSISTENT_LOGS\n"
             "#define XNU_NO_PERSISTENT_LOGS 0\n#endif")
replace_once(MU / "Platforms/OnePlus/lemonadePkg/Library/MemoryMapLib/MemoryMapLib.c",
             "  // Other Memory Regions",
             '  {"XNU Payload",       0xF0000000, 0x04000000, AddMem, SYS_MEM, SYS_MEM_CAP, Reserv, WRITE_BACK_XN},\n\n'
             "  // Other Memory Regions")
replace_once(MU / "Platforms/OnePlus/lemonadePkg/lemonade.dsc", "[PcdsFixedAtBuild]",
             "[BuildOptions]\n  *_*_AARCH64_CC_FLAGS = -march=armv8.4-a+crypto+rcpc -DXNU_NO_PERSISTENT_LOGS=1\n\n[PcdsFixedAtBuild]")
replace_once(MU / "Platforms/OnePlus/lemonadePkg/DeviceBuild.py",
             'RequiredSubmodule ("Mu_Basecore", True)',
             'RequiredSubmodule ("Mu_Basecore", False)')
print("Prepared Mu-Silicium for the OnePlus 9 UEFI build")
