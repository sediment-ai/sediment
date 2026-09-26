// SPDX-License-Identifier: MIT

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import test from "node:test";

const installedBin = process.env.SEDIMENT_PI_TEST_INSTALLED_BIN;

test("pi loads the wheel's registered extension outside the checkout", {
  skip: !installedBin ? "set SEDIMENT_PI_TEST_INSTALLED_BIN for installed wheel acceptance" : false,
}, (t) => {
  const root = mkdtempSync(join(tmpdir(), "sediment-pi-installed-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const agentDir = join(root, ".pi", "agent");
  mkdirSync(agentDir, { recursive: true });
  const env = { HOME: root, PATH: installedBin + delimiter + process.env.PATH, GIT_CONFIG_NOSYSTEM: "1" };
  execFileSync("git", ["init", "-q", root], { env });
  execFileSync(join(installedBin!, "sediment"), ["install", root, "--no-env"], { cwd: root, env });
  const { extensions } = JSON.parse(readFileSync(join(agentDir, "settings.json"), "utf8"));
  assert.equal(extensions.length, 1);
  assert.ok(extensions[0].includes("site-packages/sediment_cli/_pi"));
  assert.equal(existsSync(join(extensions[0], "node_modules")), false);

  // Only pi itself comes from the test checkout. Its native loader reads the
  // installed user's settings and imports the wheel's extension directory.
  const piModule = new URL("../node_modules/@earendil-works/pi-coding-agent/dist/index.js", import.meta.url).href;
  const output = execFileSync(process.execPath, ["--input-type=module", "-e", `
    import { DefaultResourceLoader, SettingsManager } from ${JSON.stringify(piModule)};
    const cwd = process.cwd();
    const agentDir = ${JSON.stringify(agentDir)};
    const loader = new DefaultResourceLoader({ cwd, agentDir,
      settingsManager: SettingsManager.create(cwd, agentDir),
      noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    });
    await loader.reload();
    const { extensions, errors } = loader.getExtensions();
    if (errors.length || extensions.length !== 1) throw new Error(JSON.stringify(errors));
    const ctx = { cwd, model: { provider: "sediment", api: "anthropic-messages" },
      sessionManager: { getSessionId: () => "wheel-session" },
    };
    const event = { headers: {} };
    for (const handler of extensions[0].handlers.get("before_provider_headers")) await handler(event, ctx);
    const tool = { toolName: "write", toolCallId: "wheel-call", isError: false,
      args: { path: "app.py", content: "example" },
    };
    for (const name of ["tool_execution_start", "tool_execution_end"]) {
      for (const handler of extensions[0].handlers.get(name)) await handler(tool, ctx);
    }
    console.log(JSON.stringify(event.headers));
  `], { cwd: root, env, encoding: "utf8", timeout: 30_000 });
  assert.deepEqual(JSON.parse(output), { "x-sediment-session": "wheel-session" });
  const marker = JSON.parse(readFileSync(join(root, ".git", "sediment-sessions"), "utf8"));
  assert.equal(marker.session_id, "wheel-session");
  assert.equal(marker.tool, "pi");
});
