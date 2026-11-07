"""Where concrete names may appear (design.md section 12, docs/notes.md review 9).

The same idea had been built again and again, per protocol, tool or OS: SCPI and firmware commands had two rule
mechanisms, the safety gate carried every operation as an `if action ==` branch, recon named the one JTAG tool. A test
that watched words (no device or vendor names) let all of it through. This one watches *where* things are named: each
kind of concrete name has a home, and the gate, recon and memory reach it only through that home's type.
"""

import ast
import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "benchscout"
EXT = Path(__file__).parent.parent / "vscode" / "src"

HOMES = {
    "a protocol (SCPI, *IDN?, IEEE 488.2)": (r"\bscpi\b|SCPI|\bIDN\b|ieee",
                                             {"protocols/scpi.py", "protocols/__init__.py", "actions.py", "mcp_relay.py",
                                              "model.py"}),
    "an action's name": (r"\b(serial|scpi|jtag)\.[a-z_]+\b",
                         {"actions.py", "protocols/lines.py", "protocols/scpi.py", "mcp_relay.py"}),
    "an external tool": (r"openFPGALoader|esptool|openocd|probe-rs", {"mcp_relay.py"}),
    "an operating system": (r"\bWindows\b|\bLinux\b|Darwin|win32|platform\.system",
                            {"winjob.py", "execution.py", "io.py", "winusb_strings.py", "runtime.py", "cli.py", "fetch.py", "locks.py", "memory.py",
                             "knowledge.py", "report.py"}),
    "a branch on a port's kind": (r"\.kind\s*(==|!=|in)\s*[(\"]",
                                  {"protocols/lines.py", "protocols/scpi.py", "protocols/__init__.py", "recon.py"}),
}


def _code(path: Path) -> list[tuple[int, str]]:
    """The tokens of a module that are code: comments and docstrings left out (they may cite anything)."""
    src = path.read_text(encoding="utf-8")
    docs: set[int] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                docs |= set(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    toks = tokenize.generate_tokens(io.StringIO(src).readline)
    return [(t.start[0], t.string) for t in toks if t.type != tokenize.COMMENT and t.start[0] not in docs]


def test_concrete_names_appear_only_in_their_home():
    misplaced = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        code = _code(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for what, (pattern, homes) in HOMES.items():
            if rel in homes:
                continue
            lines = sorted({ln for ln, s in code if re.search(pattern, s)})
            if what == "a branch on a port's kind":  # (spans tokens: look at the joined code)
                kinds = {"jtag", "swd", "serial", "resource", "lan-scpi", "usbtmc"}
                lines = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Compare)
                         and any(isinstance(n, ast.Attribute) and n.attr == "kind" for n in ast.walk(node))
                         and any(isinstance(n, ast.Constant) and n.value in kinds for n in ast.walk(node))]
            if lines:
                misplaced.append(f"{rel}: {what} at line(s) {lines}")
    assert not misplaced, "\n".join(misplaced)


def test_the_extension_names_no_action_protocol_or_tool_outside_its_wording():
    """The panel's words (i18n.ts) may name actions to say them in the person's language; nothing else in the extension
    decides by an action's, a protocol's or a tool's name."""
    pattern = r"\b(serial|scpi|jtag)\.[a-z_]+\b|openFPGALoader|esptool|openocd"
    found = []
    for path in sorted(EXT.rglob("*.ts")):
        if path.name == "i18n.ts":
            continue
        code = re.sub(r"//[^\n]*|/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
        if re.search(pattern, code):
            found.append(path.relative_to(EXT).as_posix())
    assert not found, found


def test_operations_use_only_what_their_context_lists():
    """Audit 2026-11-07 (16): the protocols' operations called the gate's private methods (`ctx._watch`,
    `ctx._save_artifact` ...), so what an operation may do to the session was nowhere said. protocols.base.Context lists
    it; an operation that reaches for anything else fails here (an import of the gate or recon too)."""
    from benchscout.protocols.base import Context

    allowed = {n for n in vars(Context) if not n.startswith("_")} | set(Context.__annotations__)
    wrong = []
    for path in sorted((SRC / "protocols").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "ctx" \
                    and node.attr not in allowed:
                wrong.append(f"{path.name}:{node.lineno} ctx.{node.attr}")
            if isinstance(node, (ast.Import, ast.ImportFrom)):  # in any form: from ..broker, from .. import broker, absolute
                names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                if any(n.split(".")[-1] in ("broker", "recon") for n in names):
                    wrong.append(f"{path.name}:{node.lineno} imports the gate or recon")
    assert not wrong, wrong
