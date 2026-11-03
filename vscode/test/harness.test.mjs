// The harness against a real benchscout core on the fixture bench, with fake agents (no LLM).
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";

/** Temp folders are best-effort: on Windows a core or agent that is still exiting keeps its folder busy. */
function rmrf(dir) {
  try { rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }); } catch { /* left for the OS */ }
}
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { once } from "node:events";
import { CoreClient } from "../out/core.js";
import { Harness } from "../out/harness.js";
import { ClaudeDriver } from "../out/agents/claude.js";
import { AcpDriver } from "../out/agents/acp.js";
import { strings } from "../out/i18n.js";

const repo = resolve(import.meta.dirname, "../..");
const win = process.platform === "win32";
// BENCHSCOUT_TEST_BIN runs the same tests against the frozen core (vscode/bin/benchscout/benchscout)
const bin = process.env.BENCHSCOUT_TEST_BIN ?? join(repo, ".venv", win ? "Scripts" : "bin", win ? "benchscout.exe" : "benchscout");
const py = join(repo, ".venv", win ? "Scripts" : "bin", win ? "python.exe" : "python");
const s = strings("en");

async function bench(makeDriver) {
  const root = mkdtempSync(join(win ? tmpdir() : "/tmp", "bsh"));
  const env = { ...process.env, BENCHSCOUT_FIXTURE: join(repo, "fixtures", "bench_example.json"), BENCHSCOUT_RUNTIME: root, BENCHSCOUT_MEMORY: join(root, "memory") };
  const core = new CoreClient(bin, root, env);
  const relay = { command: bin, args: ["mcp", "--workspace", root], env: { BENCHSCOUT_RUNTIME: root } };
  const h = new Harness(core, () => makeDriver(root, relay), s, { language: "English", maxTurnsPerHour: 5 });
  core.start();
  const done = () => { h.stop(); core.stop(); rmrf(root); };
  return { core, h, root, env, done };
}

async function until(h, pred, ms = 15000) {
  const end = Date.now() + ms;
  for (;;) {
    const c = h.cards.find(pred);
    if (c) return c;
    if (Date.now() > end) throw new Error("timed out; cards: " + JSON.stringify(h.cards.map((c) => [c.kind, c.title, c.status])));
    await once(h, "changed");
  }
}

function agentCall(env, ws, method, params) {  // plays an agent using benchscout through the relay's client
  const code = `import json,sys; from pathlib import Path
from benchscout.mcp_relay import CoreClient
from benchscout.runtime import agent_address
print(json.dumps(CoreClient(agent_address(Path(sys.argv[1]))[0]).call(sys.argv[2], json.loads(sys.argv[3]), timeout=30)))`;
  const p = spawn(py, ["-c", code, ws, method, JSON.stringify(params)], { env });
  let out = "";
  p.stdout.on("data", (d) => (out += d));
  return once(p, "exit").then(() => JSON.parse(out));
}

for (const [name, make] of [
  ["Claude Code (claude -p)", (ws, relay) => new ClaudeDriver(ws, relay, join(import.meta.dirname, "fake-claude.mjs"))],
  ["ACP agent", (ws, relay) => new AcpDriver({ label: "Fake ACP", command: join(import.meta.dirname, "fake-acp-agent.mjs"), args: [], mcp: { via: "session" }, install: "-" }, ws, relay)],
]) {
  test(`${name}: benchscout asks first, wakes the agent, gates only non-benchscout tools`, async () => {
    const { core, h, done } = await bench(make);
    try {
      const first = await until(h, (c) => c.kind === "question");
      const ch340 = first.options.find((o) => o.label.includes("USB Serial"));
      assert.ok(ch340, "the first question offers each device");
      assert.ok(first.freeText, "and a way to say something else");
      void h.answer(first.id, { option: ch340.id });
      const watch = await until(h, (c) => c.kind === "confirm" && /^Watch QinHeng USB Serial\?/.test(c.title));
      assert.deepEqual(watch.options.map((o) => o.id), ["once", "deny"], "benchscout offers to watch the device it cannot open safely");
      const confirm = await until(h, (c) => c.kind === "confirm" && c.status === "open" && c !== watch);
      assert.match(confirm.detail, /rm -rf build/);   // the shell command reached the human
      assert.ok(!h.cards.some((c) => c.kind === "confirm" && /bench_scan/.test(c.title)), "benchscout's own tool was not put to the human");
      assert.deepEqual(core.selected.length, 1);
      void h.answer(confirm.id, { option: confirm.options.find((o) => /deny/i.test(o.label)).id });
      const report = await until(h, (c) => c.kind === "report" && c.status === "open");
      assert.match(report.body, /bench_scan:(allow|allow_once) shell:(deny|reject_once)/);
      assert.ok(report.options.some((o) => o.id === "continue"));
    } finally { done(); }
  });
}

