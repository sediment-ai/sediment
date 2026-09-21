---
name: Bug report
about: Something behaves wrongly
labels: needs-triage
---

**What happened**

**What you expected**

**Failing stage** (installation, local startup, deployment, capture,
Derivation, export, or another stage)

**Minimal reproduction**

List the smallest setup and sanitized commands that reproduce the failure.

**Environment**
- Sediment version or commit:
- Python version:
- Operating system and version:
- Installation context: installed package / source checkout / other
- Deployment context: `sediment server` / Docker Compose / other
- Harness being captured (if capture-related): Claude Code / Codex / Cursor /
  Copilot / pi / other + version

**Safe diagnostics**

Don't attach raw transcripts, prompts, source archives, environment dumps,
tokens, webhook secrets, database URLs, or proprietary training rows. Include
only the sanitized error text and commands needed for the minimal reproduction.

How did you find Sediment? (optional — AI assistant / search / Reddit or HN / GitHub / a colleague / other)
