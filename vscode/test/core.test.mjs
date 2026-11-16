// The extension's control channel against a real benchscout core on the fixture bench.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { once } from "node:events";
import { CoreClient, deviceViews, describeArgs } from "../out/core.js";

const repo = resolve(import.meta.dirname, "../..");
const win = process.platform === "win32";
// BENCHSCOUT_TEST_BIN runs the same tests against the frozen core (vscode/bin/benchscout/benchscout)
const bin = process.env.BENCHSCOUT_TEST_BIN ?? join(repo, ".venv", win ? "Scripts" : "bin", win ? "benchscout.exe" : "benchscout");
const py = join(repo, ".venv", win ? "Scripts" : "bin", win ? "python.exe" : "python");

function setup() {
  const root = mkdtempSync(join(win ? tmpdir() : "/tmp", "bsx"));  // short: AF_UNIX path limit
  const env = { ...process.env, BENCHSCOUT_FIXTURE: join(repo, "fixtures", "bench_example.json"), BENCHSCOUT_RUNTIME: root, BENCHSCOUT_MEMORY: join(root, "memory"), BENCHSCOUT_KNOWLEDGE: join(repo, "fixtures", "knowledge") };
  return { root, env, ws: root };
}

// Plays the agent: asks for an action through the agent channel, like `benchscout mcp` does.
function agentDo(env, ws, action, port) {
  const code = `import json,sys; from pathlib import Path
from benchscout.mcp_relay import CoreClient
from benchscout.runtime import agent_address
print(json.dumps(CoreClient(agent_address(Path(sys.argv[1]))[0]).call("do", {"action": sys.argv[2], "port": sys.argv[3]}, timeout=30)))`;
  const p = spawn(py, ["-c", code, ws, action, port], { env });
  let out = "";
  p.stdout.on("data", (d) => (out += d));
  return once(p, "exit").then(() => JSON.parse(out));
}

test("start, see devices, select, approve what the agent asks, stop", async () => {
  const { root, env, ws } = setup();
  const core = new CoreClient(bin, ws, env);
  try {
    const scanned = once(core, "scan");
    core.start();
    const [bench, proposals] = await scanned;
    const views = deviceViews(bench);
    const ch340 = views.find((d) => d.name === "QinHeng USB Serial");
    assert.ok(ch340 && ch340.ports.includes("usbserial-10"));
    assert.equal(ch340.things[0].status, "unknown");  // unknown stays unknown
    const fpga = views.find((d) => d.name === "Digilent USB Device").things.find((t) => t.inside);
    assert.ok(fpga, "the fabric UART is shown as living inside the FPGA");
    assert.ok(proposals.length > 0 && proposals.every((p) => p.reason));

    // selecting a device whose port may reset on open: benchscout itself offers to watch it
    const sel = once(core, "selection");
    const offered = once(core, "approvalRequested");
    await core.select([ch340.id]);
    assert.deepEqual((await sel)[0], [ch340.id]);
    const [offer] = await offered;
    assert.equal(offer.by, "benchscout");
    assert.equal(offer.action, "serial.watch");
    assert.equal(offer.subject, "QinHeng USB Serial");
    await core.decide(offer.id, false, "once");  // "not now": the agent's listen below still asks on its own

    const asked = once(core, "approvalRequested");
    const agent = agentDo(env, ws, "serial.listen", "/dev/cu.usbserial-10");
    const [approval] = await asked;
    assert.equal(approval.by, "agent");
    assert.equal(approval.effect, "disruptive");
    assert.match(approval.impact.reason, /reset/);
    assert.equal(core.pending.size, 1);
    await core.decide(approval.id, true, "session");
    const res = await agent;
    assert.equal(res.result.status, "done");
    assert.equal(core.pending.size, 0);

    // the session grant covers the next listen: no new question
    const again = await agentDo(env, ws, "serial.listen", "/dev/cu.usbserial-10");
    assert.equal(again.result.status, "done");

    const exited = once(core, "exit");
    core.stop();
    assert.equal((await exited)[0], 0);
  } finally {
    core.stop();
    try { rmSync(root, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }); } catch { /* Windows: still busy */ }
  }
});

test("a missing benchscout is reported, not thrown", async () => {
  const core = new CoreClient("/nonexistent/benchscout", tmpdir());
  const exited = once(core, "exit");
  core.start();
  const [code, err] = await exited;
  assert.notEqual(code, 0);
  assert.match(err, /could not start|ENOENT/);
});

test("args read like words", () => {
  assert.equal(describeArgs('{"baud": 9600, "seconds": 3}'), "baud 9600, seconds 3");
});

test("control characters in args stay visible", () => {
  assert.equal(describeArgs('{"data": "panic", "eol": "\\n"}'), 'data panic, eol "\\n"');
});

test("flash maps read as offset:file in hex", () => {
  assert.equal(describeArgs('{"images": [[0, "boot.bin"], [65536, "/fw/firmware.bin"]]}'), "images 0x0:boot.bin 0x10000:/fw/firmware.bin");
});

test("approval cards show nested arguments, tool definitions and input hashes", () => {
  const plan = { args: { image: "firmware.bin", offset: 4096 },
    definition: { argv: ["programmer", "--write", "{image}"] }, files: { "firmware.bin": "0123456789abcdef" } };
  const text = describeArgs(JSON.stringify(plan));
  for (const value of Object.values(plan)) assert.ok(text.includes(JSON.stringify(value)));
  assert.ok(!text.includes("[object Object]"));
});


test("a core that cannot start rejects pending and subsequent calls", async () => {
  const { root, env, ws } = setup();
  const core = new CoreClient(join(root, "missing-core"), ws, env);
  try {
    const exited = once(core, "exit");
    core.start();
    await assert.rejects(core.scan());
    const [, message] = await exited;
    assert.match(message, /could not start/);
    assert.equal(core.running, false);
    await assert.rejects(core.scan(), /not running/);
  } finally {
    core.stop();
    rmSync(root, { recursive: true, force: true });
  }
});