test("an agent that cannot be woken is reported and nothing else is tried", async () => {
  const { h, done } = await bench((ws, relay) => new ClaudeDriver(ws, relay, "/nonexistent/claude"));
  try {
    const first = await until(h, (c) => c.kind === "question");
    void h.answer(first.id, { option: first.options[0].id });
    const n = await until(h, (c) => c.kind === "notify" && /Could not start/.test(c.title));
    assert.match(n.title, /Could not start Claude Code/);
    assert.match(n.detail, /Install Claude Code/);
    assert.equal(h.cards.filter((c) => c.kind === "report" && c.status !== "closed").length, 0);
  } finally { done(); }
});

test("broker approvals and agent questions become cards; answers go back through the control channel", async () => {
  const { core, h, root, env, done } = await bench((ws, relay) => new ClaudeDriver(ws, relay, join(import.meta.dirname, "fake-claude.mjs")));
  try {
    await until(h, (c) => c.kind === "question");
    // the agent asks to use a device -> card -> human allows -> selected
    const req = agentCall(env, root, "request_devices", { devices: ["usbserial-10"], reason: "listen to it" });
    const devCard = await until(h, (c) => c.title.includes("asks to use"));
    void h.answer(devCard.id, { option: "allow" });
    assert.equal((await req).result.status, "selected");
    // the agent wants a DISRUPTIVE action -> approval card -> allow once
    const listen = agentCall(env, root, "do", { action: "serial.listen", port: "/dev/cu.usbserial-10", args: { seconds: 0.1 } });
    const ap = await until(h, (c) => c.kind === "confirm" && c.effect === "disruptive" && c.title === s.actions["serial.listen"]);
    assert.ok(ap.options.some((o) => o.id === "session"), "DISRUPTIVE can be allowed for the session");
    void h.answer(ap.id, { option: "once" });
    assert.equal((await listen).result.status, "done");
    // the agent asks a question -> card -> the human writes their own answer
    const ask = agentCall(env, root, "ask", { question: "Which rail?", options: ["3V3", "5V"] });
    const q = await until(h, (c) => c.title === s.agentAsks && c.status === "open");
    void h.answer(q.id, { text: "the probe is on VBAT" });
    const ans = (await ask).result;
    assert.equal(ans.status, "answered");
    assert.equal(ans.reply, "the probe is on VBAT");
  } finally { done(); }
});

import { writeFileSync, readFileSync } from "node:fs";

