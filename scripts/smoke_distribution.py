#!/usr/bin/env python3
"""Exercise an extracted package and staged install without root or hardware."""
from __future__ import annotations

import argparse
import os
import stat
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="extracted source or binary bundle")
    args = parser.parse_args()
    package = args.package.resolve()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    source = (package / "p2afan").is_dir()
    with tempfile.TemporaryDirectory(prefix="p2afan-smoke-") as temp:
        work = Path(temp)
        stage = work / "stage"
        env["DESTDIR"] = str(stage)
        # A staged installer must never contact systemd, even if it is on PATH.
        tools = work / "tools"
        tools.mkdir()
        forbidden = tools / "systemctl"
        forbidden.write_text("#!/bin/sh\necho systemctl-was-called >&2\nexit 99\n")
        forbidden.chmod(0o755)
        env["PATH"] = f"{tools}:{env.get('PATH', '/usr/bin:/bin')}"
        subprocess.run([str(package / "install.sh")], cwd=work, env=env, check=True)
        config_dir = stage / "etc/p2afan"
        example = config_dir / "config.toml.example"
        assert example.is_file(), "fresh install must provide an example"
        assert not (config_dir / "config.toml").exists(), "installer activated sample config"
        assert not (config_dir / "mapping.toml").exists(), "installer created a hardware mapping"
        cli = stage / ("opt/p2afan/bin/p2afan" if source else "usr/local/bin/p2afan")
        for executable in (package / "install.sh", cli, stage / "usr/local/bin/p2afan"):
            assert stat.S_IMODE(executable.stat().st_mode) == 0o755, executable
        # Outside checkout, without PYTHONPATH; these commands must not touch BARs/IPMI.
        subprocess.run([str(cli), "--version"], cwd=work, env=env, check=True)
        subprocess.run([str(cli), "--help"], cwd=work, env=env, check=True)
        subprocess.run([str(cli), "--config", str(example), "check-config",
                        "--mapping", str(work / "missing-mapping.toml")],
                       cwd=work, env=env, check=True)
        missing = subprocess.run([str(cli), "--config", str(work / "missing.toml"),
                                  "check-config"], cwd=work, env=env)
        assert missing.returncode != 0, "missing config should be rejected"
        preserved = {
            "config.toml": b"# operator configuration\n",
            "mapping.toml": b"# operator hardware mapping\n",
            "config.toml.example": b"# operator annotated example\n",
        }
        for name, content in preserved.items():
            (config_dir / name).write_bytes(content)
        subprocess.run([str(package / "install.sh")], cwd=work, env=env, check=True)
        for name, content in preserved.items():
            assert (config_dir / name).read_bytes() == content, f"upgrade changed {name}"
        unit = stage / "etc/systemd/system/p2afan.service"
        assert unit.is_file(), "systemd unit not installed"
    print(f"PASS: isolated {('source' if source else 'binary')} CLI and staged install/upgrade")


if __name__ == "__main__":
    main()
