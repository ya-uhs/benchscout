# benchscout (prototype)

Open it and it tells you what is on the bench: what is plugged in, what each thing is
(with evidence), what is unknown, and what to look at next. Then it asks which device(s)
you want to debug or measure, and your own agent (Copilot in VS Code, Claude Code, ...)
works on those through MCP. The intelligence is plain skills + MCP; the difference is that
it starts on its own.

Intent, direction and decisions live in [docs/design.md](docs/design.md) (Japanese). That file is the
source of truth; this README is a summary of it.

## Where it is going

| | Now | Target |
|---|---|---|
| Entry | the user's agent + MCP (done for Claude Code and Copilot via `benchscout setup`) | the user's own agent + MCP; no agent embedded, no LLM of our own |
| UI | VS Code extension in `vscode/` (minimal: devices, approvals, next step; `npm run package` builds the .vsix). A terminal running `benchscout core` also works for debugging | a VS Code extension panel (devices, first read, logs, approvals, timeline), shipped as .vsix |
| Core | `benchscout core` per folder; `benchscout mcp` relays to it (done) | Python core, frozen with PyInstaller inside the .vsix, started by the extension; `benchscout mcp` only relays to it |
| Approval | in the core's memory, from the control channel only; calls wait for the decision (done) | a button in the panel, over the extension's private channel; the agent's channel cannot approve |
| First read | the agent summarizes the scan | rules show the device list and first read at once; the LLM starts when you pick devices |
| Protocols | hard-coded in Python | command dictionary / codec / transport; user-defined commands added by config |
| Knowledge | tables in `knowledge.py` | data files, preferring existing databases |
| Scope | bench observation, diagnosis, hardware operation | same; a dev pack (build/flash) can be added later |
| Platforms | macOS, Linux, Windows (Windows: named pipe and LockFile; not yet tried on real Windows hardware; CI runs the tests on all three) | same |

```
benchscout scan                           # recon, safe probes only
benchscout setup [--agent claude|copilot]  # skill + MCP config for that agent in this folder
benchscout core                           # run the core here; this terminal approves and selects:
                                          #   select <device> | approve <id> [once|session|<N>m] | deny <id> | pending | quit
benchscout mcp                            # what the agent starts: relays to this folder's core
BENCHSCOUT_FIXTURE=fixtures/bench_example.json benchscout core   # no hardware needed
```

## Model: a graph, not a protocol stack

```
Device (unplug unit) ──exposes──> Port (open/lease unit) ──protocol──> Target (impact unit)
FT2232H ─┬─ jtag ──────────────jtag──> FPGA
         └─ /dev/cu.*B ────────uart──> UART in fabric  (hosted_on FPGA)
ESP32-S3 USB ─┬─ /dev/cu.usbmodem ─uart─┐
              └─ jtag ─────────────jtag─┴> ESP32-S3
```

## Layers

| File | Layer | Job |
|---|---|---|
| (planned) `vscode/` | L4 panel | device list, first read, logs, approvals; owns the control channel |
| `mcp_relay.py`, `skills/bench-expert` | L3 agent face | MCP tools, instructions and the `bench_start` prompt (relayed to the core); expert behaviour |
| `core.py` | core | control channel (stdin/stdout) and agent channel (Unix socket) |
| `broker.py`, `approvals.py`, `locks.py` | L2 broker | session selection, approvals in memory, per-device locks across windows, artifacts |
| `policy.py` | policy | classify by effect on device state; propagate impact over the graph |
| `recon.py` | L1b identification | build the graph, run SAFE probes, make proposals |
| `knowledge.py` | knowledge | VID/PID, board wiring, IDCODE, boot-log signatures, `*IDN?` parsing (to become data files) |
| `io.py` | L0 discovery / L1a access | enumerate without opening; open and talk. `LiveIo` or `FixtureIo` |
| `cli.py` | dev tools | scan, core, mcp, setup |

## Rules it enforces

- Unknown stays unknown: names come only from evidence (descriptor, boot log, IDCODE, `*IDN?`).
- Effect, not read/write: `esptool chip_id` resets the chip, so it needs approval.
- Opening a port that can reset the device needs approval: USB-UART bridges that pulse DTR/RTS,
  and native-USB chips such as the ESP32 USB-Serial/JTAG, which was seen resetting on open.
- VISA's serial (`ASRL`) resources are never sent `*IDN?`.
- Approvals come only from the control channel and live in the core's memory. DANGEROUS grants
  are bound to (action, port, arg) and single use; DISRUPTIVE grants may cover the session or N minutes.
- Only devices selected for the session can be used; a selected device is locked against other windows
  and its serial port is opened exclusively.
- An action or command with no declared effect is treated as DANGEROUS.

## Not done yet

See [docs/design.md](docs/design.md) sections 10, 12 and 15. In short: typed action arguments (fixes the `serial.listen` baud/seconds bug), rule-based first read,
the VS Code extension, knowledge and protocols as data, long-lived port ownership, Windows/Linux,
and the `serial.send` / `jtag.halt` / `fpga.program` / scope capture executors.
