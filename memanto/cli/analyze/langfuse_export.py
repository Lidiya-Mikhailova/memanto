"""
Export Langfuse observability signal to JSON (observations and scores).

Used by ``memanto migrate langfuse``. Pure ``httpx`` — no Langfuse SDK
dependency, so users don't have to install ``langfuse`` and we don't break
when the SDK ships a new major version.

Endpoints (Langfuse public API — https://langfuse.com/docs/api-and-data-platform):
    GET  /api/public/v2/observations?fromStartTime=&toStartTime=&cursor=&level=
    GET  /api/public/v3/scores?fromTimestamp=&cursor=&valueMin=&valueMax=

Both ``GET /api/public/traces`` and the v1 ``/api/public/observations`` are
deprecated: traces are reconstructed by grouping v2 observation rows on
``traceId``. v2 uses opaque ``cursor`` pagination (not ``page``) and
``fromStartTime``/``toStartTime`` (not ``fromTimestamp``).

Auth: HTTP Basic with the public key as username and the secret key as
password. Memanto's migrate plumbing carries a single API-key string per
provider, so both keys travel as ``"pk-lf-...:sk-lf-..."`` and are split here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

DEFAULT_HOST = "https://cloud.langfuse.com"
DEFAULT_PAGE_SIZE = 500
DEFAULT_WINDOW_DAYS = 7
REQUEST_TIMEOUT_S = 60.0

# Guard against an unbounded sweep on a busy project. Each page is up to
# DEFAULT_PAGE_SIZE rows, so this caps one run at ~100k observations.
MAX_PAGES = 200

# Traces hydrated from score hits. Scores point at traces, so low-score and
# success capture need a second fetch per trace; cap it so a noisy eval run
# can't fan out into thousands of requests.
MAX_SCORED_TRACES = 200

# Field groups the mapper needs. `metrics`/`usage`/`model` carry latency and
# cost; `io` carries the input/output that error text is extracted from.
OBSERVATION_FIELDS = "core,basic,time,io,metadata,model,usage,metrics,trace_context"

CAPTURE_MODES = ("errors", "low_score", "slow", "costly", "success")

# Modes that need every observation in the window, not just the errored ones:
# latency and cost are not server-side filterable, so they are classified
# client-side in langfuse_rules.
_UNFILTERED_MODES = frozenset({"slow", "costly"})
_SCORE_MODES = frozenset({"low_score", "success"})


def split_api_key(api_key: str) -> tuple[str, str]:
    """Split a combined ``"<public_key>:<secret_key>"`` credential.

    Langfuse needs two keys but the migrate CLI, the stored ``.env`` entry,
    and the UI all carry one string per provider, so the pair travels joined.
    """
    raw = (api_key or "").strip()
    public_key, _, secret_key = raw.partition(":")
    public_key = public_key.strip()
    secret_key = secret_key.strip()
    if not public_key or not secret_key:
        raise ValueError(
            "Langfuse needs both keys as 'pk-lf-...:sk-lf-...' "
            "(public key, colon, secret key). Get them from your Langfuse "
            "project settings."
        )
    return public_key, secret_key


def normalize_host(host: str | None) -> str:
    """Normalize a Langfuse base URL (cloud EU/US or self-hosted)."""
    text = (host or "").strip().rstrip("/")
    if not text:
        return DEFAULT_HOST
    if not text.startswith(("http://", "https://")):
        text = f"https://{text}"
    return text


def _client(api_key: str, host: str) -> httpx.Client:
    public_key, secret_key = split_api_key(api_key)
    return httpx.Client(
        base_url=host,
        timeout=REQUEST_TIMEOUT_S,
        auth=httpx.BasicAuth(public_key, secret_key),
        headers={"Content-Type": "application/json"},
    )


def _get_json(
    client: httpx.Client, path: str, params: dict[str, Any] | None = None
) -> Any:
    resp = client.get(path, params=params or {})
    if resp.status_code >= 400:
        raise RuntimeError(f"GET {path} -> {resp.status_code}: {resp.text[:500]}")
    return resp.json() if resp.content else {}


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    """Pull the row list out of a Langfuse list response.

    The v2/v3 list envelopes key their rows as ``data``; older and
    self-hosted builds have shipped ``items``. Accept both rather than
    silently returning nothing against a slightly different deployment.
    """
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _extract_cursor(payload: Any) -> str | None:
    """Pull the next-page cursor out of a Langfuse list response."""
    if not isinstance(payload, dict):
        return None
    meta = payload.get("meta")
    if isinstance(meta, dict):
        cursor = meta.get("nextCursor")
        if isinstance(cursor, str) and cursor:
            return cursor
    cursor = payload.get("nextCursor")
    if isinstance(cursor, str) and cursor:
        return cursor
    return None


def paginate(
    client: httpx.Client,
    path: str,
    params: dict[str, Any],
    *,
    page_size: int,
    on_progress: Callable[[str], None] | None = None,
    label: str = "rows",
) -> list[dict[str, Any]]:
    """Walk every cursor page of a Langfuse list endpoint."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None

    for page in range(MAX_PAGES):
        page_params = {**params, "limit": page_size}
        if cursor:
            page_params["cursor"] = cursor
        payload = _get_json(client, path, params=page_params)

        batch = _extract_items(payload)
        rows.extend(batch)
        if on_progress and batch:
            on_progress(f"  fetched {len(rows)} {label} (page {page + 1})")

        cursor = _extract_cursor(payload)
        if not cursor or not batch:
            break
    else:
        if on_progress:
            on_progress(
                f"  stopped at the {MAX_PAGES}-page cap — narrow --since to "
                f"capture the rest"
            )

    return rows


