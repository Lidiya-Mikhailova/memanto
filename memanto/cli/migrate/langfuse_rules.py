"""
Langfuse -> Memanto capture rules: filter, group, and shape.

This is the single source of truth for *which* Langfuse observations become
memories and *what* those memories look like. Both the one-shot sync
(``memanto migrate langfuse``, via ``mappers.map_langfuse``) and the live
callback in the ``langfuse-memanto`` integration package import from here, so
a memory looks identical no matter which path wrote it.

The central rule is **one memory per signature, not per occurrence**. Memanto
performs no deduplication on write (``memory_write_service.store_memory``), so
a single bad deploy would otherwise write thousands of near-identical
memories and drown recall. Observations are therefore grouped by a stable
signature — for errors, the exception class plus a message with volatile
parts (ids, numbers, paths, urls) normalized away; for the threshold modes,
the operation name — and each group produces one memory whose confidence
rises with the occurrence count.

Capture modes:
    errors      level=ERROR observations                  -> type "error"
    low_score   traces an eval scored below threshold     -> type "learning"
    slow        observations over the latency budget      -> type "observation"
    costly      observations over the cost budget         -> type "observation"
    success     traces an eval scored at/above threshold  -> type "learning"
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Footer/title helpers are shared with the other providers' mappers so the
# `[Supporting data]` convention and the content cap stay in one place.
# `mappers` imports this module lazily inside `map_langfuse`, so importing it
# here at module level does not create a cycle.
from memanto.cli.migrate.mappers import (
    _attach_footer,
    _format_supporting_data,
    _parse_dt,
    _title_from,
)

CAPTURE_MODES = ("errors", "low_score", "slow", "costly", "success")

# Most actionable first: an observation that both errored and ran slow is an
# error, not a latency anomaly.
_MODE_PRIORITY = ("errors", "low_score", "costly", "slow", "success")

_MODE_TO_MEMORY_TYPE = {
    "errors": "error",
    "low_score": "learning",
    "slow": "observation",
    "costly": "observation",
    "success": "learning",
}

_MAX_MESSAGE_CHARS = 400
_MAX_DETAIL_CHARS = 1200
_MAX_NORMALIZED_CHARS = 160
_MAX_SAMPLE_TRACES = 3
_MAX_TAGS = 20
_TAG_MAX_CHARS = 64

_ERROR_CLASS_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Failure|Timeout|Fault))\b"
)

# Volatile fragments stripped before hashing, so the same fault recorded a
# thousand times with different ids and durations collapses to one signature.
_URL_RE = re.compile(r"https?://\S+")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b")
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.\-]+){2,}")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{8,}\b")
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
_WS_RE = re.compile(r"\s+")

# Moorcheh stores tags comma-joined (`core.MemoryRecord.to_moorcheh_document`),
# so a comma inside a tag corrupts filtering. Keep tags to a safe charset.
_TAG_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._=-]+")


@dataclass(frozen=True)
class CaptureConfig:
    """Which signals to capture and at what thresholds."""

    modes: frozenset[str] = frozenset({"errors"})
    score_threshold: float = 0.5
    latency_ms: float = 30_000.0
    cost_usd: float = 1.0

    def __post_init__(self) -> None:
        unknown = set(self.modes) - set(CAPTURE_MODES)
        if unknown:
            raise ValueError(
                f"Unknown capture mode(s): {sorted(unknown)}. "
                f"Valid: {', '.join(CAPTURE_MODES)}"
            )
        if not self.modes:
            raise ValueError("At least one capture mode is required.")


def parse_capture_modes(values: Iterable[str] | None) -> frozenset[str]:
    """Normalize user-supplied mode names, accepting ``low-score`` for ``low_score``.

    Shared by the CLI flag and the UI tile so the two can't drift; each
    caller renders the raised ``ValueError`` in its own idiom.
    """
    modes = {
        part.strip().lower().replace("-", "_")
        for value in (values or ["errors"])
        for part in str(value).split(",")
        if part.strip()
    }
    if not modes:
        raise ValueError("At least one capture mode is required.")
    unknown = modes - set(CAPTURE_MODES)
    if unknown:
        raise ValueError(
            f"Unknown capture mode(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(m.replace('_', '-') for m in CAPTURE_MODES)}"
        )
    return frozenset(modes)


@dataclass
class SignatureGroup:
    """One distinct failure/anomaly, plus every occurrence folded into it."""

    signature: str
    mode: str
    name: str
    label: str
    message: str = ""
    detail: str = ""
    count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    trace_ids: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    environments: list[str] = field(default_factory=list)
    total_cost: float = 0.0
    max_latency_ms: float = 0.0
    score_names: list[str] = field(default_factory=list)
    score_values: list[float] = field(default_factory=list)
    project_id: str | None = None


# --------------------------------------------------------------------------
# Field extraction
# --------------------------------------------------------------------------


def _as_text(value: Any) -> str:
    """Flatten an observation input/output field to searchable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def error_text(obs: dict[str, Any]) -> str:
    """The most error-bearing text on an observation."""
    for key in ("statusMessage", "output", "input"):
        text = _as_text(obs.get(key))
        if text:
            return text
    return ""


