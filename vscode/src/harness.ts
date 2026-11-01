// The harness: runs the loop and keeps the inbox (design.md section 8, 17).
//
//   benchscout proposes what to look at (a question card, no LLM)
//   -> the human picks or writes an intent
//   -> benchscout wakes the user's agent with it
//   -> the agent investigates with benchscout's tools; what needs a human becomes a card
//   -> the turn ends in a report card with next options
//
// No `vscode` import: tested with Node against a real core and fake agents.

import { EventEmitter } from "events";
import { AgentDriver, AgentEvent, PermissionAsk, WakeError } from "./agents/types";
import { Approval, Bench, CoreClient, DeviceEvent, MemoryEvent, Question, describeArgs, deviceViews } from "./core";
import { Strings } from "./i18n";

export type CardKind = "question" | "confirm" | "report" | "notify" | "memory";

export interface CardOption { id: string; label: string; primary?: boolean }

export interface Card {
  id: string;
  kind: CardKind;
  time: number;
  title: string;
  body: string;
  detail?: string; // command, port and args
  effect?: string; // for confirm cards from the broker
  options: CardOption[];
  freeText: boolean; // "something else…" box
  status: "open" | "working" | "closed";
  answer?: string;
  progress: string[]; // for report cards: what the agent did, in order
  pin?: boolean; // shown above the others while open (e.g. choosing the agent, which everything else waits for)
}

type Answer = { option?: string; text?: string };

export interface HarnessOptions {
  language: string; // the language the agent should reply in, e.g. "Japanese"
  maxTurnsPerHour: number; // budget; past it, the harness stops and says so
  autonomy?: "auto" | "suggest"; // auto: wake the agent on events; suggest: only offer to (design.md section 17)
  wakeCooldownMs?: number; // per device: do not wake again for this long after waking for an event
}

export class Harness extends EventEmitter {
  cards: Card[] = [];
  private handlers = new Map<string, (a: Answer) => unknown>();
  private driver?: AgentDriver;
  private turns: number[] = [];
  private nextId = 1;
  private askedFirst = false;
  private lastWake = new Map<string, number>(); // device -> when the agent was last woken for it
  private busy = false;
  private stopping = false;
  private sleeps = new Map<string, number>(); // device -> times its port was reopened after the computer slept
  private current?: { card: Card; tools: Map<string, number> }; // the report card of the turn in progress
  private deferred?: () => Promise<void>; // a wake waiting for the human to choose an agent

  constructor(private core: CoreClient, private makeDriver: () => AgentDriver, private s: Strings,
              private opts: HarnessOptions = { language: "English", maxTurnsPerHour: 20 }) {
    super();
    core.on("scan", (b: Bench) => { if (!this.askedFirst) { this.askedFirst = true; this.firstQuestion(b); } });
    core.on("approvalRequested", (a: Approval) => this.approvalCard(a));
    core.on("approvalDecided", (a: Approval) => this.closeRef(`approval:${a.id}`, a.status));
    core.on("questionAsked", (q: Question) => this.questionCard(q));
    core.on("questionAnswered", (q: Question) => this.closeRef(`question:${q.id}`, q.choice ?? q.reply ?? ""));
    core.on("deviceEvent", (e: DeviceEvent) => this.deviceEvent(e));
    core.on("memoryAdded", (m: MemoryEvent) => this.memoryCard(m));
    core.on("memoryDecided", (m: MemoryEvent) => this.closeRef(`memory:${m.entry.id}`, m.entry.status));
  }

  /** The human answered a card in the panel. */
  async answer(cardId: string, a: Answer): Promise<void> {
    const h = this.handlers.get(cardId);
    const card = this.cards.find((c) => c.id === cardId);
    if (!h || !card || card.status !== "open") return;
    this.handlers.delete(cardId);
    card.status = "closed";
    card.answer = a.text ?? card.options.find((o) => o.id === a.option)?.label ?? a.option;
    this.changed();
    await h(a);
  }

  /** Free text typed into the box at the top: always a way to say what you want. */
  async tell(text: string): Promise<void> {
    await this.wake(text, text.slice(0, 60));
  }

