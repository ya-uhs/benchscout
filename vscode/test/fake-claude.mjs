#!/usr/bin/env node
// Stands in for `claude -p --input-format stream-json --output-format stream-json`: for each user
// message it asks permission for a benchscout tool and for a shell command, then says what it got.
import * as readline from "node:readline";
const out = (m) => process.stdout.write(JSON.stringify(m) + "\n");
const waiting = new Map();
let n = 0;
const ask = (tool, input) => new Promise((resolve) => {
  const id = `req${++n}`;
  waiting.set(id, resolve);
  out({ type: "control_request", request_id: id, request: { subtype: "can_use_tool", tool_name: tool, input, description: `use ${tool}` } });
});
out({ type: "system", subtype: "init" });
readline.createInterface({ input: process.stdin }).on("line", async (line) => {
  const m = JSON.parse(line);
  if (m.type === "control_response") return waiting.get(m.response.request_id)?.(m.response.response.behavior);
  if (m.type !== "user") return;
  const a = await ask("mcp__benchscout__bench_scan", {});
  const b = await ask("Bash", { command: "rm -rf build" });
  out({ type: "assistant", message: { content: [{ type: "tool_use", id: "t1", name: "mcp__benchscout__bench_scan" }, { type: "text", text: `bench_scan:${a} shell:${b}` }] } });
  out({ type: "result", subtype: "success", is_error: false, result: `bench_scan:${a} shell:${b}` });
});
