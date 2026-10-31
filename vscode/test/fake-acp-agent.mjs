#!/usr/bin/env node
// A minimal ACP agent made with the official SDK's agent side: for each prompt it asks permission
// for a benchscout tool and for a shell command, then says what it got.
import { Readable, Writable } from "node:stream";
import * as acp from "@agentclientprotocol/sdk";
const stream = acp.ndJsonStream(Writable.toWeb(process.stdout), Readable.toWeb(process.stdin));
const opts = [{ optionId: "allow_once", name: "Allow once", kind: "allow_once" }, { optionId: "reject_once", name: "Deny", kind: "reject_once" }];
async function ask(cx, sessionId, toolCallId, title, rawInput) {
  await cx.notify(acp.methods.client.session.update, { sessionId, update: { sessionUpdate: "tool_call", toolCallId, title, status: "pending", kind: "other" } });
  const r = await cx.request(acp.methods.client.session.requestPermission, { sessionId, toolCall: { toolCallId, title, rawInput }, options: opts });
  return r.outcome.outcome === "selected" ? r.outcome.optionId : "cancelled";
}
acp.agent({ name: "fake-acp-agent" })
  .onRequest("initialize", () => ({ protocolVersion: acp.PROTOCOL_VERSION, agentCapabilities: {} }))
  .onRequest("session/new", () => ({ sessionId: "s1" }))
  .onRequest("session/prompt", async (ctx) => {
    const id = ctx.params.sessionId, cx = ctx.client;
    const a = await ask(cx, id, "t1", "benchscout-bench_scan", {});
    const b = await ask(cx, id, "t2", "Run shell command", { command: "rm -rf build" });
    await cx.notify(acp.methods.client.session.update, { sessionId: id, update: { sessionUpdate: "agent_message_chunk", content: { type: "text", text: `bench_scan:${a} shell:${b}` } } });
    return { stopReason: "end_turn" };
  })
  .connect(stream);
