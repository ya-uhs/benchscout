// The harness against a real benchscout core on the fixture bench, with fake agents (no LLM).
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";

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
import { S } from "../out/wording.js";

const repo = resolve(import.meta.dirname, "../..");
const win = process.platform === "win32";
// BENCHSCOUT_TEST_BIN runs the same tests against the frozen core (vscode/bin/benchscout/benchscout)
const bin = process.env.BENCHSCOUT_TEST_BIN ?? join(repo, ".venv", win ? "Scripts" : "bin", win ? "benchscout.exe" : "benchscout");
const py = join(repo, ".venv", win ? "Scripts" : "bin", win ? "python.exe" : "python");
const s = S;
const STLINK = "STMicroelectronics STLINK-V3";  // the device the streaming bench's crash comes from
// What the real agents did in one session each (the fakes replay them): the shell command they asked about, their last
// words, and what was answered to their permission asks (audit 2026-11-07, T21, T27)
const recorded = (file) => readFileSync(join(repo, "fixtures", "real", file), "utf8").split("\n").filter(Boolean)
  .map((l) => JSON.parse(l)).filter((l) => l.dir);
const CLAUDE = recorded("claude_p_stream_2026-11-08.jsonl");
const CLAUDE_SESSION = {
  shell: CLAUDE.find((l) => l.msg.type === "control_request" && l.msg.request.tool_name === "Bash").msg.request.input.command,
  said: CLAUDE.findLast((l) => l.msg.type === "assistant" && l.msg.message.content.some((b) => b.type === "text"))
    .msg.message.content.find((b) => b.type === "text").text.trim(),
  answers: CLAUDE.filter((l) => l.dir === "in" && l.msg.type === "control_response").map((l) => l.msg.response.response.behavior),
  answer: (logged) => logged.response.behavior,
};
const COPILOT = recorded("copilot_acp_2026-11-08.jsonl");
// the title of the tool the recorded Copilot asked about and was denied
const COPILOT_DENIED = COPILOT.find((l) => l.msg.method === "session/request_permission").msg.params.toolCall.title;
const COPILOT_SESSION = {
  shell: COPILOT.find((l) => l.msg.method === "session/request_permission").msg.params.toolCall.rawInput.command,
  said: COPILOT.filter((l) => l.msg.params?.update?.sessionUpdate === "agent_message_chunk").map((l) => l.msg.params.update.content.text).join("").trim(),
  answers: COPILOT.filter((l) => l.dir === "in" && "result" in l.msg).map((l) => l.msg.result.outcome.optionId),
  answer: (logged) => logged.response.outcome.optionId,
};

async function bench(makeDriver) {
  const root = mkdtempSync(join(win ? tmpdir() : "/tmp", "bsh"));
  const env = { ...process.env, BENCHSCOUT_FIXTURE: join(repo, "fixtures", "bench_example.json"), BENCHSCOUT_RUNTIME: root, BENCHSCOUT_MEMORY: join(root, "memory"), BENCHSCOUT_KNOWLEDGE: join(repo, "fixtures", "knowledge") };
  const core = new CoreClient(bin, root, env);
  const relay = { command: bin, args: ["mcp", "--workspace", root], env: { BENCHSCOUT_RUNTIME: root } };
  const h = new Harness(core, () => makeDriver(root, relay), s, { language: "English", maxTurnsPerHour: 5 });
  core.start();
  const done = () => { h.stop(); core.stop(); rmrf(root); };
  return { core, h, root, env, done };
}

/** The first card matching `pred`, waiting at most `ms`. It waited for the next change with no time limit, so a card that
 *  never came hung the run past the test's own time-out, its core left running (audit 2026-11-07, T23). */
