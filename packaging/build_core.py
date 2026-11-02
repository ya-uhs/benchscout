"""Freeze the Python core into vscode/bin/benchscout/ for the .vsix (design.md section 15).

    uv run python packaging/build_core.py

One folder (not one file): the core and the MCP relay start often, and a one-file build
unpacks itself on every start. Run on each OS/CPU; PyInstaller does not cross-compile.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "vscode" / "bin" / "benchscout"


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_only_runtime() -> None:
    """Everything frozen in must be a runtime dependency of the product (uv export without dev): an allow list,
    not a list of what to keep out, so a dev tool pulled in by some optional import cannot ship unnoticed."""
    sys.path.insert(0, str(Path(__file__).parent))
    from notices import frozen_distribution_names

    out = subprocess.run(["uv", "export", "--no-dev", "--extra", "mcp", "--extra", "visa", "--no-hashes",
                          "--format", "requirements-txt"], capture_output=True, text=True, check=True, cwd=ROOT).stdout
    runtime = {_norm(m.group(1)) for m in re.finditer(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", out, re.M)}
    extra = sorted(d for d in frozen_distribution_names() if _norm(d) not in runtime)
    if extra:
        raise SystemExit(f"the frozen core carries what the product does not depend on: {', '.join(extra)}. "
                         "Exclude them in build_core.py (--exclude-module)")


def main() -> None:
    build = ROOT / "build"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--name", "benchscout",
                    "--collect-data", "benchscout",          # packs/bench/data/*.toml, skills/
                    "--collect-submodules", "pyvisa_py",      # VISA backends load by name
                    "--collect-submodules", "serial.tools",
                    # dev tools some dependency imports only when present (pydantic's mypy and hypothesis plugins,
                    # PyInstaller's own setuptools hooks): not part of the product. check_only_runtime() below
                    # catches anything this list misses
                    *[a for m in ("pytest", "_pytest", "pluggy", "iniconfig", "pygments", "mypy", "hypothesis",
                                  "setuptools", "pkg_resources", "packaging") for a in ("--exclude-module", m)],
                    "--distpath", str(build / "dist"), "--workpath", str(build / "work"), "--specpath", str(build),
                    str(ROOT / "packaging" / "entry.py")], check=True, cwd=ROOT)
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(build / "dist" / "benchscout", OUT, symlinks=True)
    links = [f for f in OUT.rglob("*") if f.is_symlink()]
    if links:
        # a .vsix is a zip and cannot hold them; a framework Python (python.org's on macOS) brings them
        raise SystemExit(f"the frozen core has symlinks ({links[0].relative_to(OUT)}, ...), which a .vsix cannot hold. "
                         "Build with a uv-managed Python: UV_PYTHON_PREFERENCE=only-managed uv sync ...")
    check_only_runtime()
    exe = OUT / ("benchscout.exe" if sys.platform == "win32" else "benchscout")
    print(f"built {exe} ({sum(f.stat().st_size for f in OUT.rglob('*') if f.is_file()) / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
