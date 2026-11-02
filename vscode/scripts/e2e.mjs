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

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync } from "node:fs";
import { join, resolve } from "node:path";

const here = resolve(import.meta.dirname, "..");
const repo = resolve(here, "..");
const args = process.argv.slice(2);
const flag = (name) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : undefined; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

if (process.env.E2E_DOWNLOAD_VSCODE) {
  const { downloadAndUnzipVSCode, resolveCliArgsFromVSCodeExecutablePath } = await import("@vscode/test-electron");
  const exe = await downloadAndUnzipVSCode({ version: "stable", cachePath: join(here, ".vscode-test") });
  const [cli] = resolveCliArgsFromVSCodeExecutablePath(exe);
  process.env.VSCODE_BIN = exe;
  process.env.VSCODE_CLI = cli;
  if (process.platform === "linux") process.env.VSCODE_ARGS = "--no-sandbox --disable-gpu";
}

const core = resolve(flag("--core") ?? join(repo, ".venv", process.platform === "win32" ? "Scripts/benchscout.exe" : "bin/benchscout"));
if (!existsSync(core)) throw new Error(`no core at ${core}: run \`uv sync --extra mcp --extra visa\` in ${repo}`);

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
