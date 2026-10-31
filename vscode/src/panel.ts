// The bench panel: a webview that only shows state and sends the person's choices back.
// Order on screen = order of importance: what waits for you, the devices, the next step.

import { Strings } from "./i18n";
import { DeviceView, Proposal } from "./core";

export interface PendingView {
  id: string; effect: string; what: string; port: string; args: string; why: string; also: string[];
}

export interface PanelState {
  running: boolean;
  devices: DeviceView[];
  selected: string[];
  pending: PendingView[];
  proposals: Proposal[];
  history: string[];
  strings: Omit<Strings, "statusWaiting">;
}

export function html(nonce: string, cspSource: string): string {
  return /* html */ `<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); padding: 0 10px 16px; }
  h2 { font-size: 1.05em; margin: 18px 0 6px; }
  .status { display: flex; gap: 8px; align-items: center; margin-top: 10px; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--vscode-testing-iconPassed); flex: none; }
  .dot.off { background: var(--vscode-disabledForeground); }
  .muted { color: var(--vscode-descriptionForeground); }
  .card { border: 1px solid var(--vscode-panel-border); border-radius: 6px; padding: 8px 10px; margin: 6px 0; overflow-wrap: anywhere; }
  .card.chosen { border-color: var(--vscode-focusBorder); border-width: 2px; }
  .card.ask { border-color: var(--vscode-editorWarning-foreground); background: var(--vscode-inputValidation-warningBackground); }
  .card.ask.dangerous { border-color: var(--vscode-editorError-foreground); background: var(--vscode-inputValidation-errorBackground); }
  .badge { display: inline-block; font-size: .85em; padding: 0 6px; border-radius: 8px; margin-left: 4px; border: 1px solid currentColor; }
  .ok { color: var(--vscode-testing-iconPassed); } .warn { color: var(--vscode-editorWarning-foreground); }
  .bad { color: var(--vscode-editorError-foreground); }
  .what { font-weight: 600; margin: 4px 0; }
  .row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
  button { background: var(--vscode-button-background); color: var(--vscode-button-foreground); border: none; padding: 4px 10px; border-radius: 3px; cursor: pointer; }
  button.secondary { background: var(--vscode-button-secondaryBackground); color: var(--vscode-button-secondaryForeground); }
  button:disabled { opacity: .5; cursor: default; }
  label.dev { display: flex; gap: 8px; align-items: flex-start; cursor: pointer; }
  .thing { margin: 3px 0 0 24px; } .facts { margin: 2px 0 0 24px; font-size: .92em; }
  code { font-family: var(--vscode-editor-font-family); }
  .prompt { background: var(--vscode-textBlockQuote-background); padding: 6px 8px; border-radius: 4px; margin: 6px 0; }
  ul { margin: 4px 0; padding-left: 18px; }
</style></head>
<body><div id="app"></div>
<script nonce="${nonce}">
const vscode = acquireVsCodeApi();
let state = null;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const send = (msg) => vscode.postMessage(msg);

window.addEventListener("message", (e) => { if (e.data.type === "state") { state = e.data.state; render(); } });
send({ type: "ready" });

function render() {
  const s = state.strings, app = document.getElementById("app");
  let h = "";
  h += '<div class="status"><span class="dot ' + (state.running ? "" : "off") + '"></span><span>' +
       esc(state.running ? s.running : s.stopped) + '</span></div><div class="row">' +
       (state.running ? '<button class="secondary" data-cmd="rescan">' + esc(s.rescan) + '</button>' +
                        '<button class="secondary" data-cmd="stop">' + esc(s.stop) + '</button>'
                      : '<button data-cmd="start">' + esc(s.start) + '</button>') + '</div>';

  if (state.pending.length) {
    h += "<h2>" + esc(s.approvalsTitle) + " (" + state.pending.length + ")</h2>";
    for (const p of state.pending) {
      h += '<div class="card ask ' + esc(p.effect) + '"><div>' + esc(s.approvalLead) + '</div>' +
           '<div class="what">' + esc(p.what) + '</div>' +
           '<div class="muted"><code>' + esc(p.port) + '</code>' + (p.args ? " · " + esc(p.args) : "") +
           '<span class="badge ' + (p.effect === "dangerous" ? "bad" : "warn") + '">' + esc(s.effect[p.effect]) + '</span></div>' +
           '<div style="margin-top:6px"><b>' + esc(s.why) + ':</b> ' + esc(p.why) + '</div>' +
           (p.also.length ? '<div><b>' + esc(s.alsoAffects) + ':</b> ' + p.also.map((a) => "<code>" + esc(a) + "</code>").join(", ") + '</div>' : "") +
           '<div class="row"><button data-decide="' + esc(p.id) + '" data-scope="once">' + esc(s.allowOnce) + '</button>' +
           (p.effect === "disruptive" ? '<button class="secondary" data-decide="' + esc(p.id) + '" data-scope="session">' + esc(s.allowSession) + '</button>' : "") +
           '<button class="secondary" data-deny="' + esc(p.id) + '">' + esc(s.deny) + '</button></div></div>';
    }
  }

  h += "<h2>" + esc(s.devicesTitle) + "</h2>";
  if (!state.devices.length) {
    h += '<div class="muted">' + esc(state.running ? s.noDevices : "") + "</div>";
  } else {
    h += '<div class="muted">' + esc(s.devicesHint) + "</div>";
    for (const d of state.devices) {
      const checked = state.selected.includes(d.id) ? "checked" : "";
      const dis = d.busyBy ? "disabled" : "";
      h += '<div class="card' + (checked ? " chosen" : "") + '"><label class="dev"><input type="checkbox" class="pick" value="' + esc(d.id) + '" ' + checked + " " + dis + '>' +
           '<span><b>' + esc(d.name) + '</b> <span class="muted">' + esc(d.ports.join(", ")) + '</span>' +
           (d.alert ? '<span class="badge bad">' + esc(s.alert) + '</span>' : "") +
           (d.busyBy ? '<span class="badge warn">' + esc(s.inUse) + '</span>' : "") + '</span></label>';
      for (const t of d.things) {
        const cls = t.status === "identified" ? "ok" : t.status === "family" ? "warn" : "muted";
        const label = t.status === "identified" ? s.identified : t.status === "family" ? s.family : s.unknown;
        h += '<div class="thing"><span class="badge ' + cls + '">' + esc(label) + '</span> ' +
             esc(t.name || (t.status === "family" && t.facts[0]) || s.unknownName) + (t.inside ? ' <span class="muted">(' + esc(t.inside) + " " + esc(s.inside) + ')</span>' : "") +
             (t.how ? '<div class="muted" style="margin-left:4px">' + esc(s.proof) + ": " + esc(t.how) + "</div>" : "") + "</div>";
        const facts = t.facts.filter((f) => !f.startsWith("in use by another") && !(t.status === "family" && !t.name && f === t.facts[0]));
        if (facts.length) h += '<ul class="facts muted">' + facts.slice(0, 6).map((f) => "<li>" + esc(f) + "</li>").join("") + "</ul>";
      }
      h += "</div>";
    }
    h += '<div class="row"><button data-cmd="select">' + esc(s.useSelected) + "</button></div>";
  }

  h += "<h2>" + esc(s.nextTitle) + "</h2>";
  if (!state.selected.length) {
    h += '<div class="muted">' + esc(s.nextNone) + "</div>";
  } else {
    h += "<div>" + esc(s.nextAsk) + '</div><div class="prompt">' + esc(s.prompt) + "</div>" +
         '<div class="row"><button data-cmd="chat">' + esc(s.openChat) + '</button><button class="secondary" data-cmd="copy">' + esc(s.copy) + "</button></div>";
  }

  if (state.proposals.length) {
    h += "<h2>" + esc(s.proposalsTitle) + "</h2><ul>";
    for (const p of state.proposals.slice(0, 5)) {
      const ok = p.effect === "safe";
      h += "<li>" + esc(p.text) + ' <span class="badge ' + (ok ? "ok" : "warn") + '">' + esc(ok ? s.safe : s.needsOk) + "</span>" +
           (p.needs ? ' <span class="muted">(' + esc(s.needs) + ": " + esc(p.needs) + ")</span>" : "") + "</li>";
    }
    h += "</ul>";
  }

  if (state.history.length) {
    h += "<h2>" + esc(s.historyTitle) + '</h2><ul class="muted">' + state.history.slice(-8).reverse().map((x) => "<li>" + esc(x) + "</li>").join("") + "</ul>";
  }
  app.innerHTML = h;
}

document.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.decide) send({ type: "decide", id: b.dataset.decide, granted: true, scope: b.dataset.scope });
  else if (b.dataset.deny) send({ type: "decide", id: b.dataset.deny, granted: false });
  else if (b.dataset.cmd === "select") send({ type: "select", devices: [...document.querySelectorAll(".pick:checked")].map((c) => c.value) });
  else if (b.dataset.cmd) send({ type: b.dataset.cmd });
});
</script></body></html>`;
}
