"""
Sync ledger for ``memanto migrate langfuse``.

Memanto does not deduplicate on write, and the other migrate providers are
one-shot imports where that is acceptable — running ``memanto migrate mem0``
twice imports everything twice. Langfuse is different: it is a *repeatable*
sync (a CLI run, or a click on the UI tile), so without a ledger every re-run
would double every memory.

This module keeps ``~/.memanto/migrate/langfuse/state.json``:

    {
      "version": 1,
      "scopes": {
        "<project key>::<agent id>": {
          "last_synced_at": "2026-08-04T09:12:33+00:00",
          "signatures": {
            "3f9a1c2b7d04": {
              "memory_id": "…",
              "fingerprint": "…",
              "occurrences": 412,
              "last_seen": "2026-08-04T08:59:01+00:00"
            }
          }
        }
      }
    }

On each sync every mapped row is compared against the ledger by signature:
unseen signatures are written, known signatures whose payload changed are
updated in place via ``SdkClient.update_memory``, and unchanged ones are
skipped. The fingerprint covers the rendered content and confidence, so
"changed" means "the memory would actually read differently".

**Scoping.** The ledger is keyed by Langfuse project *and* destination agent.
A signature written to agent A tells us nothing about agent B, and two
Langfuse projects can produce identical signatures for unrelated faults. A
single flat ledger would mark such a signature "already synced" and skip a
write the destination never received — silent data loss that only appears
once someone syncs more than one project or agent.

One JSON file rather than a file per scope, or a markdown log: this is
bookkeeping the code owns, not prose anyone reads. A single file keeps every
scope's write atomic and makes lookup a dict access, while the human-readable
narrative of what was remembered already lives in the session summaries.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memanto.app.utils.validation import is_successful_write_result

STATE_FILENAME = "state.json"
STATE_VERSION = 1

# Only these can be pushed through update_memory (constants.ALLOWED_UPDATE_FIELDS).
_UPDATABLE_FIELDS = ("title", "content", "confidence", "tags", "type")


@dataclass
class Reconciliation:
    """Rows split by what the ledger says should happen to them."""

    new_rows: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    unchanged: int = 0


def state_path(base_dir: Path) -> Path:
    """Ledger location — a sibling of the timestamped per-run directories."""
    return base_dir / STATE_FILENAME


def fingerprint(row: dict[str, Any]) -> str:
    """Stable hash of the parts of a payload a reader would notice changing."""
    payload = json.dumps(
        {
            "title": row.get("title"),
            "content": row.get("content"),
            "confidence": row.get("confidence"),
            "tags": sorted(row.get("tags") or []),
            "type": row.get("type"),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def scope_key(project_key: str, agent_id: str) -> str:
    """Ledger key for one (Langfuse project, destination agent) pair."""
    return f"{project_key or 'default'}::{agent_id or 'default'}"


def _read_scopes(path: Path) -> dict[str, Any]:
    """Read every scope out of the ledger file.

    Tolerates absence and corruption: a corrupt ledger must not block a sync.
    The worst case of starting from empty is re-writing memories that already
    exist, which is visible and fixable, whereas refusing to sync is not.
    """
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    scopes = loaded.get("scopes")
    return scopes if isinstance(scopes, dict) else {}


def load_state(path: Path, scope: str) -> dict[str, Any]:
    """Read one scope's ledger."""
    raw = _read_scopes(path).get(scope)
    signatures = raw.get("signatures") if isinstance(raw, dict) else None
    return {
        "version": STATE_VERSION,
        "scope": scope,
        "last_synced_at": raw.get("last_synced_at") if isinstance(raw, dict) else None,
        "signatures": signatures if isinstance(signatures, dict) else {},
    }


def save_state(path: Path, state: dict[str, Any], scope: str | None = None) -> Path:
    """Persist one scope's ledger, leaving other scopes untouched."""
    scope = scope or state["scope"]
    state["version"] = STATE_VERSION
    state["scope"] = scope
    state["last_synced_at"] = datetime.now(timezone.utc).isoformat()

    scopes = _read_scopes(path)
    scopes[scope] = {
        "last_synced_at": state["last_synced_at"],
        "signatures": state.get("signatures") or {},
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": STATE_VERSION, "scopes": scopes},
            indent=2,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    return path


def last_synced_at(state: dict[str, Any]) -> datetime | None:
    """The previous sync time, used as the default ``--since`` cursor."""
    raw = state.get("last_synced_at")
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def reconcile(rows: list[dict[str, Any]], state: dict[str, Any]) -> Reconciliation:
    """Split mapped rows into writes, in-place updates, and no-ops."""
    signatures = state.get("signatures") or {}
    result = Reconciliation()

    for row in rows:
        signature = row.get("signature")
        known = signatures.get(signature) if signature else None
        memory_id = (known or {}).get("memory_id")

        if not signature or not memory_id:
            result.new_rows.append(row)
            continue

        if (known or {}).get("fingerprint") == fingerprint(row):
            result.unchanged += 1
            continue

        result.updates.append(
            {
                "memory_id": memory_id,
                "signature": signature,
                "occurrences": row.get("occurrences", 0),
                "fingerprint": fingerprint(row),
                "updates": {
                    key: row[key]
                    for key in _UPDATABLE_FIELDS
                    if row.get(key) is not None
                },
            }
        )

    return result


def record_written(
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    results: list[Any],
) -> int:
    """Record memory ids for freshly written rows.

    ``batch_store_memories`` appends one result per submitted memory in
    submission order (including rejected ones), so rows and results line up
    positionally. Failed items are skipped so the next sync retries them.
    """
    signatures = state.setdefault("signatures", {})
    recorded = 0

    for row, item in zip(rows, results, strict=False):
        signature = row.get("signature")
        if not signature or not is_successful_write_result(item):
            continue
        memory_id = item.get("id")
        if not memory_id:
            continue
        signatures[signature] = {
            "memory_id": memory_id,
            "fingerprint": fingerprint(row),
            "occurrences": row.get("occurrences", 0),
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }
        recorded += 1

    return recorded


def record_updated(state: dict[str, Any], update: dict[str, Any]) -> None:
    """Refresh the ledger entry after a successful in-place update."""
    entry = state["signatures"][update["signature"]]
    entry["fingerprint"] = update["fingerprint"]
    entry["occurrences"] = update.get("occurrences", entry.get("occurrences", 0))
    entry["last_seen"] = datetime.now(timezone.utc).isoformat()
