# Export for a consumer

Use an exact `--profile` to export for Hugging Face TRL (Transformer
Reinforcement Learning), Fireworks, SWE-bench, or NeMo Gym. The
[Consumer compatibility reference](../reference/compatibility.md) lists the
supported versions and checks. Without a profile, Sediment emits its canonical
rows. Diff-SFT and Recovery remain Sediment-native.

A profile changes representation at the consumer boundary. It preserves the
Evidence recipe, labels, Reward, and Provenance. Missing negative examples
remain missing. A permissive agent workflow can produce verified SFT rows while
producing no DPO pairs. Prompt steering alone isn't a rejection label.

## Check consumer capacity

Profiles use the 64 MiB encoded materialization allowance from `BundleLimits`.
SFT and DPO profiles check canonical training rows before collecting their
consumer view. RLVR profiles check complete Rollouts before hydration; the
NeMo check also includes the source Inference calls. A file-backed population
that exceeds the allowance fails before its first record is decoded.

The same allowance applies separately to projected native rows, adapted data
and evidence together, loader inputs, consumer settings, and prepared data and
evidence files together. Native projectors stage each admitted row before
collecting a consumer view. Python callers that supply iterators receive a
capacity error when their cumulative encoded rows exceed the allowance.
These limits bound encoded populations, not total process memory.

If a limit is exceeded, the complete export fails before publication. Sediment
preserves the destination and removes its private staging. Capacity failures
don't become eligibility skips or smaller successful datasets. For a larger
export, use canonical rows without `--profile` and qualify downstream conversion
separately. The [profiling guide](../operate/profile-derivations.md) explains
resource measurement and qualification boundaries.

## Install the matching optional environment

Run these commands in a separate Sediment checkout with Python 3.12. The
consumer packages don't belong in the API deployment's base environment.
`uv sync` can remove optional packages; after installing them, use the
interpreter and CLI in `.venv` directly.

```bash
uv sync --locked
uv pip install --python .venv/bin/python -r requirements/compatibility/hf.txt
```

For SWE-bench, replace the optional installation with:

```bash
uv pip install --python .venv/bin/python -r requirements/compatibility/swe.txt
uv pip install --python .venv/bin/python --no-deps \
  'swebench @ git+https://github.com/SWE-bench/SWE-bench.git@87ab1f6ced28f75ba73ca899dc759b019310944a'
```

For NeMo Gym, use:

```bash
uv pip install --python .venv/bin/python -r requirements/compatibility/nemo.txt
uv pip install --python .venv/bin/python --no-deps \
  'nemo-gym @ git+https://github.com/NVIDIA-NeMo/Gym.git@27e921137042dcdb8a39c7169128619b9108074b'
```

Those two installations qualify the loader/parser imports. They don't install
or qualify every upstream server, telemetry integration, or training runtime.
Fireworks format profiles require no optional package. They validate the
published format; they don't run a hosted upload or training job.

## Export SFT or DPO

If captured CI evidence supports verified imitation, select `sft_verified`:

```bash
.venv/bin/sediment export sft --from /data/derived/review \
  --recipe sft_verified --profile hf-trl-sft-v1 --out /data/consumer/hf-sft
```

If captured human preferences support DPO, select `dpo_human`:

```bash
.venv/bin/sediment export dpo --from /data/derived/review \
  --recipe dpo_human --profile hf-trl-dpo-v2 --out /data/consumer/hf-dpo
```

For Fireworks, use `fireworks-sft-v1` or `fireworks-dpo-v2`. Every invocation
requires an unused destination directory. An invalid profile, incompatible
package version, invalid row, or incomplete configuration fails before
publication. A populated export contains data, aligned evidence sidecars, and
a compatibility manifest. An empty export reports zero rows and creates no
directory. Preserve the sidecars for auditing; send only data to the consumer.

