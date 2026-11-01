"""Getting an external tool the first time an action needs it (design.md section 4).

Nobody installs esptool or the like up front just in case. A tool's data entry (`[get.<exe>]` in a
knowledge file) names its official release for each platform, pinned to a version and a SHA-256.
When an action needs the tool and it is neither on PATH (new enough) nor fetched before, the action's
approval card says what will be downloaded, and only after the human's OK is it fetched, checked and
unpacked into the per-user tools folder. The same pattern VS Code extensions use for their language
servers. Nothing is fetched without a pinned hash.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import knowledge as K


class FetchError(Exception):
    pass


def platform_key() -> str:
    """darwin-arm64, linux-x64, win32-x64, ... (the names VS Code uses for targets)."""
    os_name = {"darwin": "darwin", "win32": "win32"}.get(sys.platform, "linux")
    m = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64", "armv7l": "armhf"}.get(m, m)
    return f"{os_name}-{arch}"


def tools_dir() -> Path:
    if os.environ.get("BENCHSCOUT_TOOLS"):
        return Path(os.environ["BENCHSCOUT_TOOLS"])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "benchscout" / "tools"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "benchscout" / "tools"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "benchscout" / "tools"


def bundled_dir() -> Path:
    """Tools shipped with the frozen core: <core folder>/tools/<exe>/ (packaging/bundle_tools.py)."""
    if os.environ.get("BENCHSCOUT_BUNDLED_TOOLS"):
        return Path(os.environ["BENCHSCOUT_BUNDLED_TOOLS"])
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "tools"
    return Path(__file__).resolve().parents[2] / "vscode" / "bin" / "benchscout" / "tools"  # a dev checkout


def find(exe: str) -> str | None:
    """Where `exe` is: the person's own (PATH, then the usual per-user places a GUI-started app may lack),
    then the one shipped with benchscout, then one fetched before. None if nowhere."""
    found = shutil.which(exe)
    if found:
        return found
    name = exe + (".exe" if sys.platform == "win32" else "")
    for d in (Path.home() / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"), bundled_dir() / exe):
        if (d / name).is_file():
            return str(d / name)
    return cached(exe)


def plan(exe: str) -> dict | None:
    """Where this platform gets `exe` from, or None. Windows on Arm runs the x64 build (emulation)."""
    g = K.GETS.get(exe)
    if not g:
        return None
    key = platform_key()
    p = g.get(key) or (g.get("win32-x64") if key == "win32-arm64" else None)
    if not p or not p.get("sha256"):
        return None  # never fetch what is not pinned
    return {"exe": exe, "version": g["version"], "url": p["url"], "sha256": p["sha256"].lower(), "path": p["exe"],
            "size_mb": p.get("size_mb"), "home": tools_dir() / exe / g["version"]}


def describe(p: dict) -> str:
    host = p["url"].split("/")[2] if "://" in p["url"] else p["url"]
    size = f", about {p['size_mb']} MB" if p.get("size_mb") else ""
    return f"{p['exe']} {p['version']} from {host} (official release, SHA-256 checked{size})"


def cached(exe: str) -> str | None:
    p = plan(exe)
    if not p:
        return None
    f = p["home"] / p["path"]
    return str(f) if f.is_file() else None


def get(exe: str) -> str:
    """Download, check and unpack. Returns the executable's path."""
    p = plan(exe)
    if not p:
        raise FetchError(f"no pinned download of {exe} for {platform_key()}")
    target = p["home"] / p["path"]
    if target.is_file():
        return str(target)
    p["home"].parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=p["home"].parent) as tmp:
        archive = Path(tmp) / Path(p["url"].split("?")[0]).name
        try:
            with urllib.request.urlopen(p["url"], timeout=60, context=_tls()) as r, open(archive, "wb") as f:
                shutil.copyfileobj(r, f)
        except OSError as e:
            raise FetchError(f"could not download {p['url']}: {e}") from e
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != p["sha256"]:
            raise FetchError(f"{archive.name} does not match its pinned SHA-256 (got {digest}); not used")
        unpacked = Path(tmp) / "unpacked"
        _unpack(archive, unpacked)
        if not (unpacked / p["path"]).is_file():
            raise FetchError(f"{p['path']} is not in {archive.name}")
        if p["home"].exists():
            shutil.rmtree(p["home"])
        shutil.move(str(unpacked), str(p["home"]))
    if sys.platform != "win32":
        target.chmod(target.stat().st_mode | 0o111)
    return str(target)


def _tls():
    """certifi's CA list when there is one: a frozen core may not find the system's."""
    import ssl

    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _unpack(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True)
    root = dest.resolve()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if not (root / name).resolve().is_relative_to(root):
                    raise FetchError(f"{archive.name} has a path outside its folder: {name}")
            z.extractall(dest)
        return
    with tarfile.open(archive) as t:
        for m in t.getmembers():
            if not (root / m.name).resolve().is_relative_to(root) or m.issym() and not (root / m.name).parent.joinpath(m.linkname).resolve().is_relative_to(root):
                raise FetchError(f"{archive.name} has a path outside its folder: {m.name}")
        t.extractall(dest, **({"filter": "tar"} if hasattr(tarfile, "tar_filter") else {}))