async function streamingBench(autonomy) {
  const root = mkdtempSync(join(win ? tmpdir() : "/tmp", "bsw"));
  const data = JSON.parse(readFileSync(join(repo, "fixtures", "bench_example.json"), "utf8"));
  data.streams = { "/dev/cu.usbmodem21403": [{ after: 0.5, text: "boot ok\n" }, { after: 1.0, text: "abort() was called at PC 0x42001c85 on core 1\n" }] };
  const fx = join(root, "bench.json");
  writeFileSync(fx, JSON.stringify(data));
  const env = { ...process.env, BENCHSCOUT_FIXTURE: fx, BENCHSCOUT_RUNTIME: root, BENCHSCOUT_MEMORY: join(root, "memory") };
  const core = new CoreClient(bin, root, env);
  const relay = { command: bin, args: ["mcp", "--workspace", root], env: { BENCHSCOUT_RUNTIME: root } };
  process.env.FAKE_QUIET = "1";
  const h = new Harness(core, () => new ClaudeDriver(root, relay, join(import.meta.dirname, "fake-claude.mjs")), s,
    { language: "English", maxTurnsPerHour: 5, autonomy });
  core.start();
  return { core, h, done: () => { delete process.env.FAKE_QUIET; h.stop(); core.stop(); rmrf(root); } };
}

test("auto: a crash on a watched device wakes the agent with the line, without the human typing anything", async () => {
  const { h, done } = await streamingBench("auto");
  try {
    const first = await until(h, (c) => c.kind === "question");
    const st = first.options.find((o) => o.label.includes("STLINK"));
    await h.answer(first.id, { option: st.id });               // the only human action: pick the device
    const alert = await until(h, (c) => c.kind === "notify" && /something to look at/.test(c.title));
    assert.match(alert.body, /abort\(\) was called at PC 0x42001c85/);
    assert.match(alert.detail, /\.benchscout[\\/]logs[\\/]serial_usbmodem21403\.log/);
    const report = await until(h, (c) => c.kind === "report" && c.status === "open" && /something to look at/.test(c.title));
    assert.match(report.body, /got: benchscout noticed this on STMicroelectronics STLINK-V3/);
  } finally { done(); }
});

test("suggest: a crash only offers to investigate", async () => {
  const { h, done } = await streamingBench("suggest");
  try {
    const first = await until(h, (c) => c.kind === "question");
    const st = first.options.find((o) => o.label.includes("STLINK"));
    await h.answer(first.id, { option: st.id });
    await until(h, (c) => c.kind === "report" && c.status === "open");  // the selection itself wakes the agent once
    const offer = await until(h, (c) => c.kind === "question" && /something to look at/.test(c.title));
    assert.deepEqual(offer.options.map((o) => o.id), ["go", "ignore"]);
    assert.equal(h.cards.filter((c) => c.kind === "report" && /something to look at/.test(c.title)).length, 0);
  } finally { done(); }
});

test("memory: plain entries are recorded, safety-loosening ones wait for the human", async () => {
  const { h, root, env, done } = await bench((ws, relay) => new ClaudeDriver(ws, relay, join(import.meta.dirname, "fake-claude.mjs")));
  try {
    await until(h, (c) => c.kind === "question");
    await agentCall(env, root, "remember", { ref: "usbserial-10", kind: "config", text: "CH1 is on the 3V3 rail" });
    const rec = await until(h, (c) => c.kind === "memory" && /Remembered about QinHeng USB Serial/.test(c.title));
    assert.match(rec.body, /CH1 is on the 3V3 rail/);
    assert.deepEqual(rec.options.map((o) => o.id), ["forget"]);
    const res = (await agentCall(env, root, "remember", { ref: "/dev/cu.usbserial-10", kind: "safety", text: "no auto-reset circuit", data: { listen_safe: true } })).result;
    assert.equal(res.status, "pending_human");
    const pend = await until(h, (c) => c.kind === "memory" && /loosens safety/.test(c.title));
    void h.answer(pend.id, { option: "accept" });
    await until(h, (c) => c.id === pend.id && c.status === "closed");
    const recall = (await agentCall(env, root, "recall", { ref: "usbserial-10" })).result;
    assert.equal(recall[0].entries.find((e) => e.kind === "safety").status, "active");
  } finally { done(); }
});