DPO profiles v2 accept recipe-v2/schema-v4 rows. The retired DPO profile names
v1 fail explicitly. If you retained recipe-v1/schema-v3 rows, re-export canonical
evidence before selecting a successor; the adapter refuses historical rows.
See [Migrate retained DPO exports](dpo.md#migrate-retained-dpo-exports).

### Load Hugging Face data

Use Sediment's loader to preserve heterogeneous nested message and tool values.
Inferring Arrow structs from early rows can reject a later tool argument shape.

```python
from sediment_export.compatibility import load_hf_dataset

train = load_hf_dataset(
    "/data/consumer/hf-sft/data.train.jsonl", "hf-trl-sft-v1"
)
```

The loader uses explicit `List(Json())` features and verifies an exact value
round trip through `datasets.Dataset`. SFT retains separate `prompt` and
`completion` fields. DPO retains `prompt`, `chosen`, and `rejected`. The adapter
renames readable `thinking` to `reasoning_content`. It preserves structured tool
arguments and emits structured results as deterministic JSON strings.

Configure the trainer to respect the completion loss boundary. Confirm that
your model's chat template supports reasoning and tool calls before training.
Qualification exercises TRL's public conversation helper and the Qwen3-0.6B
tokenizer at revision `c1899de289a04d12100db370d81485cdf75e47ca`. It doesn't
qualify every tokenizer or run an optimizer. See the upstream
[Dataset formats](https://huggingface.co/docs/trl/dataset_formats).

### Interpret Fireworks output

Both Fireworks profiles reject `developer` messages and require any `system`
message to appear first. Sediment doesn't rewrite or reorder captured roles.

The SFT adapter sets earlier assistant messages to `weight: 0` and target
assistant messages to `weight: 1`. These values mark the loss boundary. The
adapter doesn't emit a root sample weight or derive one from Confidence.
Tool arguments become JSON strings. Readable reasoning uses
`reasoning_content`. Tool definitions remain absent because the Fact doesn't
capture them. Configure any definitions required by your training model from
an authoritative source.

The DPO profile accepts the documented text-only message contract and one
final assistant message per preferred and non-preferred output. Unsupported
roles or structured DPO messages fail visibly. Fireworks requires at least
three examples per uploaded dataset; a smaller local export isn't evidence
of hosted readiness. See the provider's
[SFT format](https://docs.fireworks.ai/fine-tuning/fine-tuning-models) and
[DPO format](https://docs.fireworks.ai/fine-tuning/dpo-fine-tuning).

## Export NeMo Gym Rollouts

Supply a JSON configuration with explicit adapter parameters:

```json
{
  "schema_version": 1,
  "nemo": {"parallel_tool_calls": false, "tool_choice": "auto", "tools": []}
}
```

These values describe your consumer setup. The evidence sidecar identifies
the operator as their source; they aren't reconstructed inference-request Facts.

```bash
.venv/bin/sediment export rlvr --from /data/derived/review \
  --target nemo-gym --profile nemo-gym-rollouts-v1 \
  --consumer-config /data/nemo.json --out /data/consumer/nemo
```

The profile hydrates full captured messages through validated Inference-call
references. It preserves Segment boundaries, ordered native Responses items,
and tool-call links. It never substitutes scoring text for model output. NeMo
Gym 0.2.1 stores readable reasoning in `summary_text`; the adapter copies the
full text without summarizing it. The sidecar also retains native output
messages. Response timestamps cite the source Inference call. Missing status
and token usage stay absent.

Every admitted row passes the upstream `BaseVerifyResponse` parser without
silent field loss. The profile requires captured numeric Reward. It counts
missing Reward, missing Inference calls, identity mismatches, missing or mixed
models, and unsupported message mappings. The closed vocabulary lives in
`consumer_rlvr.py::NEMO_PROFILE_SKIP_REASONS`.

This is a native parser contract. A particular offline training recipe may
require a further dataset formatter and tokenizer. In particular, the
[NeMo offline tutorial](https://docs.nvidia.com/nemo/gym/v0.2.1/training-tutorials/offline-training-w-rollouts/)
shows a formatting snippet with top-level `output`; this profile retains the
native verification envelope's `response.output`. Don't pass that envelope to
the snippet unchanged.

## Supply SWE-bench task and runtime inputs

The SWE-bench profile requires an explicit repair request and executable task
configuration for each canonical instance ID. First inspect the canonical
`--target swe-bench` projection and its repository/base-commit binding. Then
supply `tasks` in the consumer JSON configuration. Replace the illustrative
values with your verified task configuration:

```json
{
  "schema_version": 1,
  "tasks": {
    "exact-exported-instance-id": {
      "repo": "owner/repository",
      "base_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "problem_statement": "The repair request you verified.",
      "test_patch": "",
      "hints_text": "",
      "created_at": "2026-09-01T00:00:00Z",
      "version": "your-task-version",
      "environment_setup_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "FAIL_TO_PASS": ["test_name_that_fails_before_the_patch"],
      "PASS_TO_PASS": [],
      "image": "your-prepared-test-image",
      "eval_script": "echo '>>>>> Start Test Output'\npython -m pytest -rA\necho '>>>>> End Test Output'",
      "log_parser": "parse_log_pytest",
      "eval_type": "pass_and_fail"
    }
  }
}
```

Use an empty `test_patch` only when the required tests already exist at the
base commit. `FAIL_TO_PASS` must name at least one test; its names can't overlap
`PASS_TO_PASS`. The runtime image must contain the checked-out repository and
its dependencies. Match the command, output markers, and registered parser to
the tests you run. Sediment validates the fields and parser registration but
doesn't run the image or establish test transitions.

```bash
.venv/bin/sediment export rlvr --from /data/derived/review \
  --target swe-bench --profile swe-bench-tasks-v1 \
  --consumer-config /data/swe.json --out /data/consumer/swe
```

The repository and base commit must match the source row exactly. The patch
remains the captured passing reference patch. The sidecar preserves the
captured first user message separately from your explicit repair request and
records the configuration digest. Agent instructions or environment context
mustn't become a guessed repair request. Qualification uses the pinned
[SWE-bench loader and runtime specification](https://github.com/SWE-bench/SWE-bench/blob/87ab1f6ced28f75ba73ca899dc759b019310944a/swebench/harness/utils.py).

## Check suitability before training

Inspect the manifest's split counts, numeric Reward counts, and exclusions.
`skipped` records consumer-adapter exclusions. CLI exports also preserve
`canonical_skipped` from the training projection. RLVR exports preserve
`fragmented` from the source bundle. These fields count separate populations;
don't add them together. Low-level publication omits a source population when
the caller doesn't supply it. A supplied empty map means zero recorded counts.

An all-positive corpus can support imitation; parser acceptance doesn't create
useful preference pairs or Reward contrast. An empty labeled eval partition
can't measure held-out performance. Split exports reject identical full
prompts shared by train and eval, but different prompts can describe the same
task. Review task overlap and model context limits separately.

The pinned CI matrix qualifies every advertised profile and gates release
preparation. The weekly canary tests updated upstream packages without
changing product pins. Dependency or Python-version incompatibility counts as
drift. Fireworks has no local dataset loader; its format tests remain separate
from hosted acceptance. If an upstream release needs different output bytes,
add a profile version before changing the support claim.
