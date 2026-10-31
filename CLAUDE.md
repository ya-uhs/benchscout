# benchscout

Bench recon for hardware engineers, delivered as a VS Code extension. Open it and it finds what
is plugged in (FPGA, MCU, instruments), says what each thing is (with evidence) and what is
unknown, asks which device(s) to look at, and lets the user's own agent work on the chosen ones
through MCP. Hardware access goes only through the broker, which classifies every action by its
effect on device state and gates anything not SAFE behind a human approval in the panel.

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

- **No LLM features of our own, and no agent embedding.** Reasoning, conversation, file operations
  and shell belong to the user's own agent (Copilot in VS Code, Claude Code, Cline...), which
  connects over MCP only. Do not build an agent loop, an LLM provider layer or SDK adapters.
- **One entry: the user's agent + MCP. One UI: the VS Code extension panel.** Shipped as .vsix
  (no Marketplace) with this Python core frozen by PyInstaller inside (design.md section 14).
- **Two channels.** The extension starts the core as a child process and owns the control
  channel (its stdin/stdout): approvals, selection. The agent reaches the core only through
  `benchscout mcp`, a relay with tools and no way to approve (design.md section 3, 15 R3).
- **Recon and the first read are deterministic** (rules/data), shown at once; the LLM starts
  when the user picks devices (R4). Expert behaviour ships as one skill source, placed in
  `.claude/skills/` for Claude Code and `.agents/skills/` for the others; the few rules that must
  hold are also in the MCP instructions (S4). Calls needing approval wait for the panel (S3). Target agents: Copilot, Claude Code, OpenAI-compatible APIs incl. local LLMs
  (used through Copilot's own-model support).
- **One core per VS Code window**, a child of the extension; devices are guarded by per-device
  locks and OS-level exclusive open; the MCP relay never starts a core (S1).
- **benchscout owns**: recon and the first read, the broker (safety gate), knowledge and protocol
  definitions as data, expert behaviour, and the device/approval panel.
- **Scope is bench observation, diagnosis and hardware operation.** No edit/build/flash workflow,
  but keep the core domain-agnostic so a dev pack can be added later.
- **No hardcoding.** Code is mechanism; knowledge, protocols, procedures and settings are data files.
- **Protocols are three layers**: command dictionary / codec / transport. User-defined commands
  (e.g. an MCU's built-in shell) are added by config, each declaring its effect.
- **Not always-on.** It runs while the panel is open, only in folders that opted in (S2).
- Open review items that may still change the shape: design.md section 15, S1-S6.

## Commands

```
uv sync --extra mcp --extra visa                                        # dev environment
uv run pytest -q                                                        # all tests, fixture-only, no hardware
BENCHSCOUT_FIXTURE=fixtures/bench_example.json uv run benchscout scan   # recon without hardware
uv run benchscout scan                                                  # recon on the real bench (safe probes only)
BENCHSCOUT_FIXTURE=fixtures/bench_example.json uv run benchscout core   # core without hardware; type commands to approve
```

Until the VS Code extension exists, the terminal running `benchscout core` is the control channel.

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
- Approvals come only from the human control channel, never from the agent's channel, and live
  in the core's memory (design.md section 15 R3). DANGEROUS grants are bound to
  (action, port, arg) and single use; DISRUPTIVE grants may cover (action, port) for the session
  or a time limit (R8). `test_core.py` covers this.
- Recon (`bench_scan`) runs SAFE probes only.

## Working in this repo

- Do not touch hardware from this session: no opening `/dev/cu.*`, no esptool,
  openocd or SCPI scripts. New behaviour is tested against `FixtureIo` first; add a
  fixture case to `fixtures/` or the test instead of probing the real bench.
- Real-hardware checks happen in `../bench-session`, a workspace set up exactly as an end user
  would get it (today by `benchscout setup` for Claude Code; later by opening it in VS Code with
  the extension). Do not hand-edit its `.mcp.json` or `.claude/`; if they are wrong, fix
  `setup_workspace()` in `cli.py`.
- `src/benchscout/skills/bench-expert/SKILL.md` is the source of the agent's bench
  behaviour. `setup` copies it into the workspace, so after editing it re-run
  `benchscout setup` in `../bench-session`.
- MCP tool docstrings and INSTRUCTIONS in `mcp_relay.py` are what the agent sees; keep them accurate,
  including each action's `args` (generated from `actions.py`).
- What the real bench showed beats fixtures. Record each real-hardware behaviour in
  `fixtures/` or a test (e.g. ESP32 native USB resets on open; Tang Nano serials are shared).
- For identification, prefer the external tool's own database (openFPGALoader, USB ID lists)
  over our tables, and say in the evidence which one decided.
- Support for a new device, protocol or command should be data first. Do not put device or
  protocol names into core code.

## Open findings from the first real-hardware run (2026-10-31)

Bench: Espressif USB-Serial/JTAG (ESP32 native USB) + Sipeed Tang Nano (GW1N(R)-9C, IDCODE `0x0100481B`).
Causes are known (design.md section 10).

- Fixed: ports can be given as id or path; an unknown one lists the known ids and paths.
- Fixed: `serial.listen arg=9600` listened for 9600 s. Actions now take typed `args`
  (`actions.py`), e.g. {"seconds": 3, "baud": 9600}, with ranges; bad args are rejected.
- The fabric UART on the Tang Nano is silent at 9600-230400 baud (checked 2026-10-31), so it is
  not a common-baud problem; whether the bitstream drives the UART is still unknown.
- Fixed: MCP clients start the relay with a trimmed environment (no TMPDIR), so the runtime
  dir now depends only on the home directory. Keep anything the core and relay must agree on
  independent of environment variables.
