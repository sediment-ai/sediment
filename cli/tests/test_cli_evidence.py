# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evidence CLI transport and private packet publication contracts."""

from __future__ import annotations

import json
import os
import stat

import httpx
import pytest

from sediment_cli import cli, client
from sediment_core import (
    EVIDENCE_REQUEST_BYTES_LIMIT,
    EVIDENCE_RESPONSE_BYTES_LIMIT,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)

SESSION = "session/with%?#characters"
CALL = "call/with%?#characters"
TOKEN = "private-operator-token-not-for-output"
OBSERVED = "2026-09-21T12:00:00Z"


def reference(index=0):
    return {
        "inference_call_id": CALL,
        "side": "output",
        "message_index": 0,
        "part_index": index,
    }


def envelope(**fields):
    return {
        "schema_version": 1,
        "session_id": SESSION,
        "quarantine_revision": 3,
        **fields,
    }


def metadata():
    return {
        "inference_call_id": CALL,
        "observed_at": OBSERVED,
        "model_provider": None,
        "model": "model",
    }


def inventory():
    return envelope(
        found=True,
        capture_completeness="unknown",
        visible_inference_calls=1,
        quarantined_inference_calls=0,
        calls=[metadata()],
    )


def manifest():
    return envelope(
        call=metadata(),
        messages=[
            {
                "side": "output",
                "message_index": 0,
                "role": "assistant",
                "finish_reason": None,
                "parts": [{"type": "text", "reference": reference()}],
            }
        ],
    )


def packet(parts=None):
    if parts is None:
        parts = [TextPart(content="private evidence")]
    return envelope(
        items=[
            {
                "reference": reference(index),
                "observed_at": OBSERVED,
                "role": "assistant",
                "finish_reason": None,
                "part": part.model_dump(mode="python"),
            }
            for index, part in enumerate(parts)
        ]
    )


