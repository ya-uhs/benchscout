// The inbox webview's own script: it lives inside a template string in panel.ts, so escaping
// mistakes only show at run time. Run it against a tiny fake DOM and check what it renders.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const { html } = require("../out/panel.js");
const { strings } = require("../out/i18n.js");

function plainStrings(lang) {
  return Object.fromEntries(Object.entries(strings(lang)).filter(([, v]) => typeof v !== "function"));
}

/** Runs the webview script with a fake document; returns render(state) -> innerHTML and the posted messages. */
function webview() {
  const page = html("NONCE", "csp");
  const script = page.match(/<script nonce="NONCE">([\s\S]*)<\/script>/)[1];
  const app = { innerHTML: "" };
  const posted = [];
  const listeners = {};
  const document = { getElementById: () => app, addEventListener: () => {} };
  const window = { addEventListener: (type, f) => { listeners[type] = f; } };
  const acquireVsCodeApi = () => ({ postMessage: (m) => posted.push(m) });
  new Function("document", "window", "acquireVsCodeApi", script)(document, window, acquireVsCodeApi);
  return {
    posted,
    render(state) {
      listeners.message({ data: { type: "state", state } });
      return app.innerHTML;
    },
  };
}

const card = (over) => ({ id: "c1", kind: "report", time: Date.now(), title: "t", body: "", options: [], freeText: false,
  status: "open", progress: [], ...over });

test("the webview script runs and says it is ready", () => {
  const w = webview();
  assert.deepEqual(w.posted, [{ type: "ready" }]);
});

test("agent Markdown is rendered, and nothing it writes becomes HTML", () => {
  const w = webview();
  const out = w.render({ running: true, agent: "GitHub Copilot CLI", devices: [], strings: plainStrings("en"), cards: [card({
    body: "- **Found:** `303a:1001` on the bus\n- second <img src=x onerror=alert(1)>\n\n## Next\n1. listen at 115200",
  })] });
  assert.match(out, /<ul><li><b>Found:<\/b> <code>303a:1001<\/code> on the bus<\/li><li>second &lt;img/);
  assert.doesNotMatch(out, /<img/);
  assert.match(out, /<h4>Next<\/h4><ul><li>listen at 115200<\/li><\/ul>/);
});

test("a pinned card is shown first, and the status says when no agent is chosen", () => {
  const w = webview();
  const out = w.render({ running: true, agent: "", devices: [], strings: plainStrings("en"), cards: [
    card({ id: "a", kind: "question", title: "Which agent should benchscout wake?", pin: true, options: [{ id: "copilot", label: "Copilot" }] }),
    card({ id: "b", kind: "question", title: "What should we look at?", options: [{ id: "x", label: "Look at x" }] }),
  ] });
  assert.ok(out.indexOf("Which agent") < out.indexOf("What should we look at"), "pinned first, though older");
  assert.match(out, /no agent chosen yet/);
  assert.match(out, /class="opt" data-card="a" data-opt="copilot"/);
});

test("decisions come before reports, and remembered things come last", () => {
  const w = webview();
  const t = Date.now();
  const out = w.render({ running: true, agent: "x", devices: [], strings: plainStrings("en"), cards: [
    card({ id: "r", kind: "report", title: "REPORT", time: t }),
    card({ id: "m", kind: "memory", title: "MEMORY", time: t + 1, options: [{ id: "forget", label: "Forget" }] }),
    card({ id: "q", kind: "confirm", effect: "disruptive", title: "CONFIRM", time: t - 5, options: [{ id: "once", label: "Allow once", primary: true }] }),
  ] });
  const at = (x) => out.indexOf(x);
  assert.ok(at("CONFIRM") < at("REPORT") && at("REPORT") < at("MEMORY"), out);
  assert.match(out, /class="badge">Changes device state</);
  assert.match(out, /class="card memory  record"/);
});

test("the Japanese inbox renders with its own words", () => {
  const w = webview();
  const out = w.render({ running: true, agent: "", devices: ["x"], strings: plainStrings("ja"), cards: [] });
  assert.match(out, /エージェント未選択/);
  assert.match(out, /つながっている機器/);
});
