# username-check

This workspace holds `usernames.py`, owned by the accounts owner's team. Keep the
public function `valid` and its signature unchanged.

## Contract

valid(name) must return True when a username is acceptable and False otherwise. A username is 3 to 16 characters of lowercase ASCII letters, digits, and underscores, and it starts with a letter. Every name that meets these rules is valid. No broader validation is required.

## Files

- `usernames.py`: the module under repair.
- `check_usernames.py`: visible checks; run `python3 check_usernames.py`.
- `sample_signups.json`: a representative input from staging.
- `helpers.py`, `config.toml`: report formatting and settings, unrelated to the
  contract.
- `CHANGELOG.md`, `docs/operations.md`: history and runbook notes.

## Style

Standard library only. Return new containers rather than modifying inputs. Do
not print or log from inside the function.