def encoded(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii")


@pytest.fixture
def transport(monkeypatch, tmp_path):
    requests = []

    def install(response, status=200):
        def handle(request):
            requests.append(request)
            return httpx.Response(status, content=encoded(response))

        monkeypatch.setattr(client, "_transport", httpx.MockTransport(handle))
        return requests

    monkeypatch.setenv("SEDIMENT_URL", "https://sediment.example.com")
    monkeypatch.setenv("SEDIMENT_SESSION_TOKEN", TOKEN)
    monkeypatch.setenv("SEDIMENT_DATABASE_URL", "must-not-open-postgresql")
    monkeypatch.setattr(client, "CONFIG_PATH", tmp_path / "absent-config.json")
    return install


def selection_file(tmp_path, references=None):
    path = tmp_path / "selection.json"
    path.write_bytes(
        encoded(
            {
                "schema_version": 1,
                "references": [reference()] if references is None else references,
            }
        )
    )
    return path


def fetch_args(tmp_path, references=None):
    selection = selection_file(tmp_path, references)
    return [
        "evidence",
        "fetch",
        SESSION,
        "--references",
        str(selection),
        "--output",
        str(tmp_path / "packet.json"),
    ]


@pytest.mark.parametrize(
    ("operation", "body", "path"),
    [
        ("inventory", inventory(), "/query/evidence"),
        ("inspect", manifest(), "/query/evidence/manifest"),
    ],
)
def test_metadata_commands_preserve_ids_and_print_json(
    operation, body, path, transport, capsys
):
    requests = transport(body)
    args = ["evidence", operation, SESSION]
    if operation == "inspect":
        args.append(CALL)

    assert cli.main(args) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == body
    assert output.err == ""
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == path
    assert dict(requests[0].url.params) == {
        "session_id": SESSION,
        **({"inference_call_id": CALL} if operation == "inspect" else {}),
    }
    assert requests[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert requests[0].extensions["timeout"]["read"] == 40.0
    assert TOKEN not in output.out + output.err


def test_fetch_preserves_all_canonical_parts_in_private_packet(
    tmp_path, transport, capsys
):
    parts = [
        TextPart(content="goal\x00\ud800"),
        ReasoningPart(content="reasoning\udfff"),
        ToolCallPart(id="tool", name="run", arguments={"n": 2**90, "x": 1.25}),
        ToolCallResponsePart(id="tool", result={"out": [None, True, "\x00"]}),
    ]
    body = packet(parts)
    references = [reference(i) for i in range(4)]
    # Request order is part of the contract, independent of source order.
    references.reverse()
    body["items"].reverse()
    requests = transport(body)

    assert cli.main(fetch_args(tmp_path, references)) == 0

    output = capsys.readouterr()
    destination = tmp_path / "packet.json"
    assert json.loads(destination.read_bytes()) == body
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert output.out == f"{destination}: 4 items\n"
    assert output.err == ""
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "packet.json",
        "selection.json",
    ]
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/query/evidence/read"
    assert json.loads(requests[0].content) == {
        "schema_version": 1,
        "session_id": SESSION,
        "references": references,
    }
    assert requests[0].extensions["timeout"]["read"] == 40.0
    assert TOKEN not in output.out + output.err


def test_evidence_timeout_does_not_change_other_requests(transport):
    requests = transport({"ok": True})
    assert client.get_json("/v1/me") == {"ok": True}
    assert requests[0].extensions["timeout"]["read"] == 10.0


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        {"references": [reference()]},
        {"schema_version": 2, "references": [reference()]},
        {"schema_version": True, "references": [reference()]},
        {"schema_version": "1", "references": [reference()]},
        {"schema_version": 1, "references": []},
        {"schema_version": 1, "references": [reference(), reference()]},
        {"schema_version": 1, "references": [reference(i) for i in range(33)]},
        {"schema_version": 1, "references": [reference()], "session_id": SESSION},
        {"schema_version": 1, "references": [{**reference(), "part_index": -1}]},
        {"schema_version": 1, "references": [{**reference(), "part_index": True}]},
        {"schema_version": 1, "references": [{**reference(), "part_index": 0.0}]},
        {"schema_version": 1, "references": [{**reference(), "side": "raw"}]},
        {"schema_version": 1, "references": [{**reference(), "extra": "private"}]},
    ],
)
def test_invalid_selection_fails_before_http(value, tmp_path, transport, capsys):
    args = fetch_args(tmp_path)
    (tmp_path / "selection.json").write_bytes(encoded(value))
    requests = transport(packet())

    assert cli.main(args) == 1

    assert requests == []
    assert not (tmp_path / "packet.json").exists()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("status", [301, 401, 403, 409, 413, 422, 500, 503])
def test_http_errors_do_not_publish_or_reveal_remote_content(
    status, tmp_path, transport, capsys
):
    requests = transport({"detail": "private evidence and " + TOKEN}, status)

    assert cli.main(fetch_args(tmp_path)) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "private evidence" not in output.err
    assert TOKEN not in output.err
    assert len(requests) == 1
    assert not (tmp_path / "packet.json").exists()


@pytest.mark.parametrize(
    "mutation", ["version", "partial", "session", "reference", "part"]
)
def test_malformed_success_cannot_publish(mutation, tmp_path, transport, capsys):
    body = packet()
    if mutation == "version":
        del body["schema_version"]
    elif mutation == "partial":
        body["items"] = []
    elif mutation == "session":
        body["session_id"] = "other"
    elif mutation == "reference":
        body["items"][0]["reference"]["part_index"] = 1
    else:
        body["items"][0]["part"]["extra"] = "private data"
    transport(body)

    assert cli.main(fetch_args(tmp_path)) == 1

    output = capsys.readouterr()
    assert output.out == ""
    assert "private" not in output.err
    assert not (tmp_path / "packet.json").exists()


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling"])
def test_fetch_refuses_existing_destinations(kind, tmp_path, transport, capsys):
    destination = tmp_path / "packet.json"
    if kind == "file":
        destination.write_text("keep")
    else:
        target = tmp_path / "target"
        if kind == "symlink":
            target.write_text("keep")
        destination.symlink_to(target)
    requests = transport(packet())

    assert cli.main(fetch_args(tmp_path)) == 1

    assert requests == []
    assert capsys.readouterr().out == ""
    if kind == "file":
        assert destination.read_text() == "keep"
    else:
        assert destination.is_symlink()