  stop(): void {
    this.stopping = true;
    this.driver?.stop();
  }

  /** A question from the host (e.g. which agent to use), shown like any other card. */
  ask(title: string, body: string, options: CardOption[], handler: (a: Answer) => unknown, freeText = false, pin = false): Card {
    return this.add({ kind: "question", title, body, options, freeText, pin }, handler);
  }

  /** A note from the host (e.g. no agent installed). */
  notify(title: string, body: string, detail?: string): Card {
    return this.add({ kind: "notify", title, body, detail, options: [], freeText: false, pin: true });
  }

  /** The person changed benchscout's language: new cards and the agent's next turn use it. */
  setLanguage(s: Strings, language: string): void {
    this.s = s;
    this.opts.language = language;
  }

  /** Forget the running agent (e.g. the human picked another one); a wake that waited for the choice goes now. */
  resetAgent(): void {
    this.driver?.stop();
    this.driver = undefined;
    const d = this.deferred;
    this.deferred = undefined;
    if (d) {
      this.closeRef("wait-agent", "");
      void d();
    }
  }

  // ---- cards -------------------------------------------------------------------------

  private add(card: Omit<Card, "id" | "time" | "progress" | "status"> & Partial<Card>, handler?: (a: Answer) => unknown, ref?: string): Card {
    const c: Card = { id: ref ?? `c${this.nextId++}`, time: Date.now(), status: "open", progress: [], ...card };
    this.cards.push(c);
    if (handler) this.handlers.set(c.id, handler);
    this.changed();
    return c;
  }

  private closeRef(ref: string, answer: string): void {
    const c = this.cards.find((x) => x.id === ref);
    if (c && c.status === "open") {
      c.status = "closed";
      c.answer = c.answer ?? answer;
      this.handlers.delete(ref);
      this.changed();
    }
  }

  private changed(): void {
    this.emit("changed");
  }

  /** benchscout's own first question, built from the raw clues without an LLM. */
  private firstQuestion(bench: Bench): void {
    const views = deviceViews(bench);
    if (!views.length) return;
    const lines = views.map((d) => {
      const known = d.things.map((t) => t.name || (t.status === "family" ? t.facts[0] : "")).filter(Boolean).join(", ");
      return `• ${d.name}${known ? ` — ${known}` : ""}${d.alert ? " ⚠" : ""}`;
    });
    const ordered = [...views].sort((a, b) => Number(b.alert) - Number(a.alert));
    const options: CardOption[] = ordered.map((d, i) => ({ id: d.id, label: this.s.lookAt(d.name), primary: i === 0 }));
    if (views.length > 1) options.push({ id: "*all", label: this.s.lookAll });
    this.add({ kind: "question", title: this.s.firstAsk, body: `${this.s.firstQuestion}\n${lines.join("\n")}`, options, freeText: true },
      async (a) => {
        if (a.text) return this.wake(a.text, a.text.slice(0, 60));
        const ids = a.option === "*all" ? views.map((d) => d.id) : [a.option!];
        await this.core.select(ids);
        const names = views.filter((d) => ids.includes(d.id)).map((d) => d.name).join(", ");
        return this.wake(`Look at ${names}: what they are, what they are doing, and what to check next.`, names);
      });
  }

  private approvalCard(a: Approval): void {
    if (a.by === "benchscout" && a.action === "serial.watch") {
      // benchscout's own offer: watching is what lets it notice crashes; the human says yes or not now
      this.add({
        kind: "confirm", title: this.s.offerWatch(a.subject || a.port), body: `${this.s.offerWatchWhy}\n${this.reason(a)}`,
        detail: a.port, effect: a.effect, freeText: false,
        options: [{ id: "once", label: this.s.watchIt, primary: true }, { id: "deny", label: this.s.notNow }],
      }, (ans) => this.core.decide(a.id, ans.option === "once", "once").catch(() => undefined), `approval:${a.id}`);
      return;
    }
    const fetch = a.fetch ? `\n${this.s.fetchFirst}: ${a.fetch}` : "";
    const options: CardOption[] = [{ id: "once", label: this.s.allowOnce, primary: true }];
    if (a.effect === "disruptive") options.push({ id: "session", label: this.s.allowSession });
    options.push({ id: "deny", label: this.s.deny });
    this.add({
      kind: "confirm", title: this.s.actions[a.action] ?? a.action, body: this.reason(a) + fetch,
      detail: `${a.port} · ${describeArgs(a.args)}${a.impact.affected_ports.length ? ` · ${this.s.alsoAffects}: ${a.impact.affected_ports.join(", ")}` : ""}`,
      effect: a.effect, options, freeText: false,
    }, (ans) => this.core.decide(a.id, ans.option !== "deny", ans.option === "session" ? "session" : "once").catch(() => undefined),
    `approval:${a.id}`);
  }

