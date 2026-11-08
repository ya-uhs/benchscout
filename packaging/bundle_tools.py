"""Ship openFPGALoader with the frozen core (design.md section 12, 2026-11-02).

Other tools are fetched on first use from their official releases (fetch.py). openFPGALoader has no
official build that runs as is on every OS, it is small, and its license (Apache-2.0) allows shipping
it; its USB libraries (libftdi1, libusb: LGPL-2.1) go along as separate shared libraries with their
licenses. Result: vscode/bin/benchscout/tools/openFPGALoader/ next to the core, which the core finds
after the person's own (PATH) and before fetched tools.

  macOS    the Homebrew build (brew install openfpgaloader); its non-system dylibs are copied into lib/,
           pointed there with install_name_tool, and everything is signed again (ad hoc)
  Linux    a build of the pinned tag (CI builds it); libftdi/libusb/hidapi copied into lib/, RPATH $ORIGIN/lib
  Windows  the MSYS2 UCRT64 package; the DLLs it loads from MSYS2 copied next to it (found with ntldd)

  uv run python packaging/bundle_tools.py [--exe PATH]     (run after packaging/build_core.py)
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

VERSION = "1.2.0"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "vscode" / "bin" / "benchscout" / "tools" / "openFPGALoader"
LICENSE_URL = f"https://raw.githubusercontent.com/trabucayre/openFPGALoader/v{VERSION}/LICENSE"
# shared libraries that travel with it; everything else must be part of the OS
SHIPPED_LIBS = re.compile(r"lib(ftdi1?|usb-1\.0|hidapi[\w-]*)[.\d-]*\.(so[.\d]*|dylib|dll)$", re.I)
# each library that may travel with it (a part of its file name) and the word its license files are named by (the
# package or formula they come from: Homebrew's libftdi, Debian's libusb-1.0-0, MSYS2's gcc-libs ...). A library not
# listed here stops the build: it would ship without its license text
# (MSYS2: openFPGALoader 1.2.0 needs libftdi, libusb, zlib and the GCC runtime; libftdi also lists libiconv, gettext and
# confuse; each package keeps its texts in share/licenses/<name>, packages.msys2.org, 2026-11-08)
LICENSED_AS = (("hidapi", "hidapi"), ("ftdi", "ftdi"), ("usb-1.0", "libusb"), ("stdc++", "stdc++"), ("gcc_s", "gcc"),
               ("winpthread", "winpthread"), ("zlib", "zlib"), ("libz-1", "zlib"), ("iconv", "iconv"), ("intl", "gettext"),
               ("confuse", "confuse"))


def run(*args: str | Path) -> str:
    return subprocess.run([str(a) for a in args], check=True, capture_output=True, text=True).stdout


def bundle_macos(exe: Path) -> list[Path]:
    lib = OUT / "lib"
    lib.mkdir(parents=True)
    main = OUT / "openFPGALoader"
    shutil.copy2(exe.resolve(), main)
    main.chmod(0o755)
    todo, shipped = [main], []
    while todo:
        f = todo.pop()
        for dep in [ln.strip().split(" (")[0] for ln in run("otool", "-L", f).splitlines()[1:]]:
            if dep.startswith(("/usr/lib/", "/System/", "@")):
                continue
            name = Path(dep).name
            target = lib / name
            if not target.exists():
                shutil.copy2(Path(dep).resolve(), target)
                target.chmod(0o755)
                run("install_name_tool", "-id", f"@loader_path/{name}", target)
                todo.append(target)
                shipped.append(target)
            run("install_name_tool", "-change", dep, ("@loader_path/lib/" if f == main else "@loader_path/") + name, f)
    for f in [main, *shipped]:
        run("codesign", "--force", "--sign", "-", f)  # install_name_tool invalidated the signatures
    return shipped


def bundle_linux(exe: Path) -> list[Path]:
    lib = OUT / "lib"
    lib.mkdir(parents=True)
    main = OUT / "openFPGALoader"
    shutil.copy2(exe, main)
    shipped = []
    for m in re.finditer(r"^\s*(\S+) => (\S+)", run("ldd", main), re.M):
        if SHIPPED_LIBS.search(m.group(1)):
            target = lib / m.group(1)
            shutil.copy2(Path(m.group(2)).resolve(), target)
            run("patchelf", "--set-rpath", "$ORIGIN", target)
            shipped.append(target)
    run("patchelf", "--set-rpath", "$ORIGIN/lib", main)
    return shipped


def bundle_windows(exe: Path) -> list[Path]:
    OUT.mkdir(parents=True)
    shutil.copy2(exe, OUT / "openFPGALoader.exe")
    shipped = []
    msys = exe.parent.parent  # .../msys64/ucrt64
    for m in re.finditer(r"^\s*(\S+\.dll) => (.+?) \(0x", run("ntldd", "-R", exe), re.M | re.I):
        dll = Path(m.group(2).strip())
        if dll.is_file() and msys in dll.parents:  # from MSYS2, not from Windows itself
            shutil.copy2(dll, OUT / dll.name)
            shipped.append(OUT / dll.name)
    return shipped


def check_licensed(shipped: list[Path], licenses: Path) -> None:
    """Every library shipped has its license text beside it (LGPL and the GCC runtime require it). Audit 2026-11-07:
    only openFPGALoader's own NOTICE was checked for, so a library whose license was not found shipped without it."""
    names = [f.name.lower() for f in licenses.iterdir()]
    for lib in shipped:
        word = next((w for part, w in LICENSED_AS if part in lib.name.lower()), None)
        # hidapi's Debian package is libhidapi-libusb0: that is hidapi's license, not libusb's
        if word is None or not any(word in n and not (word == "libusb" and "hidapi" in n) for n in names):
            raise SystemExit(f"{lib.name} would ship without its license text: none of {sorted(names)} is "
                             f"{word or 'known to be'} its. Find its license (Homebrew, /usr/share/doc, MSYS2 share/licenses) "
                             "and copy it in licenses(), or add the library to LICENSED_AS")