def test_response_stream_limit_stops_reading_and_closes(
    tmp_path, transport, monkeypatch
):
    transport(packet())

    class Stream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b" " * EVIDENCE_RESPONSE_BYTES_LIMIT
            yield b" "
            pytest.fail("read after evidence response limit")

        def close(self):
            self.closed = True

    stream = Stream()
    monkeypatch.setattr(
        client,
        "_transport",
        httpx.MockTransport(lambda request: httpx.Response(200, stream=stream)),
    )

    assert cli.main(fetch_args(tmp_path)) == 1

    assert stream.closed
    assert not (tmp_path / "packet.json").exists()


@pytest.mark.parametrize(
    ("operation", "body"),
    [
        ("inventory", {**inventory(), "visible_inference_calls": 2}),
        ("inventory", {**inventory(), "found": False}),
        (
            "inventory",
            {
                **inventory(),
                "calls": [metadata(), metadata()],
                "visible_inference_calls": 2,
            },
        ),
        (
            "inspect",
            {**manifest(), "call": {**metadata(), "inference_call_id": "wrong"}},
        ),
        (
            "inspect",
            {
                **manifest(),
                "messages": [{**manifest()["messages"][0], "message_index": 1}],
            },
        ),
        (
            "inspect",
            {
                **manifest(),
                "messages": [
                    {
                        **manifest()["messages"][0],
                        "parts": [{"type": "text", "reference": reference(1)}],
                    }
                ],
            },
        ),
    ],
)
def test_inconsistent_metadata_response_fails(operation, body, transport, capsys):
    transport(body)
    args = ["evidence", operation, SESSION]
    if operation == "inspect":
        args.append(CALL)

    assert cli.main(args) == 1

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("bad_time", [1234, True, None])
def test_response_timestamp_must_be_an_aware_string(
    bad_time, tmp_path, transport, capsys
):
    body = packet()
    body["items"][0]["observed_at"] = bad_time
    transport(body)

    assert cli.main(fetch_args(tmp_path)) == 1

    assert capsys.readouterr().out == ""
    assert not (tmp_path / "packet.json").exists()


@pytest.mark.parametrize("version", [None, False, 1.0, "1", 2])
def test_success_version_is_required_integer_one(version, tmp_path, transport):
    transport({**packet(), "schema_version": version})
    assert cli.main(fetch_args(tmp_path)) == 1
    assert not (tmp_path / "packet.json").exists()


@pytest.mark.parametrize(
    "part",
    [
        {"type": "text"},
        {"type": "tool_call", "id": "tool", "name": "run"},
        {"type": "tool_call", "id": " tool ", "name": "run", "arguments": {}},
        {"type": "tool_call_response", "id": "tool"},
        {"type": "future_part", "content": "private"},
    ],
)
def test_incomplete_or_normalized_parts_cannot_publish(part, tmp_path, transport):
    body = packet()
    body["items"][0]["part"] = part
    transport(body)
    assert cli.main(fetch_args(tmp_path)) == 1
    assert not (tmp_path / "packet.json").exists()


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_response_is_rejected(
    token, tmp_path, transport, monkeypatch, capsys
):
    body = packet([ToolCallResponsePart(id="tool", result="number")])
    content = encoded(body).replace(b'"number"', token.encode())
    transport(body)
    monkeypatch.setattr(
        client,
        "_transport",
        httpx.MockTransport(lambda r: httpx.Response(200, content=content)),
    )
    assert cli.main(fetch_args(tmp_path)) == 1
    assert not (tmp_path / "packet.json").exists()
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "content",
    [
        b"not JSON",
        b"[]",
        b"{} trailing",
        b'"private"',
        b'{"schema_version":1,"schema_version":1}',
        b"\xff",
    ],
)
def test_malformed_json_fails_without_content_diagnostics(
    content, tmp_path, transport, monkeypatch, capsys
):
    transport({})
    monkeypatch.setattr(
        client,
        "_transport",
        httpx.MockTransport(lambda r: httpx.Response(200, content=content)),
    )
    assert cli.main(fetch_args(tmp_path)) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "private" not in output.err
    assert not (tmp_path / "packet.json").exists()