def error_class(text: str) -> str | None:
    """Extract an exception class name (``RateLimitError``) from *text*."""
    match = _ERROR_CLASS_RE.search(text or "")
    return match.group(1) if match else None


def normalize_message(text: str) -> str:
    """Strip volatile fragments so repeat occurrences hash identically."""
    normalized = text or ""
    normalized = _URL_RE.sub("<url>", normalized)
    normalized = _UUID_RE.sub("<uuid>", normalized)
    normalized = _PATH_RE.sub("<path>", normalized)
    normalized = _HEX_RE.sub("<hex>", normalized)
    normalized = _QUOTED_RE.sub("<str>", normalized)
    normalized = _NUM_RE.sub("<n>", normalized)
    normalized = _WS_RE.sub(" ", normalized).strip()
    return normalized[:_MAX_NORMALIZED_CHARS]


def latency_ms(obs: dict[str, Any]) -> float:
    """Observation duration in milliseconds.

    Derived from ``startTime``/``endTime`` when both are present, which is
    unambiguous. The ``latency`` field is only a fallback and is read as
    seconds, matching Langfuse's documented unit for observation latency.
    """
    start = _parse_dt(obs.get("startTime"))
    end = _parse_dt(obs.get("endTime"))
    if start and end and end >= start:
        return (end - start).total_seconds() * 1000.0

    raw = obs.get("latency")
    if isinstance(raw, (int, float)):
        return float(raw) * 1000.0
    return 0.0


def cost_usd(obs: dict[str, Any]) -> float:
    """Total cost of an observation in USD."""
    details = obs.get("costDetails")
    if isinstance(details, dict):
        total = details.get("total")
        if isinstance(total, (int, float)):
            return float(total)
        return float(
            sum(v for v in details.values() if isinstance(v, (int, float)))
        )
    for key in ("calculatedTotalCost", "totalCost"):
        raw = obs.get(key)
        if isinstance(raw, (int, float)):
            return float(raw)
    return 0.0


def _model_of(obs: dict[str, Any]) -> str | None:
    for key in ("providedModelName", "model"):
        value = obs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _operation_name(obs: dict[str, Any]) -> str:
    for key in ("name", "traceName", "type"):
        value = obs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


# --------------------------------------------------------------------------
# Classification and signatures
# --------------------------------------------------------------------------


def score_modes_by_trace(
    scores: list[dict[str, Any]], config: CaptureConfig
) -> dict[str, dict[str, Any]]:
    """Map traceId -> the score that qualified it for capture.

    The exporter tags each score row with the ``capture_mode`` it was fetched
    for; rows loaded from an older export file are re-derived from the
    threshold so ``--file`` replays keep working.
    """
    by_trace: dict[str, dict[str, Any]] = {}
    for score in scores:
        trace_id = score.get("traceId")
        if not isinstance(trace_id, str) or not trace_id:
            continue

        mode = score.get("capture_mode")
        value = score.get("value")
        if mode not in ("low_score", "success"):
            if not isinstance(value, (int, float)):
                continue
            mode = "low_score" if value < config.score_threshold else "success"
        if mode not in config.modes:
            continue

        # An errored-and-scored trace keeps the higher-priority mode.
        existing = by_trace.get(trace_id)
        if existing and _MODE_PRIORITY.index(
            str(existing.get("capture_mode"))
        ) <= _MODE_PRIORITY.index(str(mode)):
            continue
        by_trace[trace_id] = {**score, "capture_mode": mode}
    return by_trace