async function until(h, pred, ms = 15000) {
  const end = Date.now() + ms;
  for (;;) {
    const c = h.cards.find(pred);
    if (c) return c;
    const left = end - Date.now();
    if (left <= 0) throw new Error("timed out; cards: " + JSON.stringify(h.cards.map((c) => [c.kind, c.title, c.status])));
    let timer;
    await Promise.race([once(h, "changed"), new Promise((r) => { timer = setTimeout(r, left); })]);
    clearTimeout(timer);
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

for (const [name, make, real] of [
  ["Claude Code (claude -p)", (ws, relay) => new ClaudeDriver(ws, relay, join(import.meta.dirname, "fake-claude.mjs")), CLAUDE_SESSION],
  ["ACP agent (Copilot CLI)", (ws, relay) => new AcpDriver({ label: "Fake ACP", command: join(import.meta.dirname, "fake-acp-agent.mjs"), args: [], mcp: { via: "session" }, install: "-" }, ws, relay), COPILOT_SESSION],
]) {
  // benchscout's own tools are not put to the human: claude -p names their server and the harness allows them; Copilot
  // CLI does not ask for them (--allow-tool=benchscout, acp.ts). The broker gates what they do
  test(`${name}: benchscout asks first, wakes the agent, and the agent's own tools wait for the human`, async () => {
    const log = join(mkdtempSync(join(tmpdir(), "bsfc")), "answers.jsonl");  // what the driver answered the fake
    process.env.FAKE_CLAUDE_LOG = process.env.FAKE_ACP_LOG = log;
    const { core, h, done } = await bench(make);
    try {
      const first = await until(h, (c) => c.kind === "question");
      const ch340 = first.options.find((o) => o.label.includes("USB Serial"));
      assert.ok(ch340, "the first question offers each device");
      assert.ok(first.freeText, "and a way to say something else");
      void h.answer(first.id, { option: ch340.id });
      const watch = await until(h, (c) => c.kind === "confirm" && c.title === s.offerWatch("QinHeng USB Serial"));
      assert.deepEqual(watch.options.map((o) => o.id), ["once", "deny"], "benchscout offers to watch the device it cannot open safely");
      const confirm = await until(h, (c) => c.kind === "confirm" && c.status === "open" && c !== watch && c.detail.includes(real.shell));
      const asked = h.cards.filter((c) => c.kind === "confirm" && c.title.startsWith(s.agentWantsTool));
      assert.deepEqual(asked, [confirm], "of the agent's tools, only the shell command reached the human");
      assert.deepEqual(core.selected.length, 1);
      // "No, and tell it what to do differently" (Claude Code's and Copilot CLI's own prompts): words in the box deny it
      assert.equal(confirm.placeholder, s.denyInstead);
      void h.answer(confirm.id, { text: "list the folder instead" });
      const report = await until(h, (c) => c.kind === "report" && c.status === "open");
      assert.ok(report.options.some((o) => o.id === "continue"));
      const logged = () => readFileSync(log, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l));
      assert.deepEqual(logged().filter((l) => l.response).map(real.answer), real.answers, "answered as in the recorded session");
      if (name.startsWith("Claude")) {
        // Claude Code takes the words with the denial and goes on by itself
        assert.equal(report.body, real.said);
        assert.match(logged().find((l) => l.tool === "Bash").response.message, /list the folder instead/);
      } else {
        // Copilot CLI ends its turn on a denial (recorded): benchscout says so and asks it to go on, with the words
        assert.equal(report.body, `${real.said}\n\n${s.deniedGoesOn(COPILOT_DENIED)}`);
        const end = Date.now() + 15000;
        while (!logged().some((l) => l.prompt?.startsWith("The human denied")) && Date.now() < end) await new Promise((r) => setTimeout(r, 50));
        const next = logged().find((l) => l.prompt?.startsWith("The human denied"))?.prompt ?? "";
        assert.match(next, /list the folder instead/);
        assert.ok(next.includes(COPILOT_DENIED), next);
      }
    } finally { delete process.env.FAKE_CLAUDE_LOG; delete process.env.FAKE_ACP_LOG; done(); }
  });
}

test("an agent that cannot be woken is reported and nothing else is tried", async () => {
  const { h, done } = await bench((ws, relay) => new ClaudeDriver(ws, relay, "/nonexistent/claude"));
  try {
    const first = await until(h, (c) => c.kind === "question");
    void h.answer(first.id, { option: first.options[0].id });
    const n = await until(h, (c) => c.kind === "notify" && c.title === s.wakeFailed("Claude Code"));
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
    const ap = await until(h, (c) => c.kind === "confirm" && c.effect === "disruptive" && c.title === "serial.listen");  // the action's own name
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

import { writeFileSync } from "node:fs";

test("unknown resource: human approves a program, receives its observation and stops it in the panel", async () => {
  const { core, h, root, env, done } = await bench((ws, relay) => new ClaudeDriver(ws, relay, join(import.meta.dirname, "fake-claude.mjs")));
  try {
    await until(h, (c) => c.kind === "question");
    const resource = (await agentCall(env, root, "register_resource", { address: "fixture:opaque", label: "Unknown endpoint" })).result;
    await core.select([resource.device]);
    const script = join(root, "observe.py");
    writeFileSync(script, "import time\nprint('raw observation', flush=True)\ntime.sleep(60)\n");
    const request = agentCall(env, root, "run_program", {
      resources: [resource.resource], argv: [py, script], purpose: "Observe the fixture", observe: true,
    });
    const approval = await until(h, (c) => c.kind === "confirm" && c.title === "program.run");
    assert.equal(approval.effect, "dangerous");
    // this very program may be allowed for the session (design.md section 12, 2026-11-08), never "allow all"
    assert.equal(approval.options.find((o) => o.id === "session")?.label, s.again.program.label);
    assert.ok(!approval.options.some((o) => o.id === "all"));
    await h.answer(approval.id, { option: "once" });
    const started = (await request).result;
    const running = await until(h, (c) => c.title === s.programRunning);
    await until(h, (c) => c.title === s.observation(resource.device));
    await h.answer(running.id, { option: "stop" });
    const stopped = (await agentCall(env, root, "run_state", { job: started.job })).result;
    assert.equal(stopped.status, "cancelled");
    assert.deepEqual(readFileSync(stopped.stdout, "utf8").trimEnd().split(/\r?\n/), ["raw observation"]);
  } finally { done(); }
});

async function streamingBench(autonomy) {
  const root = mkdtempSync(join(win ? tmpdir() : "/tmp", "bsw"));
  const data = JSON.parse(readFileSync(join(repo, "fixtures", "bench_example.json"), "utf8"));
  data.streams = { "/dev/cu.usbmodem21403": [{ after: 0.5, text: "boot ok\n" }, { after: 1.0, text: "abort() was called at PC 0x42001c85 on core 1\n" }] };
  const knowledge = join(root, ".benchscout", "knowledge");
  mkdirSync(knowledge, { recursive: true });
  writeFileSync(join(knowledge, "fixture.toml"), '[[log]]\npattern = "abort"\nalert = "Matched abort marker"\n');
  const fx = join(root, "bench.json");
  writeFileSync(fx, JSON.stringify(data));
  const env = { ...process.env, BENCHSCOUT_FIXTURE: fx, BENCHSCOUT_RUNTIME: root, BENCHSCOUT_MEMORY: join(root, "memory"), BENCHSCOUT_KNOWLEDGE: join(repo, "fixtures", "knowledge") };
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
    const alert = await until(h, (c) => c.kind === "notify" && c.title === s.deviceAlert(STLINK));
    assert.match(alert.body, /abort\(\) was called at PC 0x42001c85/);
    assert.match(alert.detail, /\.benchscout[\\/]logs[\\/]serial_usbmodem21403\.log/);
    const report = await until(h, (c) => c.kind === "report" && c.status === "open" && c.title === `${s.report}: ${s.deviceAlert(STLINK)}`);
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
    const offer = await until(h, (c) => c.kind === "question" && c.title === s.deviceAlert(STLINK));
    assert.deepEqual(offer.options.map((o) => o.id), ["go", "ignore"]);
    assert.equal(h.cards.filter((c) => c.kind === "report" && c.title.endsWith(s.deviceAlert(STLINK))).length, 0);
  } finally { done(); }
});

test("memory: plain entries are recorded, safety-loosening ones wait for the human", async () => {
  const { h, root, env, done } = await bench((ws, relay) => new ClaudeDriver(ws, relay, join(import.meta.dirname, "fake-claude.mjs")));
  try {
    await until(h, (c) => c.kind === "question");
    await agentCall(env, root, "remember", { ref: "usbserial-10", kind: "config", text: "CH1 is on the 3V3 rail" });
    const rec = await until(h, (c) => c.kind === "memory" && c.title === s.learned("QinHeng USB Serial"));
    assert.match(rec.body, /CH1 is on the 3V3 rail/);
    assert.deepEqual(rec.options.map((o) => o.id), ["forget"]);
    const res = (await agentCall(env, root, "remember", { ref: "/dev/cu.usbserial-10", kind: "safety", text: "no auto-reset circuit", data: { listen_safe: true } })).result;
    assert.equal(res.status, "pending_human");
    const pend = await until(h, (c) => c.kind === "memory" && c.title === s.learnedSafety("QinHeng USB Serial"));
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
    const watch = await until(h, (c) => c.kind === "confirm" && c.title === s.offerWatch("QinHeng USB Serial"));
    assert.ok(watch.body.startsWith(s.offerWatchWhy));
    assert.match(watch.body, /\nOpening this port can reset the device/, "English: the core's own words, capitalised");
    void h.answer(watch.id, { option: "once" });
    const on = await until(h, (c) => c.kind === "notify" && c.title === s.watching("QinHeng USB Serial"));
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
    // the extension's card asking for the agent: the waiting request is said there, not on a second card above it
    const chooser = h.chooser = h.ask(s.chooseAgent, "", [{ id: "claude", label: "Claude Code" }], () => {}, false, true);
    void h.tell("what is on the bench?");
    await until(h, () => chooser.body.includes(s.requestWaits));
    assert.ok(!h.cards.some((c) => c.title === s.chooseAgentFirst), "one card, not two");
    chooser.status = "closed";
    void h.tell("and the scope?");  // without the extension's card (none open): a note of its own, pinned
    const wait = await until(h, (c) => c.kind === "notify" && c.title === s.chooseAgentFirst);
    assert.ok(wait.pin);
    assert.ok(!h.cards.some((c) => c.title === s.wakeFailed("benchscout")), "not choosing yet is not a failure");
    assert.ok(!h.cards.some((c) => c.kind === "report"));
    chosen = true;
    h.resetAgent();  // what choosing an agent in the card does
    const report = await until(h, (c) => c.kind === "report" && c.status === "open");
    assert.match(report.body, /got: what is on the bench\?/);
    // both requests made before the choice are answered, in order
    await until(h, (c) => c.kind === "report" && c.status === "open" && /got: and the scope\?/.test(c.body));
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

test("a port lost while the computer slept is one quiet, counted record; nobody is woken", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  let woken = 0;
  const h = new Harness(core, () => { woken++; throw new Error("not woken"); }, S, { language: "Japanese", maxTurnsPerHour: 5 });
  const ev = (kind) => ({ port: "serial:usbserial-101", device: "d1", device_name: "SIPEED JTAG Debugger", kind, what: "x", cause: "host_sleep" });
  for (let i = 0; i < 2; i++) { core.emit("deviceEvent", ev("vanished")); core.emit("deviceEvent", ev("returned")); }
  assert.equal(h.cards.length, 1);
  assert.equal(h.cards[0].status, "closed");
  assert.equal(h.cards[0].title, S.hostSlept("SIPEED JTAG Debugger", 2));
  assert.equal(woken, 0);
});

test("an approval that downloads a tool first says so on the card", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const h = new Harness(core, () => { throw new Error("not woken"); }, S, { language: "Japanese", maxTurnsPerHour: 1 });
  core.emit("approvalRequested", { id: "a1", action: "esptool.write_flash", port: "serial:usbmodem1101", args: "{}", effect: "dangerous",
    status: "pending", impact: { reason: "overwrites flash", affected_ports: [], disturbs: [], code: "action:esptool.write_flash" },
    by: "agent", fetch: "esptool 5.6.0 from github.com (official release, SHA-256 checked, about 63 MB)" });
  assert.ok(h.cards[0].body.includes(`${S.fetchFirst}: esptool 5.6.0 from github.com`));
});

test("a SCPI command's card allows that command for the session or always, and says that is all it covers", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const decided = [];
  core.decide = async (...a) => { decided.push(a); return {}; };
  const h = new Harness(core, () => { throw new Error("not woken"); }, S, { language: "Japanese", maxTurnsPerHour: 1 });
  const ask = (id, effect, subjects) => core.emit("approvalRequested", { id, action: "scpi.write", port: "visa:TCPIP0::192.168.1.50::INSTR",
    args: JSON.stringify({ command: "DISP:BRIG 5" }), effect, status: "pending", by: "agent", always: true,
    impact: { reason: "x", affected_ports: [], disturbs: [], code: subjects ? "scpi_command" : "energizing", subjects } });
  ask("a1", "disruptive", ["DISP:BRIG"]);
  const card = h.cards.find((c) => c.id === "approval:a1");
  assert.deepEqual(card.options.map((o) => o.id), ["once", "session", "always", "all", "deny"]);
  assert.ok(card.body.includes(S.commandCovers), "the card says the grant covers this command only");
  void h.answer(card.id, { option: "always" });
  assert.deepEqual(decided[0], ["a1", true, "always"]);
  ask("a2", "dangerous", ["VOLT"]);  // an output setting: once, or not at all
  assert.deepEqual(h.cards.find((c) => c.id === "approval:a2").options.map((o) => o.id), ["once", "deny"]);
});

test("allow all is on DISRUPTIVE cards only; a write may be allowed for the session where the core offers it, saying what that covers", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const decided = [];
  core.decide = async (...a) => { decided.push(a); return {}; };
  const h = new Harness(core, () => { throw new Error("not woken"); }, S, { language: "Japanese", maxTurnsPerHour: 1 });
  const ask = (id, action, effect, again) => core.emit("approvalRequested", { id, action, port: "serial:usbserial-10", args: "{}", effect,
    status: "pending", by: "agent", impact: { reason: "x", affected_ports: [], disturbs: [], code: "" }, ...(again ? { again } : {}) });
  ask("a1", "serial.send", "disruptive");
  ask("a2", "lab.fuse", "dangerous");
  ask("a3", "esptool.write_flash", "dangerous", "tool");
  const card = (id) => h.cards.find((c) => c.id === `approval:${id}`);
  assert.ok(card("a1").options.some((o) => o.id === "all"));
  assert.ok(!card("a2").options.some((o) => o.id === "all" || o.id === "session"), "what cannot be undone asks each time");
  // a write the core lets be allowed for the session (design.md section 12, 2026-11-08): the card says what that covers
  assert.ok(!card("a3").options.some((o) => o.id === "all"));
  assert.equal(card("a3").options.find((o) => o.id === "session").label, S.again.tool.label);
  assert.ok(card("a3").body.includes(S.again.tool.covers));
  void h.answer("approval:a1", { option: "all" });
  void h.answer("approval:a3", { option: "session" });
  assert.deepEqual(decided, [["a1", true, "all"], ["a3", true, "session"]]);
});

test("a tool's first-use download is one card, updated as it goes and closed when done", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const h = new Harness(core, () => { throw new Error("not woken"); }, S, { language: "Japanese", maxTurnsPerHour: 1 });
  const ev = (got, state) => core.emit("download", { tool: "esptool", version: "5.6.0", got, total: 65_000_000, state });
  ev(0, "running");
  ev(32_000_000, "running");
  assert.equal(h.cards.length, 1);
  assert.equal(h.cards[0].title, S.downloading("esptool 5.6.0"));
  assert.ok(h.cards[0].body.startsWith(S.downloadBody("32", "65").slice(0, 7)), "progress in MB");
  assert.equal(h.cards[0].status, "open");
  ev(65_000_000, "done");
  assert.equal(h.cards.length, 1);
  assert.equal(h.cards[0].title, S.downloaded("esptool 5.6.0"));
  assert.equal(h.cards[0].status, "closed");
});