def licenses(exe: Path, shipped: list[Path]) -> None:
    out = OUT / "licenses"
    out.mkdir()
    try:
        with urllib.request.urlopen(LICENSE_URL, timeout=30) as r:
            (out / "openFPGALoader-LICENSE.txt").write_bytes(r.read())
    except OSError as e:
        sys.exit(f"could not get openFPGALoader's license from {LICENSE_URL}: {e}")
    # every shipped library's license: Homebrew keeps them per formula, Debian under /usr/share/doc, MSYS2 under
    # share/licenses (on Windows the C++ runtime, winpthreads and zlib DLLs travel too)
    wanted = ("ftdi", "libusb", "hidapi", "gcc-libs", "gcc", "stdc++", "winpthread", "zlib", "iconv", "gettext", "confuse")
    roots = [Path("/opt/homebrew/opt"), Path("/usr/local/opt"), Path("/usr/share/doc"), exe.parent.parent / "share" / "licenses"]
    exact = {"libftdi", "libusb", "hidapi", "libftdi1-2", "libusb-1.0-0", "libhidapi-libusb0", "libhidapi-hidraw0"}
    for root in roots:
        if not root.is_dir():
            continue
        for d in root.iterdir():
            msys2 = root.name == "licenses"
            if not d.is_dir() or not (d.name in exact or msys2 and any(w in d.name for w in wanted)):
                continue
            for f in [*d.glob("COPYING*"), *d.glob("LICENSE*"), *d.glob("copyright")]:
                if f.is_file():
                    shutil.copy2(f, out / f"{d.name}-{f.name}.txt")
    print("licenses:", sorted(f.name for f in out.iterdir()))
    check_licensed(shipped, out)
    (OUT / "NOTICE.txt").write_text(
        f"openFPGALoader {VERSION} (Apache-2.0) https://github.com/trabucayre/openFPGALoader\n"
        "with libftdi1 and libusb-1.0 (LGPL-2.1, shipped as separate shared libraries you may replace),\n"
        "and where present hidapi, and on Windows the GCC runtime (GPL-3.0 with the GCC Runtime Library\n"
        "Exception), winpthreads and zlib. Their license texts are in licenses/.\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", help="the openFPGALoader to ship (default: the one on PATH)")
    a = ap.parse_args()
    exe = Path(a.exe or shutil.which("openFPGALoader") or "")
    if not exe.is_file():
        sys.exit("no openFPGALoader to ship: brew install openfpgaloader (macOS), build it (Linux), "
                 "or pacman -S mingw-w64-ucrt-x86_64-openFPGALoader (MSYS2), or give --exe")
    if OUT.exists():
        shutil.rmtree(OUT)
    shipped = {"darwin": bundle_macos, "win32": bundle_windows}.get(sys.platform, bundle_linux)(exe)
    licenses(exe, shipped)
    main_exe = OUT / ("openFPGALoader.exe" if sys.platform == "win32" else "openFPGALoader")
    version = subprocess.run([main_exe, "--Version"], capture_output=True, text=True).stdout.strip()
    if f"v{VERSION}" not in version:
        # the notices name v{VERSION}: shipping another build would make them wrong
        raise SystemExit(f"refusing to ship {version or 'an openFPGALoader that does not report its version'}: pinned is "
                         f"v{VERSION} (THIRD_PARTY_NOTICES names that one). Install it, or change VERSION and the notices together")
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file()) / 1e6
    print(f"shipped {main_exe.relative_to(ROOT)} ({version}) with {[f.name for f in shipped]}, {size:.1f} MB")


if __name__ == "__main__":
    main()