@pytest.mark.parametrize("over", [False, True])
def test_selection_file_exact_byte_limit(over, tmp_path, transport):
    args = fetch_args(tmp_path)
    selection = tmp_path / "selection.json"
    content = selection.read_bytes()
    selection.write_bytes(
        content + b" " * (EVIDENCE_REQUEST_BYTES_LIMIT - len(content) + over)
    )
    requests = transport(packet())

    assert cli.main(args) == int(over)
    assert len(requests) == int(not over)
    assert (tmp_path / "packet.json").exists() is not over


@pytest.mark.parametrize("over", [False, True])
def test_encoded_request_body_exact_byte_limit(over, tmp_path, transport):
    args = fetch_args(tmp_path)
    empty_body = json.dumps(
        {"schema_version": 1, "session_id": "", "references": [reference()]},
        separators=(",", ":"),
    ).encode()
    session = "s" * (EVIDENCE_REQUEST_BYTES_LIMIT - len(empty_body) + over)
    args[2] = session
    requests = transport({**packet(), "session_id": session})

    assert cli.main(args) == int(over)
    assert len(requests) == int(not over)
    if requests:
        assert len(requests[0].content) == EVIDENCE_REQUEST_BYTES_LIMIT


def test_response_exact_limit_is_published(tmp_path, transport, monkeypatch):
    body = packet()
    content = encoded(body)
    content += b" " * (EVIDENCE_RESPONSE_BYTES_LIMIT - len(content))
    transport(body)
    monkeypatch.setattr(
        client,
        "_transport",
        httpx.MockTransport(lambda r: httpx.Response(200, content=content)),
    )
    assert cli.main(fetch_args(tmp_path)) == 0
    assert (tmp_path / "packet.json").stat().st_size == EVIDENCE_RESPONSE_BYTES_LIMIT


def test_maximum_reference_count_is_accepted(tmp_path, transport):
    body = packet([TextPart(content=str(i)) for i in range(32)])
    transport(body)
    assert cli.main(fetch_args(tmp_path, [reference(i) for i in range(32)])) == 0
    assert json.loads((tmp_path / "packet.json").read_bytes()) == body


def test_interrupted_response_closes_stream_without_packet(
    tmp_path, transport, monkeypatch, capsys
):
    transport(packet())

    class Stream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b'{"private evidence":'
            raise httpx.ReadError("private evidence and " + TOKEN)

        def close(self):
            self.closed = True

    stream = Stream()
    monkeypatch.setattr(
        client,
        "_transport",
        httpx.MockTransport(lambda r: httpx.Response(200, stream=stream)),
    )

    assert cli.main(fetch_args(tmp_path)) == 1

    assert stream.closed
    output = capsys.readouterr()
    assert output.out == ""
    assert "private evidence" not in output.err
    assert TOKEN not in output.err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["selection.json"]


@pytest.mark.parametrize("race", ["file", "symlink"])
def test_publication_race_preserves_destination_and_removes_temp(
    race, tmp_path, transport, monkeypatch
):
    transport(packet())
    link = os.link

    def competing_link(source, destination):
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        assert json.loads(source.read_bytes()) == packet()
        if race == "file":
            destination.write_text("keep")
        else:
            destination.symlink_to(tmp_path / "absent")
        return link(source, destination)

    monkeypatch.setattr(os, "link", competing_link)

    assert cli.main(fetch_args(tmp_path)) == 1

    destination = tmp_path / "packet.json"
    if race == "file":
        assert destination.read_text() == "keep"
    else:
        assert destination.is_symlink()
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "packet.json",
        "selection.json",
    ]


