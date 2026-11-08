# benchscout

Bench recon for hardware engineers, delivered as a VS Code extension. Open it and it lists what
is plugged in with the raw clues (VID/PID, the name the device reports, IDCODE, `*IDN?`, logs) and
what it remembers about each device, asks which device(s) to look at, and lets the user's own agent
work on the chosen ones through MCP. Hardware access goes only through the broker, which
classifies every action by its effect on device state and gates anything not SAFE behind a human
approval in the panel.

**The source of truth for intent, direction and decisions is `docs/design.md`** (Japanese).
Work notes (where things stand, what is next, what the real bench showed, design reviews) are in
`docs/notes.md`. Keep them apart: a finding, a bug's story or a test run goes to notes.md; only the
decision that comes out of it goes into design.md, briefly, in its decision log (section 12).
Read it before design work. If this file or README.md disagrees with it, design.md wins;
change design.md (its decision log, section 12) first when the direction changes.

## Layout (design.md section 14)

```
~/dev/benchscout/            container only: no CLAUDE.md, not a repo
├── benchscout/              this git repo = the project root
└── bench-session/           real-hardware workspace, made by `benchscout setup` only
```

Nothing above this repo may carry instructions: a CLAUDE.md in `~/dev/benchscout/` would also
load in `bench-session/`, which must look exactly like an end user's workspace.

## Direction (summary of design.md; do not drift from it)

- **benchscout is the harness that runs the loop** (design.md section 17): watch the chosen devices,
  notice events, **wake the user's agent** (Claude Code via `claude -p` stream-json; every other agent via
  ACP with the official `@agentclientprotocol/sdk`), let it investigate with benchscout's tools, record what
  was learned. The human answers inbox cards (questions, approvals) and can always type an intent.
  benchscout also proposes which devices to look at. If the chosen agent cannot be woken, stop and say
  so; never fall back to another agent or LLM on its own. Use existing protocols and official interfaces;
  do not invent private ones.
- **The agent is the brain.** Identifying what a device is (searching IDs, reading datasheets)
  and interpreting logs (a crash, an RTOS) is the user's agent's job (Copilot, Claude Code,
  OpenAI-compatible APIs incl. local LLMs). benchscout has no LLM features, no agent embedding.
- **The agent also owns transport, framing, commands and observation methods.** Use `bench_register_resource`
  and `bench_run` for agent-prepared programs; built-in actions are convenience adapters, never the boundary
  of supported hardware. The harness owns selection, consent, process lifetime, ownership and raw artifacts.
  Arbitrary programs are trusted code with user privileges, not sandboxed or proven physically safe. See §12, 2026-11-04.
- **benchscout does four things only** (design.md section 3):
  1. gets **raw clues** from devices and tools; it keeps **no table of what devices are**
  2. is the **safety gate**: unknown things get cautious defaults; anything not SAFE waits for a
     human approval in the panel
  3. keeps **memory**: what was learned about the bench is written **automatically, without
     asking**; only memory that loosens safety needs the human to confirm (section 6)
  4. shows devices, approvals and memory in the **panel**
- **Unknown stays unknown, without tables**: a name is "identified" only if it came from the device
  or a tool, or the agent quoted the device's own output and benchscout found that quote in the
  captured output (`bench_identify`). Otherwise it is a guess and shown apart from the name.
- **Cautious by default**: deleting every shipped data file must only make benchscout stricter.
  Loosen only from observation plus a human's confirmation.
- **Shipped tools are a convenience, never the only way** (the twin of the rule above): with no shipped tool
  definitions, a programmer benchscout has no data for is still visible and the agent can bring its vendor's tool
  (`bench_define_tool`, the human confirms) and use it through the same gate. Which tool handles a part is the
  agent's knowledge, not benchscout's. Every non-hub USB device is listed with its raw clues; unknown ones apart.
- **No device knowledge in code, and none to collect in data either** (section 5): shipped data is
  limited to safety rules by kind of action. Device names, log formats and board wiring belong to
  the agent or to memory.