  /** The core's reason in the person's language when the panel knows its code; else its own words. */
  private reason(a: Approval): string {
    const r = (a.impact.code && this.s.reasons[a.impact.code]) || a.impact.reason;
    return r.charAt(0).toUpperCase() + r.slice(1);
  }

  private questionCard(q: Question): void {
    if (q.kind === "devices") {
      const names = Object.values(q.data.names ?? {}).join(", ");
      this.add({
        kind: "question", title: this.s.agentWantsDevices(names), body: q.data.reason ?? "",
        options: [{ id: "allow", label: this.s.allowDevices, primary: true }, { id: "deny", label: this.s.denyDevices }], freeText: true,
      }, (a) => this.core.answer(q.id, a.text ? null : a.option!, a.text).catch(() => undefined), `question:${q.id}`);
      return;
    }
    this.add({
      kind: "question", title: this.s.agentAsks, body: q.text,
      options: q.options.map((o, i) => ({ id: o, label: o, primary: i === 0 })), freeText: true,
    }, (a) => this.core.answer(q.id, a.text ? null : a.option!, a.text).catch(() => undefined), `question:${q.id}`);
  }

  // ---- memory: written without asking; only what loosens safety waits for the human ---------

  private memoryCard(m: MemoryEvent): void {
    const name = m.device_name ?? m.key;
    const e = m.entry;
    // benchscout's own proposal is said in the person's language; what the agent wrote stays as written
    const own = e.source === "benchscout" && e.kind === "safety" && e.data.listen_safe;
    const seen = Number(e.data.seen ?? e.text.match(/\((\d+) times\)/)?.[1] ?? 2);
    const body = (own ? this.s.listenSafeAsk(e.target, seen) : e.text) + (e.evidence ? `\n"${e.evidence}"` : "");
    const detail = `${e.kind} · ${e.source} · ${e.time.slice(0, 16).replace("T", " ")}`;
    if (e.status === "pending") {
      this.add({ kind: "memory", title: this.s.learnedSafety(name), body, detail, freeText: false,
        options: [{ id: "accept", label: this.s.accept, primary: true }, { id: "reject", label: this.s.reject }] },
        (a) => this.core.memoryDecide(m.key, e.id, a.option === "accept").catch(() => undefined), `memory:${e.id}`);
      return;
    }
    // remembered: shown as a record; "forget" removes it (the human can always edit the file too)
    this.add({ kind: "memory", title: this.s.learned(name), body, detail, freeText: false,
      options: [{ id: "forget", label: this.s.forget }] },
      (a) => (a.option === "forget" ? this.core.memoryDelete(m.key, e.id).catch(() => undefined) : undefined), `memory:${e.id}`);
  }

  // ---- noticing: events from the watched devices -------------------------------------------

