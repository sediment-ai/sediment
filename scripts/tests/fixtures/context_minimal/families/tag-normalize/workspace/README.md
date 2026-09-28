# tag-normalize

This workspace holds `tags.py`, a small module owned by the catalog owner's team.
It is imported by nightly batch jobs and by an interactive report. Keep the public
function name `normalize` and its signature unchanged; callers pass plain Python
values and expect plain Python values back.

## Contract

normalize(tags) must return a list of normalized tags. Strip surrounding whitespace, lower-case each tag, and replace every run of internal whitespace with a single hyphen. Drop tags that are empty after stripping. Keep only the first occurrence of each normalized tag, in input order. Every other non-empty tag appears in the output exactly as normalized. An empty list returns an empty list. No broader grammar is required.

## Examples

See `sample_tags.json` for a representative input captured from the staging
environment. The visible checks in `check_tags.py` cover ordinary inputs,
whitespace handling, and empty input. Run them with `python3 check_tags.py`.

## Operational notes

- The batch job calls `normalize` once per input and logs the result size.
- The interactive report calls it on user-filtered subsets, so it must not keep
  state between calls or mutate its arguments.
- Inputs come from upstream exports that are already schema-validated; do not add
  defensive parsing beyond what the contract describes.
- Performance is not a concern at current volumes (well under 100,000 items per
  call), so prefer clear code over clever code.
- Do not print or log from inside the function; callers own logging.

## History

- 2025-11: first version written for the staging import.
- 2026-02: moved into this workspace from the monolith; tests were rewritten as
  the visible checks in `check_tags.py`.
- 2026-06: the archived display tooling that once post-processed these results was
  retired. Its diagnostics are still runnable but unrelated to this module.

## Style

Use the standard library only. Type hints are welcome. Keep the module
self-contained: no new files, no configuration, no environment variables. Return
new containers rather than modifying inputs. Names should describe domain values,
not implementation details.

## Glossary

- Input: the value passed by the caller, described in the contract above.
- Result: the value returned to the caller.
- Visible checks: the assertions in `check_tags.py`; they are a floor, not the
  full contract.
