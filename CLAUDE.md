# benchscout

Bench recon broker for hardware engineers. Open it and the agent tells you what is
plugged in, what each thing is (with evidence), what is unknown, and what to look at
next, without being asked. Hardware access goes only through the MCP broker, which
classifies every action by its effect on device state and gates anything not SAFE
behind a human approval. See README.md for the graph model and the layer table.

## Commands

```
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
- MCP tool docstrings in `mcp_server.py` are what the agent sees; keep them accurate.

## Open findings from the first real-hardware run (2026-10-31)

Bench: Espressif USB-Serial/JTAG (ESP32 native USB) + Sipeed Tang Nano (GW1N(R)-9C, IDCODE `0x0100481B`).

- `bench_impact` / `bench_do` with `port="/dev/cu.usbmodem1101"` fail with no message;
  `port="serial:usbmodem1101"` works. The scan prints both forms, so the tools should
  accept either, or fail with a message naming the expected id.
- `bench_do serial.listen serial:usbserial-101 arg=9600` did not return within 120 s.
  Check the listen window and that `arg` is parsed as a baud rate.
- The fabric UART on the Tang Nano was silent at the default rate; not yet known whether
  that is baud or bitstream.
