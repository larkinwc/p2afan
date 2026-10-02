#!/usr/bin/env python3
"""Build local versioned archives; never publish or access fan hardware."""
from __future__ import annotations

import argparse
import hashlib
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package_version() -> str:
    match = re.search(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"$',
                      (ROOT / "p2afan/__init__.py").read_text(), re.MULTILINE)
    if match is None:
        raise SystemExit("package must declare a numeric major.minor.patch version")
    return match.group(1)


def copy_files(destination: Path, *, source: bool) -> None:
    for name in ("install.sh", "README.md", "LICENSE", "config", "systemd", "docs"):
        item = ROOT / name
        if item.is_dir():
            shutil.copytree(item, destination / name)
        else:
            shutil.copy2(item, destination / name)
    if source:
        for name in ("p2afan", "bin", "scripts", "tests", "re"):
            shutil.copytree(ROOT / name, destination / name,
                            ignore=shutil.ignore_patterns(
                                "__pycache__", "*.pyc", "*.bin", "extracted",
                                "_flash-16m.bin.extracted"))
    (destination / "install.sh").chmod(0o755)


def archive(directory: Path, output: Path) -> Path:
    target = output / f"{directory.name}.tar.gz"
    with tarfile.open(target, "w:gz") as tar:
        tar.add(directory, arcname=directory.name)
    return target


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("source", "binary", "all"), default="all")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--tag", help="refuse a release tag other than v<package version>")
    args = parser.parse_args()
    version = package_version()
    if args.tag is not None and args.tag != f"v{version}":
        parser.error(f"tag {args.tag!r} does not match package version v{version}")
    if args.kind != "source" and (platform.system() != "Linux" or platform.machine() != "x86_64"):
        parser.error("standalone binaries must be built on Linux x86_64")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifacts = []
    with tempfile.TemporaryDirectory(prefix="p2afan-release-") as temp:
        work = Path(temp)
        if args.kind in ("source", "all"):
            source = work / f"p2afan-{version}-source"
            source.mkdir()
            copy_files(source, source=True)
            (source / "bin/p2afan").chmod(0o755)
            artifacts.append(archive(source, output))
        if args.kind in ("binary", "all"):
            bundle = work / f"p2afan-{version}-linux-x86_64"
            bundle.mkdir()
            copy_files(bundle, source=False)
            (bundle / "bin").mkdir()
            entry = work / "entry.py"
            entry.write_text("from p2afan.cli import main\nraise SystemExit(main())\n")
            subprocess.run([
                sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                "--onefile", "--name", "p2afan", "--paths", str(ROOT),
                "--distpath", str(bundle / "bin"),
                "--workpath", str(work / "pyinstaller-build"),
                "--specpath", str(work), str(entry),
            ], cwd=work, check=True)
            (bundle / "bin/p2afan").chmod(0o755)
            artifacts.append(archive(bundle, output))
    checksums = output / "SHA256SUMS"
    checksums.write_text("".join(
        f"{checksum(path)}  {path.name}\n"
        for path in artifacts
    ))
    for path in [*artifacts, checksums]:
        print(path)


if __name__ == "__main__":
    main()
