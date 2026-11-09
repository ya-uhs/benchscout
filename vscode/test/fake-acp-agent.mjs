#!/usr/bin/env node
// Stands in for an ACP agent with what GitHub Copilot CLI sent in one real session (fixtures/real/
// copilot_acp_2026-11-08.jsonl; audit 2026-11-07, T27: it had been made up), sent through the official SDK's agent side,
// which checks every message against the protocol. For each prompt it sends the recorded updates and permission asks in
// order, waiting for each answer, and writes the answers it got to FAKE_ACP_LOG (one JSON line each) for the test.
// What came before the prompt (the command list) is left out: the client reads nothing from it.
import { appendFileSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { Readable, Writable } from "node:stream";
import * as acp from "@agentclientprotocol/sdk";

const recorded = readFileSync(join(import.meta.dirname, "..", "..", "fixtures", "real", "copilot_acp_2026-11-08.jsonl"), "utf8")
  .split("\n").filter(Boolean).map((l) => JSON.parse(l)).filter((l) => l.dir).map((l) => ({ dir: l.dir, ...l.msg }));
const reply = (method) => {  // the recorded result of the request the client sent with this method
  const id = recorded.find((m) => m.dir === "in" && m.method === method).id;
  return recorded.find((m) => m.dir === "out" && m.id === id && "result" in m).result;
};
const promptId = recorded.find((m) => m.dir === "in" && m.method === "session/prompt").id;
const turn = recorded.slice(recorded.findIndex((m) => m.dir === "in" && m.method === "session/prompt") + 1)
  .filter((m) => m.dir === "out" && !(m.id === promptId && "result" in m));

const stream = acp.ndJsonStream(Writable.toWeb(process.stdout), Readable.toWeb(process.stdin));
acp.agent({ name: "fake-acp-agent" })
  .onRequest("initialize", () => reply("initialize"))
  .onRequest("session/new", () => reply("session/new"))
  .onRequest("session/prompt", async (ctx) => {
    if (process.env.FAKE_ACP_LOG) {  // what it was asked, for the test (benchscout's follow-up after a denial)
      appendFileSync(process.env.FAKE_ACP_LOG, JSON.stringify({ prompt: ctx.params.prompt.map((b) => b.text ?? "").join("") }) + "\n");
    }
    for (const m of turn) {
      if (m.method === "session/update") {
        await ctx.client.notify(acp.methods.client.session.update, m.params);
      } else if (m.method === "session/request_permission") {
        const got = await ctx.client.request(acp.methods.client.session.requestPermission, m.params);
        if (process.env.FAKE_ACP_LOG) appendFileSync(process.env.FAKE_ACP_LOG, JSON.stringify({ tool: m.params.toolCall.title, response: got }) + "\n");
      }
    }
    return reply("session/prompt");
  })
  .connect(stream);
