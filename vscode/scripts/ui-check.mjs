#!/usr/bin/env node
// Look at the extension inside a real VS Code without touching the person's own VS Code:
// an isolated profile (its own user-data and extensions folders) with the given .vsix installed,
// the fixture bench (no hardware) unless --real, driven through the Chrome DevTools Protocol.
// For checking the inbox by eye, and for an agent that has no computer use: it saves screenshots
// and prints the cards as text.
//
//   node scripts/ui-check.mjs start [--vsix FILE] [--workspace DIR] [--real] [--locale ja]
//                                   [--fixture FILE] [--core PATH] [--language en|ja]
//                                                                            install, launch, open the inbox
//   node scripts/ui-check.mjs shot FILE.png                                  screenshot of the window
//   node scripts/ui-check.mjs cards                                          the cards, as text
//   node scripts/ui-check.mjs click "Button text"                            press a button in the inbox
//   node scripts/ui-check.mjs devices                                        the device list, as text
//   node scripts/ui-check.mjs device-button "Device name"                    press Release/Use on that device's row
//   node scripts/ui-check.mjs stop                                           close it, delete the profile
//
// macOS paths by default; set VSCODE_BIN (the Electron executable) and VSCODE_CLI elsewhere.
// --real opens the folder with the real bench: the same product path as a user, so anything that is
// not SAFE still waits for a human in the inbox. Do not press approval buttons for it on your own.

import { execFileSync, execSync, spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const here = resolve(import.meta.dirname, "..");
const repo = resolve(here, "..");
const STATE = join(tmpdir(), "benchscout-ui-check.json");
const APP = "/Applications/Visual Studio Code.app";
const BIN = process.env.VSCODE_BIN ?? join(APP, "Contents/MacOS/Code");
const CLI = process.env.VSCODE_CLI ?? (existsSync(join(APP, "Contents/Resources/app/bin/code")) ? join(APP, "Contents/Resources/app/bin/code") : "code");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const [cmd, ...rest] = process.argv.slice(2);
const flag = (name) => { const i = rest.indexOf(name); return i >= 0 ? rest[i + 1] : undefined; };

// ---- CDP -------------------------------------------------------------------------------
async function targets(port) {
  return (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
}

async function session(url) {
  const ws = new WebSocket(url);
  await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
  let id = 0;
  const waiting = new Map();
  ws.onmessage = (e) => { const m = JSON.parse(e.data); waiting.get(m.id)?.(m); waiting.delete(m.id); };
  const send = (method, params = {}) => new Promise((r) => { waiting.set(++id, r); ws.send(JSON.stringify({ id, method, params })); });
  return {
    send,
    async eval(js) {
      const r = await send("Runtime.evaluate", { expression: js, awaitPromise: true, returnByValue: true });
      if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 500));
      return r.result?.result?.value;
    },
    close: () => ws.close(),
  };
}

async function workbench(port) {
  const t = (await targets(port)).find((t) => t.type === "page" && t.url.includes("workbench"));
  if (!t) throw new Error("VS Code's window is not up");
  return session(t.webSocketDebuggerUrl);
}

/** Runs `js` inside the benchscout webview, where `doc` is its document. */
async function inbox(port, js) {
  const t = (await targets(port)).find((t) => t.url.startsWith("vscode-webview://") && t.url.includes("extensionId=benchscout"));
  if (!t) throw new Error("the benchscout inbox is not open");
  const s = await session(t.webSocketDebuggerUrl);
  try {
    return await s.eval(`(() => { const f = document.querySelector("iframe#active-frame") ?? document.querySelector("iframe");
      const doc = f ? f.contentDocument : document; return (${js}); })()`);
  } finally { s.close(); }
}

const clickText = (texts) => `(() => { for (const t of ${JSON.stringify(texts)}) {
  const b = [...document.querySelectorAll("a,button,.monaco-button")].find((e) => e.textContent.trim() === t);
  if (b) { b.click(); return t; } } return ""; })()`;

