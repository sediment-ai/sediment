// SPDX-License-Identifier: MIT
// A real pi session with a scripted model; every evidence HTTP call stays real.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from "@earendil-works/pi-coding-agent";
import sedimentPi from "../index.ts";
import { failure, largeInteger, requirement } from "./evidence-fixture.ts";

type Reference = { inference_call_id: string; side: "input" | "output"; message_index: number; part_index: number };
export type KnownEvidence = { session_id: string; inference_call_id: string; unavailable: boolean; revision: number };
const names = ["sediment_list_context_sessions", "sediment_discover_context", "sediment_evidence_inventory", "sediment_evidence_manifest", "sediment_read_evidence"];

export async function exerciseEvidence(endpoint: string, token: string, known?: KnownEvidence): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), "sediment-native-evidence-"));
  const previous = Object.fromEntries(Object.entries(process.env).filter(([key]) => key.startsWith("SEDIMENT_")));
  for (const key of Object.keys(previous)) delete process.env[key];
  Object.assign(process.env, { SEDIMENT_RETRIEVAL_ENDPOINT: endpoint, SEDIMENT_RETRIEVAL_TOKEN: token, SEDIMENT_RETRIEVAL_DISCOVERY: "true" });
  let dispose = () => {};
  try {
    const settingsManager = SettingsManager.inMemory({ compaction: { enabled: false }, retry: { enabled: false } });
    const loader = new DefaultResourceLoader({ cwd: root, agentDir: root, settingsManager,
      noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
      extensionFactories: [sedimentPi],
    });
    await loader.reload();
    assert.deepEqual(loader.getExtensions().errors, []);
    const modelRuntime = await ModelRuntime.create({ authPath: join(root, "auth.json"), modelsPath: null,
      modelsStorePath: join(root, "models.json"), allowModelNetwork: false, refreshOnCreate: false });
    const { session } = await createAgentSession({ cwd: root, agentDir: root, settingsManager,
      resourceLoader: loader, sessionManager: SessionManager.inMemory(root), modelRuntime, tools: names,
      model: { id: "scripted", name: "scripted", api: "openai-completions", provider: "test",
        baseUrl: "https://no-inference.invalid", reasoning: false, input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 100_000, maxTokens: 1024 },
    });
    dispose = () => session.dispose();
    let turns = 0;
    let granted: string[] = [];
    let selected = known?.session_id;
    let call = known?.inference_call_id;
    let references: Reference[] = known ? [{ inference_call_id: known.inference_call_id, side: "output", message_index: 0, part_index: 2 }] : [];
    session.agent.streamFunction = ((_model: unknown, context: Parameters<typeof session.agent.streamFunction>[1]) => {
      turns++;
      assert.equal(context.tools?.length, names.length);
      const result = (id: string, denied?: string) => {
        const message = context.messages.find((message) => message.role === "toolResult" && message.toolCallId === id);
        assert.ok(message && message.role === "toolResult");
        assert.equal(message.isError, denied !== undefined);
        assert.equal(message.content.length, 1);
        const part = message.content[0]; assert.ok(part?.type === "text");
        if (denied) assert.ok(part.text.includes(`reason=${denied}`));
        else assert.ok(!part.text.includes("provider-raw-must-stay-private") && !part.text.includes("user-identity-must-stay-private"));
        return part.text;
      };
      const checkRead = (text: string) => {
        const value = JSON.parse(text);
        assert.equal(value.session_id, selected);
        assert.deepEqual(value.items.map((item: { reference: Reference }) => item.reference), references);
        assert.equal(value.quarantine_revision, known?.revision ?? 0);
        assert.match(text, /:\s*9007199254740993\s*[,}\]]/);
        assert.ok(text.includes(largeInteger), "the original integer numeral reaches the next model turn");
        assert.ok(text.includes("\\ud800") && text.includes("\\u0000"));
        if (!known) {
          assert.ok(text.includes(failure));
          assert.ok(value.items.some((item: { part: { type: string } }) => item.part.type === "reasoning"));
          assert.equal(value.items.filter((item: { part: { content?: string } }) => item.part.content === requirement).length, 2);
        }
      };
      if (known && turns === 2) {
        const text = result("read", known.unavailable ? "evidence_unavailable" : undefined);
        if (!known.unavailable) checkRead(text);
      } else if (!known) {
        if (turns === 2) {
          const value = JSON.parse(result("sessions"));
          granted = value.source_session_ids ?? [value.source_session_id];
          assert.equal(granted.length, 3);
        } else if (turns === 3) {
          const value = JSON.parse(result("discover"));
          assert.equal(value.items.length, 2);
          const candidates = new Set(value.items.map((item: { session_id: string }) => item.session_id));
          const omitted = granted.filter((id) => !candidates.has(id));
          assert.equal(omitted.length, 1, "the consumer chooses a granted Session omitted by keyword discovery");
          selected = omitted[0];
        } else if (turns === 4) {
          const value = JSON.parse(result("inventory"));
          assert.equal(value.calls.length, 1);
          call = value.calls[0].inference_call_id;
        } else if (turns === 5) {
          const value = JSON.parse(result("manifest"));
          const parts: { type: string; reference: Reference }[] = value.messages.flatMap((message: { parts: { type: string; reference: Reference }[] }) => message.parts);
          assert.equal(parts.length, 5);
          // Select exact references from the manifest, deliberately changing order.
          const tool = parts.find((part: { type: string }) => part.type === "tool_call_response");
          assert.ok(tool);
          references = [tool, ...parts.filter((part: unknown) => part !== tool)].map((part) => part.reference);
        } else if (turns === 6) checkRead(result("read"));
        else if (turns === 7) result("outside", "forbidden");
      }
      const finalTurn = known ? 2 : 7;
      assert.ok(turns <= finalTurn, "the scripted consumer does not retry");
      const read = { type: "toolCall", id: "read", name: names[4], arguments: { session_id: selected, references } };
      const calls = known ? [read] : [
        { type: "toolCall", id: "sessions", name: names[0], arguments: {} },
        { type: "toolCall", id: "discover", name: names[1], arguments: { query: "shipment replay constraint" } },
        { type: "toolCall", id: "inventory", name: names[2], arguments: { session_id: selected } },
        { type: "toolCall", id: "manifest", name: names[3], arguments: { session_id: selected, inference_call_id: call } },
        read,
        { type: "toolCall", id: "outside", name: names[4], arguments: { session_id: "outside-grant", references } },
      ];
      const message = { role: "assistant", api: "openai-completions", provider: "test", model: "scripted", timestamp: 1,
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
        stopReason: turns < finalTurn ? "toolUse" : "stop", content: turns < finalTurn ? [calls[turns - 1]] : [{ type: "text", text: "complete" }],
      };
      return { async *[Symbol.asyncIterator]() { yield { type: "start", partial: message }; yield { type: "done", reason: message.stopReason, message }; }, result: async () => message };
    }) as unknown as typeof session.agent.streamFunction;
    await session.agent.prompt("Inspect earlier requirements and failed attempts using authorized exact evidence.");
    assert.equal(turns, known ? 2 : 7);
    assert.equal(session.agent.state.messages.filter((message) => message.role === "toolResult").length, known ? 1 : 6);
  } finally {
    dispose();
    for (const key of Object.keys(process.env)) if (key.startsWith("SEDIMENT_")) delete process.env[key];
    Object.assign(process.env, previous);
    rmSync(root, { recursive: true, force: true });
  }
}
