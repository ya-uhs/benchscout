"""Session registrations: opaque addresses, never device or protocol knowledge."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .model import Bench, Device, Evidence, Port, Target


@dataclass(frozen=True)
class Resource:
    id: str
    address: str
    label: str
    device: str
    attached: bool

    def apply(self, bench: Bench) -> None:
        if self.attached and self.device not in bench.devices:
            return
        evidence = Evidence("agent-registration", self.address)
        if not self.attached:
            bench.add_device(Device(self.device, self.label, "resource", evidence=[evidence]))
        bench.devices[self.device].other = False  # a usable registration, still not an identification
        bench.add_port(Port(self.id, self.device, "resource", self.address, evidence=[evidence]))
        target = self.id + ":target"
        bench.add_target(Target(target, None, "unknown", evidence=[evidence]))
        if not bench.links_of(self.id):
            bench.link(self.id, target, "unspecified", evidence)


class Resources:
    def __init__(self):
        self.items: dict[str, Resource] = {}

    def register(self, bench: Bench, address: str, label: str, device: str | None) -> Resource:
        if not isinstance(address, str) or not address.strip() or len(address) > 4096 or "\0" in address:
            raise ValueError("address must be a nonempty opaque string (up to 4096 characters)")
        if not isinstance(label, str) or not label.strip() or len(label) > 256:
            raise ValueError("label must describe this resource (up to 256 characters)")
        # Repeated registration cannot give the same address a different owner.
        for r in self.items.values():
            if r.address == address:
                if device and device != r.device:
                    raise ValueError("this address already has an owner")
                return r
        if any(p.path == address for p in bench.ports.values()):
            raise ValueError("this address is already a port; use its existing port id")
        if device is not None and device not in bench.devices:
            raise ValueError("parent device is not present")
        digest = hashlib.sha256(address.encode()).hexdigest()
        r = Resource("resource:" + digest, address, label, device or "resource:" + digest, device is not None)
        self.items[r.id] = r
        r.apply(bench)
        return r

    def apply(self, bench: Bench) -> None:
        for r in self.items.values():
            r.apply(bench)
