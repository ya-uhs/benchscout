# benchscout (prototype)

The agent owns device knowledge, transport/framing choices and observation logic. For a device outside the built-in
adapters, it can register an opaque connection address with `bench_register_resource`, prepare a script and execute
it with `bench_run` after human selection and approval. The harness manages ownership, lifetime, raw stdout/stderr
artifacts and observation events; `bench_job` polls or stops the program, and the panel also offers Stop.
Each run is approved with its arguments and input-file hashes. Programs run with the user's privileges: this is
a trusted-program boundary, not an OS sandbox or proof of physical safety. Registrations last one session; `bench_recipe` can save a plan and its registrations in
`.benchscout/recipes.json` for explicit reuse. Restoring a recipe neither selects devices nor restores approval;
running it requires current selection and fresh plan approval. Nothing starts automatically on restart.
Built-in operations also expose job state and Stop. Serial waits can be interrupted; an in-flight driver call
may finish or time out first. Stopping cannot undo commands already sent.

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

## Where it is

| | Today |
|---|---|
| Entry | the user's own agent + MCP; benchscout wakes it: Claude Code via `claude -p`, others (Copilot CLI) via ACP |
| UI | VS Code extension in `vscode/`: an inbox of questions, approvals, reports, notices and memory. benchscout's own words are English (as in Claude Code and Copilot CLI); the agent replies in `benchscout.language` (by default VS Code's language, or the OS's when VS Code is in English) |
| Loop | benchscout asks which agent and what to look at, wakes the agent, watches the chosen devices (asking first when opening a port may disturb it), notices reconnects and matches observation rules supplied for the bench, wakes the agent again (autonomy: auto / suggest; hourly budget) |
| Safety | approvals only from the panel (the agent's channel cannot approve); cautious defaults; selected ports held exclusively |
| Identification | the agent, with a quote of the device's output that benchscout checks (`bench_identify`) |
| Memory | automatic, per device and per folder, plain TOML; only safety-loosening entries wait for the human |
| Writing | FPGA SRAM and ESP flash as DANGEROUS actions defined as data (`packs/bench/data/tools.toml`); users add their own tools |
| Packaging | `packaging/build_core.py` + `npm run package:platform` -> a .vsix per OS/CPU with the frozen core |
| Platforms | macOS (real bench), Linux and Windows (tests; not yet on real hardware) |

Checked on a real bench (ESP32-S3 + Tang Nano 9K with the firmware/bitstream in `testbench/`): after the
human picked the ESP32, an injected crash was noticed, the agent was woken, it read the log, named the chip
from a quoted ROM banner, remembered it, asked what next, and reported; the next session's first read
already said ESP32-S3. The same loop was checked inside VS Code from the installed .vsix (fixture bench and the
Tang Nano), driven through the Chrome DevTools Protocol in an isolated VS Code profile (`npm run ui-check`).

```
benchscout scan                           # recon, safe probes only
benchscout setup [--agent claude|copilot] [--skill-only]  # skill (+ MCP config) for that agent in this folder
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
| `core.py` | core | control channel (stdin/stdout) and agent channel (Unix socket / Windows named pipe; core and relay prove the user's key to each other) |
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
  are bound to (action, port, arg) and single use, or for the session where the card offers it (an agent program while
  unchanged, a write marked `allow_session` with the same tool on the same device, an output setting up to the value
  approved); DISRUPTIVE grants may cover the session or N minutes. "Allow all" covers DISRUPTIVE only.
- Only devices selected for the session can be used; a selected device is locked against other windows
  and its serial port is opened exclusively.
- An action or command with no declared effect is treated as DANGEROUS.

## Not done yet

See [docs/notes.md](docs/notes.md) (現状): checks on a real bench still to do (an instrument's screenshot and data
saving, the driver steps on Windows x64, a device prompt that ends without a newline), and MCP registration in VS Code
forks such as Cursor.

CI: every push to main and every pull request runs the tests and the VS Code e2e on Windows, Linux and macOS; a push
to main (or a manual `test` run) also builds the .vsix for four targets. A newer push cancels the older run; a manual
run is never cancelled. Jobs stop after 30 minutes. Failure screenshots are kept for 7 days and the .vsix artifacts for
14 days; download a package you need to keep before it expires.
