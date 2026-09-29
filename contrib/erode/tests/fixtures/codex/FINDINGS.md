# Codex Responses API recording findings

Codex resends the complete captured history with `store: false` in all 21
requests. No request contains `previous_response_id`. Stage A.2 has historical
tool output available to prune, but these recordings use custom `exec` calls
that wrap JavaScript tool invocations. They don't use the spec's direct shell
or patch call shapes.

## Recording scope

- Date: `2026-09-28`.
- Recorded client: npm package `@openai/codex`, `codex --version` output
  `codex-cli 0.158.0`.
- Python: `3.12.14`; pytest: `9.1.1`; erode: `0.1.0`.
- Base: `claude/gateway-context-pruning`, commit
  `24f167b16d8350f30bf3586bfa9b56dac9dafabc` (PR #188).
- Specification: stage A.2, including “Before building,” at commit `0cdfc6b`
  on `claude/laughing-fermi-xtf9bd`.
- Branch: `codex/erode-codex-recordings`; related tracker: #134.
- Upstream: `https://api.openai.com`; recorder: `127.0.0.1:8787`.
- Model: `gpt-6-astra`, selected by Codex without a model override.
  Requests contain `reasoning: {"effort":"low","context":"all_turns"}`.
  No history, storage, compaction, reasoning, or tool-mode override was supplied.
- Authentication for the three recordings: explicit provider with
  `env_key = "OPENAI_API_KEY"`. The credential check returned HTTP 200.
- User configuration and execution rules were excluded with
  `--ignore-user-config --ignore-rules`. Codex still included its built-in
  instructions, installed skill catalog, and tool definitions. These are
  environment-specific observations of this version, not a claim about every
  Codex version or configuration.

The recorder is the supplied `record.py` wrapper, unchanged, outside the
repository. It calls `make_server(..., prune=False)` and saves POST bodies
before forwarding them. It records no headers or response stream. Every file
here is a real recorded request body. Error outputs within the Sessions remain
in the fixtures.

| Session | Task | Request files | Input items by request | Suite runs | Final suite result |
| --- | --- | --- | --- | --- | --- |
| 1 | Fix three baseline defects | `001`–`007` | 7, 10, 12, 16, 18, 20, 22 | 5 | 4 passed |
| 2 | Rename a function, add an integration test, and fix baseline defects | `001`–`007` | 7, 10, 12, 16, 18, 20, 22 | 8 | 5 passed |
| 3 | Add stock validation and tests, then fix baseline defects | `001`–`007` | 7, 10, 12, 16, 18, 20, 22 | 5 | 26 passed |

Each filename ends in `_v1_responses.json`. Final test results appear in
[Session 1 request 007](session-1/007_v1_responses.json),
[Session 2 request 007](session-2/007_v1_responses.json), and
[Session 3 request 007](session-3/007_v1_responses.json). All three Codex
processes exited with status 0. The Session 2 rename temporarily caused import
errors; those results remain recorded.

## Setup and exact commands

The npm client was installed in the temporary directory to preserve the
app-bundled executable. From the recording checkout, setup ran:

```sh
uv venv --python 3.12 --seed /tmp/erode-codex-recording/venv
/tmp/erode-codex-recording/venv/bin/python -m pip install ./contrib/erode pytest
npm install --prefix /tmp/erode-codex-recording/cli @openai/codex
/tmp/erode-codex-recording/cli/node_modules/.bin/codex --version
```

The synthetic git repository is `/tmp/erode-codex-recording/demo`. Its baseline
contains `README.md`, `parcel/{__init__,billing,labels,stock,summary}.py`, and
`tests/test_parcels.py`. The baseline suite has three failing tests and one
passing test. Billing adds instead of multiplying; labels uppercase instead of
lowercasing; stock can become negative. File reads and patches in the fixtures
preserve that synthetic content.

The private runner obtains the credential through a hidden input prompt. It
passes these environment values to Codex, with no credential literal in a
command or file:

```sh
PATH="/tmp/erode-codex-recording/venv/bin:$PATH"
OPENAI_BASE_URL=http://127.0.0.1:8787/v1
# OPENAI_API_KEY contains the supplied credential.
# CODEX_API_KEY contains the same credential; the explicit provider reads OPENAI_API_KEY.
```

Before each Session, the runner invokes `git checkout .` in the synthetic
repository. Each Session starts from the same tracked baseline. The runner
starts one recorder at a time with these exact argument lists:

```sh
/tmp/erode-codex-recording/venv/bin/python /tmp/erode-codex-recording/record.py https://api.openai.com /tmp/erode-codex-recording/raw/session-1
/tmp/erode-codex-recording/venv/bin/python /tmp/erode-codex-recording/record.py https://api.openai.com /tmp/erode-codex-recording/raw/session-2
/tmp/erode-codex-recording/venv/bin/python /tmp/erode-codex-recording/record.py https://api.openai.com /tmp/erode-codex-recording/raw/session-3
```

The runner invokes the following exact Codex argument lists. The shell input
redirections show the files that the runner supplies on standard input. Standard
output and standard error go to private per-Session logs. The runner stops each
recorder after its Codex process exits.

```sh
/tmp/erode-codex-recording/cli/node_modules/.bin/codex -a never exec --ignore-user-config --ignore-rules -c 'model_provider="erode-recording"' -c 'model_providers.erode-recording={name="erode recorder",base_url="http://127.0.0.1:8787/v1",wire_api="responses",env_key="OPENAI_API_KEY"}' -s workspace-write -C /tmp/erode-codex-recording/demo - < /tmp/erode-codex-recording/prompt-1.txt
/tmp/erode-codex-recording/cli/node_modules/.bin/codex -a never exec --ignore-user-config --ignore-rules -c 'model_provider="erode-recording"' -c 'model_providers.erode-recording={name="erode recorder",base_url="http://127.0.0.1:8787/v1",wire_api="responses",env_key="OPENAI_API_KEY"}' -s workspace-write -C /tmp/erode-codex-recording/demo - < /tmp/erode-codex-recording/prompt-2.txt
/tmp/erode-codex-recording/cli/node_modules/.bin/codex -a never exec --ignore-user-config --ignore-rules -c 'model_provider="erode-recording"' -c 'model_providers.erode-recording={name="erode recorder",base_url="http://127.0.0.1:8787/v1",wire_api="responses",env_key="OPENAI_API_KEY"}' -s workspace-write -C /tmp/erode-codex-recording/demo - < /tmp/erode-codex-recording/prompt-3.txt
```

The explicit model provider works. Its configuration is equivalent to:

```toml
[model_providers.erode-recording]
name = "erode recorder"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
```

Decision: supply that provider with per-command `-c` overrides. This selects the
requested provider without editing the shared `~/.codex/config.toml`.

The exact prompt file contents follow. Each file ends with one newline.

### Session 1 prompt

```text
This repository is a synthetic parcel exercise. Stay inside this repository. Do not access credentials, environment variables, user configuration, network services, or other repositories. Read README.md with cat, parcel/billing.py with sed -n, and parcel/labels.py with nl -ba, each as a separate shell command; also inspect stock.py, summary.py, and tests/test_parcels.py. Run python -m pytest -q before editing. Use apply_patch for edits. Fix one module at a time and rerun the entire suite after each module change, even while tests still fail. Make all tests pass according to README.md. Finish by rerunning the entire suite.
```

### Session 2 prompt

```text
This repository is a synthetic parcel exercise. Stay inside this repository. Do not access credentials, environment variables, user configuration, network services, or other repositories. Read README.md with cat, parcel/billing.py with sed -n, and parcel/labels.py with nl -ba, each as a separate shell command; also inspect stock.py, summary.py, and tests/test_parcels.py. Run python -m pytest -q before editing. Use apply_patch for edits. Fix one module at a time and rerun the entire suite after each module change, even while tests still fail. Rename clean_label to normalize_label across the package and tests. Add a test covering describe_parcel with label Cedar, quantity 3, price 7, on-hand 2, and reserved 5; expect cedar, 21, and 0. Fix the baseline defects as well. Rerun tests after the rename, after adding the integration test, and after each fix. Finish with a passing suite.
```

### Session 3 prompt

```text
This repository is a synthetic parcel exercise. Stay inside this repository. Do not access credentials, environment variables, user configuration, network services, or other repositories. Read README.md with cat, parcel/billing.py with sed -n, and parcel/labels.py with nl -ba, each as a separate shell command; also inspect stock.py, summary.py, and tests/test_parcels.py. Run python -m pytest -q before editing. Use apply_patch for edits. Fix one module at a time and rerun the entire suite after each module change, even while tests still fail. Add input validation to parcel/stock.py: on_hand and reserved must each be integers but not bools; reject other types with TypeError, and negative values with ValueError. Add tests for these rules in tests/test_parcels.py before changing stock.py and run them to observe failure. Fix the baseline defects as well. Rerun the suite after each module change and finish with a passing suite.
```

## 1. Full history or server-side history?

Every recorded request has `store: false`, `stream: true`, and an `input` list.
None has a `previous_response_id` key. Within each Session, each request's
`input` begins with an exact copy of the preceding request's entire `input`.
The counts grow from 7 to 22 items; the later requests append model messages,
reasoning, calls, and results.

Compare [Session 1 request 001](session-1/001_v1_responses.json) with
[request 007](session-1/007_v1_responses.json). The same prefix check passes
for all adjacent requests in Sessions 2 and 3. These are Codex's observed
history defaults with the provider and sandbox settings listed earlier. No
storage or history setting was changed to produce full-history requests.

## 2. Input item types and roles

All three final requests have the same item-type counts:

| Type | Explicit role | Count per final request |
| --- | --- | --- |
| `additional_tools` | `developer` | 1 |
| `message` | `developer` | 4 |
| `message` | `user` | 2 |
| `message` | `assistant` | 2 |
| `reasoning` | Absent | 1 |
| `custom_tool_call` | Absent | 6 |
| `custom_tool_call_output` | Absent | 6 |

Evidence: [Session 1 request 007](session-1/007_v1_responses.json),
[Session 2 request 007](session-2/007_v1_responses.json), and
[Session 3 request 007](session-3/007_v1_responses.json).

No recorded request contains a top-level `input` item of type `function_call`,
`function_call_output`, `local_shell_call`, or `local_shell_call_output`.
`input_text` and `output_text` occur inside content arrays; they aren't
additional top-level input item types. Assistant messages carry
`phase: "commentary"`. The final assistant answer follows the last recorded
request, so these request fixtures don't contain that final answer.

The first item, `additional_tools`, holds tool namespaces and schemas in its
`tools` array. The request has no top-level `tools` or `instructions` field.
Instruction text occurs in developer messages. Reasoning items have an empty
`summary` array and an `encrypted_content` string; the opaque string is redacted.

Custom calls carry `name: "exec"`, `input` containing JavaScript, a `call_id`,
and `status: "completed"` in the resent history. Their corresponding
`custom_tool_call_output` items preserve the same `call_id`. The `output` value
is an array of `input_text` objects. A result can contain several nested tool
results, rather than one shell result. All 18 distinct calls across the three
Sessions have a matching output in their Session's final request.

## 3. Shell representation

Shell commands occur inside JavaScript in a custom call's `input` string.
The outer wire tool is `exec`; the nested invocation is
`tools.exec_command({cmd: "...", ...})`. The `cmd` value is a string, not an
argument list. No recorded shell invocation supplies `workdir`; the user
`environment_context` supplies the synthetic working directory. The calls also
omit a `shell` argument. Don't infer a shell argument list from these bodies.

These are verbatim excerpts from the decoded `input` string in
[Session 1 request 002](session-1/002_v1_responses.json), item index 8
(zero-based):

```javascript
text(await tools.exec_command({cmd:"cat README.md",max_output_tokens:4000}));
text(await tools.exec_command({cmd:"sed -n '1,240p' parcel/billing.py",max_output_tokens:4000}));
text(await tools.exec_command({cmd:"nl -ba parcel/labels.py",max_output_tokens:4000}));
```

That one custom call contains four shell invocations. Other observed forms
include multi-file reads and compound commands:

```text
cat parcel/stock.py parcel/summary.py tests/test_parcels.py
rg -n 'clean_label|normalize_label' parcel tests; cat parcel/__init__.py
python -m pytest -q
```

The multi-file read and baseline test appear in
[Session 1 request 003](session-1/003_v1_responses.json), item index 10.
The compound command appears in
[Session 2 request 004](session-2/004_v1_responses.json), item index 14.
The fixtures preserve the command strings, including punctuation and quoting.

## 4. Patch representation

`apply_patch` is nested inside the custom `exec` call's JavaScript `input`.
It isn't a separate `function_call` or a separate custom call named
`apply_patch` in these requests. The nested `tools.apply_patch` invocation takes
one positional string. There is no nested `patch` or `arguments` key.

[Session 1 request 004](session-1/004_v1_responses.json), item index 14,
contains this exact decoded JavaScript line:

```javascript
text(await tools.apply_patch("*** Begin Patch\n*** Update File: /tmp/erode-codex-recording/demo/parcel/billing.py\n@@\n-    return quantity + unit_price\n+    return quantity * unit_price\n*** End Patch"));
```

The beginning of its patch string is:

```diff
*** Begin Patch
*** Update File: /tmp/erode-codex-recording/demo/parcel/billing.py
@@
-    return quantity + unit_price
+    return quantity * unit_price
```

The same custom call then runs pytest. Session 2 also places multiple patches
and test runs in a single custom call. All recorded patches use
`*** Update File:`; no recorded patch uses `*** Add File:`, `*** Delete File:`,
or `*** Move to:`. These fixtures establish only the forms they contain.

## 5. Does ChatGPT login honor the base URL?

**No in the checked configuration.** The npm CLI's `login status` reports
`Logged in using ChatGPT`. A separate probe unsets both API-key variables and
sets only `OPENAI_BASE_URL=http://127.0.0.1:8787/v1`. With the recorder running,
this exact command returns `OK` and exits with status 0:

```sh
/tmp/erode-codex-recording/cli/node_modules/.bin/codex -a never exec --ignore-user-config --ignore-rules -s workspace-write -C /tmp/erode-codex-recording/demo 'Reply with OK. Do not use tools.'
```

The recorder writes zero POST request files for that probe. Its output directory
and process logs remain outside the repository. Absence of a request has no
fixture filename to cite. This result is a local process observation, not a
fabricated empty request. No ChatGPT routing workaround was attempted.

Before the npm installation, the app-bundled `codex-cli 0.153.4` also completed
an initial synthetic task with the environment-variable override and wrote zero
request files. That run isn't one of the three recorded Sessions. The explicit
provider, rather than the environment variable alone, is the demonstrated
API-key recording method. API-key-only authentication with the built-in
`openai` provider wasn't isolated in a separate experiment.

## Redaction and checks

The redactor replaces approved JSON string-value spans in the original bytes.
It doesn't parse and reserialize the complete request. It preserves key order,
whitespace outside replaced strings, item order, item types, message and tool
IDs, `call_id` values, names, argument shapes, command and patch strings, and
all non-string values. Fixture filenames retain their recorded names.

The approved replacements are:

- Each developer instruction text block becomes
  `<instructions redacted: N bytes>`. There is no top-level `instructions`
  field in these recordings. `N` counts the original decoded UTF-8 bytes.
- Each `encrypted_content` string becomes `<encrypted redacted: N bytes>`.
- `prompt_cache_key` and installation, Session, thread, turn, window, and
  context-window identifiers become `<redacted>`, including identifiers inside
  the JSON string in `client_metadata`.
- Home paths become `/home/dev`, including the Python traceback paths caused
  by the Session 2 rename. Environment-context shell and timezone values become
  `bash` and `UTC`. Synthetic repository paths remain unchanged.

The 21 raw request bodies total 1,773,264 bytes. The redacted fixtures total
740,166 bytes. The redactor checks each output against its source: identical
JSON container shape and key order, exact bytes outside approved replacement
spans, unchanged calls and patches, and only approved string substitutions.
It checks the real username, home directory, and hostname without printing them.
The literal `sk-` search finds only the retained tool-schema words `task-path`
and `Task-path`; no API-key-shaped token remains.

Validation also checks all 18 adjacent history prefixes, all 18 distinct
call/result pairs, all 21 `store: false` values, and the absence of
`previous_response_id`. The existing erode proxy suite passes: **16 passed**.
Its first invocation in the recorder-only environment couldn't load the
repository's SQLAlchemy-dependent `conftest.py`; running it with the existing
repository environment passed. No production code or test logic changed.

These fixtures are frozen evidence. Don't regenerate, trim, or normalize them.
Different client versions, tool settings, or future recordings belong in a
separate corpus. Response bodies, headers, credentials, the throwaway recorder,
and private logs aren't committed. No pull request is opened.

Writing check: active voice, sentence case, exact domain terms, conditions
before instructions, and removal of filler. The published-page mode check
doesn't apply to this fixture report.
