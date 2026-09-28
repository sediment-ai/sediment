# invoice-subtotal

This workspace holds `invoice.py`, owned by the billing owner's team. Keep the
public function `subtotal` and its signature unchanged.

## Contract

subtotal(lines) must return the invoice subtotal in integer cents. Each line is a dictionary with a string sku, an integer unit_cents, and an integer quantity; ignore any other keys. Every line charges unit_cents times quantity, and an empty invoice totals 0. SKUs are case-sensitive. No broader validation is required.

## Files

- `invoice.py`: the module under repair.
- `check_invoice.py`: visible checks; run `python3 check_invoice.py`.
- `sample_invoice.json`: a representative input from staging.
- `helpers.py`, `config.toml`: report formatting and settings, unrelated to the
  contract.
- `CHANGELOG.md`, `docs/operations.md`: history and runbook notes.

## Style

Standard library only. Return new containers rather than modifying inputs. Do
not print or log from inside the function.
