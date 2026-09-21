# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from sqlalchemy import event

import pytest

from sediment_api import database
from sediment_core.postgres_engine import create_postgres_engine


@pytest.mark.parametrize("fail", [False, True])
def test_one_shot_fact_store_disposes_engine_after_success_and_failure(
    postgres_database_url: str,
    monkeypatch,
    fail: bool,
) -> None:
    engine = create_postgres_engine(postgres_database_url)
    closed_connections = 0
    disposed_engines = 0

    def record_close(*_args) -> None:
        nonlocal closed_connections
        closed_connections += 1

    event.listen(engine.pool, "close", record_close)

    def record_dispose(*_args) -> None:
        nonlocal disposed_engines
        disposed_engines += 1

    event.listen(engine, "engine_disposed", record_dispose)
    monkeypatch.setattr(database, "create_postgres_engine", lambda _url: engine)

    if fail:
        with pytest.raises(ValueError, match="operator failure"):
            with database.one_shot_fact_store(
                postgres_database_url, operation="test operation"
            ):
                raise ValueError("operator failure")
    else:
        with database.one_shot_fact_store(
            postgres_database_url, operation="test operation"
        ) as store:
            store.health_check()

    assert engine.pool.checkedout() == 0
    assert disposed_engines == 1
    if not fail:
        assert closed_connections >= 1
