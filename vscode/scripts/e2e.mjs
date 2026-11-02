#!/usr/bin/env node
// The inbox in a real VS Code, end to end, on a scripted bench with no hardware (fixtures/bench_e2e.json).
// It checks what the unit tests cannot see: the extension starting its core, the webview drawing the
// cards, a button press reaching the core, a watched port's crash turning into a card, Release/Use.
// No agent is chosen, so nothing is woken (on a machine with Claude Code or Copilot installed too).
//
//   node scripts/e2e.mjs [--vsix FILE] [--core PATH]
//
// --core: the core to run (default: the checkout's own, .venv/bin/benchscout), so the current code is tested
// whatever core a .vsix carries. E2E_DOWNLOAD_VSCODE=1 downloads VS Code (stable) first, as CI does.

import { execFileSync, spawn } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

const here = resolve(import.meta.dirname, "..");
const repo = resolve(here, "..");
const args = process.argv.slice(2);
const flag = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

if (process.env.E2E_DOWNLOAD_VSCODE) {
  const { downloadAndUnzipVSCode, resolveCliArgsFromVSCodeExecutablePath } = await import("@vscode/test-electron");
  let exe = await downloadAndUnzipVSCode({ version: "stable", cachePath: join(here, ".vscode-test") });
  // newer VS Code on macOS names its executable Code, not Electron (which test-electron still returns)
  if (!existsSync(exe) && existsSync(join(dirname(exe), "Code"))) exe = join(dirname(exe), "Code");
  const [cli] = resolveCliArgsFromVSCodeExecutablePath(exe);
  process.env.VSCODE_BIN = exe;
  process.env.VSCODE_CLI = cli;
  if (process.platform === "linux") process.env.VSCODE_ARGS = "--no-sandbox --disable-gpu";
}

const core = resolve(flag("--core") ?? join(repo, ".venv", process.platform === "win32" ? "Scripts/benchscout.exe" : "bin/benchscout"));
if (!existsSync(core)) throw new Error(`no core at ${core}: run \`uv sync --extra mcp --extra visa\` in ${repo}`);

/** Calls a benchscout MCP tool the way an agent does, through the relay started in the inbox's workspace. */
async function agentCall(tool, args) {
  const ws = JSON.parse(execFileSync(process.execPath, [join(here, "scripts", "ui-check.mjs"), "where"], { encoding: "utf8" })).workspace;
  const relay = spawn(core, ["mcp"], { cwd: ws, stdio: ["pipe", "pipe", "ignore"] });
  const lines = [];
  let buf = "";
  relay.stdout.on("data", (d) => { buf += d; let i; while ((i = buf.indexOf("\n")) >= 0) { lines.push(JSON.parse(buf.slice(0, i))); buf = buf.slice(i + 1); } });
  const send = (m) => relay.stdin.write(JSON.stringify({ jsonrpc: "2.0", ...m }) + "\n");
  const reply = async (id) => { for (let i = 0; i < 200; i++) { const m = lines.find((l) => l.id === id); if (m) return m; await sleep(50); } throw new Error(`no reply ${id}`); };
  send({ id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "e2e", version: "0" } } });
  await reply(1);
  send({ method: "notifications/initialized" });
  send({ id: 2, method: "tools/call", params: { name: tool, arguments: args } });
  const m = await reply(2);
  relay.kill();
  return (m.result?.content ?? []).map((c) => c.text).join("");
}

function ui(...a) {
  return execFileSync(process.execPath, [join(here, "scripts", "ui-check.mjs"), ...a], { encoding: "utf8" }).trim();
}

/** Polls `read` until `ok(text)` holds; fails with what was last seen. */
async function until(what, read, ok, seconds = 45) {
  let last = "";
  for (const end = Date.now() + seconds * 1000; Date.now() < end; await sleep(500)) {
    try { last = ui(read); } catch (e) { last = String(e.message ?? e); }
    if (ok(last)) return last;
  }
  throw new Error(`timed out waiting for ${what}; last seen:\n${last}`);
}

const DEVICE = "STMicroelectronics STLINK-V3";  // its console (fixtures/bench_e2e.json) crashes a few seconds after opening
const DEVICE_ID = "/dev/cu.usbmodem21403";  // its port: the agent names the device by it
let failed = false;
ui("start", ...(flag("--vsix") ? ["--vsix", flag("--vsix")] : []), "--fixture", join(repo, "fixtures", "bench_e2e.json"),
  "--core", core, "--language", "en");
try {
  const step = (s) => console.log(`- ${s}`);

  await until("the first question with the bench", "cards", (t) => t.includes("What should we look at?"));
  const first = ui("cards");
  for (const name of ["Rigol DS1054Z", "Siglent SPD3303X", DEVICE]) {  // names from *IDN? and the USB descriptor
    if (!first.includes(name)) throw new Error(`the first question does not list ${name}:\n${first}`);
  }
  step("the core ran and the inbox lists the bench, with names the instruments gave");

  console.log(`  ${ui("click", `Look at ${DEVICE}`)}`);
  const watch = await until("watching (or an offer to watch)", "cards",
    (t) => t.includes(`Watching ${DEVICE}`) || t.includes(`Watch ${DEVICE}?`));
  if (!watch.includes(`Watching ${DEVICE}`)) {
    console.log(`  ${ui("click", "Watch it")}`);
    await until("watching", "cards", (t) => t.includes(`Watching ${DEVICE}`));
  }
  step("choosing a device reached the core, which watches it");

  await until("the crash card", "cards", (t) => t.includes(`${DEVICE}: something to look at`) && /hard ?fault/i.test(t), 30);
  step("a crash on the watched port became a card");

  await until("the device list", "devices", (t) => t.includes(DEVICE) && t.includes("Release"));
  console.log(`  ${ui("device-button", DEVICE)}`);
  await until("the device released", "devices", (t) => t.split("\n").some((r) => r.includes(DEVICE) && r.includes("Use")));
  console.log(`  ${ui("device-button", DEVICE)}`);
  await until("the device in use again", "devices", (t) => t.split("\n").some((r) => r.includes(DEVICE) && r.includes("Release")));
  step("Release and Use on the device list reach the core");

  // what the agent remembers shows in the inbox's memory list, and the person can forget it there
  const saved = await agentCall("bench_remember", { ref: DEVICE_ID, kind: "observation", text: "e2e: prints sensor ok once a second" });
  if (!saved.includes("remembered")) throw new Error(`bench_remember: ${saved}`);
  await until("the memory list", "cards", () => true, 1);
  console.log(`  ${ui("toggle", "mem")}`);
  await until("the remembered entry", "memory", (t) => t.includes("e2e: prints sensor ok"));
  console.log(`  ${ui("mem-button", "e2e: prints sensor ok")}`);
  await until("the entry forgotten", "memory", (t) => !t.includes("e2e: prints sensor ok"));
  step("what the agent remembered is listed in the inbox and forgotten from there");
  console.log("e2e: ok");
} catch (e) {
  failed = true;
  console.error(`e2e: FAILED: ${e.message ?? e}`);
  mkdirSync(join(here, "e2e-out"), { recursive: true });
  try { ui("shot", join(here, "e2e-out", "failure.png")); } catch { /* the window may be gone */ }
  try { console.error(`cards:\n${ui("cards")}\ndevices:\n${ui("devices")}`); } catch { /* idem */ }
} finally {
  try { ui("stop"); } catch { /* already closed */ }
}
process.exit(failed ? 1 : 0);
