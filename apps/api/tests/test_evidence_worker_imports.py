# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evidence workers don't load unrelated report and forge handlers."""

import pytest
from pydantic import SecretStr

from sediment_api.config import settings

from test_context_query import RETRIEVAL, TOKEN
from test_evidence_query import CALL, SESSION, _call
from test_worker_processes import _command


@pytest.mark.parametrize(
    "path,body",
    [
        ("/query/context", {"schema_version": 1, "query": "goal"}),
        ("/query/context/discover", {"schema_version": 1, "query": "goal"}),
        (
            "/query/context/selected",
            {"schema_version": 1, "session_id": SESSION, "query": "goal"},
        ),
        (
            "/query/context/evidence/read",
            {
                "schema_version": 1,
                "session_id": SESSION,
                "references": [
                    {
                        "inference_call_id": CALL,
                        "side": "input",
                        "message_index": 1,
                        "part_index": 0,
                    }
                ],
            },
        ),
    ],
)
def test_public_evidence_read_needs_no_reporting_modules(
    client, monkeypatch, path, body
):
    monkeypatch.setattr(settings, "retrieval_token", SecretStr(TOKEN))
    monkeypatch.setattr(settings, "retrieval_session_id", SESSION)
    monkeypatch.setattr(settings, "retrieval_session_ids", None)
    client.app.state.fact_store.store_inference_call(_call())
    _command(
        monkeypatch,
        "import importlib.abc, sys\n"
        "class RejectUnrelated(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname.startswith(('sediment_export', "
        "'sediment_api.routers.reports', 'sediment_api.routers.forge', "
        "'sediment_api.services.operational_reports')):\n"
        "            raise ImportError('evidence loaded an unrelated handler')\n"
        "sys.meta_path.insert(0, RejectUnrelated())\n"
        "from sediment_api.worker import main\n"
        "raise SystemExit(main())\n",
    )
    response = client.post(path, headers=RETRIEVAL, json=body)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
