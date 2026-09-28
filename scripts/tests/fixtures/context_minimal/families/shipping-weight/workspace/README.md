# shipping-weight

This workspace holds `shipping.py`, a small module owned by the fulfillment owner's team.
It is imported by nightly batch jobs and by an interactive report. Keep the public
function name `total_grams` and its signature unchanged; callers pass plain Python
values and expect plain Python values back.

## Contract

total_grams(items) must return the total shipping weight in integer grams. Each item is a dictionary with a string sku, an integer grams per unit, and an integer quantity; ignore any other keys. Each item contributes grams times quantity. Every item counts toward the total, and an empty list weighs 0. SKUs are case-sensitive. No broader validation is required.

## Examples

See `sample_order.json` for a representative input captured from the staging
environment. The visible checks in `check_shipping.py` cover ordinary inputs,
whitespace handling, and empty input. Run them with `python3 check_shipping.py`.

## Operational notes

- The batch job calls `total_grams` once per input and logs the result size.
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
  the visible checks in `check_shipping.py`.
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
- Visible checks: the assertions in `check_shipping.py`; they are a floor, not the
  full contract.
