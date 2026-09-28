# threshold-alerts

This workspace holds `alerts.py`, owned by the monitoring owner's team. Keep the
public function `alerts` and its signature unchanged.

## Contract

alerts(readings, limit) must return the zero-based positions of readings strictly greater than limit, in order. Readings and limit are numbers. An empty list returns an empty list. Every reading below or at the limit is quiet. No broader validation is required.

## Files

- `alerts.py`: the module under repair.
- `check_alerts.py`: visible checks; run `python3 check_alerts.py`.
- `sample_readings.json`: a representative input from staging.
- `helpers.py`, `config.toml`: report formatting and settings, unrelated to the
  contract.
- `CHANGELOG.md`, `docs/operations.md`: history and runbook notes.

## Style

Standard library only. Return new containers rather than modifying inputs. Do
not print or log from inside the function.
