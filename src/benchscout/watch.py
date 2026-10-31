"""Watching a selected device: hold its serial port open, log everything, notice events
(design.md section 17: watch -> notice -> wake).

While a port is watched:
- everything it prints goes to `<workspace>/.benchscout/logs/<port>.log` and a recent buffer
- each line is checked against the alert patterns (shipped layer 2 data now, memory later)
- a vanished port (reset, re-enumeration) is waited for and reopened; both are events
- `serial.listen` reads from the watch (nothing new is opened, so nothing is reset) and
  `serial.send` writes through it
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from . import knowledge as K
from .io import PortGone

RECENT_BYTES = 256 * 1024
REAPPEAR_S = 10.0  # how long to wait for a vanished port before giving up
COOLDOWN_S = 30.0  # the same alert on the same port is reported at most this often


class PortWatch:
    def __init__(self, io, port_id: str, path: str, log_dir: Path, on_event: Callable[[dict], None],
                 baud: int = 115200, lines: str = "low", patterns: list[dict] | None = None):
        self.io, self.port_id, self.path = io, port_id, path
        self.baud, self.lines = baud, lines
        self.log = Path(log_dir) / (port_id.replace(":", "_") + ".log")
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self._emit = on_event
        self._stream = None
        self._recent = bytearray()
        self._total = 0  # bytes received since the watch started
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._partial = ""
        self._last_alert: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self.alive = False
        # lines this device is known to print when something is wrong, learned into memory (design.md 6)
        import re as _re

        self.patterns = [(_re.compile(p["pattern"]), p.get("alert", "remembered pattern")) for p in (patterns or [])
                         if p.get("pattern")]

    # ---- lifecycle ------------------------------------------------------------------
    def start(self) -> None:
        """Opens the port now (errors surface to the caller), then reads in the background."""
        self._stream = self.io.serial_stream(self.path, self.baud, True, self.lines)
        self.alive = True
        self._thread = threading.Thread(target=self._run, name=f"watch {self.port_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._stream:
            self._stream.close()
        self.alive = False

    # ---- use from the broker ----------------------------------------------------------
    def read_window(self, seconds: float) -> str:
        """What arrives during the next `seconds`."""
        with self._cond:
            start = self._total
        time.sleep(seconds)
        with self._cond:
            new = self._total - start
            data = bytes(self._recent[-new:]) if new else b""
        return data.decode("utf-8", errors="replace")

    def send(self, data: bytes, seconds: float) -> str:
        with self._cond:
            start = self._total
        if not self._stream:
            raise PortGone("not open")
        self._stream.write(data)
        time.sleep(seconds)
        with self._cond:
            new = self._total - start
            out = bytes(self._recent[-new:]) if new else b""
        return out.decode("utf-8", errors="replace")

    def recent(self, nbytes: int = 4000) -> str:
        with self._cond:
            return bytes(self._recent[-nbytes:]).decode("utf-8", errors="replace")

    # ---- background ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._stream.read(0.2)
            except PortGone as e:
                self._stream.close()
                self._event("vanished", str(e))
                if not self._reopen():
                    self._event("gone", f"did not come back within {REAPPEAR_S:.0f} s")
                    self.alive = False
                    return
                self._event("returned", "the port came back; reading again")
                continue
            if chunk:
                self._take(chunk)

    def _reopen(self) -> bool:
        deadline = time.monotonic() + REAPPEAR_S
        while time.monotonic() < deadline and not self._stop.is_set():
            time.sleep(0.1)
            if not self.io.port_present(self.path):
                continue
            try:
                self._stream = self.io.serial_stream(self.path, self.baud, True, self.lines)
                return True
            except Exception:
                continue
        return False

    def _take(self, chunk: bytes) -> None:
        with self._cond:
            self._recent += chunk
            del self._recent[:-RECENT_BYTES]
            self._total += len(chunk)
            self._cond.notify_all()
        with open(self.log, "ab") as f:
            f.write(chunk)
        text = self._partial + chunk.decode("utf-8", errors="replace")
        *lines, self._partial = text.split("\n")
        for line in lines:
            alerts = K.classify_log(line)["alerts"] + [a for rx, a in self.patterns if rx.search(line)]
            for a in alerts:
                now = time.monotonic()
                if now - self._last_alert.get(a, -COOLDOWN_S) >= COOLDOWN_S:
                    self._last_alert[a] = now
                    self._event("alert", a, line=line.strip())

    def _event(self, kind: str, what: str, line: str | None = None) -> None:
        self._emit({"event": "device_event", "port": self.port_id, "kind": kind, "what": what,
                    "line": line, "log": str(self.log.resolve()), "time": time.time()})
