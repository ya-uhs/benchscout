// Claude Code, woken through its own headless mode (design.md section 17):
//   claude -p --input-format stream-json --output-format stream-json --verbose --permission-prompt-tool stdio
// The binary is the user's own, unmodified, signed in by the user through Anthropic's flow.
// benchscout never touches credentials. Permission prompts arrive as control_request / can_use_tool.

import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import * as readline from "readline";
import { AgentDriver, AgentEvent, McpRelay, PermissionAsk, TurnResult, WakeError, isBenchscoutTool } from "./types";

export class ClaudeDriver implements AgentDriver {
  readonly label = "Claude Code";
  onEvent: (e: AgentEvent) => void = () => {};
  onPermission: (p: PermissionAsk) => Promise<string> = async () => "reject";
  private proc?: ChildProcessWithoutNullStreams;
  private turn?: { resolve: (r: TurnResult) => void; reject: (e: Error) => void; text: string };
  private stderr = "";
  private titles = new Map<string, string>(); // tool_use id -> tool name

  constructor(private cwd: string, private relay: McpRelay, private command = "claude", private extraArgs: string[] = []) {}

  async start(): Promise<void> {
    const mcp = { mcpServers: { benchscout: { command: this.relay.command, args: this.relay.args, env: this.relay.env } } };
    const args = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
      "--permission-prompt-tool", "stdio", "--mcp-config", JSON.stringify(mcp), ...this.extraArgs];
    const proc = spawn(this.command, args, { cwd: this.cwd });
    this.proc = proc;
    const failed = new Promise<never>((_, reject) => {
      proc.on("error", (e: NodeJS.ErrnoException) => reject(e.code === "ENOENT"
        ? new WakeError(`"${this.command}" was not found`, "Install Claude Code and sign in with `claude` once, or pick another agent.")
        : new WakeError(e.message, "Check that Claude Code runs in a terminal.")));
    });
    proc.stderr.on("data", (d) => { this.stderr = (this.stderr + d).slice(-4000); });
    proc.on("exit", (code) => this.turn?.reject(new WakeError(`Claude Code exited (${code}) ${this.stderr.trim().split("\n").pop() ?? ""}`,
      "Run `claude` in a terminal to check that it is installed and signed in.")));
    readline.createInterface({ input: proc.stdout }).on("line", (l) => this.onLine(l));
    // The process is ready when it accepts input; failures to spawn show up right away.
    await Promise.race([failed, new Promise((r) => setTimeout(r, 300))]);
  }

  prompt(text: string): Promise<TurnResult> {
    if (!this.proc || this.proc.exitCode !== null) return Promise.reject(new WakeError("Claude Code is not running", "Start it again."));
    return new Promise((resolve, reject) => {
      this.turn = { resolve, reject, text: "" };
      this.send({ type: "user", message: { role: "user", content: text } });
    });
  }

  stop(): void {
    if (this.proc && this.proc.exitCode === null) {
      this.proc.stdin.end();
      setTimeout(() => this.proc?.kill(), 2000).unref();
    }
  }

  private send(msg: object): void {
    this.proc?.stdin.write(JSON.stringify(msg) + "\n");
  }

  private async onLine(line: string): Promise<void> {
    let m: any;
    try { m = JSON.parse(line); } catch { return; }
    if (m.type === "assistant") {
      for (const b of m.message?.content ?? []) {
        if (b.type === "text") {
          this.turn && (this.turn.text += b.text);
          this.onEvent({ kind: "text", text: b.text });
        } else if (b.type === "thinking" && b.thinking) {
          this.onEvent({ kind: "thought", text: b.thinking });
        } else if (b.type === "tool_use") {
          this.titles.set(b.id, b.name);
          this.onEvent({ kind: "tool", id: b.id, title: b.name, status: "pending" });
        }
      }
    } else if (m.type === "user") {
      // results of the tools it used come back as a user message
      for (const b of Array.isArray(m.message?.content) ? m.message.content : []) {
        if (b.type === "tool_result" && this.titles.has(b.tool_use_id)) {
          this.onEvent({ kind: "tool", id: b.tool_use_id, title: this.titles.get(b.tool_use_id)!, status: b.is_error ? "failed" : "completed" });
        }
      }
    } else if (m.type === "control_request" && m.request?.subtype === "can_use_tool") {
      const r = m.request;
      const choice = await this.onPermission({
        tool: r.tool_name,
        title: r.description || r.display_name || r.tool_name,
        detail: r.input?.command ?? JSON.stringify(r.input ?? {}),
        benchscout: isBenchscoutTool(r.tool_name),
        options: [
          { id: "allow_once", label: "Allow once", kind: "allow_once" },
          { id: "reject", label: "Deny", kind: "reject" },
        ],
      });
      const response = choice.startsWith("allow")
        ? { behavior: "allow", updatedInput: r.input ?? {} }
        : { behavior: "deny", message: "The human denied this in the benchscout inbox." };
      this.send({ type: "control_response", response: { subtype: "success", request_id: m.request_id, response } });
    } else if (m.type === "result") {
      const t = this.turn;
      this.turn = undefined;
      if (!t) return;
      if (m.is_error) t.reject(new WakeError(String(m.result ?? m.subtype), "Check Claude Code in a terminal (sign-in, usage limits)."));
      else t.resolve({ stopReason: m.subtype ?? "end_turn", text: t.text || String(m.result ?? "") });
    }
  }
}
