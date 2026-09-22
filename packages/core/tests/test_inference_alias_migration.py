# SPDX-License-Identifier: AGPL-3.0-or-later
"""Physical alias upgrades preserve canonical evidence and reject stale writers."""

import json
import math

from alembic import command
import pytest
from sqlalchemy import Engine, MetaData, Table, create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError

from sediment_core import (
    FactStore,
    FactTable,
    QuarantineAction,
    QuarantineRecord,
    TextPart,
    ToolCallPart,
)
from sediment_core.postgres_migrations import (
    MigrationError,
    _alembic_config,
    inspect_revision,
    upgrade_database,
)
from sediment_core.postgres_schema import fact_quarantine
from test_inference_call_witnesses import BOUNDARY, call


def legacy_engine(postgres_database_factory):
    url = postgres_database_factory(migrated=False)
    engine = create_engine(url)
    with engine.connect() as connection:
        config = _alembic_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0010_repository_identity")
    return url, engine


def insert_legacy(connection, fact):
    table = Table("inference_calls", MetaData(), autoload_with=connection)
    values = fact.model_dump(mode="python")
    for name in ("input_messages", "output_messages", "raw"):
        values[name] = json.dumps(values[name], ensure_ascii=True)
    connection.execute(table.insert().values(**values))


def test_alias_migration_backfills_all_canonical_identifiers_and_repeats(
    postgres_database_factory,
):
    url, engine = legacy_engine(postgres_database_factory)
    exceptional = call("exceptional", ["shared", "shared", "provider-exceptional"])
    exceptional.output_messages[0].parts += [
        TextPart(content="\x00\ud800"),
        ToolCallPart(
            id="long-" + "x" * 12_800,
            name="Edit",
            arguments={"nan": float("nan"), "inf": float("inf"), "text": "\udfff\x00"},
        ),
    ]
    facts = [
        exceptional,
        call("empty", [], model_call_id=None),
        call("quarantined", ["hidden"]),
    ]
    try:
        with engine.begin() as connection:
            for fact in facts:
                insert_legacy(connection, fact)
            before = connection.execute(
                text(
                    "SELECT inference_call_id, input_messages, output_messages, raw FROM inference_calls ORDER BY inference_call_id"
                )
            ).all()
        with engine.begin() as connection:
            connection.execute(
                fact_quarantine.insert().values(
                    **QuarantineRecord(
                        org_id="acme",
                        fact_table=FactTable.INFERENCE_CALLS,
                        fact_id="quarantined",
                        action=QuarantineAction.QUARANTINE,
                        reason="test",
                    ).model_dump(mode="python")
                )
            )
        upgrade_database(url)
        upgrade_database(url)
        with engine.connect() as connection:
            after = connection.execute(
                text(
                    "SELECT inference_call_id, input_messages, output_messages, raw FROM inference_calls ORDER BY inference_call_id"
                )
            ).all()
            assert after == before
            assert connection.execute(
                text(
                    "SELECT inference_call_id, call_alias_count FROM inference_calls ORDER BY inference_call_id"
                )
            ).all() == [("empty", 0), ("exceptional", 3), ("quarantined", 2)]
        store = FactStore(engine)
        loaded = {
            fact.inference_call_id: fact
            for fact in store.read_inference_calls("acme", include_quarantined=True)
        }
        assert (
            loaded["exceptional"].output_messages[0].parts[-2].content == "\x00\ud800"
        )
        assert math.isnan(
            loaded["exceptional"].output_messages[0].parts[-1].arguments["nan"]
        )
        assert not store.read_inference_call_identity_witnesses(
            "acme", call_ids={"hidden"}, observed_through=BOUNDARY
        )
        store.release_fact(
            "acme", FactTable.INFERENCE_CALLS, "quarantined", reason="test"
        )
        assert [
            item.inference_call_id
            for item in store.read_inference_call_identity_witnesses(
                "acme", call_ids={"hidden"}, observed_through=BOUNDARY
            )
        ] == ["quarantined"]
        assert (
            store.read_inference_call_identity_witnesses(
                "acme", call_ids={"long-" + "x" * 12_800}, observed_through=BOUNDARY
            )[0].inference_call_id
            == "exceptional"
        )
    finally:
        engine.dispose()


def test_failed_alias_migration_rolls_back_and_can_retry(postgres_database_factory):
    url, engine = legacy_engine(postgres_database_factory)
    with engine.begin() as connection:
        insert_legacy(connection, call("retained", ["tool"]))

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("CREATE INDEX ix_inference_call_aliases_lookup"):
            raise RuntimeError("injected index creation failure")

    event.listen(Engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(MigrationError):
            upgrade_database(url)
    finally:
        event.remove(Engine, "before_cursor_execute", fail)
    try:
        assert inspect_revision(url).database_revision == "0010_repository_identity"
        with engine.connect() as connection:
            assert "inference_call_aliases" not in inspect(connection).get_table_names()
            assert "call_alias_count" not in {
                column["name"]
                for column in inspect(connection).get_columns("inference_calls")
            }
            assert (
                connection.execute(
                    text("SELECT count(*) FROM inference_calls")
                ).scalar_one()
                == 1
            )
        upgrade_database(url)
        assert (
            FactStore(engine)
            .read_inference_call_identity_witnesses(
                "acme", call_ids={"tool"}, observed_through=BOUNDARY
            )[0]
            .inference_call_id
            == "retained"
        )
    finally:
        engine.dispose()


def test_old_style_insert_without_alias_count_fails_atomically(postgres_engine):
    with pytest.raises(IntegrityError):
        with postgres_engine.begin() as connection:
            insert_legacy(connection, call("stale", ["tool"]))
    with postgres_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM inference_calls")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM inference_call_aliases")
            ).scalar_one()
            == 0
        )