test("an offered watch, once allowed, starts watching and says so", async () => {
  const { core, h, done } = await bench((ws, relay) => new ClaudeDriver(ws, relay, join(import.meta.dirname, "fake-claude.mjs")));
  try {
    const first = await until(h, (c) => c.kind === "question");
    await core.select([first.options.find((o) => o.label.includes("USB Serial")).id]);  // as the panel does, without waking anyone
    const watch = await until(h, (c) => c.kind === "confirm" && /^Watch /.test(c.title));
    assert.match(watch.body, /notice crashes and resets/);
    assert.match(watch.body, /\nOpening this port can reset the device/, "English: the core's own words, capitalised");
    void h.answer(watch.id, { option: "once" });
    const on = await until(h, (c) => c.kind === "notify" && /^Watching /.test(c.title));
    assert.match(on.detail, /\.benchscout[\\/]logs[\\/]serial_usbserial-10\.log/);
    assert.ok(!h.cards.some((c) => c.kind === "report"), "watching wakes nobody");
  } finally { done(); }
});

test("with no agent chosen, a pick waits for the choice and then wakes the chosen one", async () => {
  process.env.FAKE_QUIET = "1";
  let chosen = false;
  const { WakeError } = await import("../out/agents/types.js");
  const { h, done } = await bench((ws, relay) => chosen ? new ClaudeDriver(ws, relay, join(import.meta.dirname, "fake-claude.mjs")) : ({
    label: "benchscout", onEvent: () => {}, onPermission: async () => "reject", prompt: async () => ({ stopReason: "error", text: "" }), stop: () => {},
    start: async () => { throw new WakeError("No agent is chosen", "choose", "no_agent"); },
  }));
  try {
    await until(h, (c) => c.kind === "question" && c.title === s.firstAsk);
    void h.tell("what is on the bench?");
    const wait = await until(h, (c) => c.kind === "notify" && c.title === s.chooseAgentFirst);
    assert.ok(wait.pin);
    assert.ok(!h.cards.some((c) => /Could not start/.test(c.title)), "not choosing yet is not a failure");
    assert.ok(!h.cards.some((c) => c.kind === "report"));
    chosen = true;
    h.resetAgent();  // what choosing an agent in the card does
    const report = await until(h, (c) => c.kind === "report" && c.status === "open");
    assert.match(report.body, /got: what is on the bench\?/);
    assert.equal(h.cards.find((c) => c.id === wait.id).status, "closed");
  } finally { delete process.env.FAKE_QUIET; done(); }
});

test("each turn's progress goes to its own card, one line per tool call, updated in place", async () => {
  process.env.FAKE_QUIET = "1";
  const { h, done } = await bench((ws, relay) => new ClaudeDriver(ws, relay, join(import.meta.dirname, "fake-claude.mjs")));
  try {
    await until(h, (c) => c.kind === "question");
    await h.tell("first");
    const one = h.cards.find((c) => c.kind === "report");
    assert.deepEqual(one.progress, ["✓ bench_scan"]);
    await h.tell("second");
    const two = h.cards.filter((c) => c.kind === "report")[1];
    assert.deepEqual(one.progress, ["✓ bench_scan"], "the second turn wrote nothing into the first card");
    assert.deepEqual(two.progress, ["✓ bench_scan"]);
    assert.match(two.body, /got: second/);
  } finally { delete process.env.FAKE_QUIET; done(); }
});

test("approval reasons are shown in the person's language by the core's code", async () => {
  const ja = strings("ja");
  const root = mkdtempSync(join(win ? tmpdir() : "/tmp", "bsj"));
  const env = { ...process.env, BENCHSCOUT_FIXTURE: join(repo, "fixtures", "bench_example.json"), BENCHSCOUT_RUNTIME: root, BENCHSCOUT_MEMORY: join(root, "memory") };
  const core = new CoreClient(bin, root, env);
  const h = new Harness(core, () => { throw new Error("not woken here"); }, ja, { language: "Japanese", maxTurnsPerHour: 5 });
  core.start();
  try {
    const first = await until(h, (c) => c.kind === "question");
    await core.select([first.options.find((o) => o.label.includes("QinHeng")).id]);
    const watch = await until(h, (c) => c.kind === "confirm");
    assert.match(watch.title, /QinHeng USB Serial を見張りますか/);
    assert.match(watch.body, /このポートを開くと機器がリセットされることがあります/);
  } finally { core.stop(); rmrf(root); }
});

