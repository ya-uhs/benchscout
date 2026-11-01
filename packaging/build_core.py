"""Freeze the Python core into vscode/bin/benchscout/ for the .vsix (design.md section 15).

    uv run python packaging/build_core.py

One folder (not one file): the core and the MCP relay start often, and a one-file build
unpacks itself on every start. Run on each OS/CPU; PyInstaller does not cross-compile.
"""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "vscode" / "bin" / "benchscout"


def main() -> None:
    build = ROOT / "build"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--name", "benchscout",
                    "--collect-data", "benchscout",          # packs/bench/data/*.toml, skills/
                    "--collect-submodules", "pyvisa_py",      # VISA backends load by name
                    "--collect-submodules", "serial.tools",
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
    exe = OUT / ("benchscout.exe" if sys.platform == "win32" else "benchscout")
    print(f"built {exe} ({sum(f.stat().st_size for f in OUT.rglob('*') if f.is_file()) / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
