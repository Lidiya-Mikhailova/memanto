"""Tests for the Langfuse exporter: credentials, hosts, and cursor pagination."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from memanto.cli.analyze import langfuse_export
from memanto.cli.analyze.langfuse_export import (
    MAX_PAGES,
    normalize_host,
    paginate,
    run_langfuse_export,
    split_api_key,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x"
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeClient:
    """Stands in for httpx.Client, recording every request it serves."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if self._pages:
            return FakeResponse(self._pages.pop(0))
        return FakeResponse({"data": [], "meta": {"nextCursor": None}})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# --------------------------------------------------------------------------
# Credentials and host
# --------------------------------------------------------------------------


def test_split_api_key_splits_the_combined_pair():
    assert split_api_key("pk-lf-abc:sk-lf-xyz") == ("pk-lf-abc", "sk-lf-xyz")
    assert split_api_key("  pk-lf-abc : sk-lf-xyz  ") == ("pk-lf-abc", "sk-lf-xyz")


@pytest.mark.parametrize("bad", ["", "pk-lf-only", "pk-lf-abc:", ":sk-lf-xyz"])
def test_split_api_key_rejects_a_half_credential(bad):
    with pytest.raises(ValueError, match="both keys"):
        split_api_key(bad)


def test_normalize_host_handles_cloud_and_self_hosted():
    assert normalize_host(None) == "https://cloud.langfuse.com"
    assert normalize_host("") == "https://cloud.langfuse.com"
    assert normalize_host("https://us.cloud.langfuse.com/") == (
        "https://us.cloud.langfuse.com"
    )
    assert normalize_host("langfuse.internal") == "https://langfuse.internal"
    assert normalize_host("http://localhost:3000") == "http://localhost:3000"


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def test_paginate_follows_the_cursor_until_exhausted():
    client = FakeClient(
        [
            {"data": [{"id": "a"}], "meta": {"nextCursor": "cur-1"}},
            {"data": [{"id": "b"}], "meta": {"nextCursor": "cur-2"}},
            {"data": [{"id": "c"}], "meta": {"nextCursor": None}},
        ]
    )

    rows = paginate(client, "/api/public/v2/observations", {}, page_size=500)

    assert [r["id"] for r in rows] == ["a", "b", "c"]
    assert [call[1].get("cursor") for call in client.calls] == [None, "cur-1", "cur-2"]


def test_paginate_accepts_the_items_envelope_too():
    client = FakeClient([{"items": [{"id": "a"}], "nextCursor": None}])

    assert paginate(client, "/x", {}, page_size=10) == [{"id": "a"}]


def test_paginate_stops_at_the_page_cap():
    """A busy project must not spin forever on a server that always returns a cursor."""
    client = FakeClient(
        [{"data": [{"id": str(i)}], "meta": {"nextCursor": "next"}} for i in range(500)]
    )

    rows = paginate(client, "/x", {}, page_size=1)

    assert len(rows) == MAX_PAGES
    assert len(client.calls) == MAX_PAGES


def test_paginate_sends_the_page_size_as_limit():
    client = FakeClient([{"data": [], "meta": {}}])
    paginate(client, "/x", {"level": "ERROR"}, page_size=250)

    _, params = client.calls[0]
    assert params["limit"] == 250
    assert params["level"] == "ERROR"


# --------------------------------------------------------------------------
# Export orchestration
# --------------------------------------------------------------------------


def test_errors_only_uses_the_server_side_level_filter(tmp_path, monkeypatch):
    client = FakeClient([{"data": [{"id": "a", "level": "ERROR"}], "meta": {}}])
    monkeypatch.setattr(langfuse_export, "_client", lambda *a, **k: client)

    _, export = run_langfuse_export("pk:sk", tmp_path, capture={"errors"})

    assert client.calls[0][0] == "/api/public/v2/observations"
    assert client.calls[0][1]["level"] == "ERROR"
    assert export["summary"]["observation_count"] == 1


def test_latency_modes_sweep_unfiltered(tmp_path, monkeypatch):
    """Latency and cost are not server-side filterable, so the level filter is dropped."""
    client = FakeClient([{"data": [{"id": "a"}], "meta": {}}])
    monkeypatch.setattr(langfuse_export, "_client", lambda *a, **k: client)

    run_langfuse_export("pk:sk", tmp_path, capture={"errors", "slow"})

    assert "level" not in client.calls[0][1]


def test_score_modes_hydrate_the_traces_they_point_at(tmp_path, monkeypatch):
    client = FakeClient(
        [
            # scores page
            {
                "data": [{"id": "s1", "traceId": "trace-7", "value": 0.1}],
                "meta": {"nextCursor": None},
            },
            # hydrated observations for trace-7
            {"data": [{"id": "o1", "traceId": "trace-7"}], "meta": {}},
        ]
    )
    monkeypatch.setattr(langfuse_export, "_client", lambda *a, **k: client)

    _, export = run_langfuse_export("pk:sk", tmp_path, capture={"low_score"})

    paths = [call[0] for call in client.calls]
    assert "/api/public/v3/scores" in paths[0]
    assert client.calls[0][1]["valueMax"] == 0.5
    assert client.calls[1][1]["traceId"] == "trace-7"
    assert export["scores"][0]["capture_mode"] == "low_score"


def test_export_writes_a_replayable_file(tmp_path, monkeypatch):
    client = FakeClient([{"data": [{"id": "a", "level": "ERROR"}], "meta": {}}])
    monkeypatch.setattr(langfuse_export, "_client", lambda *a, **k: client)
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)

    path, export = run_langfuse_export(
        "pk:sk",
        tmp_path,
        host="https://us.cloud.langfuse.com",
        since=since,
        capture={"errors"},
        latency_ms=1234.0,
        cost_usd=5.0,
    )

    assert path.name == "langfuse_export.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["api_base"] == "https://us.cloud.langfuse.com"
    # Thresholds ride along so a --file replay reproduces the same mapping.
    assert on_disk["summary"]["latency_ms"] == 1234.0
    assert on_disk["summary"]["cost_usd"] == 5.0
    assert on_disk["summary"]["capture_modes"] == ["errors"]
    assert export["summary"]["from_time"] == since.isoformat()


def test_duplicate_observations_are_collapsed(tmp_path, monkeypatch):
    """Score hydration can re-fetch rows the error sweep already returned."""
    client = FakeClient(
        [
            {"data": [{"id": "dup"}, {"id": "dup"}, {"id": "other"}], "meta": {}},
        ]
    )
    monkeypatch.setattr(langfuse_export, "_client", lambda *a, **k: client)

    _, export = run_langfuse_export("pk:sk", tmp_path, capture={"errors"})

    assert [o["id"] for o in export["observations"]] == ["dup", "other"]


def test_unknown_capture_mode_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown capture mode"):
        run_langfuse_export("pk:sk", tmp_path, capture={"whoops"})
