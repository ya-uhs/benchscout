# benchscout (prototype)

Open it and it lists what is on the bench with the raw clues (USB VID/PID and the name each
device reports, JTAG IDCODE, `*IDN?`, console output) and what it remembers from earlier
sessions. Then it asks which device(s) you want to debug or measure, and your own agent
(Copilot in VS Code, Claude Code, ...) works on those through MCP: the agent identifies parts
and reads logs; benchscout gets the clues, guards the hardware, and remembers.

Intent, direction and decisions live in [docs/design.md](docs/design.md) (Japanese). That file is the
source of truth; this README is a summary of it.

## Roles

| | benchscout (deterministic) | your agent (LLM) |
|---|---|---|
| Devices | enumerate; get raw clues; no table of what devices are | identify them (search, datasheets), quoting the device's own output |
| Safety | the gate: cautious defaults, human approval in the panel for anything not SAFE | asks; quotes the reason |
| Logs | capture and store | interpret |
| Memory | stores it; records its own observations automatically | writes what it learned, automatically |

A name is "identified" only if it came from the device or a tool, or the agent quoted the
device's output and benchscout found the quote in what it captured. Memory loosening safety
("this port does not reset on open") needs a human to confirm; everything else is written
without asking and can be edited or deleted in the panel.

## Where it is going

| | Now | Target |
|---|---|---|
| Entry | the user's agent + MCP (`benchscout setup --agent claude|copilot`) | same; no agent embedded, no LLM of our own |
| UI | VS Code extension in `vscode/` (minimal); a terminal running `benchscout core` for debugging | panel with devices, approvals, memory, timeline; shipped as .vsix with the core inside |
| Core | `benchscout core` per folder; `benchscout mcp` relays to it | same, frozen with PyInstaller |
| Approval | in the core's memory, from the control channel only; calls wait for the decision | same, from panel buttons |
| Identification | shipped tables and log patterns (prototype; being removed) | the agent, with quotes checked by `bench_identify` |
| Memory | per-process only | automatic, per device and per workspace, editable in the panel |
| Writing | not in benchscout | FPGA SRAM / flash as one DANGEROUS action defined as data |
| Platforms | macOS, Linux, Windows (Windows not yet tried on real hardware; CI on all three) | same |

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

- Unknown stays unknown: a name needs the device or a tool to say it, or a quote of the device's
  own output that benchscout can find in what it captured.
- Effect, not read/write: `esptool chip_id` resets the chip, so it needs approval.
- Cautious by default: opening a serial port benchscout knows nothing about needs approval; it
  becomes SAFE only after it was observed not to reset anything and a human confirmed that.
- VISA's serial (`ASRL`) resources are never sent `*IDN?`.
- Approvals come only from the control channel and live in the core's memory. DANGEROUS grants
  are bound to (action, port, arg) and single use; DISRUPTIVE grants may cover the session or N minutes.
- Only devices selected for the session can be used; a selected device is locked against other windows
  and its serial port is opened exclusively.
- An action or command with no declared effect is treated as DANGEROUS.

## Not done yet

See [docs/design.md](docs/design.md) sections 11 and 13. In order: move identification to the
agent and make safety defaults cautious; automatic memory; `bench_identify` with quote checking;
the memory view in the panel; external-tool actions defined as data (FPGA SRAM, flash); device
commands from memory; long-lived port ownership; bundling the core into the .vsix.
