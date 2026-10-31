# benchscout (prototype)

Start it and it tells you what is on the bench: what is plugged in, what each thing is
(with evidence), what is unknown, and what to look at next. Then it asks which device(s)
you want to debug or measure and works on those. The intelligence is plain skills + MCP
running inside an existing agent (Claude Code, GitHub Copilot); the difference is that it
starts on its own.

Intent, direction and decisions live in [docs/design.md](docs/design.md) (Japanese). That file is the
source of truth; this README is a summary of it.

## Where it is going

| | Now | Target |
|---|---|---|
| Host | `benchscout start` launches Claude Code | benchscout is a GUI app that embeds the agent via its SDK (Claude Agent SDK, then Copilot SDK) |
| LLM / file ops | Claude Code | still the existing agent; benchscout never runs its own LLM loop |
| Approval | `benchscout approve <id>` on the CLI | a button in the GUI, enforced by the SDK's tool-permission callback |
| Protocols | hard-coded in Python | command dictionary / codec / transport; user-defined commands added by config |
| Knowledge | tables in `knowledge.py` | data files, preferring existing databases |
| Scope | bench observation, diagnosis, hardware operation | same; a dev pack (edit/build/flash) can be added later |

```
benchscout scan                  # recon, safe probes only
benchscout setup                 # make this folder a bench workspace: plain `claude` / `claude --resume` just work
benchscout start                 # recon + setup + launch Claude Code with an opening prompt
benchscout approvals             # what the agent is waiting on
benchscout approve <id>          # grant one action, once
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
| (planned) | L4 GUI | device list, logs, conversation, proposal and approval cards |
| `cli.py` | L3 backend | today: launches Claude Code. Planned: SDK adapters (Claude Agent SDK, Copilot SDK) |
| `mcp_server.py` | L2 face | the only door the agent has to hardware |
| `broker.py` | L2 broker | leases, approval queue, artifacts |
| `policy.py` | policy | classify by effect on device state; propagate impact over the graph |
| `recon.py` | L1b identification | build the graph, run SAFE probes, make proposals |
| `knowledge.py` | knowledge | VID/PID, board wiring, IDCODE, boot-log signatures, `*IDN?` parsing (to become data files) |
| `io.py` | L0 discovery / L1a access | enumerate without opening; open and talk. `LiveIo` or `FixtureIo` |
| `skills/bench-expert` | expert behaviour | how the expert behaves; copied into `.claude/skills` by `setup` |

## Rules it enforces

- Unknown stays unknown: names come only from evidence (descriptor, boot log, IDCODE, `*IDN?`).
- Effect, not read/write: `esptool chip_id` resets the chip, so it needs approval.
- Opening a port that can reset the device needs approval: USB-UART bridges that pulse DTR/RTS,
  and native-USB chips such as the ESP32 USB-Serial/JTAG, which was seen resetting on open.
- VISA's serial (`ASRL`) resources are never sent `*IDN?`.
- Approvals are bound to (action, port, arg) and single use.
- An action or command with no declared effect is treated as DANGEROUS.

## Not done yet

See [docs/design.md](docs/design.md) sections 10 and 12. In short: typed action arguments (fixes the
`serial.listen` baud/seconds bug), knowledge and protocols as data, long-lived port ownership,
SDK backend adapters, the GUI, and the `serial.send` / `jtag.halt` / `fpga.program` / scope
capture executors.
