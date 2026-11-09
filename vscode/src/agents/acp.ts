// Any ACP agent (Copilot CLI, Codex via its ACP adapter, Gemini CLI, ...), driven with the
// official SDK `@agentclientprotocol/sdk` as an ACP client (design.md section 17).

import { ChildProcessWithoutNullStreams } from "child_process";
// cross-spawn: on Windows, npm-installed CLIs are .cmd shims and scripts start with #!; it resolves both
// and quotes arguments for cmd.exe (the MCP config is JSON). Elsewhere it is plain child_process.spawn.
import crossSpawn = require("cross-spawn");
const spawn = (command: string, args: string[], opts: { cwd: string }) =>
  crossSpawn(command, args, { ...opts, stdio: "pipe" }) as ChildProcessWithoutNullStreams;
import { Readable, Writable } from "stream";
import type * as Acp from "@agentclientprotocol/sdk" with { "resolution-mode": "import" };
import { AgentDriver, AgentEvent, McpRelay, PermissionAnswer, PermissionAsk, TurnResult, WakeError } from "./types";

/** How a particular ACP agent is started and how it gets benchscout's MCP relay. */
export interface AcpProfile {
  label: string;
  command: string;
  args: string[];
  // "flag": the agent takes MCP servers on its command line (Copilot CLI rejects stdio servers sent in
  // session/new, docs/notes.md review T8); "session": pass them in session/new as ACP intends.
  mcp: { via: "flag"; build: (relay: McpRelay) => string[] } | { via: "session" };
  install: string; // what to tell the human when it cannot be started
}

export const COPILOT: AcpProfile = {
  label: "GitHub Copilot CLI",
  command: "copilot",
  // benchscout's own tools run without Copilot asking (its permission rule for the MCP server named benchscout): the
  // broker gates what they do, as for Claude Code. Without it every bench_scan was a card (real Copilot CLI 1.0.105,
  // fixtures/real/copilot_acp_2026-11-08.jsonl). Its other tools still ask, and the asks become cards
  args: ["--acp", "--allow-tool=benchscout"],
  mcp: {
    via: "flag",
    build: (r) => ["--additional-mcp-config", JSON.stringify({
      mcpServers: { benchscout: { type: "local", command: r.command, args: r.args, env: r.env, tools: ["*"] } },
    })],
  },
  install: "Install it with `npm install -g @github/copilot` and sign in once with `copilot`.",
};

export class AcpDriver implements AgentDriver {
  onEvent: (e: AgentEvent) => void = () => {};
  onPermission: (p: PermissionAsk) => Promise<PermissionAnswer> = async () => ({ option: "reject" });
  private proc?: ChildProcessWithoutNullStreams;
  private session?: Acp.ActiveSession;
  private close?: () => void;
  private titles = new Map<string, string>(); // toolCallId -> title from session/update
  private stderr = "";
  private denied = false; // the human denied a tool and the agent has not started another since

  constructor(private profile: AcpProfile, private cwd: string, private relay: McpRelay) {}

  get label(): string {
    return this.profile.label;
  }

