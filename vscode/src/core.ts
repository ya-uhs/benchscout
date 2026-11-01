// The control channel to a benchscout core (design.md section 3, 15 R3).
//
// The extension starts the core as its own child process; the core's stdin/stdout IS the
// control channel. Only this side can select devices and decide approvals. Agents reach
// the core separately, through `benchscout mcp`, which has no way to approve anything.
//
// No `vscode` import here, so it can be tested with plain Node against a real core.

import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import { EventEmitter } from "events";
import * as readline from "readline";

export type Effect = "safe" | "disruptive" | "dangerous";

export interface Evidence { source: string; detail: string }
export interface Device { id: string; name: string; serial?: string | null; evidence: Evidence[] }
export interface Port { id: string; device_id: string; kind: string; path?: string | null }
export interface Target {
  id: string; name: string | null; kind: string; confidence: "identified" | "family" | "unknown";
  hosted_on?: string | null; facts: string[]; evidence: Evidence[];
}
export interface Link { port_id: string; target_id: string; protocol: string }
export interface Bench { devices: Device[]; ports: Port[]; targets: Target[]; links: Link[] }
export interface Proposal { text: string; action?: string; port?: string; effect: Effect; needs?: string | null; reason?: string | null }

export interface Approval {
  id: string; action: string; port: string; args: string; effect: Effect; status: string;
  impact: { reason: string; affected_ports: string[]; disturbs: string[] };
  by?: "agent" | "benchscout"; // benchscout asks itself, e.g. to watch a selected device
  subject?: string; // the device's name
  purpose?: string;
}

export interface Question {
  id: string; text: string; options: string[]; status: string; choice: string | null; reply: string | null;
  kind: "question" | "devices"; data: { devices?: string[]; names?: Record<string, string>; reason?: string };
}

export interface DeviceEvent {
  port: string; device?: string; device_name?: string;
  kind: "alert" | "vanished" | "returned" | "gone" | "unwatchable" | "watching"; what: string; line?: string | null; log?: string; time?: number;
}

export interface MemoryEntry {
  id: string; kind: string; text: string; source: string; evidence: string; target: string; time: string; status: string; data: Record<string, unknown>;
}
export interface MemoryEvent { key: string; device?: string; device_name?: string; entry: MemoryEntry }

export interface CoreEvents {
  ready: [];
  scan: [Bench, Proposal[]];
  selection: [string[], Record<string, { workspace?: string; pid?: number }>];
  approvalRequested: [Approval];
  approvalDecided: [Approval];
  questionAsked: [Question];
  deviceEvent: [DeviceEvent];
  memoryAdded: [MemoryEvent];
  memoryDecided: [MemoryEvent];
  questionAnswered: [Question];
  exit: [number | null, string];
}

export class CoreClient extends EventEmitter {
  private proc?: ChildProcessWithoutNullStreams;
  private nextId = 1;
  private waiting = new Map<number | string, { resolve: (v: any) => void; reject: (e: Error) => void }>();
  private stderr = "";
  selected: string[] = [];
  pending = new Map<string, Approval>();
  questions = new Map<string, Question>();

  constructor(private command: string, private workspace: string, private env: NodeJS.ProcessEnv = process.env) {
    super();
  }

  get running(): boolean {
    return !!this.proc && this.proc.exitCode === null;
  }

  start(): void {
    this.proc = spawn(this.command, ["core", "--workspace", this.workspace, "--json"],
      { cwd: this.workspace, env: this.env });
    this.proc.on("error", (e) => this.emit("exit", null, `could not start "${this.command}": ${e.message}`));
    this.proc.stderr.on("data", (d) => { this.stderr = (this.stderr + d.toString()).slice(-4000); });
    this.proc.on("exit", (code) => {
      for (const w of this.waiting.values()) w.reject(new Error("benchscout core stopped"));
      this.waiting.clear();
      this.emit("exit", code, this.stderr.trim());
    });
    readline.createInterface({ input: this.proc.stdout }).on("line", (line) => this.onLine(line));
  }

  stop(): void {
    if (this.running) this.proc!.stdin.end(); // closing the control channel stops the core
  }

  scan(): Promise<any> { return this.call("scan"); }
  select(devices: string[]): Promise<any> { return this.call("select", { devices }); }
  decide(approvalId: string, granted: boolean, scope: "once" | "session" | number = "once"): Promise<any> {
    return this.call("decide", { approval_id: approvalId, granted, scope });
  }
  memoryDecide(key: string, id: string, accept: boolean): Promise<any> {
    return this.call("memory_decide", { key, id, accept });
  }
  memoryDelete(key: string, id: string): Promise<any> {
    return this.call("memory_delete", { key, id });
  }
  answer(questionId: string, choice: string | null, reply?: string): Promise<any> {
    return this.call("answer", { question_id: questionId, choice, reply: reply ?? null });
  }

