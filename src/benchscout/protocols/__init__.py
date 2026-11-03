"""Protocols: the one place that knows how text sent to a device is read (design.md section 3, 4; notes.md review 9).
The gate, recon and memory use only `Protocol` and this registry; none of them names a protocol."""

from __future__ import annotations

from .base import Protocol, judge, judge_rule, outside
from .lines import LINES
from .scpi import SCPI

PROTOCOLS: dict[str, Protocol] = {p.name: p for p in (LINES, SCPI)}


def get(name: str) -> Protocol | None:
    return PROTOCOLS.get(name)


def for_port(port) -> Protocol | None:
    """The protocol a port carries (by its kind), or None (JTAG, SWD: tools reach those)."""
    return next((p for p in PROTOCOLS.values() if port.kind in p.port_kinds), None)


__all__ = ["PROTOCOLS", "Protocol", "for_port", "get", "judge", "judge_rule", "outside"]
