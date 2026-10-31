// The harness against a real benchscout core on the fixture bench, with fake agents (no LLM).
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
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
  const done = () => { h.stop(); core.stop(); rmSync(root, { recursive: true, force: true }); };
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
      const confirm = await until(h, (c) => c.kind === "confirm" && c.status === "open");
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
    const n = await until(h, (c) => c.kind === "notify");
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
    const ap = await until(h, (c) => c.kind === "confirm" && c.effect === "disruptive");
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
  return { core, h, done: () => { delete process.env.FAKE_QUIET; h.stop(); core.stop(); rmSync(root, { recursive: true, force: true }); } };
}

test("auto: a crash on a watched device wakes the agent with the line, without the human typing anything", async () => {
  const { h, done } = await streamingBench("auto");
  try {
    const first = await until(h, (c) => c.kind === "question");
    const st = first.options.find((o) => o.label.includes("STLINK"));
    await h.answer(first.id, { option: st.id });               // the only human action: pick the device
    const alert = await until(h, (c) => c.kind === "notify" && /something to look at/.test(c.title));
    assert.match(alert.body, /abort\(\) was called at PC 0x42001c85/);
    assert.match(alert.detail, /\.benchscout\/logs\/serial_usbmodem21403\.log/);
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
