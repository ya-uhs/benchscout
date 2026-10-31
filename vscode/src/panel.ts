// The inbox panel (design.md section 8): cards for what needs the human, newest first, and a box
// at the top to say what you want. It only shows state and sends the person's choices back.

import { Card } from "./harness";
import { Strings } from "./i18n";

export interface PanelState {
  running: boolean;
  agent: string; // label of the agent benchscout wakes, or "" if none chosen yet
  cards: Card[];
  devices: string[]; // one line per device, for the small summary at the bottom
  strings: Omit<Strings, "statusWaiting" | "lookAt" | "wakeFailed" | "budget" | "agentWantsDevices" | "learned" | "learnedSafety"
    | "deviceAlert" | "deviceVanished" | "deviceReset" | "deviceGone">;
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
  .card { border: 1px solid var(--vscode-panel-border); border-left-width: 4px; border-radius: 6px; padding: 8px 10px; margin: 8px 0; overflow-wrap: anywhere; }
  .card.question { border-left-color: var(--vscode-focusBorder); }
  .card.confirm { border-left-color: var(--vscode-editorWarning-foreground); background: var(--vscode-inputValidation-warningBackground); }
  .card.confirm.dangerous { border-left-color: var(--vscode-editorError-foreground); background: var(--vscode-inputValidation-errorBackground); }
  .card.report { border-left-color: var(--vscode-testing-iconPassed); }
  .card.notify { border-left-color: var(--vscode-editorInfo-foreground); }
  .card.memory { border-left-color: var(--vscode-charts-purple, #b180d7); }
  .card.closed { opacity: .6; }
  .head { display: flex; justify-content: space-between; gap: 6px; font-weight: 600; }
  .kind { font-weight: normal; font-size: .85em; }
  .body { white-space: pre-wrap; margin: 6px 0 0; }
  .detail { font-family: var(--vscode-editor-font-family); font-size: .9em; margin-top: 4px; }
  .row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
  button { background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); border: none; padding: 4px 10px; border-radius: 3px; cursor: pointer; }
  button.primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
  details { margin-top: 6px; } summary { cursor: pointer; }
  .answer { margin-top: 6px; font-size: .9em; }
  .spin::after { content: " …"; }
</style></head>
<body><div id="app"></div>
<script nonce="${nonce}">
const vscode = acquireVsCodeApi();
let state = null;
const drafts = {};  // what the person typed, kept across re-renders
const esc = (x) => String(x ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const send = (m) => vscode.postMessage(m);
const ICON = { question: "❓", confirm: "🔔", report: "📋", notify: "ℹ️", memory: "🧠" };

function card(c, s) {
  const kindLabel = { question: "", confirm: c.effect ? s.effect[c.effect] ?? "" : "", report: "", notify: "", memory: "" }[c.kind];
  let h = '<div class="card ' + c.kind + " " + esc(c.effect ?? "") + (c.status === "closed" ? " closed" : "") + '">';
  h += '<div class="head"><span>' + ICON[c.kind] + " " + esc(c.title) + (c.status === "working" ? '<span class="spin"></span>' : "") +
       '</span><span class="kind muted">' + esc(kindLabel) + " " + new Date(c.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + "</span></div>";
  if (c.body) h += '<div class="body">' + esc(c.body) + "</div>";
  if (c.detail) h += '<div class="detail muted">' + esc(c.detail) + "</div>";
  if (c.progress.length) h += "<details><summary class=\\"muted\\">" + esc(s.showProgress) + " (" + c.progress.length + ")</summary><div class=\\"detail muted\\">" + c.progress.map(esc).join("<br>") + "</div></details>";
  if (c.status === "open" && c.options.length) {
    h += '<div class="row">' + c.options.map((o) => '<button class="' + (o.primary ? "primary" : "") + '" data-card="' + esc(c.id) + '" data-opt="' + esc(o.id) + '">' + esc(o.label) + "</button>").join("") + "</div>";
  }
  if (c.status === "open" && c.freeText) {
    h += '<div class="row" style="flex-wrap:nowrap"><textarea data-draft="' + esc(c.id) + '" placeholder="' + esc(s.typeHere) + '">' + esc(drafts[c.id] ?? "") + '</textarea><button data-card="' + esc(c.id) + '" data-text="1">' + esc(s.send) + "</button></div>";
  }
  if (c.status === "closed" && c.answer) h += '<div class="answer muted">→ ' + esc(c.answer) + "</div>";
  return h + "</div>";
}

function render() {
  const s = state.strings;
  let h = '<div class="top"><div class="status"><span class="dot ' + (state.running ? "" : "off") + '"></span><span title="' + esc(s.running) + '">' +
          esc(state.running ? (state.agent ? s.runningWith + " · " + state.agent : s.running) : s.stopped) + "</span></div>";
  h += '<div class="row" style="flex-wrap:nowrap;margin-top:0"><textarea data-draft="_top" placeholder="' + esc(s.newInput) + '">' + esc(drafts._top ?? "") +
       '</textarea><button class="primary" data-tell="1">' + esc(s.send) + "</button></div>";
  h += '<div class="row">' + (state.running ? '<button data-cmd="rescan">' + esc(s.rescan) + '</button><button data-cmd="stop">' + esc(s.stop) + "</button>"
                                            : '<button class="primary" data-cmd="start">' + esc(s.start) + "</button>") + "</div></div>";
  const open = state.cards.filter((c) => c.status !== "closed").reverse();
  const closed = state.cards.filter((c) => c.status === "closed").reverse().slice(0, 15);
  h += open.map((c) => card(c, s)).join("") + closed.map((c) => card(c, s)).join("");
  if (state.devices.length) h += "<details><summary class=\\"muted\\">" + esc(s.devicesTitle) + " (" + state.devices.length + ")</summary><div class=\\"detail muted\\">" + state.devices.map(esc).join("<br>") + "</div></details>";
  document.getElementById("app").innerHTML = h;
}

window.addEventListener("message", (e) => { if (e.data.type === "state") { state = e.data.state; render(); } });
send({ type: "ready" });

document.addEventListener("input", (e) => { const id = e.target.dataset?.draft; if (id) drafts[id] = e.target.value; });
document.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.opt) send({ type: "answer", id: b.dataset.card, option: b.dataset.opt });
  else if (b.dataset.text) { const t = (drafts[b.dataset.card] ?? "").trim(); if (t) { send({ type: "answer", id: b.dataset.card, text: t }); delete drafts[b.dataset.card]; } }
  else if (b.dataset.tell) { const t = (drafts._top ?? "").trim(); if (t) { send({ type: "tell", text: t }); drafts._top = ""; } }
  else if (b.dataset.cmd) send({ type: b.dataset.cmd });
});
</script></body></html>`;
}
