"""THIRD_PARTY_NOTICES.txt for what a .vsix ships (design.md section 15).

One file, vscode/THIRD_PARTY_NOTICES.txt, with each component's name, version, license and full license
text, taken from what is actually shipped:

  - the extension's npm production dependencies (`npm ls --omit=dev`)
  - when the frozen core is there (vscode/bin/benchscout, a platform .vsix):
      the Python runtime: python-build-standalone's own license texts for this exact build (its "full"
        archive carries them; the install_only one the core is built from does not)
      the Python distributions PyInstaller froze into it (from its TOC), with their license files
        (from the sdist on PyPI when the wheel has none)
      openFPGALoader and its libraries (packaging/bundle_tools.py collected their licenses)

  uv run --with zstandard python packaging/notices.py      (after build_core.py and bundle_tools.py)
Fails when a shipped component has no license text, so a .vsix never goes out without one.
"""

from __future__ import annotations

import ast
import importlib.metadata as md
import io
import json
import re
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VSCODE = ROOT / "vscode"
CORE = VSCODE / "bin" / "benchscout"
WORK = ROOT / "build" / "work" / "benchscout"
CACHE = ROOT / "build" / "notices-cache"
OUT = VSCODE / "THIRD_PARTY_NOTICES.txt"
LICENSE_FILE = re.compile(r"(^|/)(licen[cs]e|copying|notice|authors)[^/]*$", re.I)
PBS = "https://github.com/astral-sh/python-build-standalone/releases/download"


class Missing(Exception):
    pass


def section(title: str, version: str, license: str, texts: list[tuple[str, str]]) -> str:
    head = f"{title} {version}".strip() + (f"  ({license})" if license else "")
    body = "\n\n".join(f"--- {name} ---\n{text.strip()}" for name, text in texts)
    return f"{'=' * 78}\n{head}\n{'=' * 78}\n{body}\n"


# ---- the extension ------------------------------------------------------------------------------
def npm_packages() -> list[str]:
    out = subprocess.run(["npm", "ls", "--omit=dev", "--all", "--json", "--long"], cwd=VSCODE, capture_output=True,
                         text=True, encoding="utf-8", shell=sys.platform == "win32").stdout
    found: dict[str, dict] = {}

    def walk(deps: dict) -> None:
        for name, d in (deps or {}).items():
            key = f"{name}@{d.get('version')}"
            if key not in found and d.get("path"):
                found[key] = d
                walk(d.get("dependencies"))

    walk(json.loads(out).get("dependencies"))
    sections = []
    for key, d in sorted(found.items()):
        path = Path(d["path"])
        texts = [(f.name, f.read_text(encoding="utf-8", errors="replace"))
                 for f in sorted(path.iterdir()) if f.is_file() and LICENSE_FILE.search(f.name)]
        if not texts:
            raise Missing(f"npm package {key} has no license file in {path}")
        lic = d.get("license") if isinstance(d.get("license"), str) else json.dumps(d.get("license"))
        sections.append(section(d.get("name", key), d.get("version", ""), lic or "", texts))
    return sections


# ---- the Python runtime -------------------------------------------------------------------------
def pbs_archive() -> Path:
    """The full archive of the python-build-standalone build the core was frozen with."""
    base = Path(sys.base_prefix)
    tag = (base / "BUILD").read_text(encoding="utf-8").strip() if (base / "BUILD").is_file() else ""
    if not tag:
        raise Missing(f"{base} is not a python-build-standalone build (no BUILD file); build the core with uv's Python")
    ver = ".".join(map(str, sys.version_info[:3]))
    m = re.search(r"-(macos|linux|windows)-(aarch64|x86_64)", base.name)
    os_name, arch = (m.group(1), m.group(2)) if m else ("", "")
    triple = {"macos": f"{arch}-apple-darwin", "linux": f"{arch}-unknown-linux-gnu", "windows": f"{arch}-pc-windows-msvc"}[os_name]
    flavor = "pgo" if os_name == "windows" else "pgo+lto"
    name = f"cpython-{ver}+{tag}-{triple}-{flavor}-full.tar.zst"
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / name
    if not f.is_file():
        url = f"{PBS}/{tag}/{name.replace('+', '%2B')}"
        print(f"getting {url}", file=sys.stderr)
        with urllib.request.urlopen(url, timeout=120) as r:
            f.write_bytes(r.read())
    return f


