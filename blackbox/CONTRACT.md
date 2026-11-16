# What benchscout promises, as seen from outside

A summary of `design.md` (Japanese; the source of truth) in terms a black-box test can check: the two channels and
what reached the bench (INTERFACES.md). Section numbers are design.md's; dates are entries of its decision log (§12).
This list was written by someone who has read the code, so it may share the code's blind spots: **design.md decides**.
Where design.md promises something this list leaves out, or says it differently, test design.md and say so in
FINDINGS.md.

## The session and the devices

- **S1 Nothing happens to a device the human has not selected.** An agent's action on a device outside the session is
  refused, and nothing reaches it (§3).
- **S2 The first read asks no device anything that could change it.** Scanning only runs SAFE probes: nothing is
  written to a serial port, no instrument is sent a command, a port is opened to listen only where that is known to
  be safe, and a VISA serial (`ASRL`) resource is never sent `*IDN?` (§3, §17).
- **S3 One window at a time per device.** A device selected by one core is reported busy to another core on the same
  machine until the first lets it go (closing the control channel lets everything go) (§3).
- **S4 A device pulled out and plugged back is noticed**, and whatever was allowed for it before is not allowed any
  more (§17, 2026-11-03).

## The gate

- **G1 Anything not SAFE waits for the human.** The agent's call answers `approval_required` (or waits, with `--wait`),
  and nothing reaches the device until the human grants it on the control channel (§3).
- **G2 The agent cannot decide.** No MCP tool approves, selects, turns on "allow all", or confirms memory; an approval
  id the agent invents or reuses for another call does nothing (§3, review R3).
- **G3 An OK is for what the card showed.** "Once" covers exactly that action, port and arguments, one time. The card
  shows the exact bytes, command or program that will run (§3, 2026-11-04).
- **G4 Effect, not read or write.** An action that only reads but resets the chip asks. An action with no declared
  effect is DANGEROUS. Text sent to a device whose effect neither the standard nor memory fixes is DISRUPTIVE (§3).
- **G5 What may last, and how long.** DISRUPTIVE: once, for N minutes, for the session, "always on this device"
  (remembered), or "allow all" (every DISRUPTIVE call, for the session). For SCPI a lasting OK covers only the command
  headers the human saw. DANGEROUS: once, except where the card offers "for the session" for that call: an agent's
  program while its content is unchanged, a write the data allows it for (same tool, same device), an output setting
  up to the value on the card. "Allow all" never covers DANGEROUS (2026-11-08).
- **G6 A lasting OK ends** when the session ends, when a device the call touches leaves the session, or when a rule
  learned since makes the call need more than the OK gave (2026-11-16).
- **G7 Waiting changes nothing.** A call that waited for the human is judged again when the answer comes: if the
  device left, the content changed or a rule was learned meanwhile, it does not run as approved (§3, review 7).
- **G8 Instruments (SCPI).** Only the standard's status and identity queries are SAFE by name; anything else asks.
  One message per call, printable ASCII only (a line break would smuggle a second message). An output setting asks
  and may be allowed for the session up to the value on its card, from 0 (2026-11-02, 2026-11-08).
- **G9 Opening is cautious.** A serial port benchscout knows nothing about may reset what is behind it when opened, so
  opening it asks, unless data or memory the human confirmed says it is safe (§3, §6).

## Names

- **N1 Unknown stays unknown.** A part is named only from what the device or a tool reported, or by the agent quoting
  the device's own output, which benchscout finds in what it captured from that device (not in what it sent there).
  Anything else is a guess, shown apart from the name (§3 "名前の付け方", §5).
- **N2 Every USB device is listed** with its raw clues, unknown ones too, and nothing is guessed about them (§5).

## Memory

- **M1 Written without asking.** What the agent and benchscout learn is remembered at once; only an entry that would
  make benchscout ask less (e.g. "this port does not reset when opened", "always allow this command") waits for the
  human to confirm on the control channel, and has no effect until then (§6).
- **M2 Kept across sessions**: a new core on the same bench shows what was remembered at its first read, before any
  agent is involved. Approvals are not kept (except "always", which is memory) (§6, review R4).
- **M3 One unit's memory is that unit's.** It never applies to another unit, of another product or of the same
  product with another serial. A name remembered for a unit that cannot be told apart from others (no serial, or a
  serial every unit of its model shares) is shown as a maybe, not as its name; memory that asks less applies only to a
  unit that can be told apart, or to the only unit plugged in of a model whose units all share a serial (§6, 2026-11-08).
- **M4 A folder the person does not trust** (`--no-workspace-knowledge`): its own knowledge and memory files are not
  read (§3, S2).

## The agent's own programs

- **P1 A program runs only on the selected resources the human approved it for**, with the arguments and input files
  (their content) on the card; a changed file asks again. It can be stopped, and deselecting its device stops it
  (§4, 2026-11-04).

## Tools

- **T1 Nothing is installed up front.** A tool missing on this machine is downloaded only after the human's OK on that
  action's card, from a pinned official release (§3, §15).
