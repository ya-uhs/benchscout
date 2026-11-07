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
export interface Device { id: string; name: string; serial?: string | null; evidence: Evidence[]; usb_node?: string | null;
  /** why something cannot open it yet, one form for every OS (model.py Device.access) */
  access?: { need: "driver" | "winusb" | "udev"; interfaces?: number[]; node?: string | null; tools?: string[] }[]; other?: boolean }
export interface Port { id: string; device_id: string; kind: string; path?: string | null }
export interface Target {
  id: string; name: string | null; kind: string; confidence: "identified" | "family" | "unknown";
  hosted_on?: string | null; facts: string[]; evidence: Evidence[];
}
export interface Link { port_id: string; target_id: string; protocol: string }
/** A tool's first-use download, after the human's OK (state: running, done, failed). */
export interface DownloadEvent { tool: string; version: string; got: number; total: number | null; state: string; error?: string }

/** An action the agent proposed for a tool benchscout ships nothing for (proposals.py); the human decides. */
export interface ToolProposal {
  id: string; status: string; evidence: string;
  tool: { name: string; argv: string[]; effect: "safe" | "disruptive" | "dangerous"; reason: string; doc?: string; port_kinds?: string[] };
  probes: { vid: number; pid: number; probe: string }[];
}

export interface Bench { devices: Device[]; ports: Port[]; targets: Target[]; links: Link[] }
export interface Proposal { text: string; action?: string; port?: string; effect: Effect; needs?: string | null; reason?: string | null }

export interface Approval {
  id: string; action: string; port: string; args: string; effect: Effect; status: string;
  impact: { reason: string; affected_ports: string[]; disturbs: string[]; code?: string; subjects?: string[] | null };
  by?: "agent" | "benchscout"; // benchscout asks itself, e.g. to watch a selected device
  fetch?: string; // a tool downloaded first (official release, pinned) if this is allowed
  always?: boolean; // the core can keep "always on this device": the unit's memory is its own (its serial singles it out)
  subject?: string; // the device's name
  purpose?: string;
}

export interface Question {
  id: string; text: string; options: string[]; status: string; choice: string | null; reply: string | null;
  kind: "question" | "devices"; data: { devices?: string[]; names?: Record<string, string>; reason?: string };
}

export interface DeviceEvent {
  stderr?: string; job?: string;
  port: string; device?: string; device_name?: string;
  kind: "alert" | "vanished" | "returned" | "gone" | "unwatchable" | "watching" | "observation"; what: string; line?: string | null; log?: string; time?: number;
  cause?: "host_sleep"; // the computer had been asleep: the device did not necessarily do anything
  through_benchscout?: string[]; // what benchscout itself did on this device just before (its own record)
}

/** Devices plugged in or pulled out since the last scan (the core notices by itself; design.md section 17). */
export interface PluggedDevice { id: string; name: string; other: boolean; selected: boolean }
export interface DevicesChanged { added: PluggedDevice[]; removed: PluggedDevice[] }

export interface MemoryEntry {
  id: string; kind: string; text: string; source: string; evidence: string; target: string; time: string; status: string; data: Record<string, unknown>;
}
export interface MemoryEvent { key: string; device?: string; device_name?: string; entry: MemoryEntry }
/** What is remembered about one device on the bench, and the file it lives in (the inbox's memory list). */
export interface MemoryGroup { device: string; name: string; key: string; file: string; entries: MemoryEntry[] }

export interface CoreEvents {
  runChanged: [RunEvent];
  ready: [];
  scan: [Bench, Proposal[], string[]];  // the last: knowledge files that could not be read
  selection: [string[], Record<string, { workspace?: string; pid?: number }>];
  approvalRequested: [Approval];
  approvalDecided: [Approval];
  allowAll: [boolean];
  download: [DownloadEvent];
  toolProposed: [ToolProposal];
  toolDecided: [ToolProposal];
  questionAsked: [Question];
  deviceEvent: [DeviceEvent];
  devicesChanged: [DevicesChanged];
  memoryAdded: [MemoryEvent];
  memoryDecided: [MemoryEvent];
  memoryForgotten: [{ key: string; id: string }];
  questionAnswered: [Question];
  exit: [number | null, string];
}

export interface RunEvent {
  job: string; status: string; purpose: string; stdout: string; stderr: string; error: string;
  resources: string[]; observe: boolean;
}

export class CoreClient extends EventEmitter {
  stopRun(job: string): Promise<any> { return this.call("run_state", { job, stop: true }); }
  private proc?: ChildProcessWithoutNullStreams;
  private nextId = 1;
  private waiting = new Map<number | string, { resolve: (v: any) => void; reject: (e: Error) => void }>();
  private stderr = "";
  selected: string[] = [];
  pending = new Map<string, Approval>();
  allowAll = false;  // the human allowed every DISRUPTIVE call for this session
  questions = new Map<string, Question>();

