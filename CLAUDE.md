# benchscout

Bench recon host for hardware engineers. Start it and it finds what is plugged in (FPGA, MCU,
instruments), says what each thing is (with evidence) and what is unknown, asks which device(s)
to look at, and drives observation, diagnosis and hardware operation for the chosen ones,
without being asked. Hardware access goes only through the broker, which classifies every
action by its effect on device state and gates anything not SAFE behind a human approval.

**The source of truth for intent, direction and decisions is `docs/design.md`** (Japanese).
Read it before design work. If this file or README.md disagrees with it, design.md wins;
change design.md (its decision log, section 11) first when the direction changes.

## Layout (design.md section 13)

```
~/dev/benchscout/            container only: no CLAUDE.md, not a repo
├── benchscout/              this git repo = the project root
└── bench-session/           real-hardware workspace, made by `benchscout setup` only
```

Nothing above this repo may carry instructions: a CLAUDE.md in `~/dev/benchscout/` would also
load in `bench-session/`, which must look exactly like an end user's workspace.

## Direction (summary of design.md; do not drift from it)

- **No LLM features of our own.** Reasoning, file operations, shell and conversation belong to
  existing agents (Claude Code, GitHub Copilot). Do not build an agent loop or an LLM provider layer.
- **Embed those agents through their SDKs** (Claude Agent SDK first, then Copilot SDK, ACP later),
  with benchscout as the host app (GUI). The plain Claude Code path (`benchscout setup`) stays.
- **Ship as a single binary via Tauri** (not Electron): thin Rust shell + TypeScript UI + this
  Python core as a PyInstaller sidecar, talking over a token-protected localhost WebSocket.
  No policy or approval logic in the shell (design.md section 14).
- **benchscout owns**: startup recon, the broker (safety gate), knowledge and protocol definitions
  as data, and the proposal/approval UI.
- **Scope is bench observation, diagnosis and hardware operation.** No edit/build/flash workflow,
  but keep the core domain-agnostic so a dev pack can be added later.
- **No hardcoding.** Code is mechanism; knowledge, protocols, procedures and settings are data files.
- **Protocols are three layers**: command dictionary / codec / transport. User-defined commands
  (e.g. an MCU's built-in shell) are added by config, each declaring its effect.
- **Not always-on.** Recon at startup is the baseline.

## Commands

```
uv sync --extra mcp --extra visa                                        # dev environment
uv run pytest -q                                                        # all tests, fixture-only, no hardware
BENCHSCOUT_FIXTURE=fixtures/bench_example.json uv run benchscout scan   # recon without hardware
uv run benchscout scan                                                  # recon on the real bench (safe probes only)
```

`~/.local/bin/benchscout` is a `uv tool` editable install of this directory, so code
changes reach the CLI and the MCP server without reinstalling (reconnect with `/mcp`).

## Invariants (tests cover these; keep them true)

- Unknown stays unknown. A part name comes only from evidence: USB descriptor, boot log,
  JTAG IDCODE, `*IDN?`. Never add a "probably X" guess.
- Classify by effect, not read/write. `esptool chip_id` reads but resets the chip, so it
  is DISRUPTIVE. Impact propagates over the graph (`policy.py`): reprogramming an FPGA
  also disturbs the UART implemented in its fabric.
- An action or command with no declared effect is DANGEROUS.
- Every proposal carries a `why:` that is the policy's actual reason
  (`test_proposal_reason_matches_policy`).
- VISA `ASRL` resources are never sent `*IDN?`.
- Approvals are bound to (action, port, arg) and are single use.
- Recon (`bench_scan`) runs SAFE probes only.

## Working in this repo

- Do not touch hardware from this session: no opening `/dev/cu.*`, no esptool,
  openocd or SCPI scripts. New behaviour is tested against `FixtureIo` first; add a
  fixture case to `fixtures/` or the test instead of probing the real bench.
- Real-hardware checks happen in `../bench-session`, a workspace made by
  `benchscout setup` exactly as an end user would get it. Do not hand-edit its
  `.mcp.json` or `.claude/`; if they are wrong, fix `setup_workspace()` in `cli.py`.
- `src/benchscout/skills/bench-expert/SKILL.md` is the source of the agent's bench
  behaviour. `setup` copies it into the workspace, so after editing it re-run
  `benchscout setup` in `../bench-session`.
- MCP tool docstrings in `mcp_server.py` are what the agent sees; keep them accurate,
  including what `arg` means for each action.
- What the real bench showed beats fixtures. Record each real-hardware behaviour in
  `fixtures/` or a test (e.g. ESP32 native USB resets on open; Tang Nano serials are shared).
- For identification, prefer the external tool's own database (openFPGALoader, USB ID lists)
  over our tables, and say in the evidence which one decided.
- Support for a new device, protocol or command should be data first. Do not put device or
  protocol names into core code.

## Open findings from the first real-hardware run (2026-10-31)

Bench: Espressif USB-Serial/JTAG (ESP32 native USB) + Sipeed Tang Nano (GW1N(R)-9C, IDCODE `0x0100481B`).
Causes are known, fixes are not in yet (design.md section 10).

- `bench_impact` / `bench_do` with `port="/dev/cu.usbmodem1101"` fail with no message;
  `port="serial:usbmodem1101"` works. Cause: `classify` looks ports up by id only and
  raises `KeyError`. Accept either form, or fail with a message naming the expected ids.
- `bench_do serial.listen serial:usbserial-101 arg=9600` did not return within 120 s.
  Cause: `Broker._execute` treats `arg` as seconds (`float(arg)`), so it listened for
  9600 s; baud is fixed at 115200 in `recon.listen_into`. Give actions typed arguments.
- The fabric UART on the Tang Nano was silent at the default rate; baud vs bitstream is
  not yet known because the baud could not be changed (previous item).