- **One entry: the user's agent + MCP. One UI: the VS Code extension panel.** Shipped as .vsix
  (no Marketplace) with this Python core frozen by PyInstaller inside (section 15).
- **Two channels.** The extension starts the core as a child process and owns the control channel
  (its stdin/stdout): approvals, selection. The agent reaches the core only through
  `benchscout mcp`, a relay with tools and no way to approve (section 3, 16 R3). The core and the relay prove the
  OS user's key (`runtime.agent_key`) to each other before anything else (another account can take a Windows pipe name).
- **The first read needs no LLM** (raw clues + memory, shown at once); the LLM starts when the user
  picks devices (R4). Calls needing approval wait for the panel (S3). Expert behaviour ships as one
  skill source, in `.claude/skills/` for Claude Code and `.agents/skills/` for the others (S4).
- **One core per VS Code window**; devices are guarded by per-device locks, and the serial ports the built-in adapters
  open by OS-level exclusive open (a program run with `bench_run` is trusted, not contained); the MCP relay never starts
  a core and takes its tool descriptions from it (S1). A folder VS Code does not trust: its knowledge and its memory
  file are not read (S2). The folder's memory file holds its setup only (kind config).
- **Scope**: bench observation, diagnosis and hardware operation, including writing a bitstream
  to FPGA SRAM or an image to flash as one DANGEROUS action defined as data. Editing and building
  code are out of scope.
- What exists today and what is next: docs/notes.md (現状, 次にやること). Shipped data is limited to
  how USB interfaces behave when opened, what they carry and how a tool reaches them, board
  wiring assumptions for impact warnings, and external-tool actions; do not
  grow it. `test_without_any_shipped_data_benchscout_only_gets_stricter` guards the rule that
  deleting it only makes benchscout stricter; `tests/test_unseen.py` keeps a bench of devices it
  ships nothing about (other MCUs, an unknown FPGA board, a probe, instruments) honest and cautious.

## Commands

```
uv sync --extra mcp --extra visa                                        # dev environment
uv run pytest -q                                                        # all tests, fixture-only, no hardware
uv run mypy && uvx ruff@0.14.3 check src tests packaging                  # types and lint (CI runs both)
BENCHSCOUT_FIXTURE=fixtures/bench_example.json uv run benchscout scan   # recon without hardware
uv run benchscout scan                                                  # recon on the real bench (safe probes only)
BENCHSCOUT_FIXTURE=fixtures/bench_example.json uv run benchscout core   # core without hardware; type commands to approve
```

