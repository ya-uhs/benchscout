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
  const docListeners = {};
  const document = { getElementById: () => app, addEventListener: (type, f) => { docListeners[type] = f; } };
  const window = { addEventListener: (type, f) => { listeners[type] = f; } };
  const acquireVsCodeApi = () => ({ postMessage: (m) => posted.push(m) });
  new Function("document", "window", "acquireVsCodeApi", script)(document, window, acquireVsCodeApi);
  return {
    posted,
    /** Clicks an element carrying these data attributes (a button unless toggle is given), as the page's handler sees it. */
    click(dataset, { button = !("toggle" in dataset) } = {}) {
      const el = { dataset };
      el.closest = (sel) => (sel === "button" ? (button ? el : null)
        : sel.startsWith("[data-") && sel.slice(6, -1).replace(/-(.)/g, (_, c) => c.toUpperCase()) in dataset ? el : null);
      docListeners.click({ target: el });
      return app.innerHTML;
    },
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
  const out = w.render({ running: true, agent: "", strings: plainStrings("ja"), cards: [],
    devices: [{ id: "d1", label: "x", selected: false, watched: false, busy: false }] });
  assert.match(out, /エージェント未選択/);
  assert.match(out, /つながっている機器/);
});

test("language: the setting, else VS Code's language when it has one, else the OS language", async () => {
  const { pickLanguage, languageName } = require("../out/i18n.js");
  assert.equal(pickLanguage("en", "ja", "ja-JP"), "en", "the setting wins");
  assert.equal(pickLanguage("auto", "ja", "en-US"), "ja", "a Japanese VS Code");
  assert.equal(pickLanguage("auto", "en", "ja-JP"), "ja-JP", "an English VS Code (no language pack) on a Japanese OS");
  assert.equal(pickLanguage(undefined, "en-us", ""), "en");
  assert.equal(languageName("ja-JP"), "Japanese");
});

test("each device can be released for the person's own tools, or used again", () => {
  const w = webview();
  const out = w.render({ running: true, agent: "x", cards: [], strings: plainStrings("ja"), devices: [
    { id: "esp", label: "Espressif USB JTAG/serial debug unit — ESP32-S3", selected: true, watched: true, busy: false },
    { id: "fpga", label: "SIPEED JTAG Debugger", selected: false, watched: false, busy: false },
    { id: "other", label: "held elsewhere", selected: false, watched: false, busy: true },
  ] });
  assert.match(out, /ESP32-S3<\/span><span class="state muted">見張り中<\/span><button data-dev="esp" data-use="0">手放す<\/button>/);
  assert.match(out, /SIPEED JTAG Debugger<\/span><span class="state muted">使っていない<\/span><button data-dev="fpga" data-use="1">使う<\/button>/);
  assert.match(out, /held elsewhere<\/span><span class="state muted">別のウィンドウで使用中<\/span><\/div>/, "no button for a device another window holds");
  assert.ok(out.indexOf("つながっている機器") < out.indexOf("自分のツール"));
});

test("while everything is allowed, the inbox says so at the top with a way to turn it off", () => {
  const w = webview();
  const on = w.render({ running: true, agent: "x", cards: [], devices: [], allowAll: true, strings: plainStrings("ja") });
  assert.match(on, /<div class="allowall"><span>すべて許可中:[^<]*元に戻せない操作は毎回確認します。<\/span><button data-cmd="allowAllOff">解除<\/button><\/div>/);
  const off = w.render({ running: true, agent: "x", cards: [], devices: [], allowAll: false, strings: plainStrings("ja") });
  assert.ok(!off.includes("allowall"));
});

test("what is remembered is listed per device, folded at first; each entry can be forgotten or confirmed, and its file opened", () => {
  const w = webview();
  const memory = [{ device: "d1", name: "QinHeng USB Serial", key: "1a86-7523-x", file: "/home/u/.config/benchscout/memory/devices/1a86-7523-x.toml",
    entries: [{ id: "m1", kind: "observation", text: "prints sensor ok", source: "agent", time: "2026-11-02T10:00", status: "active" },
              { id: "m2", kind: "safety", text: "listening does not reset it", source: "benchscout", time: "2026-11-02T10:05", status: "pending" }] }];
  const s = plainStrings("ja");
  const folded = w.render({ running: true, agent: "x", cards: [], devices: [], memory, strings: s });
  assert.ok(folded.includes(`${s.memoryTitle} (2)`) && !folded.includes("prints sensor ok"), "folded until opened");
  const open = w.click({ toggle: "mem" });
  assert.ok(open.includes("prints sensor ok") && open.includes(s.memoryKinds.observation) && open.includes(s.memoryPending));
  w.click({ memforget: "1", key: "1a86-7523-x", id: "m1" });
  w.click({ memdecide: "1", key: "1a86-7523-x", id: "m2" });
  w.click({ memfile: memory[0].file });
  assert.deepEqual(w.posted.filter((m) => m.type.startsWith("memory")), [
    { type: "memoryForget", key: "1a86-7523-x", id: "m1" },
    { type: "memoryDecide", key: "1a86-7523-x", id: "m2", accept: true },
    { type: "memoryOpen", file: memory[0].file },
  ]);
});
