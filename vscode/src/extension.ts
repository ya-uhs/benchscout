// benchscout VS Code extension: the harness and its inbox (design.md section 3, 8, 17).
//
// - starts the core for this folder as a child process and owns its control channel
// - proposes what to look at, wakes the agent the human chose (Claude Code via `claude -p`,
//   others via ACP), and turns everything that needs the human into inbox cards
// - registers the MCP server so the person's own agent can also use the bench from its chat
// No LLM here: the agent is whatever the person already uses, signed in by them.

import { execFile, execFileSync } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { AcpDriver, COPILOT } from "./agents/acp";
import { ClaudeDriver } from "./agents/claude";
import { AgentDriver, McpRelay, WakeError } from "./agents/types";
import { Bench, CoreClient, DeviceEvent, deviceViews, MemoryGroup } from "./core";
import { Card, Harness, memoryText } from "./harness";
import { languageName, pickLanguage, strings } from "./i18n";
import { DeviceRow, PanelState, html } from "./panel";

/** The OS UI language: macOS's first preferred language, else the locale (Windows reports its own here). */
function osLanguage(): string {
  if (process.platform === "darwin") {
    try {
      const out = execFileSync("defaults", ["read", "-g", "AppleLanguages"], { encoding: "utf8", timeout: 2000 });
      const first = out.replace(/[()",]/g, " ").trim().split(/\s+/)[0];
      if (first) return first;
    } catch { /* fall through */ }
  }
  return process.env.LC_ALL || process.env.LANG?.split(".")[0] || Intl.DateTimeFormat().resolvedOptions().locale || "en";
}

function currentLanguage(): string {
  return pickLanguage(vscode.workspace.getConfiguration("benchscout").get<string>("language"), vscode.env.language, osLanguage());
}

let s = strings(currentLanguage());

const AGENTS: Record<string, { label: string; exe: string }> = {
  copilot: { label: "GitHub Copilot CLI", exe: "copilot" },
  claude: { label: "Claude Code", exe: "claude" },
};

let core: CoreClient | undefined;
let harness: Harness | undefined;
let bench: Bench | undefined;
let memoryGroups: MemoryGroup[] = [];  // the inbox's memory list, fetched from the core
const watched = new Set<string>(); // serial ports the core holds open and logs
let busyElsewhere: Record<string, unknown> = {}; // devices another benchscout window holds
let view: vscode.WebviewView | undefined;
let status: vscode.StatusBarItem;
let log: vscode.OutputChannel;
const notified = new Set<string>();
const mcpChanged = new vscode.EventEmitter<void>();

function folder(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

/** An executable by name: PATH, then the usual per-user places (a Dock-launched VS Code on macOS lacks them on PATH).
 *  On Windows, npm installs CLIs as .cmd shims (copilot.cmd); the drivers start those through cross-spawn. */
function which(exe: string): string | undefined {
  const win = process.platform === "win32";
  const names = win ? [".exe", ".cmd", ".bat"].map((x) => exe + x) : [exe];
  const extra = win ? [path.join(process.env.APPDATA ?? "", "npm")] : ["/opt/homebrew/bin", "/usr/local/bin"];
  const dirs = [...(process.env.PATH ?? "").split(path.delimiter), path.join(os.homedir(), ".local", "bin"), ...extra];
  for (const d of dirs) {
    for (const n of names) {
      if (d && fs.existsSync(path.join(d, n))) return path.join(d, n);
    }
  }
  return undefined;
}

function coreCommand(ctx: vscode.ExtensionContext): string {
  // which program runs is the person's choice, never a folder's: only their own (machine/user) setting counts
  const set = vscode.workspace.getConfiguration("benchscout").inspect<string>("corePath");
  const own = set?.globalValue;
  if (own) return own;
  // the frozen core shipped inside a platform .vsix (packaging/build_core.py)
  const exe = process.platform === "win32" ? "benchscout.exe" : "benchscout";
  const bundled = path.join(ctx.extensionPath, "bin", "benchscout", exe);
  if (fs.existsSync(bundled)) {
    // a .vsix is a zip: the executable bit may not survive installation
    if (process.platform !== "win32") {
      try { fs.chmodSync(bundled, 0o755); } catch { /* read-only install: try as is */ }
    }
    return bundled;
  }
  return which("benchscout") ?? "benchscout";
}

function chosenAgent(): string {
  return vscode.workspace.getConfiguration("benchscout").get<string>("agent") ?? "";
}

/** The expert skill, where the chosen agent reads it (docs/notes.md review S4); the core carries the one source. */
function installSkill(ctx: vscode.ExtensionContext, ws: string): void {
  const id = chosenAgent();
  if (!AGENTS[id]) return;
  execFile(coreCommand(ctx), ["setup", "--agent", id, "--skill-only"], { cwd: ws }, (err, _out, stderr) => {
    if (err) log.appendLine(`could not write the skill for ${id}: ${stderr || err.message}`);
  });
}

function makeDriver(ctx: vscode.ExtensionContext, ws: string): AgentDriver {
  const relay: McpRelay = { command: coreCommand(ctx), args: ["mcp", "--workspace", ws], env: {} };
  const id = chosenAgent();
  if (id === "claude") return new ClaudeDriver(ws, relay, which("claude") ?? "claude");
  if (id === "copilot") return new AcpDriver({ ...COPILOT, command: which("copilot") ?? "copilot" }, ws, relay);
  // Nothing chosen: say so instead of picking one (design.md section 17: never switch on our own).
  return {
    label: "benchscout", onEvent: () => {}, onPermission: async () => ({ option: "reject" }),
    start: async () => { throw new WakeError("No agent is chosen", s.chooseAgent, "no_agent"); },
    prompt: async () => ({ stopReason: "error", text: "" }), stop: () => {},
  };
}

let agentCard: Card | undefined;

/** benchscout asks which agent to wake (never picks one itself); pinned on top, since waking waits for it. */
function askForAgent(): void {
  if (!harness || chosenAgent() || agentCard?.status === "open") return;
  const found = Object.entries(AGENTS).filter(([, a]) => which(a.exe));
  if (!found.length) {
    agentCard = harness.notify(s.noAgentFound, "", s.noAgentFix);
    return;
  }
  agentCard = harness.ask(s.chooseAgent, found.map(([, a]) => a.label).join(" / "),
    found.map(([id, a], i) => ({ id, label: a.label, primary: i === 0 })),
    async (a) => {
      // the configuration listener below then writes the skill and wakes whatever waited for the choice
      await vscode.workspace.getConfiguration("benchscout").update("agent", a.option, vscode.ConfigurationTarget.Global);
      refresh();
    }, false, true);
}

/** What is remembered, for the inbox's list (design.md section 6: the person sees, fixes and deletes it). */
async function loadMemory(): Promise<void> {
  if (!core?.running) return;
  memoryGroups = await core.memoryView().catch(() => memoryGroups);
  refresh();
}

/** How far benchscout goes on its own (design.md section 17): set in the panel or in the settings. */
function currentAutonomy(): "auto" | "suggest" {
  return vscode.workspace.getConfiguration("benchscout").get<string>("autonomy") === "suggest" ? "suggest" : "auto";
}

function refresh(): void {
  const cards: Card[] = harness?.cards ?? [];
  // everything that waits for the person: questions, approvals, and memory that would loosen safety
  const waiting = cards.filter((c) => c.status === "open" &&
    (c.kind === "confirm" || c.kind === "question" || (c.kind === "memory" && c.options.some((o) => o.primary)))).length;
  status.text = (waiting ? `$(bell-dot) ${s.statusWaiting(waiting)}` : `$(circuit-board) ${s.statusIdle}`)
    + (core?.running && core.allowAll ? ` · $(unlock) ${s.statusAllowAll}` : "");
  status.backgroundColor = waiting ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
  for (const c of cards) {
    if (c.kind === "confirm" && c.status === "open" && !notified.has(c.id)) {
      notified.add(c.id);
      vscode.window.showWarningMessage(`benchscout: ${c.title}`, s.notifyShow).then((p) => p && vscode.commands.executeCommand("benchscout.panel.focus"));
    }
  }
  if (!view) return;
  // the webview gets the plain strings; the ones that take arguments are used here, on this side
  const plain = Object.fromEntries(Object.entries(s).filter(([, v]) => typeof v !== "function")) as PanelState["strings"];
  const views = bench ? deviceViews(bench) : [];
  const devices: DeviceRow[] = views.filter((d) => !d.other).map((d) => {
    const known = d.things.map((t) => t.name).filter(Boolean).join(", ");
    const ports = bench!.ports.filter((p) => p.device_id === d.id).map((p) => p.id);
    return { id: d.id, label: d.name + (known ? ` — ${known}` : ""), selected: !!core?.selected.includes(d.id),
             watched: ports.some((p) => watched.has(p)), busy: !!busyElsewhere[d.id] || !!d.busyBy, noDriver: d.noDriver.length > 0,
             needsWinusb: d.needsWinusb.length > 0, needsUdev: d.needsUdev !== null };
  });
  const state: PanelState = { running: !!core?.running, agent: AGENTS[chosenAgent()]?.label ?? "", cards, devices,
    others: views.filter((d) => d.other).length,
    memory: memoryGroups.map((g) => ({ ...g, entries: g.entries.map((e) => ({ ...e, text: memoryText(e, s) })) })),
    allowAll: !!core?.running && !!core?.allowAll, autonomy: currentAutonomy(), strings: plain };
  view.webview.postMessage({ type: "state", state });
}

async function start(ctx: vscode.ExtensionContext): Promise<void> {
  const ws = folder();
  if (!ws) {
    vscode.window.showWarningMessage(s.noFolder);
    return;
  }
  if (core?.running) return;
  fs.mkdirSync(path.join(ws, ".benchscout"), { recursive: true }); // next time, open on its own (S2)
  // VS Code's Workspace Trust: in a folder not trusted, its own .benchscout/knowledge is not read
  const c = new CoreClient(coreCommand(ctx), ws, process.env, vscode.workspace.isTrusted ? [] : ["--no-workspace-knowledge"]);
  core = c;
  const max = vscode.workspace.getConfiguration("benchscout").get<number>("maxTurnsPerHour") ?? 20;
  harness?.stop();
  harness = new Harness(c, () => makeDriver(ctx, ws), s, { language: languageName(currentLanguage()), maxTurnsPerHour: max,
    autonomy: currentAutonomy() });
  harness.on("changed", refresh);
  watched.clear();
  c.on("scan", (b: Bench) => { bench = b; refresh(); void loadMemory(); });
  for (const ev of ["memoryAdded", "memoryDecided", "memoryForgotten"] as const) c.on(ev, () => void loadMemory());
  c.on("selection", (selected: string[], busy: Record<string, unknown>) => {
    busyElsewhere = busy;
    for (const p of bench?.ports ?? []) if (!selected.includes(p.device_id)) watched.delete(p.id);  // released: not held any more
    refresh();
  });
  c.on("deviceEvent", (e: DeviceEvent) => {
    if (e.kind === "watching" || e.kind === "returned") watched.add(e.port);
    if (e.kind === "gone" || e.kind === "unwatchable") watched.delete(e.port);
    refresh();
  });
  c.on("exit", (code: number | null, err: string) => {
    if (core === c) core = undefined;
    if (code !== 0) vscode.window.showErrorMessage(`${s.coreMissing}${err ? "\n" + err.split("\n").pop() : ""}`);
    refresh();
  });
  agentCard = undefined; // a new harness: ask again if still nothing is chosen
  installSkill(ctx, ws);
  c.start();
  askForAgent();
  mcpChanged.fire();
  refresh();
}

export function activate(ctx: vscode.ExtensionContext): void {
  log = vscode.window.createOutputChannel("benchscout");
  ctx.subscriptions.push(log);
  status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  status.command = "benchscout.panel.focus";
  status.show();
  ctx.subscriptions.push(status);

  ctx.subscriptions.push(vscode.window.registerWebviewViewProvider("benchscout.panel", {
    resolveWebviewView(v) {
      view = v;
      v.webview.options = { enableScripts: true };
      const nonce = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
      v.webview.html = html(nonce, v.webview.cspSource);
      v.onDidDispose(() => { view = undefined; });
      v.webview.onDidReceiveMessage(async (m) => {
        switch (m.type) {
          case "ready": refresh(); void loadMemory(); break;
          case "start": await start(ctx); break;
          case "stop": harness?.stop(); core?.stop(); break;
          case "rescan": askForAgent(); await core?.scan().catch(() => undefined); break;
          case "allowAllOff": await core?.setAllowAll(false).catch(() => undefined); break;
          case "autonomy":  // kept in the setting, so it holds for the next window too; the change below applies it
            await vscode.workspace.getConfiguration("benchscout").update("autonomy", currentAutonomy() === "auto" ? "suggest" : "auto",
              vscode.ConfigurationTarget.Global);
            break;
          case "memoryForget": await core?.memoryDelete(m.key, m.id).catch(() => undefined); break;
          case "memoryDecide": await core?.memoryDecide(m.key, m.id, !!m.accept).catch(() => undefined); break;
          case "memoryOpen": {  // the file is plain TOML: the person fixes it in the editor
            if (typeof m.file === "string" && memoryGroups.some((g) => g.file === m.file)) {
              await vscode.window.showTextDocument(vscode.Uri.file(m.file));
            }
            break;
          }
          case "answer": await harness?.answer(m.id, { option: m.option, text: m.text }); break;
          case "tell": await harness?.tell(m.text); break;
          case "device": {  // the person releases a device for their own tools, or takes it back
            const sel = new Set(core?.selected ?? []);
            if (m.use) sel.add(m.id); else sel.delete(m.id);
            await core?.select([...sel]).catch((e) => vscode.window.showWarningMessage(`benchscout: ${e.message ?? e}`));
            break;
          }
        }
      });
    },
  }, { webviewOptions: { retainContextWhenHidden: true } }));

  // The person's own agent can also use the bench from its chat (Copilot in VS Code).
  ctx.subscriptions.push(vscode.lm.registerMcpServerDefinitionProvider("benchscout", {
    onDidChangeMcpServerDefinitions: mcpChanged.event,
    provideMcpServerDefinitions: () => {
      const ws = folder();
      return ws ? [new vscode.McpStdioServerDefinition("benchscout", coreCommand(ctx), ["mcp", "--workspace", ws])] : [];
    },
  }));

  ctx.subscriptions.push(
    vscode.commands.registerCommand("benchscout.start", async () => {
      await start(ctx);
      vscode.commands.executeCommand("benchscout.panel.focus");
    }),
    vscode.commands.registerCommand("benchscout.rescan", () => { askForAgent(); return core?.scan(); }),
    vscode.commands.registerCommand("benchscout.stop", () => { harness?.stop(); core?.stop(); }),
    vscode.workspace.onDidGrantWorkspaceTrust(() => {  // trusted now: start again, reading the folder's knowledge
      const old = core;
      if (!old?.running) return;
      old.once("exit", () => void start(ctx));  // once it has stopped: start() does nothing while one runs
      harness?.stop();
      old.stop();
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("benchscout.autonomy")) {
        harness?.setAutonomy(currentAutonomy());
        refresh();
      }
      if (e.affectsConfiguration("benchscout.language")) {  // new cards and the panel speak it from now on
        s = strings(currentLanguage());
        harness?.setLanguage(s, languageName(currentLanguage()));
        refresh();
      }
      if (!e.affectsConfiguration("benchscout.agent")) return;
      const ws = folder();
      if (ws && core?.running) installSkill(ctx, ws);
      harness?.resetAgent();
      refresh();
    }),
    { dispose: () => { harness?.stop(); core?.stop(); } },
  );

  // Opened on its own only in folders that already use benchscout (docs/notes.md review S2).
  if (folder() && fs.existsSync(path.join(folder()!, ".benchscout"))) void start(ctx);
  refresh();
}

export function deactivate(): void {
  harness?.stop();
  core?.stop(); // closing the control channel stops the core and frees the devices
}
