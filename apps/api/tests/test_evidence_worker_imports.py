# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evidence workers don't load write machinery or unrelated handlers."""

import pytest
from pydantic import SecretStr

from sediment_api.config import settings

from test_context_query import RETRIEVAL, TOKEN
from test_evidence_query import AUTH, CALL, SESSION, _call, _ref
from test_worker_processes import _command


@pytest.mark.parametrize(
    "method,path,values",
    [
        ("GET", "/query/evidence", {"session_id": SESSION}),
        (
            "GET",
            "/query/evidence/manifest",
            {"session_id": SESSION, "inference_call_id": CALL},
        ),
        (
            "POST",
            "/query/evidence/read",
            {"schema_version": 1, "session_id": SESSION, "references": [_ref()]},
        ),
        ("POST", "/query/context", {"schema_version": 1, "query": "goal"}),
        (
            "POST",
            "/query/context/discover",
            {"schema_version": 1, "query": "goal"},
        ),
        (
            "POST",
            "/query/context/selected",
            {"schema_version": 1, "session_id": SESSION, "query": "goal"},
        ),
        ("GET", "/query/context/evidence", {"session_id": SESSION}),
        (
            "GET",
            "/query/context/evidence/manifest",
            {"session_id": SESSION, "inference_call_id": CALL},
        ),
        (
            "POST",
            "/query/context/evidence/read",
            {"schema_version": 1, "session_id": SESSION, "references": [_ref()]},
        ),
    ],
)
def test_public_evidence_read_needs_no_write_or_reporting_modules(
    client, monkeypatch, method, path, values
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
        "        if fullname.startswith(('alembic', 'mako', 'sediment_export', "
        "'sediment_api.routers.reports', 'sediment_api.routers.forge', "
        "'sediment_api.services.operational_reports')):\n"
        "            raise ImportError('evidence loaded an unrelated handler')\n"
        "sys.meta_path.insert(0, RejectUnrelated())\n"
        "from sediment_api.worker import main\n"
        "raise SystemExit(main())\n",
    )
    response = client.request(
        method,
        path,
        headers=RETRIEVAL if path.startswith("/query/context") else AUTH,
        **{"params" if method == "GET" else "json": values},
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    if path.endswith("/read"):
        assert response.json()["items"][0]["part"]["arguments"] == {"huge": 2**100}
