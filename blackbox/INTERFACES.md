# How to drive benchscout from outside

benchscout has three faces. A black-box test uses these and nothing else; `harness.py` wraps them.

```
 the human (VS Code's inbox)          the agent (Claude Code, Copilot, ...)
   control channel                       MCP tools
   `benchscout core --json`              `benchscout mcp` (started in the same workspace)
   stdin/stdout, JSON lines              stdin/stdout, MCP (JSON-RPC)
            \                               /
             \______ the core (one per workspace) ______/
                              |
                     the bench (here: a fake one, a JSON file)
```

The two channels are deliberately unequal: approvals, device selection, "allow all" and confirming memory exist only
on the control channel. The agent can ask, never decide.

## Starting it (what `harness.World` does)

```
benchscout core --workspace <dir> --json --wait <seconds>     # the human's side; stdin closing stops it
benchscout mcp                                                 # the agent's side, started with cwd = <dir>
```

`--wait`: how long an agent's call that needs the human waits for a decision before it answers
`approval_required` (the agent then calls again with the approval id). 0 answers at once.

Environment (set for both processes; `World` does it):

| Variable | Meaning |
|---|---|
| `BENCHSCOUT_FIXTURE` | the fake bench file, read instead of the OS (USB devices, serial ports, VISA instruments) |
| `BENCHSCOUT_FIXTURE_LOG` | a file the fake bench appends what reached it to (below) |
| `BENCHSCOUT_RUNTIME` | the folder for the agent channel's socket and the device locks (short path: a Unix socket's path is limited) |
| `BENCHSCOUT_MEMORY` | the user's memory folder (per device) |
| `BENCHSCOUT_KNOWLEDGE` | the user's knowledge files (what the example bench assumes, e.g. which port is safe to open) |
| `BENCHSCOUT_TOOLS` | where downloaded tools go |
| `BENCHSCOUT_APPROVAL_WAIT` | the relay's side of `--wait` (give both the same number) |

Two cores given the same `BENCHSCOUT_RUNTIME` and bench are two VS Code windows on one machine: they share the device
locks. `--no-workspace-knowledge` is what VS Code passes for a folder the person has not trusted.

## The control channel (the human)

Request: `{"id": "1", "method": "<method>", "params": {...}}` -> `{"id": "1", "result": ...}` or `{"id": "1", "error": "..."}`.
Events arrive unasked on the same stdout: `{"event": "<name>", ...}`.

