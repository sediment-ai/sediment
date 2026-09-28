# SPDX-License-Identifier: AGPL-3.0-or-later
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bounded_evidence_selection as sel  # noqa: E402
import jev_judgment_eval as judgment  # noqa: E402


def test_cases_use_the_j1_request_and_label_only_the_missing_note():
    families = json.loads(judgment.FIXTURE.read_bytes())["families"]
    cases = judgment.cases(families)
    assert len(cases) == 24
    for case in cases:
        rule = next(f["rule"] for f in families if f["name"] == case["family"])
        stated = rule.split(": ", 1)[1] in case["task"]
        assert stated is (case["profile"] == "redundant")
        assert case["required"]["note"] is (case["profile"] == "missing")
        assert sum(case["required"].values()) == int(not stated)
        body = sel._request_body(case["task"], case["initial"], case["candidates"])
        request = json.loads(body)
        assert len(body) <= sel.JEV_REQUEST_BYTES
        assert len(request["questions"]) == 12
        assert (
            request["questions"]["c4_relevant"]
            == (sel.candidate_questions(3, "c4")["c4_relevant"])
        )

    def row(profile, added):
        return {
            "profile": profile,
            "scores": {k: dict.fromkeys(sel.PROPOSITIONS, 0.5) for k in judgment.KINDS},
            "added": {k: k == "note" and added for k in judgment.KINDS},
            "usage": {"input_tokens": 1},
        }

    rows = [row("missing", i < 11) for i in range(12)]
    rows += [row("redundant", i < 2) for i in range(12)]
    assert judgment.summarize(rows)["usable_as_filter"] is True
    rows[0]["added"]["note"] = False
    assert judgment.summarize(rows)["usable_as_filter"] is False