def python_runtime() -> list[str]:
    import zstandard  # uv run --with zstandard

    archive = pbs_archive()
    texts, info = [], {}
    with open(archive, "rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as z:
        with tarfile.open(fileobj=z, mode="r|") as t:
            for m in t:
                if m.name.startswith("python/licenses/") and m.isfile():
                    texts.append((Path(m.name).name, t.extractfile(m).read().decode("utf-8", errors="replace")))
                elif m.name == "python/PYTHON.json":
                    info = json.loads(t.extractfile(m).read())
    if not texts:
        raise Missing(f"no license texts in {archive.name}")
    mods = {name: sorted({lic for v in variants for lic in (v.get("licenses") or [])})
            for name, variants in info.get("build_info", {}).get("extensions", {}).items()}
    used = "; ".join(f"{k}: {', '.join(v)}" for k, v in sorted(mods.items()) if v)
    intro = ("Python, built by python-build-standalone (https://github.com/astral-sh/python-build-standalone). "
             f"Its native modules link these libraries: {used}")
    return [section("Python runtime", ".".join(map(str, sys.version_info[:3])), "PSF-2.0 and the licenses below",
                     [("about", intro), *sorted(texts)])]


# ---- the Python distributions frozen into the core --------------------------------------------------
def frozen_distributions() -> list[str]:
    def entries(x, out):
        if isinstance(x, tuple) and len(x) == 3 and all(isinstance(i, str) for i in x):
            out.append(x)
        elif isinstance(x, (list, tuple)):
            for i in x:
                entries(i, out)
        return out

    names = set()
    for toc in ("PYZ-00.toc", "COLLECT-00.toc"):
        for name, _, _ in entries(ast.literal_eval((WORK / toc).read_text(encoding="utf-8")), []):
            names.add(re.split(r"[./\\]", name)[0])
    owners = md.packages_distributions()
    dists = sorted({d for n in names for d in owners.get(n, [])} - {"benchscout"})
    sections = []
    for name in dists:
        d = md.distribution(name)
        texts = [(str(f), f.read_text()) for f in (d.files or []) if LICENSE_FILE.search(str(f))]
        texts = [(n, t) for n, t in texts if t] or sdist_licenses(name, d.version)
        if not texts:
            raise Missing(f"no license text for {name} {d.version}")
        lic = d.metadata.get("License-Expression") or d.metadata.get("License") or ""
        sections.append(section(name, d.version, lic.splitlines()[0][:80] if lic else "", texts))
    return sections


def sdist_licenses(name: str, version: str) -> list[tuple[str, str]]:
    """Some wheels carry no license file (pyserial): take it from the source distribution on PyPI."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60) as r:
        urls = [u["url"] for u in json.load(r)["urls"] if u["packagetype"] == "sdist"]
    if not urls:
        return []
    with urllib.request.urlopen(urls[0], timeout=120) as r:
        data = r.read()
    out = []
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        for m in t.getmembers():
            if m.isfile() and m.name.count("/") == 1 and LICENSE_FILE.search(m.name):
                out.append((f"{Path(m.name).name} (from the sdist)", t.extractfile(m).read().decode("utf-8", errors="replace")))
    return out


# ---- openFPGALoader ---------------------------------------------------------------------------------
def shipped_tools() -> list[str]:
    sections = []
    for tool in sorted((CORE / "tools").glob("*")) if (CORE / "tools").is_dir() else []:
        texts = [("NOTICE", (tool / "NOTICE.txt").read_text(encoding="utf-8"))] if (tool / "NOTICE.txt").is_file() else []
        texts += [(f.name, f.read_text(encoding="utf-8", errors="replace")) for f in sorted((tool / "licenses").glob("*"))]
        if not texts:
            raise Missing(f"{tool.name} is shipped without its licenses")
        sections.append(section(tool.name, "", "", texts))
    return sections


def main() -> None:
    parts = ["Third-party software shipped in this extension, with its licenses.\n"]
    try:
        parts += ["\n# The VS Code extension (npm packages)\n", *npm_packages()]
        if (CORE / ("benchscout.exe" if sys.platform == "win32" else "benchscout")).is_file():
            parts += ["\n# The benchscout core (a frozen Python program)\n", *python_runtime(), *frozen_distributions()]
            parts += ["\n# Tools shipped with the core\n", *shipped_tools()]
    except Missing as e:
        sys.exit(f"notices: {e}")
    OUT.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {sum(p.count('=' * 78) for p in parts) // 2} components, {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