@pytest.mark.parametrize("failure", ["fsync", "link"])
def test_publication_io_failure_cleans_temp(
    failure, tmp_path, transport, monkeypatch, capsys
):
    transport(packet())

    def failed(*args):
        raise OSError("private evidence and " + TOKEN)

    monkeypatch.setattr(os, failure, failed)

    assert cli.main(fetch_args(tmp_path)) == 1

    assert sorted(p.name for p in tmp_path.iterdir()) == ["selection.json"]
    output = capsys.readouterr()
    assert output.out == ""
    assert "private evidence" not in output.err
    assert TOKEN not in output.err


def test_compressed_response_is_refused_before_decompression(
    tmp_path, transport, monkeypatch
):
    transport(packet())

    class Unreadable(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            pytest.fail("read a compressed evidence response")
            yield b""

        def close(self):
            self.closed = True

    stream = Unreadable()

    def compressed(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(compressed))
    assert cli.main(fetch_args(tmp_path)) == 1
    assert stream.closed
    assert not (tmp_path / "packet.json").exists()


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
def test_connection_failure_is_content_free(
    failure, tmp_path, transport, monkeypatch, capsys
):
    transport(packet())

    def failed(request):
        raise failure("private " + TOKEN, request=request)

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(failed))
    assert cli.main(fetch_args(tmp_path)) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert TOKEN not in output.err
    assert "private" not in output.err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["selection.json"]


def test_redirect_cannot_forward_operator_token(tmp_path, transport, monkeypatch):
    requests = transport(packet())

    def redirect(request):
        requests.append(request)
        assert len(requests) == 1
        return httpx.Response(
            307, headers={"Location": "https://elsewhere.example.com/read"}
        )

    monkeypatch.setattr(client, "_transport", httpx.MockTransport(redirect))
    assert cli.main(fetch_args(tmp_path)) == 1
    assert len(requests) == 1


@pytest.mark.parametrize("operation", ["inventory", "inspect", "fetch"])
def test_evidence_reuses_url_validation_before_token_send(
    operation, tmp_path, transport, monkeypatch
):
    requests = transport(packet())
    monkeypatch.setenv("SEDIMENT_URL", "http://remote.example.com")
    args = ["evidence", operation, SESSION]
    if operation == "inspect":
        args.append(CALL)
    elif operation == "fetch":
        args = fetch_args(tmp_path)
    assert cli.main(args) == 1
    assert requests == []


def test_evidence_uses_operator_login_without_capture_token(
    transport, tmp_path, monkeypatch
):
    requests = transport(inventory())
    monkeypatch.delenv("SEDIMENT_URL")
    monkeypatch.delenv("SEDIMENT_SESSION_TOKEN")
    client.write_config(
        {
            "current": "https://sediment.example.com",
            "servers": {
                "https://sediment.example.com": {
                    "token": TOKEN,
                    "capture_token": "capture-token-must-not-read",
                }
            },
        }
    )
    assert cli.main(["evidence", "inventory", SESSION]) == 0
    assert requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("found", [False, True])
def test_unknown_or_empty_inventory_is_success(found, transport, capsys):
    body = envelope(
        found=found,
        capture_completeness="unknown",
        visible_inference_calls=0,
        quarantined_inference_calls=0,
        calls=[],
    )
    transport(body)
    assert cli.main(["evidence", "inventory", SESSION]) == 0
    assert json.loads(capsys.readouterr().out) == body


def test_empty_messages_and_sides_remain_visible(transport, capsys):
    body = manifest()
    body["messages"][0]["parts"] = []
    transport(body)
    assert cli.main(["evidence", "inspect", SESSION, CALL]) == 0
    assert json.loads(capsys.readouterr().out) == body


def test_selection_rejects_duplicate_normalized_references(tmp_path, transport):
    requests = transport(packet())
    refs = [reference(), {**reference(), "inference_call_id": f" {CALL} "}]
    assert cli.main(fetch_args(tmp_path, refs)) == 1
    assert requests == []
