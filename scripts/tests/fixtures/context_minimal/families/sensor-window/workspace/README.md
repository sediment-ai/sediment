# sensor-window

This workspace holds `window.py`, a small module owned by the sensor owner's team.
It is imported by nightly batch jobs and by an interactive report. Keep the public
function name `moving_average` and its signature unchanged; callers pass plain Python
values and expect plain Python values back.

## Contract

moving_average(readings, size) must return the average of every window of size consecutive readings, in order, each rounded to two decimals as a float. Readings are numbers. When there are fewer readings than size, return an empty list. Every reading belongs to the windows that cover its position. size is a positive integer. No broader validation is required.

## Examples

See `sample_readings.json` for a representative input captured from the staging
environment. The visible checks in `check_window.py` cover ordinary inputs,
whitespace handling, and empty input. Run them with `python3 check_window.py`.

## Operational notes

- The batch job calls `moving_average` once per input and logs the result size.
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
  the visible checks in `check_window.py`.
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
- Visible checks: the assertions in `check_window.py`; they are a floor, not the
  full contract.
