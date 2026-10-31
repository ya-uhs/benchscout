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
const bin = join(repo, ".venv", win ? "Scripts" : "bin", win ? "benchscout.exe" : "benchscout");
const py = join(repo, ".venv", win ? "Scripts" : "bin", win ? "python.exe" : "python");
const s = strings("en");

async function bench(makeDriver) {
  const root = mkdtempSync(join(win ? tmpdir() : "/tmp", "bsh"));
  const env = { ...process.env, BENCHSCOUT_FIXTURE: join(repo, "fixtures", "bench_example.json"), BENCHSCOUT_RUNTIME: root };
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
      const ch340 = first.options.find((o) => o.label.includes("CH340"));
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
