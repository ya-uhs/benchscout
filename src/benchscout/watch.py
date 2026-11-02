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
# The computer slept: the wall clock ran on while this process's monotonic clock stood still (macOS and
# Linux; real bench 2026-11-01: a lid closed overnight made both boards' ports vanish and return at every
# dark wake while their counters never reset). A port that vanishes around then is put down to that.
SLEEP_GAP_S = 20.0
SLEEP_WINDOW_S = 120.0


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
        self._answers: list[tuple[int, int]] = []  # byte ranges (in _total) read as answers to sent commands
        self._thread: threading.Thread | None = None
        self.alive = False
        self.vanishes = 0  # how often the port vanished while watched (a reset, an unplug)
        self._wall, self._mono = time.time, time.monotonic  # replaceable in tests
        self._drift = self._wall() - self._mono()
        self._slept_until = 0.0  # monotonic time until which vanishing is put down to the host sleeping
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

    def send(self, data: bytes, seconds: float, until=None) -> tuple[str, str]:
        """Write `data`; return (what arrived until a line matched `until` or `seconds` passed, what the device had
        printed by itself before: the recent output less what answered earlier commands)."""
        from .codec import ended

        with self._cond:
            start = self._total
            before = self._unprompted(start)
        if not self._stream:
            raise PortGone("not open")
        self._stream.write(data)
        end = time.monotonic() + seconds
        with self._cond:
            while True:
                new = self._total - start
                out = bytes(self._recent[-new:]) if new else b""
                left = end - time.monotonic()
                if left <= 0 or ended(out, until):
                    break
                self._cond.wait(min(left, 0.2))
            self._answers = (self._answers + [(start, self._total)])[-50:]
        return out.decode("utf-8", errors="replace"), before

    def _unprompted(self, upto: int, nbytes: int = 16384) -> str:
        """The device's own output before `upto` (the last `nbytes`), without the answers to earlier commands."""
        base = self._total - len(self._recent)
        pos = max(base, upto - nbytes)
        out = bytearray()
        for a, b in sorted(self._answers):
            if b <= pos or a >= upto:
                continue
            out += self._recent[pos - base:max(pos, a) - base]
            pos = max(pos, b)
        if pos < upto:
            out += self._recent[pos - base:upto - base]
        return out.decode("utf-8", errors="replace")

    def recent(self, nbytes: int = 4000) -> str:
        with self._cond:
            return bytes(self._recent[-nbytes:]).decode("utf-8", errors="replace")

    # ---- background ------------------------------------------------------------------
    def _check_sleep(self) -> None:
        drift = self._wall() - self._mono()
        if drift - self._drift > SLEEP_GAP_S:
            self._slept_until = self._mono() + SLEEP_WINDOW_S
        self._drift = drift

    def _run(self) -> None:
        while not self._stop.is_set():
            self._check_sleep()
            stream = self._stream
            if stream is None:  # never opened: nothing to read
                self.alive = False
                return
            try:
                chunk = stream.read(0.2)
            except PortGone as e:
                self._check_sleep()
                stream.close()
                self.vanishes += 1
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
        ev = {"event": "device_event", "port": self.port_id, "kind": kind, "what": what,
              "line": line, "log": str(self.log.resolve()), "time": time.time()}
        if kind in ("vanished", "returned", "gone") and self._mono() < self._slept_until:
            ev["cause"] = "host_sleep"
            ev["what"] = f"{what} (the computer had been asleep)"
        self._emit(ev)