def classify(
    obs: dict[str, Any],
    config: CaptureConfig,
    trace_scores: dict[str, dict[str, Any]] | None = None,
) -> str | None:
    """Return the capture mode an observation qualifies for, or ``None``."""
    trace_scores = trace_scores or {}

    if "errors" in config.modes:
        if str(obs.get("level") or "").upper() == "ERROR":
            return "errors"

    trace_id = obs.get("traceId")
    scored = trace_scores.get(trace_id) if isinstance(trace_id, str) else None
    scored_mode = str(scored.get("capture_mode")) if scored else None
    if scored_mode == "low_score":
        return "low_score"

    if "costly" in config.modes and cost_usd(obs) > config.cost_usd:
        return "costly"
    if "slow" in config.modes and latency_ms(obs) > config.latency_ms:
        return "slow"

    if scored_mode == "success":
        return "success"
    return None


def signature_for(obs: dict[str, Any], mode: str) -> tuple[str, str]:
    """Return ``(signature, label)`` for an observation in *mode*.

    Errors group by exception class + normalized message, so distinct root
    causes stay distinct. Threshold modes group by operation, since the
    interesting unit there is "this step is slow/expensive", not the payload.
    """
    name = _operation_name(obs)

    if mode == "errors":
        text = error_text(obs)
        label = error_class(text) or "UnclassifiedError"
        key = f"{mode}|{name}|{label}|{normalize_message(text)}"
    else:
        label = _model_of(obs) or name
        key = f"{mode}|{name}|{label}"

    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return digest, label


def _remember(bucket: list[str], value: str | None, limit: int = 5) -> None:
    if value and value not in bucket and len(bucket) < limit:
        bucket.append(value)


def group_observations(
    observations: list[dict[str, Any]],
    config: CaptureConfig,
    scores: list[dict[str, Any]] | None = None,
) -> list[SignatureGroup]:
    """Fold observations into one :class:`SignatureGroup` per distinct signal."""
    trace_scores = score_modes_by_trace(scores or [], config)
    groups: dict[str, SignatureGroup] = {}

    for obs in observations:
        if not isinstance(obs, dict):
            continue
        mode = classify(obs, config, trace_scores)
        if mode is None:
            continue

        signature, label = signature_for(obs, mode)
        group = groups.get(signature)
        if group is None:
            group = SignatureGroup(
                signature=signature,
                mode=mode,
                name=_operation_name(obs),
                label=label,
            )
            groups[signature] = group

        group.count += 1

        start = _parse_dt(obs.get("startTime"))
        if start:
            if group.first_seen is None or start < group.first_seen:
                group.first_seen = start
            if group.last_seen is None or start > group.last_seen:
                group.last_seen = start

        _remember(group.trace_ids, obs.get("traceId"), _MAX_SAMPLE_TRACES)
        _remember(group.models, _model_of(obs))
        _remember(group.environments, obs.get("environment"))

        group.total_cost += cost_usd(obs)
        group.max_latency_ms = max(group.max_latency_ms, latency_ms(obs))
        if group.project_id is None and isinstance(obs.get("projectId"), str):
            group.project_id = obs["projectId"]

        # Keep the first non-empty message/detail as the representative sample.
        if not group.message:
            group.message = _as_text(obs.get("statusMessage"))[:_MAX_MESSAGE_CHARS]
        if not group.detail:
            group.detail = _as_text(obs.get("output"))[:_MAX_DETAIL_CHARS]

        trace_id = obs.get("traceId")
        scored = trace_scores.get(trace_id) if isinstance(trace_id, str) else None
        if scored:
            _remember(group.score_names, scored.get("name"))
            value = scored.get("value")
            if isinstance(value, (int, float)) and len(group.score_values) < 5:
                group.score_values.append(round(float(value), 4))

    # Loudest first, so a truncated preview shows what matters.
    return sorted(groups.values(), key=lambda g: g.count, reverse=True)


# --------------------------------------------------------------------------
# Memory payload
# --------------------------------------------------------------------------


def confidence_for(count: int) -> float:
    """Confidence rises with recurrence: 1x -> 0.60, 10x -> 0.75, 100x -> 0.90."""
    return round(min(0.95, 0.60 + 0.15 * math.log10(max(count, 1))), 2)


def _tag(raw: Any) -> str | None:
    text = _TAG_UNSAFE_RE.sub("-", str(raw or "").strip()).strip("-")
    return text[:_TAG_MAX_CHARS] or None