  async start(): Promise<void> {
    const acp: typeof Acp = await import("@agentclientprotocol/sdk");
    const p = this.profile;
    const args = [...p.args, ...(p.mcp.via === "flag" ? p.mcp.build(this.relay) : [])];
    const proc = spawn(p.command, args, { cwd: this.cwd });
    this.proc = proc;
    proc.stderr.on("data", (d) => { this.stderr = (this.stderr + d).slice(-4000); });
    const spawnFailed = new Promise<never>((_, reject) => proc.on("error", (e: NodeJS.ErrnoException) =>
      reject(e.code === "ENOENT" ? new WakeError(`"${p.command}" was not found`, p.install) : new WakeError(e.message, p.install))));
    const exited = new Promise<never>((_, reject) => proc.on("exit", (code) =>
      reject(new WakeError(`${p.label} exited (${code}) ${this.stderr.trim().split("\n").pop() ?? ""}`, p.install))));

    const stream = acp.ndJsonStream(Writable.toWeb(proc.stdin) as WritableStream<Uint8Array>,
      Readable.toWeb(proc.stdout) as unknown as ReadableStream<Uint8Array>);
    const ready = new Promise<void>((resolveReady, rejectReady) => {
      acp.client({ name: "benchscout" })
        .onRequest(acp.methods.client.session.requestPermission, (ctx) => this.permission(ctx.params))
        .connectWith(stream, async (ctx) => {
          const init = await ctx.request(acp.methods.agent.initialize, { protocolVersion: acp.PROTOCOL_VERSION, clientCapabilities: {} });
          if (!init) throw new WakeError(`${p.label} did not answer initialize`, p.install);
          let builder = ctx.buildSession(this.cwd);
          if (p.mcp.via === "session") {
            builder = builder.withMcpServer({ name: "benchscout", command: this.relay.command, args: this.relay.args,
              env: Object.entries(this.relay.env).map(([name, value]) => ({ name, value })) });
          }
          this.session = await builder.start();
          resolveReady();
          await new Promise<void>((r) => { this.close = r; }); // keep the connection for later prompts
          this.session.dispose();
        })
        .catch(rejectReady);
    });
    await Promise.race([ready, spawnFailed, exited]);
  }

  async prompt(text: string): Promise<TurnResult> {
    const s = this.session;
    if (!s) throw new WakeError(`${this.label} is not running`, "Start it again.");
    void s.prompt(text);
    let said = "";
    this.denied = false;
    for (;;) {
      const m = await s.nextUpdate();
      if (m.kind === "stop") return { stopReason: m.response.stopReason, text: said, stoppedOnDenial: this.denied };
      const u = m.notification.update as any;
      if (u.sessionUpdate === "agent_message_chunk" && u.content?.type === "text") {
        said += u.content.text;
        this.onEvent({ kind: "text", text: u.content.text });
      } else if (u.sessionUpdate === "agent_thought_chunk" && u.content?.type === "text") {
        this.onEvent({ kind: "thought", text: u.content.text });
      } else if (u.sessionUpdate === "tool_call") {
        this.denied = false;  // it went on with something else
        this.titles.set(u.toolCallId, u.title);
        this.onEvent({ kind: "tool", id: u.toolCallId, title: u.title, status: u.status });
      } else if (u.sessionUpdate === "tool_call_update" && u.status) {
        this.onEvent({ kind: "tool", id: u.toolCallId, title: this.titles.get(u.toolCallId) ?? "", status: u.status });
      }
    }
  }

  stop(): void {
    this.close?.();
    if (this.proc && this.proc.exitCode === null) setTimeout(() => this.proc?.kill(), 1000).unref();
  }

  private async permission(params: Acp.RequestPermissionRequest): Promise<Acp.RequestPermissionResponse> {
    const tc = params.toolCall as any;
    const title = this.titles.get(tc.toolCallId) ?? tc.title ?? "";
    const raw = tc.rawInput ?? {};
    const answer = await this.onPermission({
      tool: title,
      title: tc.title ?? title,
      detail: raw.command ?? (Array.isArray(raw.commands) ? raw.commands.join("; ") : JSON.stringify(raw)),
      benchscout: false, // ACP supplies a display title, not an authenticated MCP server identity.
      options: params.options.map((o) => ({ id: o.optionId, label: o.name,
        kind: o.kind.startsWith("allow") ? (o.kind as "allow_once" | "allow_always") : "reject" })),
    });
    // ACP's answer carries no words: what the human said goes to the agent with the next prompt (the harness)
    this.denied = !params.options.find((o) => o.optionId === answer.option)?.kind.startsWith("allow");
    return { outcome: { outcome: "selected", optionId: answer.option } };
  }
}
