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
| Entry | `benchscout start` launches Claude Code | the user's own agent + MCP; no agent embedded, no LLM of our own |
| UI | CLI | a VS Code extension panel (devices, first read, logs, approvals, timeline), shipped as .vsix |
| Core | `benchscout mcp` runs its own broker per agent | Python core, frozen with PyInstaller inside the .vsix, started by the extension; `benchscout mcp` only relays to it |
| Approval | `benchscout approve <id>` writes a file | a button in the panel, over the extension's private channel; the agent's channel cannot approve |
| First read | the agent summarizes the scan | rules show the device list and first read at once; the LLM starts when you pick devices |
| Protocols | hard-coded in Python | command dictionary / codec / transport; user-defined commands added by config |
| Knowledge | tables in `knowledge.py` | data files, preferring existing databases |
| Scope | bench observation, diagnosis, hardware operation | same; a dev pack (build/flash) can be added later |

```
benchscout scan                  # recon, safe probes only
benchscout setup                 # make this folder a bench workspace: plain `claude` / `claude --resume` just work
benchscout start                 # recon + setup + launch Claude Code with an opening prompt
benchscout approvals             # what the agent is waiting on
benchscout approve <id>          # grant one action, once (to be replaced by the panel)
BENCHSCOUT_FIXTURE=fixtures/bench_example.json benchscout scan   # no hardware needed
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
| `mcp_server.py`, `skills/bench-expert` | L3 agent face | MCP tools and expert behaviour (to move to MCP instructions/prompts first) |
| `broker.py` | L2 broker | leases, approvals, artifacts |
| `policy.py` | policy | classify by effect on device state; propagate impact over the graph |
| `recon.py` | L1b identification | build the graph, run SAFE probes, make proposals |
| `knowledge.py` | knowledge | VID/PID, board wiring, IDCODE, boot-log signatures, `*IDN?` parsing (to become data files) |
| `io.py` | L0 discovery / L1a access | enumerate without opening; open and talk. `LiveIo` or `FixtureIo` |
| `cli.py` | dev tools | scan, setup for plain Claude Code, start |

## Rules it enforces

- Unknown stays unknown: names come only from evidence (descriptor, boot log, IDCODE, `*IDN?`).
- Effect, not read/write: `esptool chip_id` resets the chip, so it needs approval.
- Opening a port that can reset the device needs approval: USB-UART bridges that pulse DTR/RTS,
  and native-USB chips such as the ESP32 USB-Serial/JTAG, which was seen resetting on open.
- VISA's serial (`ASRL`) resources are never sent `*IDN?`.
- Approvals are bound to (action, port, arg) and single use. (Planned: grants only from the human
  control channel, held in the core's memory; DISRUPTIVE grants may be session-scoped. See design.md section 15.)
- An action or command with no declared effect is treated as DANGEROUS.

## Not done yet

See [docs/design.md](docs/design.md) sections 10, 12 and 15. In short: the control channel and
relay, typed action arguments (fixes the `serial.listen` baud/seconds bug), rule-based first read,
the VS Code extension, knowledge and protocols as data, long-lived port ownership, Windows/Linux,
and the `serial.send` / `jtag.halt` / `fpga.program` / scope capture executors.
