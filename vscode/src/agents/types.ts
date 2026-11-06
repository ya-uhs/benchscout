// What every way of waking the user's agent looks like to the harness (design.md section 17).
// Implementations: claude.ts (Claude Code headless, `claude -p` stream-json) and acp.ts (any
// ACP agent through the official SDK). No `vscode` import, so they are testable with Node.

export type AgentEvent =
  | { kind: "text"; text: string } // part of what the agent says
  | { kind: "thought"; text: string }
  | { kind: "tool"; id: string; title: string; status?: string };

export interface PermissionOption {
  id: string;
  label: string;
  kind: "allow_once" | "allow_always" | "reject";
}

/** The agent asks before using one of its own tools (a shell command, an edit, a benchscout tool). */
export interface PermissionAsk {
  tool: string; // tool name as the agent reports it
  title: string; // short human description
  detail: string; // the command / input, for the card
  benchscout: boolean; // one of benchscout's own MCP tools: the broker is the gate, so it is allowed without asking
  options: PermissionOption[];
}

export interface TurnResult {
  stopReason: string;
  text: string; // everything the agent said in this turn
}

/** Waking failed. Shown to the human as is; benchscout never switches to another agent on its own. */
export class WakeError extends Error {
  // code "no_agent": none is chosen yet; the harness waits for the human to choose instead of failing
  constructor(message: string, readonly fix: string, readonly code?: "no_agent") {
    super(message);
  }
}

export interface McpRelay {
  command: string; // the benchscout executable
  args: string[]; // ["mcp", "--workspace", ws]
  env: Record<string, string>;
}

export interface AgentDriver {
  readonly label: string;
  onEvent: (e: AgentEvent) => void;
  onPermission: (p: PermissionAsk) => Promise<string>; // returns the chosen option id
  start(): Promise<void>; // spawn + handshake; throws WakeError
  prompt(text: string): Promise<TurnResult>; // one turn
  stop(): void;
}

/** Names of benchscout's MCP tools, to recognise them in permission requests. */
// Kept in sync with mcp_relay.py by tests/test_relay_tools.py.
export const BENCHSCOUT_TOOLS = ["bench_scan", "bench_inventory_json", "bench_impact", "bench_do", "bench_log",
  "bench_identify", "bench_remember", "bench_define_tool", "bench_recall", "ask_user", "request_devices",
  "bench_register_resource", "bench_run", "bench_job", "bench_recipe"];

export function isBenchscoutTool(name: string): boolean {
  const prefix = "mcp__benchscout__";
  return name.startsWith(prefix) && BENCHSCOUT_TOOLS.includes(name.slice(prefix.length));
}