  private call(method: string, params: object = {}): Promise<any> {
    if (!this.running) return Promise.reject(new Error("benchscout core is not running"));
    const id = this.nextId++;
    this.proc!.stdin.write(JSON.stringify({ id, method, params }) + "\n");
    return new Promise((resolve, reject) => this.waiting.set(id, { resolve, reject }));
  }

  private onLine(line: string): void {
    let msg: any;
    try { msg = JSON.parse(line); } catch { return; }
    switch (msg.event) {
      case "ready": this.emit("ready"); return;
      case "selection_changed":
        this.selected = msg.selected;
        this.emit("selection", msg.selected, msg.busy || {});
        return;
      case "approval_requested":
        this.pending.set(msg.approval.id, msg.approval);
        this.emit("approvalRequested", msg.approval);
        return;
      case "approval_decided":
        this.pending.delete(msg.approval.id);
        this.emit("approvalDecided", msg.approval);
        return;
      case "memory_added":
        this.emit("memoryAdded", msg as MemoryEvent);
        return;
      case "memory_decided":
        this.emit("memoryDecided", msg as MemoryEvent);
        return;
      case "device_event":
        this.emit("deviceEvent", msg as DeviceEvent);
        return;
      case "question_asked":
        this.questions.set(msg.question.id, msg.question);
        this.emit("questionAsked", msg.question);
        return;
      case "question_answered":
        this.questions.delete(msg.question.id);
        this.emit("questionAnswered", msg.question);
        return;
    }
    if (msg.id === undefined) return;
    if (msg.result && msg.result.bench) {
      // a scan (ours, or the core's own first one with id "initial")
      this.emit("scan", msg.result.bench, (msg.result.proposals ?? []) as Proposal[]);
    }
    const w = this.waiting.get(msg.id);
    if (!w) return;
    this.waiting.delete(msg.id);
    if ("error" in msg) w.reject(new Error(msg.error));
    else w.resolve(msg.result);
  }
}

// ---- helpers for the panel: turn the bench graph into what a person wants to see ----

export interface DeviceView {
  id: string;
  name: string;
  ports: string[];
  things: { name: string; status: "identified" | "family" | "unknown"; how: string; facts: string[]; inside?: string }[];
  alert: boolean;
  busyBy?: string;
}

export function deviceViews(bench: Bench): DeviceView[] {
  const targets = new Map(bench.targets.map((t) => [t.id, t]));
  return bench.devices.map((d) => {
    const ports = bench.ports.filter((p) => p.device_id === d.id);
    const tids = [...new Set(bench.links.filter((l) => ports.some((p) => p.id === l.port_id)).map((l) => l.target_id))];
    const things = tids.map((tid) => {
      const t = targets.get(tid)!;
      const proof = t.evidence.filter((e) => e.source !== "board-knowledge").map((e) => `${e.source}: ${e.detail}`);
      return {
        name: t.name ?? "",
        status: t.confidence,
        how: proof.join(" · "),
        facts: t.facts,
        // shown even when the host's part is still unknown: "inside the FPGA"
        inside: t.hosted_on ? (targets.get(t.hosted_on)?.name ?? targets.get(t.hosted_on)?.kind.toUpperCase()) : undefined,
      };
    });
    const busy = things.flatMap((t) => t.facts).find((f) => f.startsWith("in use by another benchscout window"));
    return {
      id: d.id,
      name: d.name,
      ports: ports.map(shortPort),
      things,
      alert: things.some((t) => t.facts.some((f) => f.startsWith("ALERT"))),
      busyBy: busy?.match(/\((.*)\)/)?.[1],
    };
  });
}

// What a person recognises: the serial port name, or just the kind for JTAG/SWD/VISA ids.
export function shortPort(p: Port): string {
  if (p.kind === "serial" && p.path) return p.path.split(/[\\/]/).pop()!.replace(/^cu\./, "");
  if (p.kind === "lan-scpi") return "LAN";
  if (p.kind === "usbtmc") return "USB-TMC";
  return p.kind.toUpperCase();
}

export function describeArgs(args: string): string {
  try {
    const a = JSON.parse(args);
    // control characters (a "\n" line ending) are shown escaped, not as a real line break
    return Object.entries(a).map(([k, v]) => `${k} ${typeof v === "string" && /[\x00-\x1f]/.test(v) ? JSON.stringify(v) : v}`).join(", ");
  } catch {
    return args;
  }
}
