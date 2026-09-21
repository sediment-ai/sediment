// SPDX-License-Identifier: MIT
// Real pinned pi registration, argument validation, agent loop, and conversion
// into the next model request. Only the model's stream is scripted; no inference.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager,
} from "@earendil-works/pi-coding-agent";
import sedimentPi from "../index.ts";

const text = '{"schema_version":1,"policy_version":1,"source_session_id":"source","quarantine_revision":0,"status":"matched","capture_completeness":"unknown","coverage":{"visible_inference_calls":1,"quarantined_inference_calls":0,"scanned_parts":1,"complete_visible_scan":true},"skipped":{"reasoning_part":0,"non_finite_number":0,"no_match":0,"repeated_content":0,"item_limit":0,"response_budget":0},"items":[{"score":1,"evidence":{"reference":{"inference_call_id":"call","side":"output","message_index":0,"part_index":0},"observed_at":"2026-09-21T12:00:00+00:00","role":"tool","finish_reason":null,"part":{"type":"tool_call_response","id":"tool","result":{"integer":9007199254740993,"text":"\\ud800\\u0000"}}}}]}';

test("pi 0.84.1 routes a native retrieval call into the next model request losslessly", async () => {
  const root = mkdtempSync(join(tmpdir(), "sediment-native-retrieval-"));
  const originalFetch = globalThis.fetch;
  const previous = Object.fromEntries(Object.entries(process.env).filter(([key]) => key.startsWith("SEDIMENT_")));
  for (const key of Object.keys(previous)) delete process.env[key];
  process.env.SEDIMENT_RETRIEVAL_ENDPOINT = "https://retrieval.example.com";
  process.env.SEDIMENT_RETRIEVAL_TOKEN = "native-tool-test-token";
  let requests = 0;
  globalThis.fetch = async (url, init) => {
    assert.equal(url, "https://retrieval.example.com/query/context");
    assert.equal(init?.redirect, "error");
    requests++;
    return new Response(text, { headers: { "Content-Type": "application/json" } });
  };
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
      resourceLoader: loader, sessionManager: SessionManager.inMemory(root), modelRuntime,
      tools: ["sediment_retrieve_context"],
      model: { id: "scripted", name: "scripted", api: "openai-completions", provider: "test",
        baseUrl: "https://no-inference.invalid", reasoning: false, input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 100_000, maxTokens: 1024 },
    });
    dispose = () => session.dispose();
    let turns = 0;
    let observedResult: unknown;
    let rejectedResult: unknown;
    session.agent.streamFunction = ((_model: unknown, context: Parameters<typeof session.agent.streamFunction>[1]) => {
      turns++;
      assert.equal(context.tools?.length, 1);
      if (turns === 2) {
        observedResult = context.messages.find((message) => message.role === "toolResult" && message.toolCallId === "native-call");
        rejectedResult = context.messages.find((message) => message.role === "toolResult" && message.toolCallId === "native-invalid");
      }
      const message = {
        role: "assistant", api: "openai-completions", provider: "test", model: "scripted",
        timestamp: 1, usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
        stopReason: turns === 1 ? "toolUse" : "stop",
        content: turns === 1 ? [
          { type: "toolCall", id: "native-call", name: "sediment_retrieve_context", arguments: { query: "constraint failure" } },
          { type: "toolCall", id: "native-invalid", name: "sediment_retrieve_context", arguments: { query: "failure", session_id: "unauthorized-source" } },
        ]
          : [{ type: "text", text: "complete" }],
      };
      return {
        async *[Symbol.asyncIterator]() { yield { type: "start", partial: message }; yield { type: "done", reason: message.stopReason, message }; },
        result: async () => message,
      };
    }) as unknown as typeof session.agent.streamFunction;
    await session.agent.prompt("Continue the earlier task.");
    assert.equal(turns, 2);
    assert.equal(requests, 1);
    assert.ok(observedResult && typeof observedResult === "object");
    const result = observedResult as { content: unknown; isError: boolean; details?: unknown };
    assert.deepEqual(result.content, [{ type: "text", text }]);
    assert.equal(result.isError, false);
    assert.ok(result.details === undefined || Object.keys(result.details as object).length === 0);
    assert.equal((rejectedResult as { isError: boolean }).isError, true);
    assert.equal(session.agent.state.messages.filter((m) => m.role === "toolResult").length, 2);
  } finally {
    dispose();
    globalThis.fetch = originalFetch;
    for (const key of Object.keys(process.env)) if (key.startsWith("SEDIMENT_")) delete process.env[key];
    Object.assign(process.env, previous);
    rmSync(root, { recursive: true, force: true });
  }
});