test("benchscout's own safety proposal is said in the person's language; the agent's words stay", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const h = new Harness(core, () => { throw new Error("not woken"); }, strings("ja"), { language: "Japanese", maxTurnsPerHour: 1 });
  const entry = (over) => ({ id: "e1", kind: "safety", text: "Listening to /dev/cu.x did not reset X (2 times). Treat listening as SAFE?",
    source: "benchscout", evidence: "", target: "serial:usbmodem1101", time: "2026-11-01T15:49:00", status: "pending", data: { listen_safe: true }, ...over });
  core.emit("memoryAdded", { key: "k", device_name: "ESP32", entry: entry({}) });
  core.emit("memoryAdded", { key: "k", device_name: "ESP32", entry: entry({ id: "e2", source: "agent", text: "no auto-reset circuit" }) });
  const [own, agent] = h.cards;
  assert.equal(own.body, strings("ja").listenSafeAsk("serial:usbmodem1101", 2), "benchscout's own words, in the person's language");
  assert.equal(agent.body, "no auto-reset circuit");
});

test("a port lost while the computer slept is one quiet, counted record; nobody is woken", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  let woken = 0;
  const h = new Harness(core, () => { woken++; throw new Error("not woken"); }, strings("ja"), { language: "Japanese", maxTurnsPerHour: 5 });
  const ev = (kind) => ({ port: "serial:usbserial-101", device: "d1", device_name: "SIPEED JTAG Debugger", kind, what: "x", cause: "host_sleep" });
  for (let i = 0; i < 2; i++) { core.emit("deviceEvent", ev("vanished")); core.emit("deviceEvent", ev("returned")); }
  assert.equal(h.cards.length, 1);
  assert.equal(h.cards[0].status, "closed");
  assert.match(h.cards[0].title, /Mac のスリープで SIPEED JTAG Debugger のポートが切れ、開き直しました\(2 回\)/);
  assert.equal(woken, 0);
});

test("an approval that downloads a tool first says so on the card", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const h = new Harness(core, () => { throw new Error("not woken"); }, strings("ja"), { language: "Japanese", maxTurnsPerHour: 1 });
  core.emit("approvalRequested", { id: "a1", action: "esptool.write_flash", port: "serial:usbmodem1101", args: "{}", effect: "dangerous",
    status: "pending", impact: { reason: "overwrites flash", affected_ports: [], disturbs: [], code: "action:esptool.write_flash" },
    by: "agent", fetch: "esptool 5.6.0 from github.com (official release, SHA-256 checked, about 63 MB)" });
  assert.match(h.cards[0].body, /先にダウンロードするもの: esptool 5\.6\.0 from github\.com/);
});

test("a SCPI command's card allows that command for the session or always, and says that is all it covers", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const decided = [];
  core.decide = async (...a) => { decided.push(a); return {}; };
  const h = new Harness(core, () => { throw new Error("not woken"); }, strings("ja"), { language: "Japanese", maxTurnsPerHour: 1 });
  const ask = (id, effect, subjects) => core.emit("approvalRequested", { id, action: "scpi.write", port: "visa:TCPIP0::192.168.1.50::INSTR",
    args: JSON.stringify({ command: "DISP:BRIG 5" }), effect, status: "pending", by: "agent",
    impact: { reason: "x", affected_ports: [], disturbs: [], code: subjects ? "scpi_command" : "energizing", subjects } });
  ask("a1", "disruptive", ["DISP:BRIG"]);
  const card = h.cards.find((c) => c.id === "approval:a1");
  assert.deepEqual(card.options.map((o) => o.id), ["once", "session", "always", "all", "deny"]);
  assert.ok(card.body.includes(strings("ja").commandCovers), "the card says the grant covers this command only");
  void h.answer(card.id, { option: "always" });
  assert.deepEqual(decided[0], ["a1", true, "always"]);
  ask("a2", "dangerous", ["VOLT"]);  // an output setting: once, or not at all
  assert.deepEqual(h.cards.find((c) => c.id === "approval:a2").options.map((o) => o.id), ["once", "deny"]);
});