// ---- commands ------------------------------------------------------------------------------
async function start() {
  if (existsSync(STATE)) throw new Error(`already running (${STATE}); run "stop" first`);
  const vsix = flag("--vsix") ?? readdirSync(here).filter((f) => /^benchscout.*\.vsix$/.test(f))
    .map((f) => join(here, f)).sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs)[0];
  if (!vsix) throw new Error("no .vsix: run `npm run package` (or package:platform) first");
  const real = rest.includes("--real");
  // short: VS Code's IPC socket lives in the profile and must stay under ~103 characters
  const profile = mkdtempSync(join(tmpdir(), "bsv"));
  const ws = resolve(flag("--workspace") ?? join(profile, "bench"));
  mkdirSync(join(ws, ".benchscout"), { recursive: true });
  mkdirSync(join(profile, "ud", "User"), { recursive: true });
  writeFileSync(join(profile, "ud", "User", "settings.json"), JSON.stringify({
    "workbench.startupEditor": "none", "security.workspace.trust.enabled": false, "telemetry.telemetryLevel": "off",
    "update.mode": "none", "extensions.autoCheckUpdates": false,
    // --core: this core instead of the one inside the .vsix (a checkout's own, so a test sees the current code)
    ...(flag("--core") ? { "benchscout.corePath": resolve(flag("--core")) } : {}),
    ...(flag("--language") ? { "benchscout.language": flag("--language") } : {}),
  }, null, 1));
  const install = ["--user-data-dir", join(profile, "ud"), "--extensions-dir", join(profile, "ext"), "--install-extension", vsix, "--force"];
  if (process.platform === "win32") {
    // the CLI is code.cmd, which only a shell runs: one quoted command line (Node no longer takes args with shell)
    execSync([CLI, ...install].map((a) => `"${a}"`).join(" "), { stdio: "ignore" });
  } else {
    execFileSync(CLI, install, { stdio: "ignore" });
  }
  const port = 9300 + Math.floor(Math.random() * 600);
  const env = { ...process.env };
  if (real) { delete env.BENCHSCOUT_FIXTURE; } else {
    env.BENCHSCOUT_FIXTURE = resolve(flag("--fixture") ?? join(repo, "fixtures", "bench_example.json"));
    env.BENCHSCOUT_MEMORY = join(profile, "memory");
  }
  const p = spawn(BIN, ["--user-data-dir", join(profile, "ud"), "--extensions-dir", join(profile, "ext"),
    `--remote-debugging-port=${port}`, "--disable-workspace-trust", ...(process.env.VSCODE_ARGS ?? "").split(" ").filter(Boolean), ...(flag("--locale") ? ["--locale", flag("--locale")] : []),
    "--new-window", ws], { env, detached: true, stdio: "ignore" });
  p.on("error", (e) => { console.error(`could not start VS Code (${BIN}): ${e.message}`); process.exit(1); });
  p.unref();
  writeFileSync(STATE, JSON.stringify({ port, pid: p.pid, profile, ws, vsix, real }));
  for (let i = 0; ; i++) {
    try { (await workbench(port)).close(); break; } catch { if (i > 60) throw new Error("VS Code did not come up"); await sleep(500); }
  }
  const s = await workbench(port);
  try {
    for (let i = 0; i < 40; i++) {  // a new profile shows a sign-in page, then a theme page
      const clicked = await s.eval(clickText(["Continue without Signing In", "Get Started", "Skip"]));
      const open = await s.eval(`(() => { const a = [...document.querySelectorAll(".activitybar .action-item a")]
        .find((a) => a.getAttribute("aria-label") === "benchscout"); if (!a) return false;
        if (!a.closest(".action-item").classList.contains("checked")) a.click(); return true; })()`);
      if (open && !clicked && i > 6) break;  // nothing left to dismiss for a few seconds
      await sleep(500);
    }
  } finally { s.close(); }
  await sleep(2500);
  console.log(JSON.stringify({ port, profile, workspace: ws, vsix, bench: real ? "real" : "fixture" }));
}

function state() {
  if (!existsSync(STATE)) throw new Error('nothing running; use "start"');
  return JSON.parse(readFileSync(STATE, "utf8"));
}

async function main() {
  if (cmd === "start") return start();
  const st = state();
  if (cmd === "shot") {
    const s = await workbench(st.port);
    const r = await s.send("Page.captureScreenshot", { format: "png" });
    s.close();
    writeFileSync(rest[0], Buffer.from(r.result.data, "base64"));
    console.log(rest[0]);
  } else if (cmd === "cards") {
    console.log(await inbox(st.port, `[...doc.querySelectorAll(".card")].map((c) =>
      (c.classList.contains("closed") ? "  (done) " : "") + c.innerText.replace(/\\n+/g, " | ")).join("\\n")`));
  } else if (cmd === "devices") {
    console.log(await inbox(st.port, `[...doc.querySelectorAll(".dev")].map((d) => d.innerText.replace(/\\n+/g, " | ")).join("\\n")`));
  } else if (cmd === "device-button") {  // the Release/Use button on the row of the device named rest[0]
    const hit = await inbox(st.port, `(() => { const row = [...doc.querySelectorAll(".dev")].find((d) => d.innerText.includes(${JSON.stringify(rest[0])}));
      const b = row?.querySelector("button"); if (!b) return ""; b.click(); return b.textContent.trim(); })()`);
    if (!hit) throw new Error(`no device row with a button for "${rest[0]}"`);
    console.log(`pressed: ${hit}`);
  } else if (cmd === "click") {
    const hit = await inbox(st.port, `(() => { const b = [...doc.querySelectorAll("button")].find((b) => b.textContent.trim() === ${JSON.stringify(rest[0])}
      || b.textContent.includes(${JSON.stringify(rest[0])})); if (!b) return ""; b.click(); return b.textContent.trim(); })()`);
    if (!hit) throw new Error(`no button "${rest[0]}" in the inbox`);
    console.log(`pressed: ${hit}`);
  } else if (cmd === "stop") {
    try {
      const v = await (await fetch(`http://127.0.0.1:${st.port}/json/version`)).json();
      const b = await session(v.webSocketDebuggerUrl);
      void b.send("Browser.close");  // no reply comes back: the browser is gone first
      await sleep(500);
      b.close();
    } catch { try { process.kill(st.pid); } catch { /* already gone */ } }
    await sleep(1500);
    rmSync(st.profile, { recursive: true, force: true });
    rmSync(STATE, { force: true });
    console.log("closed");
  } else {
    console.error("usage: ui-check.mjs start [--vsix F] [--workspace D] [--real] [--locale ja] [--fixture F] [--core P] [--language L]"
      + " | shot F.png | cards | click TEXT | stop");
    process.exit(2);
  }
}

main().catch((e) => { console.error(String(e.message ?? e)); process.exit(1); });
