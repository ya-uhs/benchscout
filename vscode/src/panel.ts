// The inbox panel (design.md section 8): cards for what needs the human, newest first, and a box
// at the top to say what you want. It only shows state and sends the person's choices back.

import { Card } from "./harness";
import { Wording } from "./wording";

/** One device in the list at the top: the person can release it (for their own tools) or use it again. */
export interface DeviceRow {
  id: string;
  label: string; // its name, and what it was identified as
  selected: boolean; // in this session: benchscout holds it and the agent may use it
  watched: boolean; // its serial port is held open and logged
  busy: boolean; // held by another benchscout window
  noDriver: boolean; // the OS has no driver for it (Windows): nothing can open it until one is installed
  needsWinusb: boolean; // Windows: a libusb tool needs WinUSB on one of its interfaces
  needsUdev?: boolean; // Linux: a libusb tool needs a udev rule for this device
}

export interface PanelState {
  running: boolean;
  agent: string; // label of the agent benchscout wakes, or "" if none chosen yet
  cards: Card[];
  devices: DeviceRow[];
  allowAll: boolean; // the human allowed every DISRUPTIVE call for this session
  autonomy: "auto" | "suggest"; // wake the agent on events, or only offer to (design.md section 17)
  others?: number; // USB devices benchscout has no data for (the agent sees them; listed here only as a count)
  // what is remembered about each device on the bench (design.md section 6): seen, fixed in its file, deleted here
  memory?: { device: string; name: string; key: string; file: string;
             entries: { id: string; kind: string; text: string; source: string; time: string; status: string; device?: string }[] }[];
  strings: { [K in keyof Wording as Wording[K] extends (...a: never[]) => unknown ? never : K]: Wording[K] };  // the plain ones
}

