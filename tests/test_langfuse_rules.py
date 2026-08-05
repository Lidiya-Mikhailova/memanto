"""Tests for Langfuse capture rules: classification, signatures, payloads."""

from __future__ import annotations

from memanto.cli.migrate.langfuse_rules import (
    CaptureConfig,
    build_rows,
    classify,
    confidence_for,
    cost_usd,
    error_class,
    group_observations,
    latency_ms,
    normalize_message,
    signature_for,
    to_memory_payload,
)

ALL_MODES = CaptureConfig(
    modes=frozenset({"errors", "low_score", "slow", "costly", "success"})
)


def observation(**overrides):
    """A minimal errored observation; override any field."""
    base = {
        "id": "obs-1",
        "traceId": "trace-1",
        "projectId": "proj-1",
        "name": "summarize_node",
        "type": "GENERATION",
        "level": "ERROR",
        "statusMessage": "RateLimitError: quota exceeded",
        "startTime": "2026-08-01T12:00:00Z",
        "endTime": "2026-08-01T12:00:01Z",
        "providedModelName": "claude-opus-5",
        "environment": "production",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Normalization and signatures
# --------------------------------------------------------------------------


def test_normalize_message_strips_volatile_fragments():
    text = (
        "Request 1a2b3c4d-1111-2222-3333-444455556666 to https://api.example.com/v1 "
        "failed at /var/log/app/run.log after 42 attempts (0xdeadbeef)"
    )
    normalized = normalize_message(text)

    assert "1a2b3c4d" not in normalized
    assert "https://" not in normalized
    assert "42" not in normalized
    assert "deadbeef" not in normalized
    assert "failed at" in normalized


def test_same_fault_with_different_ids_shares_one_signature():
    """The whole point of grouping: volatile detail must not fork the signature."""
    first = observation(
        statusMessage=(
            "RateLimitError: quota exceeded for request "
            "1a2b3c4d-1111-2222-3333-444455556666 after 30 attempts"
        )
    )
    second = observation(
        id="obs-2",
        traceId="trace-2",
        statusMessage=(
            "RateLimitError: quota exceeded for request "
            "9f8e7d6c-9999-8888-7777-666655554444 after 77 attempts"
        ),
    )

    assert signature_for(first, "errors") == signature_for(second, "errors")


def test_distinct_error_classes_get_distinct_signatures():
    rate_limit = observation(statusMessage="RateLimitError: quota exceeded")
    timeout = observation(statusMessage="TimeoutError: upstream did not respond")

    assert signature_for(rate_limit, "errors")[0] != signature_for(timeout, "errors")[0]


def test_same_error_in_different_operations_stays_separate():
    here = observation(name="summarize_node")
    there = observation(name="retrieve_node")

    assert signature_for(here, "errors")[0] != signature_for(there, "errors")[0]


def test_error_class_extraction():
    assert error_class("RateLimitError: nope") == "RateLimitError"
    assert error_class("raised ValueError somewhere") == "ValueError"
    assert error_class("everything is fine") is None


def test_unparseable_error_still_gets_a_label():
    _, label = signature_for(observation(statusMessage="it just broke"), "errors")
    assert label == "UnclassifiedError"


# --------------------------------------------------------------------------
# Derived metrics
# --------------------------------------------------------------------------


def test_latency_prefers_timestamps_over_the_latency_field():
    obs = observation(
        startTime="2026-08-01T12:00:00Z",
        endTime="2026-08-01T12:00:45Z",
        latency=999,
    )
    assert latency_ms(obs) == 45_000.0


def test_latency_falls_back_to_seconds_field():
    obs = observation(startTime=None, endTime=None, latency=2.5)
    assert latency_ms(obs) == 2_500.0


def test_cost_reads_total_then_sums_then_falls_back():
    assert cost_usd(observation(costDetails={"total": 1.25, "input": 1.0})) == 1.25
    assert cost_usd(observation(costDetails={"input": 0.5, "output": 0.25})) == 0.75
    assert cost_usd(observation(calculatedTotalCost=3.0)) == 3.0
    assert cost_usd(observation()) == 0.0


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_error_level_classifies_as_errors():
    assert classify(observation(), ALL_MODES) == "errors"


def test_non_error_observation_is_ignored_when_only_errors_captured():
    config = CaptureConfig(modes=frozenset({"errors"}))
    assert classify(observation(level="DEFAULT"), config) is None


def test_slow_and_costly_thresholds():
    config = CaptureConfig(
        modes=frozenset({"slow", "costly"}), latency_ms=30_000, cost_usd=1.0
    )
    slow = observation(
        level="DEFAULT",
        startTime="2026-08-01T12:00:00Z",
        endTime="2026-08-01T12:00:45Z",
    )
    costly = observation(level="DEFAULT", costDetails={"total": 2.5})
    cheap_and_fast = observation(level="DEFAULT")

    assert classify(slow, config) == "slow"
    assert classify(costly, config) == "costly"
    assert classify(cheap_and_fast, config) is None


def test_errors_outrank_slow_for_the_same_observation():
    """An errored slow call is an error, not a latency anomaly."""
    config = CaptureConfig(modes=frozenset({"errors", "slow"}), latency_ms=1_000)
    both = observation(
        startTime="2026-08-01T12:00:00Z", endTime="2026-08-01T12:00:45Z"
    )
    assert classify(both, config) == "errors"


def test_scores_drive_low_score_and_success():
    config = CaptureConfig(modes=frozenset({"low_score", "success"}))
    scores = [
        {"traceId": "trace-1", "name": "correctness", "value": 0.2},
        {"traceId": "trace-9", "name": "correctness", "value": 0.9},
    ]
    from memanto.cli.migrate.langfuse_rules import score_modes_by_trace

    by_trace = score_modes_by_trace(scores, config)

    assert classify(observation(level="DEFAULT"), config, by_trace) == "low_score"
    assert (
        classify(observation(level="DEFAULT", traceId="trace-9"), config, by_trace)
        == "success"
    )


def test_thresholds_are_not_captured_when_mode_is_off():
    config = CaptureConfig(modes=frozenset({"errors"}), latency_ms=1)
    slow_but_fine = observation(
        level="DEFAULT",
        startTime="2026-08-01T12:00:00Z",
        endTime="2026-08-01T12:00:45Z",
    )
    assert classify(slow_but_fine, config) is None


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------


def test_grouping_collapses_many_occurrences_into_one_group():
    observations = [
        observation(
            id=f"obs-{i}",
            traceId=f"trace-{i}",
            statusMessage=f"RateLimitError: quota exceeded on attempt {i}",
            startTime=f"2026-08-01T12:{i:02d}:00Z",
        )
        for i in range(50)
    ]

    groups = group_observations(observations, ALL_MODES)

    assert len(groups) == 1
    assert groups[0].count == 50
    assert len(groups[0].trace_ids) == 3  # capped sample, not all 50
    assert groups[0].first_seen is not None
    assert groups[0].last_seen is not None
    assert groups[0].first_seen < groups[0].last_seen


def test_groups_are_ordered_loudest_first():
    observations = [observation(id="a", statusMessage="TimeoutError: slow")] + [
        observation(id=f"b{i}", statusMessage="RateLimitError: quota exceeded")
        for i in range(5)
    ]
    groups = group_observations(observations, ALL_MODES)

    assert [g.count for g in groups] == [5, 1]


def test_unclassified_observations_are_dropped():
    config = CaptureConfig(modes=frozenset({"errors"}))
    assert group_observations([observation(level="DEFAULT")], config) == []


# --------------------------------------------------------------------------
# Payload shaping
# --------------------------------------------------------------------------


def test_confidence_rises_with_recurrence_and_is_capped():
    assert confidence_for(1) == 0.60
    assert confidence_for(10) == 0.75
    assert confidence_for(100) == 0.90
    assert confidence_for(10_000) == 0.95
    assert confidence_for(0) == 0.60


def test_payload_respects_every_memanto_schema_cap():
    observations = [
        observation(
            id=f"obs-{i}",
            name="a,b,c node with spaces",  # commas and spaces must not reach tags
            statusMessage="RateLimitError: " + ("x" * 5_000),
            output={"trace": "y" * 50_000},
        )
        for i in range(3)
    ]
    group = group_observations(observations, ALL_MODES)[0]
    payload = to_memory_payload(group, "https://cloud.langfuse.com")

    assert len(payload["title"]) <= 100
    assert len(payload["content"]) <= 10_000
    assert len(payload["tags"]) <= 20
    assert all("," not in tag for tag in payload["tags"])
    assert all(len(tag) <= 64 for tag in payload["tags"])
    assert len(payload["source_ref"]) <= 512
    assert 0.0 <= payload["confidence"] <= 1.0


def test_payload_uses_langfuse_source_and_imported_provenance():
    group = group_observations([observation()], ALL_MODES)[0]
    payload = to_memory_payload(group, "https://cloud.langfuse.com")

    # `source` is deliberately open (constants.SourceType) and names the writer,
    # matching how map_mem0 stamps "mem0". `imported` is the only provenance
    # that preserves the source timestamps.
    assert payload["source"] == "langfuse"
    assert payload["provenance"] == "imported"
    assert payload["type"] == "error"
    assert payload["created_at"] == group.first_seen


def test_payload_links_back_to_the_langfuse_trace():
    group = group_observations([observation()], ALL_MODES)[0]
    payload = to_memory_payload(group, "https://cloud.langfuse.com")

    assert payload["source_ref"] == (
        "https://cloud.langfuse.com/project/proj-1/traces/trace-1"
    )


def test_payload_carries_signature_and_count_for_reconciliation():
    group = group_observations([observation(), observation(id="obs-2")], ALL_MODES)[0]
    payload = to_memory_payload(group, "https://cloud.langfuse.com")

    assert payload["signature"] == group.signature
    assert payload["occurrences"] == 2


def test_capture_mode_maps_to_memory_type():
    config = CaptureConfig(modes=frozenset({"slow"}), latency_ms=1_000)
    slow = observation(
        level="DEFAULT",
        startTime="2026-08-01T12:00:00Z",
        endTime="2026-08-01T12:00:45Z",
    )
    group = group_observations([slow], config)[0]

    assert to_memory_payload(group, "https://cloud.langfuse.com")["type"] == (
        "observation"
    )


# --------------------------------------------------------------------------
# End-to-end mapping
# --------------------------------------------------------------------------


def test_build_rows_reads_capture_config_from_the_export_summary():
    export = {
        "api_base": "https://self-hosted.example.com",
        "summary": {"capture_modes": ["errors"], "score_threshold": 0.5},
        "observations": [
            observation(id="a"),
            observation(id="b", level="DEFAULT"),  # not captured
        ],
        "scores": [],
    }

    rows = build_rows(export)

    assert len(rows) == 1
    assert rows[0]["source_ref"].startswith("https://self-hosted.example.com")


def test_build_rows_on_an_empty_export():
    assert build_rows({"observations": [], "scores": [], "summary": {}}) == []