VS Code extension (`vscode/`): `npm test` (its control channel against a real core on the fixture
bench), `npm run package` -> `vscode/benchscout.vsix` (no core inside). Platform build with the frozen
core: `uv run python packaging/build_core.py`, `uv run python packaging/bundle_tools.py` (ships
openFPGALoader next to it; on macOS from `brew install openfpgaloader`), `uv run --with zstandard python
packaging/notices.py` (THIRD_PARTY_NOTICES.txt: every shipped component's license text; fails if one is missing), then
`VSCE_TARGET=darwin-arm64 npm run package:platform`
(same tests against the frozen core: `BENCHSCOUT_TEST_BIN=$PWD/bin/benchscout/benchscout npm test`). Keep `src/core.ts` free of `vscode` imports
so it stays testable with plain Node. Panel wording lives in `src/i18n.ts` (en/ja); clarity first.

To look at the extension in a real VS Code (no computer use needed, the user's own VS Code untouched):
`npm run e2e` (in `vscode/`, after `npm run package`) drives that same isolated VS Code through a scripted bench
(`fixtures/bench_e2e.json`) end to end against the checkout's core; run it before pushing a change to the extension or the core.
CI minutes are limited (private repo; macOS counts 10x): a push runs one Linux job; the full set (three OSes, e2e, the
.vsix for four targets) runs by hand before a release (`gh workflow run test`). Push in batches, not after every commit.
`cd vscode && npm run ui-check -- start` installs the newest .vsix into an isolated profile, opens the
fixture bench and the inbox; then `shot <file.png>`, `cards`, `click "<button text>"`, `stop`. After
Python changes, rebuild the frozen core before packaging, or the .vsix carries the old one. `--real`
opens the real bench through the product path: do not press approval buttons there without the user's OK.

For debugging without VS Code, the terminal running `benchscout core` is the control channel:
at a terminal it prints a readable view (devices numbered, approvals with the commands to answer
them); `--json` or a pipe gives the JSON lines the extension will use.

`~/.local/bin/benchscout` is a `uv tool` editable install of this directory, so code
changes reach the CLI and the MCP server without reinstalling (reconnect with `/mcp`).

## Invariants (tests cover these; keep them true)

- Unknown stays unknown. A part name comes only from evidence: USB descriptor, boot log,
  JTAG IDCODE, `*IDN?`. Never add a "probably X" guess.
- Classify by effect, not read/write. `esptool chip_id` reads but resets the chip, so it
  is DISRUPTIVE. Impact propagates over the graph (`policy.py`): reprogramming an FPGA
  also disturbs the UART implemented in its fabric.
- An action (a tool definition) with no declared effect is DANGEROUS. Text sent to a device (serial, SCPI) whose effect
  neither the standard nor memory fixes is DISRUPTIVE, and a grant covers only the command word/header the human saw.
  A remembered effect applies through every entrance (one: `serial.send`; SCPI's `scpi.query`/`scpi.write`).
- Opening is cautious unless data or a confirmed memory says otherwise: a `usb.toml` entry without `may_reset_on_open`
  means "may reset". A tool argument naming the unit (`{device.serial}`, `{port.path}`) is filled only where it singles
  the unit out (`recon.unit_args`); otherwise the tool does not run. A name remembered for a unit that cannot be told
  apart is shown as a maybe. A quote is checked against the device's output less what benchscout sent (echoes).
  A call that waited for the human is judged again before it runs. A unit's memory applies only to the ports and
  targets it has now (ids follow the OS's names, which another unit may have) (docs/audit-2026-11-07.md).
- Runtime contracts (design.md §3, review 7): every open or reopen of a device port is decided by the broker on the current
  state (selected, same unit, safe to open now); an approval binds to the resolved content (bytes, argv, definition); an
  operation's effect is the strongest of its steps (open, settings, content, tool); "loosening" is judged against the
  strongest rule in force, and loosening memory applies only to a unit with a unique serial; indistinguishable units are
  never matched by guess; recon applies memory before talking to a device.
- A list of risky patterns may only add caution on top of a default that is acceptable alone: lists miss
  things. SCPI: only the standard's status/identity queries are SAFE; anything else asks, and a standing grant
  (for the session, or "always on this instrument", which the human's channel writes to memory) covers that
  command header only, within the value limits in force; an output setting asks every time. One message per call, printable ASCII
  (`tests/test_scpi.py`, `tests/test_gate_properties.py`, docs/notes.md review U9, U12).
- Every proposal carries a `why:` that is the policy's actual reason
  (`test_proposal_reason_matches_policy`).
- VISA `ASRL` resources are never sent `*IDN?`.
- Approvals come only from the human control channel, never from the agent's channel, and live
  in the core's memory (design.md §3; docs/notes.md review R3). DANGEROUS grants are bound to
  (action, port, arg) and single use; DISRUPTIVE grants may cover (action, port) for the session
  or a time limit (R8), for SCPI only the command headers the human saw. The human may also allow every
  DISRUPTIVE call for the session ("allow all", control channel only); DANGEROUS still asks each time.
  `test_core.py` and `tests/test_gate_properties.py` cover this.
- Recon (`bench_scan`) runs SAFE probes only.
- An external tool is never installed up front: an action fetches it on first use from a pinned
  official release (version + SHA-256 in `[get.<tool>]`), and only after the human's OK on that
  action's card (`fetch.py`). Nothing unpinned is downloaded.

## Working in this repo

- Real hardware from this session goes through the product path only: `benchscout core`
  (with `bench-session` as its workspace) and the MCP relay, the same gate any agent gets.
  Never open `/dev/cu.*` or run esptool, openocd or SCPI scripts directly. SAFE actions are
  fine; anything that is not SAFE (it resets or writes something) only after the user says OK
  in chat for that action. Why: this session has a shell and can edit code, so a direct probe
  could reset or rewrite a board the user is working on, and would test a path no user takes.
- The extension's tests drive fake agents that replay real sessions (`fixtures/real/claude_p_stream_*.jsonl`,
  `copilot_acp_*.jsonl`); record a new one the same way when an agent's behaviour matters, rather than editing the fakes.
- Tests read `fixtures/knowledge` as the user's knowledge layer (what the fixture benches assume, e.g. that the
  example probe's serial port is safe to open): never the developer's own `~/.config/benchscout/knowledge`.
- **Add a test only for one of these**: a safety promise (the gate, approvals, the policy; property tests where
  inputs are open-ended), something a real bench or a real OS did (record its output in `fixtures/real/` and test
  against the recording), or a regression that was found. Do not add tests that restate the implementation (retyped
  panel wording: compare with the `i18n.ts` string instead) or that test a parser against output you made up: such
  tests pass while the real system behaves otherwise (the invented WMI test passed while Windows named devices by
  their drivers). Where no recording exists yet, mark the test `@pytest.mark.assumed` and replace it once that
  system is on the bench. Prefer one end-to-end check (`npm run e2e`) over many unit tests of the same path.
- New behaviour is tested against `FixtureIo` first. When the real bench shows something,
  record it in `fixtures/` or a test so the suite keeps covering it without hardware.
  `FixtureIo` takes only what an OS or VISA gives and fails as `LiveIo` does (OSError, not None); a test checks "nothing
  was opened" with `io.opened()` and what was written with `io.sent()`, never by guessing call names (audit T5, T9, T12).
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
- **One home per concept** (design.md section 12, docs/notes.md review 9): protocols (how sent text becomes commands,
  defaults, how a device says what it is, their operations) live in `src/benchscout/protocols/`; probing tools,
  wiring (`inside`), line rules and access needs are data or one form each. The gate, recon and memory use only those
  types and never name a protocol, an action, a tool or an OS; `tests/test_structure.py` watches where such names
  appear. Before adding a branch for a new case, find the concept's home; if it has none, give it one.
- Knowledge (design.md section 5): names and models come from devices and tools, or from the
  agent (with a quoted device output); interpretation is the agent's; what was learned goes to
  memory. Shipped data in `src/benchscout/packs/bench/data/` is only for safety rules by kind of
  action. `knowledge.py` only loads and matches; `test_no_device_knowledge_in_code` keeps device,
  vendor and USB-id words out of all product code (core, relay, extension; comments may cite the bench). Before adding any device-specific entry anywhere, ask whether the agent or
  memory should own it instead (almost always yes).

## Real bench (2026-10-31)

Bench: ESP32-S3 on its native USB-Serial/JTAG + Sipeed Tang Nano 9K (GW1N(R)-9C), both running the
test firmware/bitstream from `testbench/` (known output; see testbench/README.md). Everything learned
there is in docs/notes.md (実機で学んだこと); real outputs are kept in `fixtures/real/` for the tests. Points that
still shape the code:

- ESP32-S3 native USB resets when DTR/RTS are dropped after opening; opened with the lines left
  asserted it does not (seen twice). Ports that vanish (reset, re-enumeration) are reopened.
- MCP clients start the relay with a trimmed environment (no TMPDIR): anything the core and relay
  must agree on stays independent of environment variables.
- PlatformIO's bundled esptool.py v4.5.1 could not write the ESP32-S3 over USB-Serial/JTAG;
  esptool v5.6.0 could.
- Claude Code (CLI) keeps a call open through a 95 s approval wait; results say who approved.
- A device handed to a VM (UTM) stays in macOS's USB list; only its serial port goes. Plugs are noticed by
  polling USB devices and serial ports (`Io.plugged`), and a rescan never opens a device the session is using.
