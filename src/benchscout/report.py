"""Human/agent-readable rendering of a recon, grouped by physical device (board).

Each target is printed once, with every port that reaches it underneath.
"""

from __future__ import annotations

from .model import Confidence
from .recon import Recon

_MARK = {Confidence.IDENTIFIED: "identified", Confidence.FAMILY: "family only", Confidence.UNKNOWN: "unknown"}


NO_DRIVER = ("Windows has no driver for its USB interface(s) {ifaces}, so no program can open them yet. A serial port "
             "needs its maker's driver (Windows Update often installs it); a tool that talks to the chip over USB "
             "through libusb (openFPGALoader, OpenOCD, probe-rs) needs WinUSB on that interface (Zadig installs it). "
             "Windows may refuse Zadig's self-signed driver on Arm PCs or where Code Integrity is enforced; then the "
             "vendor's own programmer (with the maker's driver) is the way: propose it with bench_define_tool. "
             "Tell the user; installing a driver is theirs to do")


def _hex(n: int | None) -> str:
    return f"{n:04x}" if n is not None else "????"


def render(r: Recon) -> str:
    b = r.bench
    lines: list[str] = []
    for dev in b.devices.values():
        ports = b.ports_of(dev.id)
        if dev.other or not ports and not dev.no_driver:
            continue
        sn = f"  SN {dev.serial}" if dev.serial else ""
        lines.append(f"■ {dev.name}{sn}")
        if dev.no_driver:
            lines.append(f"  · {NO_DRIVER.format(ifaces=', '.join(map(str, dev.no_driver)))}")
        if dev.needs_udev:
            lines.append("  · Linux: this user cannot open the USB device a libusb tool needs (a udev rule, then replug; "
                         "see the target below)")
        if dev.needs_winusb:
            lines.append(f"  · Windows: interface(s) {', '.join(map(str, dev.needs_winusb))} need the WinUSB driver for the "
                         "tool that opens them through libusb (see the target below)")
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
    others = [d for d in b.devices.values() if d.other]
    if others:
        lines.append("")
        lines.append("Other USB devices (benchscout has no data for them; if one is a programmer or probe for a part on "
                     "the bench, find its tool and propose it with bench_define_tool, giving this vid:pid in probes):")
        for d in others:
            lines.append(f"  - {d.name}  {_hex(d.vid)}:{_hex(d.pid)}" + (f"  SN {d.serial}" if d.serial else ""))
    from . import fetch

    missing = [k for k, v in b.tools.items() if not v]
    later = [k for k in missing if fetch.plan(k)]  # pinned: downloaded on first use, after the human's OK
    if missing:
        lines.append("")
        if set(missing) - set(later):
            lines.append("Tools not installed: " + ", ".join(k for k in missing if k not in later))
        if later:
            lines.append("Downloaded on first use (official release, after the human's OK): " + ", ".join(later))
    from . import knowledge as K

    if K.PROBLEMS:
        lines.append("")
        lines.append("Knowledge files that could not be read (none of their rules are in effect until they are fixed; "
                     "tell the user):")
        lines += [f"  - {p}" for p in K.PROBLEMS]
    return "\n".join(lines)