test("allow all is on DISRUPTIVE cards only, and the inbox shows it with a way to turn it off", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const decided = [];
  core.decide = async (...a) => { decided.push(a); return {}; };
  const h = new Harness(core, () => { throw new Error("not woken"); }, strings("ja"), { language: "Japanese", maxTurnsPerHour: 1 });
  const ask = (id, action, effect) => core.emit("approvalRequested", { id, action, port: "serial:usbserial-10", args: "{}", effect,
    status: "pending", by: "agent", impact: { reason: "x", affected_ports: [], disturbs: [], code: "" } });
  ask("a1", "serial.send", "disruptive");
  ask("a2", "esptool.write_flash", "dangerous");
  assert.ok(h.cards.find((c) => c.id === "approval:a1").options.some((o) => o.id === "all"));
  assert.ok(!h.cards.find((c) => c.id === "approval:a2").options.some((o) => o.id === "all"), "what cannot be undone asks each time");
  void h.answer("approval:a1", { option: "all" });
  assert.deepEqual(decided[0], ["a1", true, "all"]);
});

test("a tool's first-use download is one card, updated as it goes and closed when done", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const h = new Harness(core, () => { throw new Error("not woken"); }, strings("ja"), { language: "Japanese", maxTurnsPerHour: 1 });
  const ev = (got, state) => core.emit("download", { tool: "esptool", version: "5.6.0", got, total: 65_000_000, state });
  ev(0, "running");
  ev(32_000_000, "running");
  assert.equal(h.cards.length, 1);
  assert.equal(h.cards[0].title, strings("ja").downloading("esptool 5.6.0"));
  assert.ok(h.cards[0].body.startsWith(strings("ja").downloadBody("32", "65").slice(0, 7)), "progress in MB");
  assert.equal(h.cards[0].status, "open");
  ev(65_000_000, "done");
  assert.equal(h.cards.length, 1);
  assert.equal(h.cards[0].title, strings("ja").downloaded("esptool 5.6.0"));
  assert.equal(h.cards[0].status, "closed");
});

test("Windows: a device that needs a driver gets one card saying which interface and how, closed once it is right", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const h = new Harness(core, () => { throw new Error("not woken"); }, strings("ja"), { language: "Japanese", maxTurnsPerHour: 1 });
  const bench = (dev) => ({ devices: [{ id: "usb:0403:6010:x", name: "SIPEED JTAG Debugger", evidence: [], ...dev }], ports: [], targets: [], links: [] });
  core.emit("scan", bench({ access: [{ need: "driver", interfaces: [0, 1] }] }), []);
  const card = () => h.cards.find((c) => c.id === "driver:usb:0403:6010:x");
  assert.equal(card().body, strings("ja").driverBody("0, 1", ""), "which interfaces lack a driver");
  assert.ok(h.cards.find((c) => c.kind === "question").body.includes(`SIPEED JTAG Debugger (${strings("ja").stateNoDriver})`));
  core.emit("scan", bench({ access: [{ need: "winusb", interfaces: [0], tools: ["openFPGALoader"] }] }), []);  // the maker's driver came; openFPGALoader still needs WinUSB
  assert.equal(h.cards.filter((c) => c.id.startsWith("driver:")).length, 1);
  assert.equal(card().body, strings("ja").driverBody("", "0"), "which interface needs WinUSB, with how");
  core.emit("scan", bench({}), []);  // after Zadig and a rescan
  assert.equal(card().status, "closed");
});

