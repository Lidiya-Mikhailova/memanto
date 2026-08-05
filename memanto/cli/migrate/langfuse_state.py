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

On each sync every mapped row is compared against the ledger by signature:
unseen signatures are written, known signatures whose payload changed are
updated in place via ``SdkClient.update_memory``, and unchanged ones are
skipped. The fingerprint covers the rendered content and confidence, so
"changed" means "the memory would actually read differently".
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

    @property
    def total(self) -> int:
        return len(self.new_rows) + len(self.updates) + self.unchanged


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


def load_state(path: Path) -> dict[str, Any]:
    """Read the ledger, returning an empty one when absent or unreadable.

    A corrupt ledger must not block a sync — the worst case of starting from
    empty is re-writing memories that already exist, which is visible and
    fixable, whereas refusing to sync is not.
    """
    empty: dict[str, Any] = {
        "version": STATE_VERSION,
        "last_synced_at": None,
        "signatures": {},
    }
    if not path.exists():
        return empty
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(loaded, dict):
        return empty

    signatures = loaded.get("signatures")
    return {
        "version": loaded.get("version", STATE_VERSION),
        "last_synced_at": loaded.get("last_synced_at"),
        "signatures": signatures if isinstance(signatures, dict) else {},
    }


def save_state(path: Path, state: dict[str, Any]) -> Path:
    """Persist the ledger, stamping the sync time."""
    state["version"] = STATE_VERSION
    state["last_synced_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, default=str),
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
    signatures = state.setdefault("signatures", {})
    entry = signatures.get(update["signature"])
    if not isinstance(entry, dict):
        return
    entry["fingerprint"] = update["fingerprint"]
    entry["occurrences"] = update.get("occurrences", entry.get("occurrences", 0))
    entry["last_seen"] = datetime.now(timezone.utc).isoformat()