def fetch_observations(
    client: httpx.Client,
    *,
    from_time: datetime,
    to_time: datetime,
    level: str | None = None,
    trace_id: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Fetch observation rows in a time window, optionally filtered by level."""
    params: dict[str, Any] = {
        "fields": OBSERVATION_FIELDS,
        "fromStartTime": from_time.isoformat(),
        "toStartTime": to_time.isoformat(),
    }
    if level:
        params["level"] = level
    if trace_id:
        params["traceId"] = trace_id

    return paginate(
        client,
        "/api/public/v2/observations",
        params,
        page_size=page_size,
        on_progress=on_progress,
        label="observations",
    )


def fetch_scores(
    client: httpx.Client,
    *,
    from_time: datetime,
    value_min: float | None = None,
    value_max: float | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Fetch score rows, bounded by a numeric range."""
    params: dict[str, Any] = {"fromTimestamp": from_time.isoformat()}
    if value_min is not None:
        params["valueMin"] = value_min
    if value_max is not None:
        params["valueMax"] = value_max

    return paginate(
        client,
        "/api/public/v3/scores",
        params,
        page_size=page_size,
        on_progress=on_progress,
        label="scores",
    )


def _trace_ids_from_scores(scores: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for score in scores:
        trace_id = score.get("traceId")
        if isinstance(trace_id, str) and trace_id and trace_id not in seen:
            seen.add(trace_id)
            ordered.append(trace_id)
    return ordered


def _dedupe_observations(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per observation id — score hydration can re-fetch known rows."""
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        row_id = row.get("id")
        if isinstance(row_id, str) and row_id:
            if row_id in seen:
                continue
            seen.add(row_id)
        unique.append(row)
    return unique


def run_langfuse_export(
    api_key: str,
    dest_dir: Path,
    *,
    host: str = DEFAULT_HOST,
    since: datetime | None = None,
    capture: set[str] | None = None,
    score_threshold: float = 0.5,
    latency_ms: float = 30_000.0,
    cost_usd: float = 1.0,
    page_size: int = DEFAULT_PAGE_SIZE,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """
    Export Langfuse observations and scores and write JSON into *dest_dir*.

    Which rows get pulled depends on *capture*:

    ``errors``
        ``level=ERROR`` observations — filtered server-side, so this is the
        cheap path and the default.
    ``slow`` / ``costly``
        Every observation in the window (latency and cost are not
        server-side filterable); ``langfuse_rules`` classifies them.
    ``low_score`` / ``success``
        Scores below/above *score_threshold*, then the observations of the
        traces those scores point at.

    *latency_ms* and *cost_usd* are not fetch filters — they are applied
    client-side by ``langfuse_rules`` — but they are recorded in the export
    summary so replaying the file with ``--file`` reproduces the same mapping.

    Returns the written file path and the full export dict.
    """
    modes = set(capture or {"errors"})
    unknown = modes - set(CAPTURE_MODES)
    if unknown:
        raise ValueError(
            f"Unknown capture mode(s): {sorted(unknown)}. "
            f"Valid: {', '.join(CAPTURE_MODES)}"
        )
    if not modes:
        raise ValueError("At least one capture mode is required.")

    page_size = max(1, min(page_size, 1000))
    host = normalize_host(host)
    to_time = datetime.now(timezone.utc)
    from_time = since or (to_time - timedelta(days=DEFAULT_WINDOW_DAYS))

    observations: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []

    with _client(api_key, host) as client:
        window = f"{from_time.date()} → {to_time.date()}"

        if modes & _UNFILTERED_MODES:
            # One unfiltered sweep also covers `errors`, so don't re-fetch.
            if on_progress:
                on_progress(f"Fetching all observations ({window})...")
            observations.extend(
                fetch_observations(
                    client,
                    from_time=from_time,
                    to_time=to_time,
                    page_size=page_size,
                    on_progress=on_progress,
                )
            )
        elif "errors" in modes:
            if on_progress:
                on_progress(f"Fetching errored observations ({window})...")
            observations.extend(
                fetch_observations(
                    client,
                    from_time=from_time,
                    to_time=to_time,
                    level="ERROR",
                    page_size=page_size,
                    on_progress=on_progress,
                )
            )

        for mode in sorted(modes & _SCORE_MODES):
            if mode == "low_score":
                bounds: dict[str, float] = {"value_max": score_threshold}
                described = f"below {score_threshold}"
            else:
                bounds = {"value_min": score_threshold}
                described = f"at or above {score_threshold}"

            if on_progress:
                on_progress(f"Fetching scores {described}...")
            mode_scores = fetch_scores(
                client,
                from_time=from_time,
                page_size=page_size,
                on_progress=on_progress,
                **bounds,  # type: ignore[arg-type]
            )
            for score in mode_scores:
                score["capture_mode"] = mode
            scores.extend(mode_scores)

            trace_ids = _trace_ids_from_scores(mode_scores)[:MAX_SCORED_TRACES]
            if trace_ids and on_progress:
                on_progress(f"Hydrating {len(trace_ids)} scored traces...")
            for trace_id in trace_ids:
                observations.extend(
                    fetch_observations(
                        client,
                        from_time=from_time,
                        to_time=to_time,
                        trace_id=trace_id,
                        page_size=page_size,
                    )
                )

    observations = _dedupe_observations(observations)

    export = {
        "exported_at": to_time.isoformat(),
        "api_base": host,
        "summary": {
            "observation_count": len(observations),
            "score_count": len(scores),
            "capture_modes": sorted(modes),
            "from_time": from_time.isoformat(),
            "to_time": to_time.isoformat(),
            "score_threshold": score_threshold,
            "latency_ms": latency_ms,
            "cost_usd": cost_usd,
            "page_size": page_size,
        },
        "observations": observations,
        "scores": scores,
        "notes": {
            "endpoints": "v2 observations + v3 scores; /traces and v1 "
            "/observations are deprecated and unused.",
            "grouping": "Rows are raw. `memanto migrate langfuse` groups them "
            "into one memory per error signature — see cli/migrate/langfuse_rules.py.",
            "caps": f"At most {MAX_PAGES} pages per query and "
            f"{MAX_SCORED_TRACES} hydrated traces per score mode.",
        },
    }

    dest_dir.mkdir(parents=True, exist_ok=True)
    out_path = dest_dir / "langfuse_export.json"

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(export, f, indent=2, ensure_ascii=False, default=str)

    return out_path, export
