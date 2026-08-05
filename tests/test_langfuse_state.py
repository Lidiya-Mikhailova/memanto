"""Tests for the Langfuse sync ledger — the thing that makes re-syncing safe.

Memanto does not deduplicate on write, so without this ledger a second
``memanto migrate langfuse`` (or a second click on the UI tile) would write
every signature again.
"""

from __future__ import annotations

import json

from memanto.cli.migrate.langfuse_state import (
    Reconciliation,
    fingerprint,
    last_synced_at,
    load_state,
    reconcile,
    record_updated,
    record_written,
    save_state,
    state_path,
)


def row(signature="sig-a", content="RateLimitError happened", occurrences=1, **extra):
    base = {
        "title": "RateLimitError in summarize_node",
        "content": content,
        "type": "error",
        "tags": ["langfuse", f"sig={signature}"],
        "confidence": 0.6,
        "signature": signature,
        "occurrences": occurrences,
    }
    base.update(extra)
    return base


def ok(memory_id):
    """A successful per-item result from batch_store_memories."""
    return {"id": memory_id, "status": "queued"}


def empty_state():
    return {"version": 1, "last_synced_at": None, "signatures": {}}


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------


def test_fingerprint_ignores_irrelevant_fields_and_tag_order():
    a = row(occurrences=1, source_ref="https://x/1")
    b = row(occurrences=999, source_ref="https://x/2")
    b["tags"] = list(reversed(b["tags"]))

    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_changes_when_the_memory_would_read_differently():
    assert fingerprint(row()) != fingerprint(row(content="TimeoutError happened"))
    assert fingerprint(row()) != fingerprint(row(confidence=0.9))


# --------------------------------------------------------------------------
# Reconciliation — the idempotency contract
# --------------------------------------------------------------------------


def test_first_sync_treats_every_signature_as_new():
    plan = reconcile([row("sig-a"), row("sig-b")], empty_state())

    assert len(plan.new_rows) == 2
    assert plan.updates == []
    assert plan.unchanged == 0


def test_second_sync_of_identical_data_writes_nothing():
    """The critical test: syncing twice must not duplicate memories."""
    rows = [row("sig-a"), row("sig-b")]
    state = empty_state()

    first = reconcile(rows, state)
    record_written(state, first.new_rows, [ok("mem-a"), ok("mem-b")])

    second = reconcile(rows, state)

    assert second.new_rows == []
    assert second.updates == []
    assert second.unchanged == 2


def test_recurring_signature_is_updated_in_place_not_rewritten():
    state = empty_state()
    first = reconcile([row("sig-a", occurrences=1)], state)
    record_written(state, first.new_rows, [ok("mem-a")])

    louder = row("sig-a", content="RateLimitError happened 400x", occurrences=400)
    second = reconcile([louder], state)

    assert second.new_rows == []
    assert second.unchanged == 0
    assert len(second.updates) == 1

    update = second.updates[0]
    assert update["memory_id"] == "mem-a"
    assert update["occurrences"] == 400
    assert set(update["updates"]) <= {"title", "content", "confidence", "tags", "type"}


def test_updating_the_ledger_settles_the_signature():
    state = empty_state()
    first = reconcile([row("sig-a")], state)
    record_written(state, first.new_rows, [ok("mem-a")])

    louder = row("sig-a", content="now louder", occurrences=9)
    plan = reconcile([louder], state)
    record_updated(state, plan.updates[0])

    assert reconcile([louder], state).unchanged == 1
    assert state["signatures"]["sig-a"]["occurrences"] == 9


def test_failed_writes_are_not_recorded_so_the_next_sync_retries():
    state = empty_state()
    rows = [row("sig-a"), row("sig-b")]
    plan = reconcile(rows, state)

    record_written(
        state,
        plan.new_rows,
        [ok("mem-a"), {"id": "mem-b", "status": "failed", "error": "boom"}],
    )

    retry = reconcile(rows, state)
    assert [r["signature"] for r in retry.new_rows] == ["sig-b"]
    assert retry.unchanged == 1


def test_rows_without_a_signature_are_always_written():
    plan = reconcile([row(signature=None)], empty_state())
    assert len(plan.new_rows) == 1


def test_reconciliation_total_accounts_for_every_row():
    state = empty_state()
    rows = [row("sig-a"), row("sig-b"), row("sig-c")]
    first = reconcile(rows, state)
    record_written(state, first.new_rows, [ok("m-a"), ok("m-b"), ok("m-c")])

    rows[0] = row("sig-a", content="changed", occurrences=5)
    plan: Reconciliation = reconcile(rows, state)

    assert plan.total == 3
    assert (len(plan.new_rows), len(plan.updates), plan.unchanged) == (0, 1, 2)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_state_round_trips_through_disk(tmp_path):
    path = state_path(tmp_path)
    state = empty_state()
    plan = reconcile([row("sig-a")], state)
    record_written(state, plan.new_rows, [ok("mem-a")])
    save_state(path, state)

    reloaded = load_state(path)

    assert reloaded["signatures"]["sig-a"]["memory_id"] == "mem-a"
    assert last_synced_at(reloaded) is not None
    assert reconcile([row("sig-a")], reloaded).unchanged == 1


def test_missing_ledger_starts_empty(tmp_path):
    state = load_state(state_path(tmp_path / "never-synced"))

    assert state["signatures"] == {}
    assert state["last_synced_at"] is None


def test_corrupt_ledger_does_not_block_a_sync(tmp_path):
    """Starting over re-writes memories, which is visible; refusing to sync isn't."""
    path = state_path(tmp_path)
    path.write_text("{ this is not json", encoding="utf-8")

    state = load_state(path)

    assert state["signatures"] == {}


def test_save_state_stamps_the_cursor(tmp_path):
    path = state_path(tmp_path)
    save_state(path, empty_state())

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["last_synced_at"] is not None
    assert last_synced_at(written).tzinfo is not None


def test_last_synced_at_tolerates_junk():
    assert last_synced_at({"last_synced_at": None}) is None
    assert last_synced_at({"last_synced_at": "not-a-date"}) is None
    assert last_synced_at({"last_synced_at": "2026-08-01T12:00:00Z"}) is not None