export function html(nonce: string, cspSource: string): string {
  return /* html */ `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); padding: 0 10px 16px; }
  .top { position: sticky; top: 0; background: var(--vscode-sideBar-background); padding: 8px 0; z-index: 1; }
  .status { display: flex; gap: 6px; align-items: center; font-size: .92em; margin-bottom: 6px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--vscode-testing-iconPassed); flex: none; }
  .dot.off { background: var(--vscode-disabledForeground); }
  .muted { color: var(--vscode-descriptionForeground); }
  textarea { width: 100%; box-sizing: border-box; resize: vertical; min-height: 2.4em; background: var(--vscode-input-background);
             color: var(--vscode-input-foreground); border: 1px solid var(--vscode-input-border, transparent); border-radius: 3px; padding: 4px 6px; font: inherit; }
  .allowall { display: flex; gap: 8px; align-items: center; margin: 6px 0; padding: 6px 8px; border-radius: 4px;
              background: var(--vscode-inputValidation-warningBackground); border: 1px solid var(--vscode-inputValidation-warningBorder, transparent); }
  .allowall span { flex: 1; }
  .card { border: 1px solid var(--vscode-panel-border); border-left-width: 4px; border-radius: 6px; padding: 8px 10px; margin: 8px 0; overflow-wrap: anywhere; }
  .card.question { border-left-color: var(--vscode-focusBorder); }
  .card.confirm { border-left-color: var(--vscode-editorWarning-foreground); background: var(--vscode-inputValidation-warningBackground); }
  .card.confirm.dangerous { border-left-color: var(--vscode-editorError-foreground); background: var(--vscode-inputValidation-errorBackground); }
  .card.report { border-left-color: var(--vscode-testing-iconPassed); }
  .card.notify { border-left-color: var(--vscode-editorInfo-foreground); }
  .card.memory { border-left-color: var(--vscode-charts-purple, #b180d7); }
  .card.closed { opacity: .6; }
  .card.pin { box-shadow: 0 0 0 1px var(--vscode-focusBorder); }
  .head { display: flex; justify-content: space-between; gap: 6px; font-weight: 600; }
  .kind { font-weight: normal; font-size: .85em; flex: none; white-space: nowrap; }
  .badge { display: inline-block; font-size: .85em; margin-top: 3px; padding: 0 6px; border-radius: 8px; border: 1px solid currentColor; opacity: .85; }
  .card.record .body { display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .body { margin: 6px 0 0; }
  .body p { margin: 0 0 4px; } .body ul { margin: 2px 0 4px; padding-left: 18px; } .body li { margin: 1px 0; }
  .body h4 { margin: 6px 0 2px; font-size: 1em; }
  .body code { font-family: var(--vscode-editor-font-family); font-size: .92em; background: var(--vscode-textCodeBlock-background); padding: 0 3px; border-radius: 3px; }
  .card.closed .body { display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .detail { font-family: var(--vscode-editor-font-family); font-size: .9em; margin-top: 4px; }
  .row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
  button { background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); border: none; padding: 4px 10px; border-radius: 3px; cursor: pointer;
           flex: none; white-space: nowrap; font: inherit; }
  button.opt { flex: 0 1 auto; white-space: normal; text-align: left; max-width: 100%; }
  button.primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
  details { margin-top: 6px; } summary { cursor: pointer; }
  .answer { margin-top: 6px; font-size: .9em; }
  .spin::after { content: " …"; }
  .devs { margin: 8px 0 4px; }
  .devhead { cursor: pointer; font-weight: 600; user-select: none; }
  .dev { display: flex; align-items: center; gap: 6px; padding: 4px 0; border-bottom: 1px solid var(--vscode-panel-border); }
  .dev .dname { flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere; }
  .dev .state { flex: none; font-size: .85em; }
  .dot.sel { background: var(--vscode-editorInfo-foreground); }
  .memdev { display: flex; align-items: center; gap: 6px; margin-top: 6px; font-weight: 600; }
  .memdev .dname { flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere; }
  .mem-entry { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 6px; padding: 3px 0 3px 10px; border-bottom: 1px solid var(--vscode-panel-border); }
  .mem-entry .memtext { flex: 1 1 60%; min-width: 0; overflow-wrap: anywhere; }
  .mem-entry .memmeta { font-size: .85em; }
  .hint { font-size: .85em; margin-top: 4px; }
</style></head>
<body><div id="app"></div>
<script nonce="${nonce}">
const vscode = acquireVsCodeApi();
let state = null;
let devsOpen = true;  // the device list, open until the person folds it
let memOpen = false;  // what is remembered: folded until the person opens it
const drafts = {};  // what the person typed, kept across re-renders
const esc = (x) => String(x ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const send = (m) => vscode.postMessage(m);
const ICON = { question: "❓", confirm: "🔔", report: "📋", notify: "ℹ️", memory: "🧠" };

// The agent writes Markdown. A small subset, applied after escaping, so nothing it says becomes HTML.
function md(text) {
  const inline = (t) => esc(t).replace(/\\x60([^\\x60]+)\\x60/g, "<code>$1</code>").replace(/\\*\\*([^*]+)\\*\\*/g, "<b>$1</b>");
  let out = "", list = false;
  for (const raw of String(text ?? "").split("\\n")) {
    const line = raw.trimEnd();
    const item = line.match(/^\\s*[-*•]\\s+(.*)$/) || line.match(/^\\s*\\d+[.)]\\s+(.*)$/);
    if (item) { if (!list) { out += "<ul>"; list = true; } out += "<li>" + inline(item[1]) + "</li>"; continue; }
    if (list) { out += "</ul>"; list = false; }
    const h = line.match(/^#{1,6}\\s+(.*)$/);
    if (h) out += "<h4>" + inline(h[1]) + "</h4>";
    else if (line.trim()) out += "<p>" + inline(line) + "</p>";
  }
  return out + (list ? "</ul>" : "");
}

function card(c, s) {
  const badge = c.kind === "confirm" && c.effect ? s.effect[c.effect] ?? "" : "";
  const record = c.kind === "memory" && !c.options.some((o) => o.primary);  // remembered, nothing to decide
  let h = '<div class="card ' + c.kind + " " + esc(c.effect ?? "") + (c.status === "closed" ? " closed" : "") + (record ? " record" : "") +
          (c.pin && c.status !== "closed" ? " pin" : "") + '">';
  h += '<div class="head"><span>' + ICON[c.kind] + " " + esc(c.title) + (c.status === "working" ? '<span class="spin"></span>' : "") +
       '</span><span class="kind muted">' + new Date(c.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + "</span></div>";
  if (badge) h += '<span class="badge">' + esc(badge) + "</span>";
  if (c.body) h += '<div class="body">' + md(c.body) + "</div>";
  if (c.detail) h += '<div class="detail muted">' + esc(c.detail) + "</div>";
  if (c.progress.length) h += "<details><summary class=\\"muted\\">" + esc(s.showProgress) + " (" + c.progress.length + ")</summary><div class=\\"detail muted\\">" + c.progress.map(esc).join("<br>") + "</div></details>";
  if (c.status === "open" && c.options.length) {
    h += '<div class="row">' + c.options.map((o) => '<button class="opt' + (o.primary ? " primary" : "") + '" data-card="' + esc(c.id) + '" data-opt="' + esc(o.id) + '">' + esc(o.label) + "</button>").join("") + "</div>";
  }
  if (c.status === "open" && c.freeText) {
    h += '<div class="row" style="flex-wrap:nowrap"><textarea data-draft="' + esc(c.id) + '" placeholder="' + esc(c.placeholder ?? s.typeHere) + '">' + esc(drafts[c.id] ?? "") + '</textarea><button data-card="' + esc(c.id) + '" data-text="1">' + esc(s.send) + "</button></div>";
  }
  if (c.status === "closed" && c.answer) h += '<div class="answer muted">→ ' + esc(c.answer) + "</div>";
  return h + "</div>";
}

function render() {
  const s = state.strings;
  let h = '<div class="top"><div class="status"><span class="dot ' + (state.running ? "" : "off") + '"></span><span title="' + esc(s.running) + '">' +
          esc(state.running ? s.runningWith + " · " + (state.agent || s.noAgentYet) : s.stopped) + "</span></div>";
  h += '<div class="row" style="flex-wrap:nowrap;margin-top:0"><textarea data-draft="_top" placeholder="' + esc(s.newInput) + '">' + esc(drafts._top ?? "") +
       '</textarea><button class="primary" data-tell="1">' + esc(s.send) + "</button></div>";
  h += '<div class="row">' + (state.running ? '<button data-cmd="rescan">' + esc(s.rescan) + '</button><button data-cmd="stop">' + esc(s.stop) + "</button>"
                                            : '<button class="primary" data-cmd="start">' + esc(s.start) + "</button>") +
       '<button data-cmd="autonomy" title="' + esc(s.autonomyHint) + '">' + esc(state.autonomy === "suggest" ? s.autonomySuggest : s.autonomyAuto) + "</button></div>";
  // while everything is allowed, say so at the top, with the way back
  if (state.allowAll) h += '<div class="allowall"><span>' + esc(s.allowAllOn) + '</span><button data-cmd="allowAllOff">' + esc(s.allowAllOff) + "</button></div>";
  h += "</div>";
  // what needs the person first: pinned, then decisions, then reports, then records (newest first within each)
  if (state.others && !state.devices.length) h += '<div class="hint muted">' + esc(s.otherDevices.replace("{n}", String(state.others))) + "</div>";
  const rank = (c) => (c.pin ? 0 : c.kind === "confirm" || c.kind === "question" || (c.kind === "memory" && c.options.some((o) => o.primary)) ? 1 : c.kind === "report" ? 2 : 3);
  if (state.devices.length) {
    h += '<div class="devs"><div class="devhead" data-toggle="devs">' + (devsOpen ? "▾ " : "▸ ") + esc(s.devicesTitle) + " (" + state.devices.length + ")</div>";
    if (devsOpen) {
      for (const d of state.devices) {
        const st = d.busy ? s.inUse : d.noDriver ? s.stateNoDriver : d.needsWinusb ? s.stateNeedsWinusb : d.needsUdev ? s.stateNeedsUdev : d.watched ? s.stateWatched : d.selected ? s.stateSelected : s.stateFree;
        const btn = d.busy ? "" : '<button data-dev="' + esc(d.id) + '" data-use="' + (d.selected ? "0" : "1") + '">' + esc(d.selected ? s.releaseDevice : s.useDevice) + "</button>";
        h += '<div class="dev"><span class="dot ' + (d.watched ? "" : d.selected ? "sel" : "off") + '"></span><span class="dname">' + esc(d.label) +
             '</span><span class="state muted">' + esc(st) + "</span>" + btn + "</div>";
      }

      if (state.others) h += '<div class="hint muted">' + esc(s.otherDevices.replace("{n}", String(state.others))) + "</div>";
      h += '<div class="hint muted">' + esc(s.releaseHint) + "</div>";
    }
    h += "</div>";
  }
  if (state.memory && state.memory.length) {
    const n = state.memory.reduce((a, g) => a + g.entries.length, 0);
    h += '<div class="devs"><div class="devhead" data-toggle="mem">' + (memOpen ? "▾ " : "▸ ") + esc(s.memoryTitle) + " (" + n + ")</div>";
    if (memOpen) {
      for (const g of state.memory) {
        h += '<div class="memdev"><span class="dname">' + esc(g.name) + '</span><button data-memfile="' + esc(g.file) + '">' + esc(s.memoryOpenFile) + "</button></div>";
        for (const e of g.entries) {
          const pending = e.status === "pending";
          const ids = ' data-key="' + esc(g.key) + '" data-id="' + esc(e.id) + '"';
          h += '<div class="mem-entry"><span class="muted">' + esc((s.memoryKinds && s.memoryKinds[e.kind]) || e.kind) + "</span>" +
               '<span class="memtext">' + esc(e.text) + "</span>" +
               '<span class="muted memmeta">' + esc(((s.memorySources && s.memorySources[e.source]) || e.source) + " · " + String(e.time || "").slice(0, 10) + (e.device ? " · " + s.memoryHere : "")
                 + (pending ? " · " + s.memoryPending : "")) + "</span>" +
               (pending ? '<button data-memdecide="1"' + ids + ">" + esc(s.accept) + '</button><button data-memdecide="0"' + ids + ">" + esc(s.reject) + "</button>"
                        : '<button data-memforget="1"' + ids + ">" + esc(s.forget) + "</button>") + "</div>";
        }
      }
      h += '<div class="hint muted">' + esc(s.memoryHint) + "</div>";
    }
    h += "</div>";
  }
  const open = state.cards.filter((c) => c.status !== "closed").reverse().sort((a, b) => rank(a) - rank(b));
  const closed = state.cards.filter((c) => c.status === "closed").reverse().slice(0, 15);
  h += open.map((c) => card(c, s)).join("") + closed.map((c) => card(c, s)).join("");
  // the box being typed in survives the redraw: the same box, focused, with the caret where it was
  const typing = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.draft : undefined;
  const caret = typing !== undefined ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  document.getElementById("app").innerHTML = h;
  if (typing !== undefined) {
    const box = document.querySelector('textarea[data-draft="' + CSS.escape(typing) + '"]');
    if (box) { box.focus(); box.setSelectionRange(caret[0], caret[1]); }
  }
}

// while a word is being composed (Japanese and other input methods), a redraw would throw the composition away: the
// redraw waits for the word to be committed (the agent's output can stream in many times a second)
let composing = false, redrawLater = false;
document.addEventListener("compositionstart", () => { composing = true; });
document.addEventListener("compositionend", () => { composing = false; if (redrawLater) { redrawLater = false; render(); } });
window.addEventListener("message", (e) => {
  if (e.data.type !== "state") return;
  state = e.data.state;
  window.benchscoutState = state;  // the last state, so a check can send it again (scripts/ui-check.mjs typing)
  if (composing) redrawLater = true; else render();
});
send({ type: "ready" });

document.addEventListener("input", (e) => { const id = e.target.dataset?.draft; if (id) drafts[id] = e.target.value; });
document.addEventListener("click", (e) => {
  const tog = e.target.closest("[data-toggle]");
  if (tog) { if (tog.dataset.toggle === "mem") memOpen = !memOpen; else devsOpen = !devsOpen; render(); return; }
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.dev) { send({ type: "device", id: b.dataset.dev, use: b.dataset.use === "1" }); return; }
  if (b.dataset.memfile) { send({ type: "memoryOpen", file: b.dataset.memfile }); return; }
  if (b.dataset.memforget) { send({ type: "memoryForget", key: b.dataset.key, id: b.dataset.id }); return; }
  if (b.dataset.memdecide) { send({ type: "memoryDecide", key: b.dataset.key, id: b.dataset.id, accept: b.dataset.memdecide === "1" }); return; }
  if (b.dataset.opt) send({ type: "answer", id: b.dataset.card, option: b.dataset.opt });
  else if (b.dataset.text) { const t = (drafts[b.dataset.card] ?? "").trim(); if (t) { send({ type: "answer", id: b.dataset.card, text: t }); delete drafts[b.dataset.card]; } }
  else if (b.dataset.tell) { const t = (drafts._top ?? "").trim(); if (t) { send({ type: "tell", text: t }); drafts._top = ""; } }
  else if (b.dataset.cmd) send({ type: b.dataset.cmd });
});
</script></body></html>`;
}
