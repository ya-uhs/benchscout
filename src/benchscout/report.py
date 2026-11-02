"""Human/agent-readable rendering of a recon, grouped by physical device (board).

Each target is printed once, with every port that reaches it underneath.
"""

from __future__ import annotations

from .model import Confidence
from .recon import Recon

_MARK = {Confidence.IDENTIFIED: "identified", Confidence.FAMILY: "family only", Confidence.UNKNOWN: "unknown"}


def render(r: Recon) -> str:
    b = r.bench
    lines: list[str] = []
    for dev in b.devices.values():
        ports = b.ports_of(dev.id)
        if not ports:
            continue
        sn = f"  SN {dev.serial}" if dev.serial else ""
        lines.append(f"■ {dev.name}{sn}")
        targets: dict[str, list] = {}
        for p in ports:
            for link in b.links_of(p.id):
                targets.setdefault(link.target_id, []).append((p, link))
        for tid, via in targets.items():
            t = b.targets[tid]
            host = f"  (inside {b.targets[t.hosted_on].name or 'the FPGA'})" if t.hosted_on else ""
            lines.append(f"  ├ {t.name or '?'}{host}  [{_MARK[t.confidence]}]")
            for p, link in via:
                path = f"  {p.path}" if p.path and p.path not in p.id else ""
                lines.append(f"  │   via {link.protocol:<4} {p.id}{path}")
            for e in t.evidence:
                lines.append(f"  │   evidence  {e.source}: {e.detail}")
            for f in t.facts:
                lines.append(f"  │   · {f}")
        lines.append("")
    if r.ran:
        lines.append("Ran automatically (safe only):")
        lines += [f"  - {x}" for x in r.ran]
        lines.append("")
    if r.proposals:
        lines.append("Proposals:")
        for i, pr in enumerate(r.proposals, 1):
            need = f"  (needs: {pr.needs})" if pr.needs else ""
            lines.append(f"  {i}. [{pr.effect.upper()}] {pr.text}{need}")
            if pr.reason:
                lines.append(f"     why: {pr.reason}")
    missing = [k for k, v in b.tools.items() if not v]
    if missing:
        lines.append("")
        lines.append("Tools not installed: " + ", ".join(missing))
    return "\n".join(lines)