  private deviceEvent(e: DeviceEvent): void {
    const name = e.device_name ?? e.port;
    const dev = e.device ?? e.port;
    // shown from the folder's .benchscout/ on: the full path is long and the folder is known
    const log = e.log ? `${this.s.logAt}: ${e.log.replace(/^.*?(\.benchscout[\\/])/, "$1")}` : undefined;
    if (e.cause === "host_sleep" && e.kind !== "gone") {
      // the computer slept, not the device: one quiet record per device, counted; nobody is woken
      if (e.kind !== "returned") return;
      const n = (this.sleeps.get(dev) ?? 0) + 1;
      this.sleeps.set(dev, n);
      const old = this.cards.find((c) => c.id === `sleep:${dev}`);
      if (old) {
        this.cards = this.cards.filter((c) => c !== old);
        this.cards.push({ ...old, title: this.s.hostSlept(name, n), time: Date.now() });
        this.changed();
      } else {
        this.add({ kind: "notify", title: this.s.hostSlept(name, n), body: "", detail: log, options: [], freeText: false, status: "closed" },
          undefined, `sleep:${dev}`);
      }
      return;
    }
    if (e.kind === "vanished") {
      this.add({ kind: "notify", title: this.s.deviceVanished(name), body: e.what, options: [], freeText: false, status: "closed" });
      return;
    }
    if (e.kind === "watching") {
      this.add({ kind: "notify", title: this.s.watching(name), body: "", detail: log, options: [], freeText: false, status: "closed" });
      return;
    }
    if (e.kind !== "alert" && e.kind !== "returned") {  // gone, unwatchable, or anything new: say it, do not wake
      this.add({ kind: "notify", title: this.s.deviceGone(name), body: e.what, detail: log, options: [], freeText: false });
      return;
    }
    const title = e.kind === "alert" ? this.s.deviceAlert(name) : this.s.deviceReset(name);
    const body = e.kind === "alert" ? `${e.what}${e.line ? `\n> ${e.line}` : ""}` : e.what;
    const request = [
      `benchscout noticed this on ${name} (${e.port}): ${e.kind === "alert" ? e.what
        : "its port vanished and came back (a reset, a USB re-enumeration or a loose cable; check the device's own output to tell)"}.`,
      e.line ? `The line: ${e.line}` : "",
      e.log ? `Everything the port printed is in ${e.log} (read the end of it).` : "",
      "Find out what happened and why, with evidence from the device's output.",
    ].filter(Boolean).join("\n");
    const recent = Date.now() - (this.lastWake.get(dev) ?? 0) < (this.opts.wakeCooldownMs ?? 60_000);
    if (recent) {
      // one crash prints several alert lines (abort, then a backtrace): the agent already woke for this
      // device and reads the whole log, so record it, do not ask again (real bench, 2026-11-01)
      this.add({ kind: "notify", title, body, detail: log, options: [], freeText: false, status: "closed" });
      return;
    }
    if ((this.opts.autonomy ?? "auto") === "auto") {
      this.add({ kind: "notify", title, body, detail: log, options: [], freeText: false, status: "closed" });
      this.lastWake.set(dev, Date.now());
      void this.wake(request, title);
      return;
    }
    // suggest mode: offer, do not act
    this.lastWake.set(dev, Date.now());
    this.add({ kind: "question", title, body, detail: log, freeText: true,
      options: [{ id: "go", label: this.s.investigate, primary: true }, { id: "ignore", label: this.s.ignore }] },
      (a) => (a.text ? this.wake(`${request}\n\nThe human adds: ${a.text}`, title) : a.option === "go" ? this.wake(request, title) : undefined));
  }

  // ---- waking the agent ---------------------------------------------------------------

  private overBudget(): boolean {
    const hourAgo = Date.now() - 3600_000;
    this.turns = this.turns.filter((t) => t > hourAgo);
    return this.turns.length >= this.opts.maxTurnsPerHour;
  }

