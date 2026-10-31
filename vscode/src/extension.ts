// benchscout VS Code extension: the human's side of the bench (design.md section 3, 7, 15).
//
// - starts the core for this folder as a child process and owns its control channel
// - shows the devices and asks the person before anything changes device state
// - registers the MCP server so the person's own agent (Copilot, ...) can use the bench
// No LLM here and no agent embedded: the agent is whatever the person already uses.

import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { Approval, Bench, CoreClient, Proposal, describeArgs, deviceViews } from "./core";
import { strings } from "./i18n";
import { PanelState, PendingView, html } from "./panel";

const s = strings(vscode.env.language);

let core: CoreClient | undefined;
let bench: Bench | undefined;
let proposals: Proposal[] = [];
const history: string[] = [];
let view: vscode.WebviewView | undefined;
let status: vscode.StatusBarItem;
const mcpChanged = new vscode.EventEmitter<void>();

function folder(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

// Which benchscout to run: the setting, then the copy bundled in the .vsix, then PATH,
// then ~/.local/bin (a Dock-launched VS Code on macOS often lacks it on PATH).
function coreCommand(ctx: vscode.ExtensionContext): string {
  const set = vscode.workspace.getConfiguration("benchscout").get<string>("corePath");
  if (set) return set;
  const exe = process.platform === "win32" ? "benchscout.exe" : "benchscout";
  const bundled = path.join(ctx.extensionPath, "bin", exe);
  if (fs.existsSync(bundled)) return bundled;
  for (const dir of (process.env.PATH ?? "").split(path.delimiter)) {
    if (dir && fs.existsSync(path.join(dir, exe))) return path.join(dir, exe);
  }
  const local = path.join(os.homedir(), ".local", "bin", exe);
  return fs.existsSync(local) ? local : exe;
}

function note(text: string): void {
  history.push(`${new Date().toLocaleTimeString()}  ${text}`);
  if (history.length > 50) history.shift();
}

function pendingViews(): PendingView[] {
  return [...(core?.pending.values() ?? [])].map((a: Approval) => ({
    id: a.id,
    effect: a.effect,
    what: s.actions[a.action] ?? a.action,
    port: a.port,
    args: describeArgs(a.args),
    why: a.impact.reason,
    also: a.impact.affected_ports,
  }));
}

function refresh(): void {
  const n = core?.pending.size ?? 0;
  status.text = n ? `$(bell-dot) ${s.statusWaiting(n)}` : `$(circuit-board) ${s.statusIdle}`;
  status.backgroundColor = n ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
  if (!view) return;
  const { statusWaiting, ...plain } = s;
  const state: PanelState = {
    running: !!core?.running,
    devices: bench ? deviceViews(bench) : [],
    selected: core?.selected ?? [],
    pending: pendingViews(),
    proposals,
    history,
    strings: plain,
  };
  view.webview.postMessage({ type: "state", state });
}

async function startCore(ctx: vscode.ExtensionContext): Promise<void> {
  const ws = folder();
  if (!ws) {
    vscode.window.showWarningMessage(s.noFolder);
    return;
  }
  if (core?.running) return;
  fs.mkdirSync(path.join(ws, ".benchscout"), { recursive: true }); // next time, open on its own (S2)
  const c = new CoreClient(coreCommand(ctx), ws);
  core = c;
  c.on("scan", (b: Bench, p: Proposal[]) => { bench = b; proposals = p; refresh(); });
  c.on("selection", (sel: string[], busy: Record<string, { workspace?: string }>) => {
    const names = sel.map((id) => bench?.devices.find((d) => d.id === id)?.name ?? id);
    note(`${s.selectedNow}: ${names.join(", ") || "-"}`);
    for (const [id, h] of Object.entries(busy)) note(`${bench?.devices.find((d) => d.id === id)?.name ?? id}: ${s.inUse} (${h.workspace ?? ""})`);
    refresh();
  });
  c.on("approvalRequested", async (a: Approval) => {
    refresh();
    const what = `${s.actions[a.action] ?? a.action} (${a.port})`;
    const pick = await vscode.window.showWarningMessage(`benchscout: ${what}. ${a.impact.reason}`,
      s.notifyAllow, s.notifyDeny, s.notifyShow);
    if (pick === s.notifyAllow) await decide(a.id, true, "once");
    else if (pick === s.notifyDeny) await decide(a.id, false, "once");
    else if (pick === s.notifyShow) vscode.commands.executeCommand("benchscout.panel.focus");
  });
  c.on("approvalDecided", (a: Approval) => {
    const word = a.status === "granted" ? s.granted : a.status === "denied" ? s.denied : s.withdrawn;
    note(`${s.actions[a.action] ?? a.action} (${a.port}): ${word}`);
    refresh();
  });
  c.on("exit", (code: number | null, err: string) => {
    if (core === c) core = undefined;
    if (code !== 0) vscode.window.showErrorMessage(`${s.coreMissing}${err ? "\n" + err.split("\n").pop() : ""}`);
    refresh();
  });
  c.start();
  mcpChanged.fire();
  refresh();
}

async function decide(id: string, granted: boolean, scope: "once" | "session"): Promise<void> {
  try {
    await core?.decide(id, granted, scope);
  } catch (e) {
    vscode.window.showWarningMessage(String((e as Error).message)); // e.g. already decided in the panel
  }
}

export function activate(ctx: vscode.ExtensionContext): void {
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
          case "ready": refresh(); break;
          case "start": await startCore(ctx); break;
          case "stop": core?.stop(); break;
          case "rescan": await core?.scan().catch(() => undefined); break;
          case "select": await core?.select(m.devices).catch((e: Error) => vscode.window.showWarningMessage(e.message)); break;
          case "decide": await decide(m.id, m.granted, m.scope ?? "once"); break;
          case "chat":
            await vscode.commands.executeCommand("workbench.action.chat.open", { query: s.prompt }).then(undefined,
              () => vscode.env.clipboard.writeText(s.prompt).then(() => vscode.window.showInformationMessage(s.copied)));
            break;
          case "copy":
            await vscode.env.clipboard.writeText(s.prompt);
            vscode.window.showInformationMessage(s.copied);
            break;
        }
      });
    },
  }));

  // The person's own agent finds the bench here (Copilot). Claude Code reads .mcp.json instead.
  ctx.subscriptions.push(vscode.lm.registerMcpServerDefinitionProvider("benchscout", {
    onDidChangeMcpServerDefinitions: mcpChanged.event,
    provideMcpServerDefinitions: () => {
      const ws = folder();
      return ws ? [new vscode.McpStdioServerDefinition("benchscout", coreCommand(ctx), ["mcp", "--workspace", ws])] : [];
    },
  }));

  ctx.subscriptions.push(
    vscode.commands.registerCommand("benchscout.start", async () => {
      await startCore(ctx);
      vscode.commands.executeCommand("benchscout.panel.focus");
    }),
    vscode.commands.registerCommand("benchscout.rescan", () => core?.scan()),
    vscode.commands.registerCommand("benchscout.stop", () => core?.stop()),
    { dispose: () => core?.stop() },
  );

  // Opened on its own only in folders that already use benchscout (design.md 15 S2).
  if (folder() && fs.existsSync(path.join(folder()!, ".benchscout"))) startCore(ctx);
  refresh();
}

export function deactivate(): void {
  core?.stop(); // closing the control channel stops the core and frees the devices
}