test("Windows: a device that needs a driver gets one card saying which interface and how, closed once it is right", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const h = new Harness(core, () => { throw new Error("not woken"); }, S, { language: "Japanese", maxTurnsPerHour: 1 });
  const bench = (dev) => ({ devices: [{ id: "usb:0403:6010:x", name: "SIPEED JTAG Debugger", evidence: [], ...dev }], ports: [], targets: [], links: [] });
  core.emit("scan", bench({ access: [{ need: "driver", interfaces: [0, 1] }] }), []);
  const card = () => h.cards.find((c) => c.id === "driver:usb:0403:6010:x");
  assert.equal(card().body, S.driverBody("0, 1", "", ""), "which interfaces lack a driver");
  assert.ok(h.cards.find((c) => c.kind === "question").body.includes(`SIPEED JTAG Debugger (${S.stateNoDriver})`));
  core.emit("scan", bench({ access: [{ need: "winusb", interfaces: [0], tools: ["openFPGALoader"] }] }), []);  // the maker's driver came; openFPGALoader still needs WinUSB
  assert.equal(h.cards.filter((c) => c.id.startsWith("driver:")).length, 1);
  assert.equal(card().body, S.driverBody("", "0", "openFPGALoader"), "which interface needs WinUSB, with how");
  core.emit("scan", bench({}), []);  // after Zadig and a rescan
  assert.equal(card().status, "closed");
});