  private async wake(request: string, title: string): Promise<void> {
    if (this.stopping) return;
    if (this.overBudget()) {
      this.add({ kind: "notify", title: this.s.budget(this.opts.maxTurnsPerHour), body: "", options: [], freeText: false });
      return;
    }
    if (this.busy) {
      // one turn at a time; the request is not lost, it goes to the agent next
      this.once("idle", () => void this.wake(request, title));
      return;
    }
    this.busy = true;
    const report = this.add({ kind: "report", title: `${this.s.investigating}: ${title}`, body: this.s.working, options: [], freeText: false, status: "working" });
    this.current = { card: report, tools: new Map() };
    try {
      if (!this.driver) {
        const d = this.makeDriver();
        d.onEvent = (e) => this.agentEvent(e);
        d.onPermission = (p) => this.permission(p);
        try {
          await d.start();
        } catch (e) {
          const w = e instanceof WakeError ? e : new WakeError(String(e), "");
          this.cards = this.cards.filter((c) => c !== report);
          if (w.code === "no_agent") {
            // not a failure: nothing is chosen yet. Wait for the human's choice, then go on with this request.
            this.deferred = () => this.wake(request, title);
            if (!this.cards.some((c) => c.id === "wait-agent" && c.status === "open")) {
              this.add({ kind: "notify", title: this.s.chooseAgentFirst, body: "", options: [], freeText: false, pin: true }, undefined, "wait-agent");
            }
            return;
          }
          this.add({ kind: "notify", title: this.s.wakeFailed(d.label), body: w.message, detail: w.fix ? `${this.s.wakeFix}: ${w.fix}` : undefined, options: [], freeText: false });
          return; // never fall back to another agent or LLM
        }
        this.driver = d;
      }
      this.turns.push(Date.now());
      const r = await this.driver.prompt(this.instructions(request));
      report.status = "open";
      report.title = `${this.s.report}: ${title}`;
      report.body = r.text.trim() || report.body;
      report.options = [{ id: "continue", label: this.s.continueIt, primary: true }, { id: "done", label: this.s.doneIt }];
      report.freeText = true;
      this.handlers.set(report.id, (a) => (a.text ? this.wake(a.text, a.text.slice(0, 60)) : a.option === "continue" ? this.wake("Continue.", title) : undefined));
    } catch (e) {
      report.status = "closed";
      if (this.stopping) return; // stopped on purpose: not a failure to report
      const w = e instanceof WakeError ? e : new WakeError(String(e), "");
      this.add({ kind: "notify", title: this.s.wakeFailed(this.driver?.label ?? "agent"), body: w.message, detail: w.fix || undefined, options: [], freeText: false });
      this.driver?.stop();
      this.driver = undefined;
    } finally {
      this.busy = false;
      this.current = undefined;
      this.changed();
      this.emit("idle");
    }
  }

  /** What the agent is doing, shown on the report card of the turn in progress. */
  private agentEvent(e: AgentEvent): void {
    const cur = this.current;
    if (!cur) return;
    const report = cur.card;
    if (e.kind === "tool") {
      // one line per call, updated in place: "… bench_scan" then "✓ bench_scan"
      const name = e.title.replace(/^mcp__benchscout__/, "").replace(/^benchscout[-_]/, "");
      const mark = e.status === "completed" ? "✓" : e.status === "failed" ? "✗" : "…";
      const i = cur.tools.get(e.id);
      if (i === undefined) {
        cur.tools.set(e.id, report.progress.length);
        report.progress.push(`${mark} ${name}`);
      } else {
        report.progress[i] = `${mark} ${name}`;
      }
    } else if (e.kind === "text") {
      report.body = (report.body === this.s.working ? "" : report.body) + e.text;
    }
    this.changed();
  }

  /** benchscout's own tools are gated by the broker, so the agent's ask about them is answered here. */
  private permission(p: PermissionAsk): Promise<string> {
    const allow = p.options.find((o) => o.kind === "allow_once") ?? p.options.find((o) => o.kind !== "reject");
    if (p.benchscout && allow) return Promise.resolve(allow.id);
    return new Promise((resolve) => {
      this.add({
        kind: "confirm", title: `${this.s.agentWantsTool}: ${p.title}`, body: "", detail: p.detail,
        options: p.options.map((o, i) => ({ id: o.id, primary: i === 0,
          label: o.kind === "allow_once" ? this.s.allowOnce : o.kind === "allow_always" ? this.s.allowAlways : this.s.deny })), freeText: false,
      }, (a) => resolve(a.option ?? p.options.find((o) => o.kind === "reject")?.id ?? "reject"));
    });
  }

  private instructions(request: string): string {
    return [
      request,
      "",
      "Use the benchscout MCP tools for anything on the bench (start with bench_scan). Name a part only with a",
      "quote of the device's own output. When you need a decision, call ask_user with short options; if you",
      "need a device that is not selected, call request_devices. Keep the report short: what you found, the",
      `evidence, and what to check next. Reply in ${this.opts.language}.`,
    ].join("\n");
  }
}