| Method | Params | What it does |
|---|---|---|
| `scan` | `only_new` (bool) | read the bench again; result: `text` (a readable report), `bench` (`devices`, `ports`, `targets`, `links`, `tools`), `proposals`, `selected`, `knowledge_errors` |
| `inventory` | | the last scan, as above |
| `select` | `devices`: list of device ids | the session's devices become exactly these; result: `selected`, `busy` (held by another window), `watching` |
| `pending` | | the approval cards waiting for the human |
| `decide` | `approval_id`, `granted` (bool), `scope`: `once` / `session` / `always` / `all` / minutes (int) | the human's answer to a card. `always`: remember for this device; `all`: allow every DISRUPTIVE call for the session. A scope the card does not offer is refused |
| `allow_all` | `on` (bool) | allow (or stop allowing) every DISRUPTIVE call for the session |
| `questions` | | the agent's questions waiting for an answer |
| `answer` | `question_id`, `choice` or `reply` | answer one |
| `memory` | `ref` (optional) | what is remembered, per device |
| `memory_view` | | the same, as the inbox lists it (with each device's memory file) |
| `memory_decide` | `key`, `id`, `accept` (bool) | confirm or reject a memory entry waiting for the human (one that would ask less) |
| `memory_delete` | `key`, `id` | forget an entry |
| `tool_decide` | `id`, `accept` (bool) | accept or reject a tool definition the agent proposed |
| `run_state` | `job`, `stop` (bool) | a program run's state, or stop it |
| `shutdown` | | stop (closing stdin does the same) |

Events: `ready`, `scanned`, `bench`, `devices_changed`, `selection_changed`, `approval_requested`, `approval_decided`,
`allow_all`, `memory_added`, `memory_decided`, `memory_forgotten`, `question_asked`, `question_answered`,
`tool_proposed`, `tool_decided`, `device_event` (watching, output, a crash, a port gone, ...), `run_changed`, `download`,
`stopped`. An `approval_requested` event carries the card: `id`, `action`, `port`, `args` (the exact arguments, as
JSON), `effect` (`disruptive` / `dangerous`), `impact` (what else it disturbs: `disturbs`, `affected_ports`, `reason`),
`by` (`agent` or `benchscout`), `again` (what "for the session" would cover on a DANGEROUS card; empty: once only),
`always`, `status`.

Effects: SAFE (runs without asking), DISRUPTIVE (may disturb a device: asks; may be allowed for a while), DANGEROUS
(may change it for good: asks every time, except where the card offers "for the session").

## The MCP tools (the agent)

`harness.Agent.tools()` lists them with their full descriptions and argument schemas; a copy taken from the program
under test is at the end of this file (an exported kit has it). Most answer JSON text; a failed call is an MCP tool
error (`isError`). The main ones: `bench_scan`, `bench_inventory_json`, `bench_impact` (what an action would disturb),
`bench_do` (perform an action: `action`, `port`, `args`, `approval_id`), `bench_identify` (name a part by quoting the
device's own output), `bench_remember` / `bench_recall`, `bench_log`, `bench_define_tool`, `bench_register_resource`,
`bench_run` / `bench_job` (a program the agent brings), `bench_recipe`. `bench_do` answers a `status`: `done`,
`approval_required` (with `approval_id`), `rejected`, `not_selected`, `approval_mismatch`, `busy`, `invalid_args`, ...
(each with a `message` saying why).

## The fake bench

A JSON file standing in for what the OS and VISA report. Only what an OS gives may be in it (an unknown field is
refused). The core reads it again when it changes: writing a device in plugs it in, taking it out unplugs it
(noticed within a few seconds).

| Key | What |
|---|---|
| `usb` | USB devices: `vid`, `pid` (hex strings, `"0x303a"`), `serial`, `location`, `name`, `vendor`, `device_class`, ... |
| `serial` | serial ports: `device` (its path), `vid`, `pid`, `serial_number`, `location`, `interface`, `description` |
| `visa` | instruments: `{resource: {command: answer}}`; `{resource: {"error": "..."}}` for one that fails |
| `visa_raw` | binary answers: `{resource: {command: hex}}` |
| `listen` | what a port prints when listened to: `{path: text}`; `{"error": "..."}` vanishes when opened; `{"then": text}` vanishes and comes back |
| `replies` | what a device answers: `{path: {text sent: answer}}` |
| `streams` | a port held open by a watch: `{path: [{"after": s, "text": ...} or {"after": s, "vanish": s}]}` |
| `probes` | a probing tool's output, by one of its arguments |
| `tools` | the external tools installed on this machine (names) |

`bench/bench_example.json` (an FPGA board, an ESP32-S3, a USB-serial bridge, a debug probe, an oscilloscope, a LAN
power supply) and `bench/knowledge/` (what the person told benchscout about it) are the example. Make new benches as
files in `bench/`.

## What reached the bench

With `BENCHSCOUT_FIXTURE_LOG`, every access appends one JSON line `{"what", "where", "data"}`: what a real device would
have felt. `harness.Bench.seen()` reads them; `sent()` and `opened()` sum up the serial ones.

| `what` | `where` | `data` |
|---|---|---|
| `serial_listen` | port path | (opened to listen) |
| `serial_send` | port path | the text written (opened to send) |
| `serial_stream` | port path | (opened and held, for a watch) |
| `serial_write` | port path | the text written through a watch that holds the port |
| `visa_query`, `visa_query_raw` | resource | the command |
| `visa_write` | resource | the command |
| `probe` | the tool's arguments | |

Programs the agent runs (`bench_run`) are real processes: what they did is what they leave behind (write a file).