  constructor(private command: string, private workspace: string, private env: NodeJS.ProcessEnv = process.env,
              private extraArgs: string[] = []) {
    super();
  }

  get running(): boolean {
    return !!this.proc && this.proc.exitCode === null;
  }

  start(): void {
    this.proc = spawn(this.command, ["core", "--workspace", this.workspace, "--json", ...this.extraArgs],
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
  /** The human confirms (writes it into the folder's knowledge) or rejects a tool the agent proposed. */
  toolDecide(id: string, accept: boolean): Promise<any> {
    return this.call("tool_decide", { id, accept });
  }

  /** Allow every DISRUPTIVE call for the rest of the session (DANGEROUS still asks), or stop. */
  setAllowAll(on: boolean): Promise<any> {
    return this.call("allow_all", { on });
  }

  decide(approvalId: string, granted: boolean, scope: "once" | "session" | "always" | "all" | number = "once"): Promise<any> {
    return this.call("decide", { approval_id: approvalId, granted, scope });
  }
  /** What is remembered about each device on the bench, with each file (control channel). */
  memoryView(): Promise<MemoryGroup[]> {
    return this.call("memory_view", {});
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
      case "tool_proposed":
        this.emit("toolProposed", msg.proposal as ToolProposal);
        return;
      case "tool_decided":
        this.emit("toolDecided", msg.proposal as ToolProposal);
        return;
      case "download":
        this.emit("download", msg as DownloadEvent);
        return;
      case "allow_all":
        this.allowAll = !!msg.on;
        this.emit("allowAll", this.allowAll);
        return;
      case "memory_added":
        this.emit("memoryAdded", msg as MemoryEvent);
        return;
      case "memory_decided":
        this.emit("memoryDecided", msg as MemoryEvent);
        return;
      case "memory_forgotten":
        this.emit("memoryForgotten", { key: msg.key, id: msg.id });
        return;
      case "device_event":
        this.emit("deviceEvent", msg as DeviceEvent);
        return;
      case "run_changed":
        this.emit("runChanged", msg as RunEvent);
        return;
      case "bench":  // the bench changed on the agent's side (its scan, a name it proved): the panel shows it now
        this.emit("scan", msg.bench, (msg.proposals ?? []) as Proposal[], (msg.knowledge_errors ?? []) as string[]);
        return;
      case "devices_changed":
        this.emit("devicesChanged", { added: msg.added ?? [], removed: msg.removed ?? [] } as DevicesChanged);
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
      // a scan (ours, or the core's own: the first one with id "initial", one after a plug with id "plugged")
      this.emit("scan", msg.result.bench, (msg.result.proposals ?? []) as Proposal[], (msg.result.knowledge_errors ?? []) as string[]);
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
  noDriver: number[]; // USB interfaces the OS has no driver for (Windows): nothing can open them yet
  needsWinusb: number[]; // Windows: interfaces a libusb tool opens, whose driver must be replaced with WinUSB
  other: boolean; // a USB device benchscout has no data for: the agent sees it, the device list only counts it
  needsUdev: string | null; // Linux: the USB node this user cannot open (a libusb tool needs a udev rule), else null
}

export function deviceViews(bench: Bench): DeviceView[] {
  const targets = new Map(bench.targets.map((t) => [t.id, t]));
  return bench.devices.map((d) => {
    const need = (n: string) => (d.access ?? []).find((a) => a.need === n);
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
        // the part it is inside (its name, or its id while the part is still unknown: nothing is guessed)
        inside: t.hosted_on ? (targets.get(t.hosted_on)?.name ?? t.hosted_on) : undefined,
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
      noDriver: need("driver")?.interfaces ?? [],
      needsWinusb: need("winusb")?.interfaces ?? [],
      other: !!d.other,
      needsUdev: need("udev") ? (need("udev")?.node ?? d.usb_node ?? "") : null,
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
    const show = (v: unknown): string => {
      // a flash map [[offset, file], ...] reads as people write it: 0x10000:firmware.bin
      if (Array.isArray(v) && v.length && v.every((p) => Array.isArray(p) && p.length === 2 && typeof p[0] === "number")) {
        return v.map(([o, f]) => `0x${(o as number).toString(16)}:${f}`).join(" ");
      }
      if (v !== null && typeof v === "object") return JSON.stringify(v);
      // control characters (a "\n" line ending) are shown escaped, not as a real line break
      return typeof v === "string" && /[\x00-\x1f]/.test(v) ? JSON.stringify(v) : String(v);
    };
    return Object.entries(a).map(([k, v]) => `${k} ${show(v)}`).join(", ");
  } catch {
    return args;
  }
}
