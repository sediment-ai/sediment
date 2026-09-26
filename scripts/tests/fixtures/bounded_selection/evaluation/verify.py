# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private checks; the live runner executes this only in a networkless container.

Usage: python -I verify.py <family> <workspace>. Prints one JSON object with
behavior_pass and constraint_pass. Neither model input contains this file.
"""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys


def _events(*rows):
    return "".join(json.dumps({"id": i, "action": a}) + "\n" for i, a in rows)


EVENT_BEHAVIOR = (
    (
        _events(("e1", "login"), ("e2", "logout"), ("e3", "login")),
        {"login": 2, "logout": 1},
    ),
    (
        '\n \t\n{"id":"e1","action":" login "}\n\n{"id":"e2","action":"login\\t"}\n',
        {"login": 2},
    ),
    ('{"id":"e1","action":"a"}\r\n{"id":"e2","action":"b"}\r\n', {"a": 1, "b": 1}),
    (
        _events(("e1", "Login"), ("e2", "login"), ("e3", "LOGIN")),
        {"Login": 1, "login": 1, "LOGIN": 1},
    ),
    (_events(("e1", "sign in"), ("e2", "sign in")), {"sign in": 2}),
    ('{"id":"e1","action":"a","user":"u","n":3}\n', {"a": 1}),
    ('{"id":"e1","action":"caf\\u00e9"}\n', {"café": 1}),
    ('  {"id": "e1", "action": "a"}  \n{"id":"e2","action":"b"}', {"a": 1, "b": 1}),
    ("", {}),
    ("\n \r\n\t\n", {}),
)
EVENT_CONSTRAINTS = (
    (_events(("e1", "a"), ("e1", "a"), ("e2", "a")), {"a": 2}),
    (_events(("e1", "a"), ("e1", "b")), {"b": 1}),
    (_events(("e1", "a"), ("e2", "c"), ("e1", "b")), {"b": 1, "c": 1}),
    (
        _events(("e1", "a"), ("e2", "b"), ("e1", "c"), ("e2", "b"), ("e3", "a")),
        {"c": 1, "b": 1, "a": 1},
    ),
    (_events(("e1", "a"), ("E1", "a")), {"a": 2}),
    ('{"id":"e1","action":" a "}\r\n\r\n{"id":"e1","action":"b"}\r\n', {"b": 1}),
)

CONFIG_BEHAVIOR = (
    (["a = 1\nb=2\n"], {"a": "1", "b": "2"}),
    (["a=1\nb=2", "b = 3\n"], {"a": "1", "b": "3"}),
    (["a=1", "a=2", "a=3"], {"a": "3"}),
    (["# c\n\n  # indented\na=1\n   \n"], {"a": "1"}),
    (["url = http://x/?a=b=c\n"], {"url": "http://x/?a=b=c"}),
    (["  key  =   spaced value  \n"], {"key": "spaced value"}),
    (["Key=1\nkey=2\nKEY=3"], {"Key": "1", "key": "2", "KEY": "3"}),
    (["a=1\r\nb=2\r\n"], {"a": "1", "b": "2"}),
    (["a = x # not a comment"], {"a": "x # not a comment"}),
    ([], {}),
    ([""], {}),
)
CONFIG_CONSTRAINTS = (
    (["a=1\nb=2", "b =\n"], {"a": "1"}),
    (["a=1", "a=", "a=3"], {"a": "3"}),
    (["a=", "b=2"], {"b": "2"}),
    (["a=1", "a =   \t"], {}),
    (["a=1\na=\n"], {}),
    (["Mode=1\nmode=2", "Mode ="], {"mode": "2"}),
)

DURATION_BEHAVIOR = (
    ("2h", 7200),
    ("5m", 300),
    ("9s", 9),
    ("0s", 0),
    (" 1h30m ", 5400),
    ("1h 2m 3s", 3723),
    ("10m5s", 605),
    ("1h5s", 3605),
    ("12h", 43200),
    ("\t90m\n", 5400),
)
DURATION_CONSTRAINTS = (
    ("45", 2700),
    (" 3 ", 180),
    ("0", 0),
    ("90", 5400),
)

FAMILIES = {
    "event-rollup": (
        "event_rollup.py",
        "action_counts",
        int,
        EVENT_BEHAVIOR,
        EVENT_CONSTRAINTS,
    ),
    "layered-config": (
        "layered_config.py",
        "resolve",
        str,
        CONFIG_BEHAVIOR,
        CONFIG_CONSTRAINTS,
    ),
    "duration-parse": (
        "durations.py",
        "to_seconds",
        None,
        DURATION_BEHAVIOR,
        DURATION_CONSTRAINTS,
    ),
}


def _exact(actual, expected, value_type) -> bool:
    if value_type is None:
        return type(actual) is int and actual == expected
    return (
        type(actual) is dict
        and all(
            type(key) is str and type(value) is value_type
            for key, value in actual.items()
        )
        and actual == expected
    )


def _passes(function, value_type, cases) -> bool:
    try:
        for argument, expected in cases:
            if not _exact(function(argument), expected, value_type):
                return False
    except BaseException:
        # A generated program's SystemExit is a failed check, not a verdict.
        return False
    return True


def verify(family: str, workspace: Path) -> dict[str, bool]:
    result = {"behavior_pass": False, "constraint_pass": False}
    module_name, function_name, value_type, behavior, constraints = FAMILIES[family]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            spec = importlib.util.spec_from_file_location(
                "program_under_test", workspace / module_name
            )
            if spec is None or spec.loader is None:
                return result
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            function = getattr(module, function_name)
            result["behavior_pass"] = _passes(function, value_type, behavior)
            result["constraint_pass"] = _passes(function, value_type, constraints)
    except BaseException:
        pass
    return result


if __name__ == "__main__":
    print(json.dumps(verify(sys.argv[1], Path(sys.argv[2]))))
