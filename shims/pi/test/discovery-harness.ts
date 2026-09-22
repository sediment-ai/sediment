// SPDX-License-Identifier: MIT
// Real pinned pi loop; the model is scripted, while HTTP remains caller-owned.
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from "@earendil-works/pi-coding-agent";
import sedimentPi from "../index.ts";

export async function exerciseDiscovery(endpoint: string, token: string): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), "sediment-native-discovery-"));
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
      resourceLoader: loader, sessionManager: SessionManager.inMemory(root), modelRuntime,
      tools: ["sediment_discover_context", "sediment_retrieve_context"],
      model: { id: "scripted", name: "scripted", api: "openai-completions", provider: "test",
        baseUrl: "https://no-inference.invalid", reasoning: false, input: ["text"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 100_000, maxTokens: 1024 },
    });
    dispose = () => session.dispose();
    let turns = 0;
    let selectedId: string | undefined;
    session.agent.streamFunction = ((_model: unknown, context: Parameters<typeof session.agent.streamFunction>[1]) => {
      turns++;
      assert.equal(context.tools?.length, 2);
      const result = (id: string) => {
        const message = context.messages.find((message) => message.role === "toolResult" && message.toolCallId === id);
        assert.ok(message && message.role === "toolResult");
        return message;
      };
      if (turns === 2) {
        const discovered = result("discover");
        assert.equal(discovered.isError, false);
        assert.equal(discovered.content.length, 1);
        const part = discovered.content[0]; assert.ok(part?.type === "text");
        const candidates = JSON.parse(part.text).items as { session_id: string; preview: unknown }[];
        assert.ok(candidates.length >= 2, "acceptance requires multiple candidates");
        selectedId = candidates.find((candidate) => JSON.stringify(candidate.preview).includes("Exclude replay shipments."))?.session_id;
        assert.ok(selectedId, "the source must be selected from a returned exact preview");
      }
      if (turns === 3) {
        const selected = result("selected");
        assert.equal(selected.isError, false);
        assert.equal(selected.content.length, 1);
        const part = selected.content[0]; assert.ok(part?.type === "text");
        assert.ok(part.text.includes("Exclude replay shipments."));
        assert.match(part.text, /:\s*9007199254740993\s*[,}\]]/);
        assert.equal(JSON.parse(part.text).source_session_id, selectedId);
      }
      if (turns === 4) {
        const denied = result("outside");
        assert.equal(denied.isError, true);
        assert.ok(denied.content.some((part) => part.type === "text" && part.text.includes("reason=forbidden")));
      }
      assert.ok(turns <= 4, "scripted acceptance must not retry");
      const query = "shipment replay constraint";
      const calls = [
        { type: "toolCall", id: "discover", name: "sediment_discover_context", arguments: { query } },
        { type: "toolCall", id: "selected", name: "sediment_retrieve_context", arguments: { query, session_id: selectedId } },
        { type: "toolCall", id: "outside", name: "sediment_retrieve_context", arguments: { query, session_id: "outside-grant" } },
      ];
      const message = { role: "assistant", api: "openai-completions", provider: "test", model: "scripted", timestamp: 1,
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
        stopReason: turns < 4 ? "toolUse" : "stop", content: turns < 4 ? [calls[turns - 1]] : [{ type: "text", text: "complete" }],
      };
      return { async *[Symbol.asyncIterator]() { yield { type: "start", partial: message }; yield { type: "done", reason: message.stopReason, message }; }, result: async () => message };
    }) as unknown as typeof session.agent.streamFunction;
    await session.agent.prompt("Find the earlier shipment task context.");
    assert.equal(turns, 4);
    assert.equal(session.agent.state.messages.filter((message) => message.role === "toolResult").length, 3);
  } finally {
    dispose();
    for (const key of Object.keys(process.env)) if (key.startsWith("SEDIMENT_")) delete process.env[key];
    Object.assign(process.env, previous);
    rmSync(root, { recursive: true, force: true });
  }
}
