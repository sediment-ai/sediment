# sediment fixtures

This synthetic fixture records the **golden contract shape** defined in
`docs/agents/capture-clients.md`. Vendor fixture dirs hold frozen real
captures; this dir holds the shape every harness shim must be able to
produce, so the conformance test (`test_sediment_decision_conformance_fixture`)
pins the two ends of the contract together.

- `tool_decision.json` — one batch, three `sediment.tool_decision`
  records: a `write` accept (implicit), an `edit` accept (implicit), and
  an `edit` reject (explicit, `file_path=""` — the reject convention).
  `user.id` rides the resource scope. Changing this file is a contract
  change: update the contract doc and every shim in the same PR.
