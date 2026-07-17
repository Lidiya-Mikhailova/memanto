"""
Regression test for MemoryReadService._build_filtered_query.

When the caller supplies an empty/whitespace query together with filters, the
method returns a string that *starts* with a space (e.g. " #memory_type:fact").
That leading whitespace is meaningless and can confuse downstream Moorcheh
query parsing. The query should be trimmed / joined cleanly.
"""

from memanto.app.services.memory_read_service import MemoryReadService


def test_empty_query_with_filters_has_no_leading_space():
    svc = MemoryReadService.__new__(MemoryReadService)
    q = svc._build_filtered_query(query="", type=["fact"], tags=["prod"])
    assert not q.startswith(" "), f"unexpected leading space in query: {q!r}"
    assert q == "#memory_type:fact #prod"
