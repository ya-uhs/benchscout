# benchscout (prototype)

Open it and it tells you what is on the bench: what is plugged in, what each thing is
(with evidence), what is unknown, and what to look at next. The intelligence is plain
skills + MCP; the difference is that it starts on its own.

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
| `io.py` | L0 discovery / L1 access | enumerate without opening; open and talk. `LiveIo` or `FixtureIo` |
| `knowledge.py` | knowledge | VID/PID, board wiring, IDCODE, boot-log signatures, `*IDN?` parsing |
| `recon.py` | identification | build the graph, run SAFE probes, make proposals |
| `policy.py` | policy | classify by effect on device state; propagate impact over the graph |
| `broker.py` | L2 broker | leases, approval queue, artifacts |
| `mcp_server.py` | L2 face | the only door the agent has to hardware |
| `skills/bench-expert` | L3 | how the expert behaves; copied into `.claude/skills` by `start` |
| `cli.py` | L3 launcher | backend command templates (`BENCHSCOUT_BACKEND_CMD` to override) |

## Rules it enforces

- Unknown stays unknown: names come only from evidence (descriptor, boot log, IDCODE, `*IDN?`).
- Effect, not read/write: `esptool chip_id` resets the chip, so it needs approval.
- Opening a USB-UART bridge can pulse DTR/RTS, so listening on one needs approval; native USB CDC does not.
- VISA's serial (`ASRL`) resources are never sent `*IDN?`.
- Approvals are bound to (action, port, arg) and single use.

## Not done yet

- Real-hardware runs (only fixture + an empty live bench tested)
- Streaming log ownership (broker opens per request; no long-lived multiplexed reader)
- `serial.send`, `jtag.halt`, `fpga.program`, scope capture executors
- Copilot backend template; GUI
