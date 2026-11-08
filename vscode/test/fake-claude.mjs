#!/usr/bin/env node
// Stands in for `claude -p --input-format stream-json --output-format stream-json --permission-prompt-tool stdio` with
// what the real one printed (fixtures/real/claude_p_stream_2026-11-08.jsonl; audit 2026-11-07, T21: it had been made
// up). For each user message it prints the recorded session's lines in order; at each recorded permission ask it waits
// for the answer, and writes the answer it got to FAKE_CLAUDE_LOG (one JSON line each) for the test to compare.
// FAKE_QUIET: no permission asks, one benchscout tool call, then it says the first line it was asked; built from the
// recorded messages, so every message still has the real fields.
import { appendFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import * as readline from "node:readline";

export const RECORDING = join(import.meta.dirname, "..", "..", "fixtures", "real", "claude_p_stream_2026-11-08.jsonl");
const recorded = readFileSync(RECORDING, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)).filter((l) => l.dir);
const printed = recorded.filter((l) => l.dir === "out").map((l) => l.msg);
const out = (m) => process.stdout.write(JSON.stringify(m) + "\n");
const waiting = new Map();
const answered = (id) => new Promise((resolve) => waiting.set(id, resolve));

// the recorded messages a quiet turn is made of: the benchscout tool call, its result, the text, the result line
const call = printed.find((m) => m.type === "assistant" && m.message.content.some((b) => b.name === "mcp__benchscout__bench_scan"));
const callId = call.message.content[0].id;
const result = printed.find((m) => m.type === "user" && m.message.content.some((b) => b.tool_use_id === callId));
const text = printed.findLast((m) => m.type === "assistant" && m.message.content.some((b) => b.type === "text"));
const end = printed.find((m) => m.type === "result");
let n = 0;

async function replay() {
  for (const m of printed) {
    if (m.type === "system") continue;  // once, at start
    out(m);
    if (m.type === "control_request") {
      const got = await answered(m.request_id);
      if (process.env.FAKE_CLAUDE_LOG) {
        appendFileSync(process.env.FAKE_CLAUDE_LOG, JSON.stringify({ tool: m.request.tool_name, response: got }) + "\n");
      }
    }
  }
}

function quiet(first) {
  const id = `toolu_quiet_${++n}`;
  out({ ...call, message: { ...call.message, content: [{ ...call.message.content[0], id }] } });
  out({ ...result, message: { ...result.message, content: [{ ...result.message.content[0], tool_use_id: id }] } });
  out({ ...text, message: { ...text.message, content: [{ type: "text", text: `got: ${first}` }] } });
  out({ ...end, result: `got: ${first}`, permission_denials: [] });
}

out(printed.find((m) => m.type === "system"));
let turn = Promise.resolve();
readline.createInterface({ input: process.stdin }).on("line", (line) => {
  const m = JSON.parse(line);
  if (m.type === "control_response") return waiting.get(m.response.request_id)?.(m.response.response);
  if (m.type !== "user") return;
  turn = turn.then(() => (process.env.FAKE_QUIET ? quiet(String(m.message.content).split("\n")[0]) : replay()));
});