test("a tool the agent proposes is a card with the exact command, its effect and the agent's source", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const decided = [];
  core.toolDecide = async (...a) => { decided.push(a); return {}; };
  const h = new Harness(core, () => { throw new Error("not woken"); }, S, { language: "Japanese", maxTurnsPerHour: 1 });
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

test("Linux: a device a libusb tool cannot open gets one card naming the node, the tool and the udev fix", () => {
  const core = new CoreClient("/nonexistent", tmpdir());
  const h = new Harness(core, () => { throw new Error("not woken"); }, S, { language: "Japanese", maxTurnsPerHour: 1 });
  const bench = (dev) => ({ devices: [{ id: "usb:0403:6010:x", name: "SIPEED JTAG Debugger", evidence: [], ...dev }], ports: [], targets: [], links: [] });
  // the tool is the one the core's data names (a person's knowledge may add one): the panel named openFPGALoader always
  // (audit 2026-11-07, files read last)
  core.emit("scan", bench({ access: [{ need: "udev", node: "/dev/bus/usb/003/002", tools: ["openocd"] }] }), []);
  const card = () => h.cards.find((c) => c.id === "driver:usb:0403:6010:x");
  assert.equal(card().title, S.udevTitle("SIPEED JTAG Debugger"));
  assert.equal(card().body, S.udevBody("/dev/bus/usb/003/002", "openocd"));
  core.emit("scan", bench({}), []);  // the rule took effect
  assert.equal(card().status, "closed");
});

test("with only devices benchscout has no data for, the first question still offers them to the agent", () => {
  // review 7 (17): the first question and the device list showed nothing, so an unknown programmer had no way in
  const core = new CoreClient("/nonexistent", tmpdir());
  const s = S;
  const h = new Harness(core, () => { throw new Error("not woken"); }, s, { language: "English", maxTurnsPerHour: 1 });
  const bench = { devices: [{ id: "usb:1234:5678:x", name: "Acme Cable", evidence: [], other: true }], ports: [], targets: [], links: [] };
  core.emit("scan", bench, [], []);
  const q = h.cards.find((c) => c.kind === "question");
  assert.ok(q, "a first question is asked");
  assert.deepEqual(q.options.map((o) => o.label), [s.lookOthers(1)]);
  assert.match(q.body, /Acme Cable/);
});


test("'always on this device' is offered for a command whatever its protocol, where the core says it can keep it", () => {
  // audit 2026-11-07 (23): it was offered for a unit whose memory may be another's, and the next call asked again
  const core = new CoreClient("/nonexistent", tmpdir());
  const s = S;
  const h = new Harness(core, () => { throw new Error("not woken"); }, s, { language: "English", maxTurnsPerHour: 1 });
  const ask = (id, always, model) => core.emit("approvalRequested", { id, action: "serial.send", port: "serial:x", args: '{"data": "status"}',
    effect: "disruptive", status: "pending", by: "agent", always, model,
    impact: { reason: "sends bytes", affected_ports: [], disturbs: [], subjects: ["status"] } });
  ask("a1", true, false);
  ask("a2", false, false);
  ask("a3", true, true);  // a model whose units share a serial, plugged in alone (design.md section 12, 2026-11-08)
  const card = (id) => h.cards.find((c) => c.id === `approval:${id}`);
  const labels = (id) => card(id).options.map((o) => o.label);
  assert.ok(labels("a1").includes(s.allowCommandSession) && labels("a1").includes(s.allowAlwaysHere));
  assert.ok(labels("a2").includes(s.allowCommandSession) && !labels("a2").includes(s.allowAlwaysHere));
  assert.ok(labels("a3").includes(s.allowAlwaysModel) && card("a3").body.includes(s.modelWide), "the card says it is the model's");
  assert.ok(!card("a1").body.includes(s.modelWide));
});


test("the panel can stop a built-in listen without waiting for its full duration", async () => {
  const { core, h, root, env, done } = await bench((ws, relay) => new ClaudeDriver(ws, relay, join(import.meta.dirname, "fake-claude.mjs")));
  try {
    await until(h, (c) => c.kind === "question");
    await core.select(["/dev/cu.usbmodem21403"]);
    const request = agentCall(env, root, "do", { action: "serial.listen", port: "/dev/cu.usbmodem21403", args: { seconds: 30 } });
    const running = await until(h, (c) => c.title === s.programRunning && c.body === "serial.listen");
    await h.answer(running.id, { option: "stop" });
    assert.equal((await request).result.status, "cancelled");
  } finally { done(); }
});

test("a failed answer stays available for retry and the panel shows why", async () => {
  const { h, done } = await bench(() => { throw new Error("unexpected wake"); });
  try {
    let attempts = 0;
    const card = h.ask("Use device?", "", [{ id: "allow", label: "Allow" }], () => {
      if (++attempts === 1) throw new Error("device is busy");
    });
    await h.answer(card.id, { option: "allow" });
    assert.equal(card.status, "open");
    assert.ok(h.cards.some(c => c.title === s.answerFailed && c.body.includes("device is busy")));
    await h.answer(card.id, { option: "allow" });
    assert.equal(card.status, "closed");
    assert.equal(attempts, 2);
  } finally { done(); }
});
