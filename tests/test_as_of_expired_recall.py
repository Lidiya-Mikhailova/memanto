"""Regression tests for point-in-time (``search_as_of``) recall.

These guard against timeline amnesia: memories that were valid at the target
``as_of`` date but have *since* expired must still be recalled, because the
query asks "what was true at this point in time?", not "what is true now?".
"""

from memanto.app.services.memory_read_service import MemoryReadService


def _memory(memory_id: str, created_at: str, expires_at: str | None = None) -> dict:
    m = {
        "id": memory_id,
        "text": f"[FACT] {memory_id}\n\nStored memory",
        "memory_type": "fact",
        "agent_id": "agent-1",
        "actor_id": "agent-1",
        "source": "user",
        "confidence": 0.9,
        "status": "active",
        "created_at": created_at,
        "updated_at": created_at,
    }
    if expires_at:
        m["expires_at"] = expires_at
    return m


class _FakeDocuments:
    def fetch_text_data(self, **kwargs):
        return {
            "items": [
                # Created before as_of, valid at as_of, but expired *after* as_of
                # and before "now" -> must still be recalled by search_as_of.
                _memory(
                    "temporal-fact",
                    "2026-01-10T09:00:00Z",
                    expires_at="2026-06-01T00:00:00Z",
                ),
                # Created before as_of, never expires -> always recalled.
                _memory("permanent-fact", "2026-01-12T09:00:00Z"),
                # Already expired *before* as_of -> must be excluded.
                _memory(
                    "stale-fact",
                    "2026-01-05T09:00:00Z",
                    expires_at="2026-01-08T00:00:00Z",
                ),
            ],
            "pagination": {"has_more": False},
        }


class _FakeClient:
    documents = _FakeDocuments()


def test_as_of_recalls_memory_that_has_since_expired():
    service = MemoryReadService(_FakeClient())

    result = service.search_as_of(
        as_of_date="2026-01-15", agent_id="agent-1", limit=None
    )
    ids = {m["id"] for m in result["results"]}

    assert "temporal-fact" in ids, "timeline amnesia: since-expired memory lost"
    assert "permanent-fact" in ids
    assert "stale-fact" not in ids, "memory expired before as_of should be excluded"