def trace_url(host: str, group: SignatureGroup) -> str | None:
    """Canonical Langfuse URL for a representative trace of this group."""
    if not group.trace_ids:
        return None
    base = (host or "").rstrip("/")
    trace_id = group.trace_ids[0]
    if group.project_id:
        return f"{base}/project/{group.project_id}/traces/{trace_id}"[:512]
    return f"{base}/trace/{trace_id}"[:512]


def _headline(group: SignatureGroup) -> str:
    plural = "s" if group.count != 1 else ""
    if group.mode == "errors":
        return (
            f"Langfuse recorded {group.count} failing '{group.name}' "
            f"observation{plural} with {group.label}."
        )
    if group.mode == "low_score":
        return (
            f"'{group.name}' was scored below threshold in {group.count} "
            f"observation{plural}."
        )
    if group.mode == "costly":
        return (
            f"'{group.name}' exceeded the cost budget in {group.count} "
            f"observation{plural} (${group.total_cost:.4f} total)."
        )
    if group.mode == "slow":
        return (
            f"'{group.name}' exceeded the latency budget in {group.count} "
            f"observation{plural} (peak {group.max_latency_ms:.0f} ms)."
        )
    return (
        f"'{group.name}' scored at or above threshold in {group.count} "
        f"observation{plural}."
    )


def to_memory_payload(group: SignatureGroup, host: str) -> dict[str, Any]:
    """Shape one group into a ``SdkClient.batch_remember`` item.

    ``signature`` and ``occurrences`` ride along for reconciliation and for
    the dry-run preview; ``batch_remember`` reads only the keys it knows, so
    the extras are inert on write.
    """
    body = [_headline(group)]
    if group.message:
        body.append(group.message)

    if group.first_seen and group.last_seen and group.count > 1:
        body.append(
            f"Seen {group.count}x between {group.first_seen.isoformat()} "
            f"and {group.last_seen.isoformat()}."
        )

    if group.detail:
        body.append(f"Representative output:\n{group.detail}")

    footer = _format_supporting_data(
        [
            ("Signature", f"langfuse:{group.signature}"),
            ("Capture mode", group.mode),
            ("Operation", group.name),
            ("Occurrences", group.count),
            ("Models", group.models),
            ("Environments", group.environments),
            ("Peak latency (ms)", round(group.max_latency_ms) or None),
            ("Total cost (USD)", round(group.total_cost, 6) or None),
            ("Scores", group.score_names),
            ("Score values", group.score_values),
            ("Sample traces", group.trace_ids),
            (
                "First seen",
                group.first_seen.isoformat() if group.first_seen else None,
            ),
            ("Last seen", group.last_seen.isoformat() if group.last_seen else None),
        ]
    )

    content = _attach_footer("\n\n".join(part for part in body if part), footer)

    tags: list[str] = []
    for raw in (
        "langfuse",
        f"capture={group.mode}",
        f"sig={group.signature}",
        f"op={group.name}",
    ):
        tag = _tag(raw)
        if tag:
            tags.append(tag)
    for model in group.models[:2]:
        tag = _tag(f"model={model}")
        if tag:
            tags.append(tag)
    for env in group.environments[:2]:
        tag = _tag(f"env={env}")
        if tag:
            tags.append(tag)

    return {
        "title": _title_from(f"{group.label} in {group.name}"),
        "content": content,
        "type": _MODE_TO_MEMORY_TYPE[group.mode],
        "tags": list(dict.fromkeys(tags))[:_MAX_TAGS],
        "confidence": confidence_for(group.count),
        "source": "langfuse",
        "source_ref": trace_url(host, group),
        "provenance": "imported",
        "created_at": group.first_seen,
        "updated_at": datetime.now(timezone.utc),
        "signature": group.signature,
        "occurrences": group.count,
    }


def build_rows(
    export: dict[str, Any], config: CaptureConfig | None = None
) -> list[dict[str, Any]]:
    """Map a Langfuse export dict onto grouped Memanto memory payloads."""
    summary = export.get("summary") or {}
    if config is None:
        config = CaptureConfig(
            modes=frozenset(summary.get("capture_modes") or {"errors"}),
            score_threshold=float(summary.get("score_threshold", 0.5)),
            latency_ms=float(summary.get("latency_ms", 30_000.0)),
            cost_usd=float(summary.get("cost_usd", 1.0)),
        )

    host = str(export.get("api_base") or "https://cloud.langfuse.com")
    groups = group_observations(
        export.get("observations") or [],
        config,
        export.get("scores") or [],
    )
    return [to_memory_payload(group, host) for group in groups]
