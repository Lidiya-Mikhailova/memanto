"""Unit tests for the live Mem0 export transport."""

from __future__ import annotations

from typing import Any

from memanto.cli.analyze.mem0_export import paginate_memories


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self.text = ""
        self.content = b"{}"
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = iter(responses)
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        path: str,
        *,
        params: dict[str, Any],
        json: dict[str, Any],
    ) -> _FakeResponse:
        self.calls.append({"path": path, "params": params, "json": json})
        return _FakeResponse(next(self._responses))


def test_paginate_memories_uses_v3_filter_body_for_every_page() -> None:
    client = _FakeClient(
        [
            {
                "count": 3,
                "next": "https://api.mem0.ai/v3/memories/?page=2&page_size=2",
                "results": [{"id": "m1"}, {"id": "m2"}],
            },
            {
                "count": 3,
                "next": None,
                "results": [{"id": "m3"}],
            },
        ]
    )

    memories, pagination = paginate_memories(
        client,  # type: ignore[arg-type]
        {"user_id": "alice"},
        page_size=2,
    )

    assert [memory["id"] for memory in memories] == ["m1", "m2", "m3"]
    assert pagination["count"] == 3
    assert client.calls == [
        {
            "path": "/v3/memories/",
            "params": {"page": 1, "page_size": 2},
            "json": {"filters": {"user_id": "alice"}},
        },
        {
            "path": "/v3/memories/",
            "params": {"page": 2, "page_size": 2},
            "json": {"filters": {"user_id": "alice"}},
        },
    ]