test("a tool the agent proposes is a card with the exact command, its effect and the agent's source", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const decided = [];
  core.toolDecide = async (...a) => { decided.push(a); return {}; };
  const h = new Harness(core, () => { throw new Error("not woken"); }, strings("ja"), { language: "Japanese", maxTurnsPerHour: 1 });
  core.emit("toolProposed", { id: "p1", status: "pending", evidence: "vendor guide ch. 4",
    tool: { name: "vendortool.program", argv: ["vendortool", "--program", "{bitstream}"], effect: "dangerous", reason: "loads a bitstream" },
    probes: [{ vid: 0x03fd, pid: 0x0013, probe: "jtag" }] });
  const card = h.cards.find((c) => c.id === "tool:p1");
  assert.ok(card.body.includes("`vendortool --program {bitstream}`"), "the exact command, as it will run");
  assert.ok(card.body.includes("vendor guide ch. 4"), "the agent's source");
  assert.match(card.body, /03fd:0013 \(jtag\)/);
  assert.equal(card.effect, "dangerous");
  void h.answer("tool:p1", { option: "accept" });
  assert.deepEqual(decided[0], ["p1", true]);
});

test("Linux: a device a libusb tool cannot open gets one card naming the node and the udev fix", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const h = new Harness(core, () => { throw new Error("not woken"); }, strings("ja"), { language: "Japanese", maxTurnsPerHour: 1 });
  const bench = (dev) => ({ devices: [{ id: "usb:0403:6010:x", name: "SIPEED JTAG Debugger", evidence: [], ...dev }], ports: [], targets: [], links: [] });
  core.emit("scan", bench({ access: [{ need: "udev", node: "/dev/bus/usb/003/002", tools: ["openFPGALoader"] }] }), []);
  const card = () => h.cards.find((c) => c.id === "driver:usb:0403:6010:x");
  assert.equal(card().title, strings("ja").udevTitle("SIPEED JTAG Debugger"));
  assert.equal(card().body, strings("ja").udevBody("/dev/bus/usb/003/002"));
  core.emit("scan", bench({}), []);  // the rule took effect
  assert.equal(card().status, "closed");
});

test("with only devices benchscout has no data for, the first question still offers them to the agent", () => {
  // review 7 (17): the first question and the device list showed nothing, so an unknown programmer had no way in
  const core = new CoreClient("/nonexistent", tmpdir());
  const s = strings("en");
  const h = new Harness(core, () => { throw new Error("not woken"); }, s, { language: "English", maxTurnsPerHour: 1 });
  const bench = { devices: [{ id: "usb:1234:5678:x", name: "Acme Cable", evidence: [], other: true }], ports: [], targets: [], links: [] };
  core.emit("scan", bench, [], []);
  const q = h.cards.find((c) => c.kind === "question");
  assert.ok(q, "a first question is asked");
  assert.deepEqual(q.options.map((o) => o.label), [s.lookOthers(1)]);
  assert.match(q.body, /Acme Cable/);
});


test("'always on this device' is offered for a command whatever its protocol (the core keeps it for any)", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const s = strings("en");
  const h = new Harness(core, () => { throw new Error("not woken"); }, s, { language: "English", maxTurnsPerHour: 1 });
  core.emit("approvalRequested", { id: "a1", action: "serial.send", port: "serial:x", args: '{"data": "status"}', effect: "disruptive",
    status: "pending", by: "agent", impact: { reason: "sends bytes", affected_ports: [], disturbs: [], subjects: ["status"] } });
  const card = h.cards.find((c) => c.id === "approval:a1");
  assert.ok(card.options.some((o) => o.label === s.allowCommandSession) && card.options.some((o) => o.label === s.allowAlwaysHere));
});
