"""Make the folder a test-writing model works in: only what it may see (blackbox/README.md).

    uv run python blackbox/export.py <dest> [--core DIR | --vsix FILE]

<dest> gets the kit (TASK, CONTRACT, INTERFACES, harness, the example tests), the specification (design.md,
README.md), the example bench, and the program under test in core/: a frozen core (`packaging/build_core.py` builds
one into vscode/bin/benchscout, the default) or the one inside a .vsix. No source code goes there. The MCP tools'
descriptions are taken from that program and added to INTERFACES.md. <dest> becomes a git repository with one commit,
so what the model wrote comes back as a diff.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

KIT = Path(__file__).resolve().parent
ROOT = KIT.parent
EXE = "benchscout.exe" if sys.platform == "win32" else "benchscout"


def copy_core(dest: Path, core: Path | None, vsix: Path | None) -> Path:
    out = dest / "core"
    if vsix:
        with zipfile.ZipFile(vsix) as z:
            for member in z.namelist():
                if member.startswith("extension/bin/benchscout/"):
                    target = out / member.removeprefix("extension/bin/benchscout/")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(member))
        (out / EXE).chmod(0o755)
    else:
        src = core or ROOT / "vscode" / "bin" / "benchscout"
        if not (src / EXE).exists():
            sys.exit(f"no frozen core at {src}: run `uv run python packaging/build_core.py` first, or pass --vsix")
        shutil.copytree(src, out, symlinks=True)
    return out / EXE


def mcp_tools(dest: Path) -> str:
    """The MCP tools as the program under test describes them (it is the relay that tells the agent)."""
    sys.path.insert(0, str(dest))
    from harness import World  # the exported harness, with the exported program

    w = World().start()
    try:
        assert w.agent is not None
        tools = w.agent.tools()
    finally:
        w.close()
    out = ["\n## The MCP tools, as the program under test describes them\n"]
    for t in sorted(tools, key=lambda t: t["name"]):
        out.append(f"### `{t['name']}`\n\n{t.get('description', '').strip()}\n\n```json\n"
                   f"{json.dumps(t.get('inputSchema', {}), indent=1)}\n```\n")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dest", type=Path)
    ap.add_argument("--core", type=Path, help="a frozen core folder (default: vscode/bin/benchscout)")
    ap.add_argument("--vsix", type=Path, help="take the frozen core from this .vsix instead")
    a = ap.parse_args()
    dest = a.dest.resolve()
    if dest.exists() and any(dest.iterdir()):
        sys.exit(f"{dest} is not empty")
    if dest.is_relative_to(ROOT):
        sys.exit("put it outside the repository: the model must not find the source next to it")
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("TASK.md", "CONTRACT.md", "INTERFACES.md", "harness.py", "conftest.py"):
        shutil.copy2(KIT / name, dest / name)
    shutil.copytree(KIT / "tests", dest / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "docs" / "design.md", dest / "design.md")
    shutil.copy2(ROOT / "README.md", dest / "README.md")
    (dest / "bench").mkdir()
    shutil.copy2(ROOT / "fixtures" / "bench_example.json", dest / "bench" / "bench_example.json")
    shutil.copytree(ROOT / "fixtures" / "knowledge", dest / "bench" / "knowledge")
    exe = copy_core(dest, a.core, a.vsix)
    with open(dest / "INTERFACES.md", "a", encoding="utf-8") as f:
        f.write(mcp_tools(dest))
    (dest / ".gitignore").write_text("__pycache__/\n.pytest_cache/\ncore/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(["git", "-c", "user.name=blackbox", "-c", "user.email=blackbox@localhost", "commit", "-q", "-m",
                    "the kit as exported"], cwd=dest, check=True)
    print(f"{dest}\n  program under test: {exe}\n  run: cd {dest} && uv run --no-project --with pytest pytest -q tests")


if __name__ == "__main__":
    main()
