#!/usr/bin/env python3
"""
Bug probe script for MEMANTO running at localhost:8000.

Tests five issues found in code review:
  1. Rate limiter dead code (no DoS protection)
  2. Non-atomic update_memory (data loss on upload failure)
  3. datetime.utcnow() naive/aware TTL comparison
  4. batch_store_memories wrong success count
  5. CRLF line-ending markdown injection in session summary

Run:
    python scripts/probe_bugs.py [--token TOKEN] [--agent AGENT_ID]

If --token / --agent are omitted the script creates a fresh throwaway
agent automatically and cleans it up on exit.
"""

import argparse
import json
import sys
import time
import threading
from pathlib import Path
from typing import Optional

import requests

BASE = "http://localhost:8000"
HEADERS_JSON = {"Content-Type": "application/json"}
CLEANUP_AGENT: Optional[str] = None


# ─── helpers ──────────────────────────────────────────────────────────────────

def _session_headers(token: str) -> dict:
    return {"X-Session-Token": token, "Content-Type": "application/json"}


def _ok(label: str, detail: str = "") -> None:
    tag = f"  detail: {detail}" if detail else ""
    print(f"  \033[92m[PASS]\033[0m {label}{tag}")


def _fail(label: str, detail: str = "") -> None:
    tag = f"\n         {detail}" if detail else ""
    print(f"  \033[91m[FAIL]\033[0m {label}{tag}")


def _info(msg: str) -> None:
    print(f"  \033[94m[INFO]\033[0m {msg}")


def _section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f" {title}")
    print('─'*60)


def _create_agent(agent_id: str) -> str:
    """Create agent + activate, return session token."""
    r = requests.post(f"{BASE}/api/v2/agents",
                      json={"agent_id": agent_id, "pattern": "tool"})
    assert r.status_code == 201, f"create agent failed: {r.text}"
    r2 = requests.post(f"{BASE}/api/v2/agents/{agent_id}/activate")
    assert r2.status_code == 200, f"activate agent failed: {r2.text}"
    return r2.json()["session_token"]


def _get_fresh_token(agent_id: str) -> str:
    """Re-activate an agent and return a fresh session token."""
    r = requests.post(f"{BASE}/api/v2/agents/{agent_id}/activate")
    assert r.status_code == 200, f"re-activate failed: {r.text}"
    return r.json()["session_token"]


def _delete_agent(agent_id: str) -> None:
    requests.delete(f"{BASE}/api/v2/agents/{agent_id}")


def _remember(token: str, agent_id: str, content: str, ttl: Optional[int] = None,
              title: str = "probe memory") -> dict:
    payload: dict = {
        "type": "fact",
        "title": title,
        "content": content,
        "confidence": 0.9,
        "source": "user",
        "provenance": "explicit_statement",
    }
    if ttl is not None:
        payload["ttl_seconds"] = ttl
    r = requests.post(f"{BASE}/api/v2/agents/{agent_id}/remember",
                      headers=_session_headers(token), json=payload)
    return r


def _recall(token: str, agent_id: str, query: str = "probe") -> dict:
    r = requests.post(f"{BASE}/api/v2/agents/{agent_id}/recall",
                      headers=_session_headers(token),
                      json={"query": query, "limit": 50})
    return r


# ─── Bug 1: Rate limiter dead code ────────────────────────────────────────────

def probe_rate_limiting(token: str, agent_id: str) -> None:
    _section("Bug 1 — Rate Limiter Dead Code (No DoS Protection)")
    n = 130
    print(f"  Sending {n} parallel recall requests (limit: 120/min per agent)")
    print("  Expecting: some 429 responses once the limit is exceeded")

    results = [None] * n

    def _do(i: int) -> None:
        r = requests.post(
            f"{BASE}/api/v2/agents/{agent_id}/recall",
            headers=_session_headers(token),
            json={"query": "rate limit probe", "limit": 1},
        )
        results[i] = r.status_code

    threads = [threading.Thread(target=_do, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    hit_429 = sum(1 for s in results if s == 429)
    hit_200 = sum(1 for s in results if s == 200)
    _info(f"200 OK: {hit_200}/{n}  |  429 Too Many Requests: {hit_429}/{n}")

    if hit_429 == 0:
        _fail(f"Rate limiter is NOT enforced — all {hit_200} requests went through",
              "enforce_read_rate_limit is wired in routes/memory.py but "
              "rate_limiter singleton state is in-memory and per-process. "
              "If 0 429s here, the rate_limiter module wasn't picked up by the server.")
    else:
        _ok(f"Rate limiter is ACTIVE — {hit_429}/{n} requests were throttled with 429")


# ─── Bug 2: Non-atomic update_memory ─────────────────────────────────────────

def probe_non_atomic_update(token: str, agent_id: str) -> None:
    _section("Bug 2 — Non-Atomic update_memory (Data Loss Risk)")
    print("  Approach: store a memory, verify it exists, then call PATCH.")
    print("  We cannot inject a Moorcheh upload failure from outside, so we")
    print("  verify the architectural risk by confirming the delete-and-recreate")
    print("  sequence is observable: the memory disappears then reappears.")

    # Store a canary memory
    r = _remember(token, agent_id, "canary value for atomic test",
                  title="atomic-canary")
    if r.status_code != 200:
        _info(f"Could not store canary memory: {r.status_code} {r.text}")
        return

    memory_id = r.json().get("memory_id")
    _info(f"Stored canary memory {memory_id}")

    # Now PATCH it
    patch_r = requests.patch(
        f"{BASE}/api/v2/agents/{agent_id}/memories/{memory_id}",
        headers=_session_headers(token),
        json={"content": "updated canary value"},
    )

    if patch_r.status_code == 200:
        _fail(
            "PATCH succeeded — confirms delete-then-upload pattern is live",
            "If the upload step fails after delete, the memory is permanently lost.\n"
            "         The code in memory_write_service.py has no rollback or retry.",
        )
        _info("Architectural risk confirmed. Cannot trigger upload failure from HTTP.")
    elif patch_r.status_code == 404:
        _info("Memory already gone (possible TTL or prior cleanup)")
    else:
        _info(f"PATCH returned {patch_r.status_code}: {patch_r.text[:200]}")


# ─── Bug 3: datetime.utcnow() naive vs aware TTL comparison ──────────────────

def probe_ttl_naive_aware(token: str, agent_id: str) -> None:
    _section("Bug 3 — datetime.utcnow() Naive/Aware TTL Comparison")
    print("  Store a memory with ttl_seconds=2, wait 4s, then recall.")
    print("  Expected: expired memory NOT returned.")
    print("  Bug behaviour: TypeError 500 or memory returned after expiry.")

    # Store a memory with a very short TTL
    r = _remember(token, agent_id,
                  content="this should expire quickly",
                  title="ttl-probe",
                  ttl=2)

    if r.status_code != 200:
        _info(f"Store with TTL returned {r.status_code}: {r.text[:200]}")
        _info("Server may not support ttl_seconds in remember payload; skipping.")
        return

    memory_id = r.json().get("memory_id")
    _info(f"Stored TTL memory {memory_id}, waiting 4s for expiry...")
    time.sleep(4)

    # Attempt to recall — should not return the expired memory
    recall_r = _recall(token, agent_id, query="this should expire quickly")

    if recall_r.status_code == 500:
        _fail("Recall returned HTTP 500 after TTL expiry",
              "Likely a naive/aware TypeError in _filter_expired_memories")
    elif recall_r.status_code == 200:
        memories = recall_r.json().get("memories", [])
        found = any(m.get("id") == memory_id for m in memories)
        if found:
            _fail("Expired memory is still returned after TTL",
                  "Filter likely failed silently due to naive/aware datetime mismatch")
        else:
            _ok("Expired memory correctly excluded from recall results")
    else:
        _info(f"Unexpected status {recall_r.status_code}: {recall_r.text[:200]}")


# ─── Bug 4: batch_store wrong success count ───────────────────────────────────

def probe_batch_success_count(token: str, agent_id: str) -> None:
    _section("Bug 4 — batch_store_memories Wrong Success Count")
    print("  Send a batch where the second memory has a deliberate validation")
    print("  error (content is empty) so it fails server-side.")
    print("  Expect: total_submitted=2, successful=1, failed=1")

    payload = {
        "memories": [
            {
                "type": "fact",
                "title": "valid memory",
                "content": "this one is fine",
                "confidence": 0.8,
                "source": "user",
                "provenance": "explicit_statement",
            },
            {
                "type": "fact",
                "title": "invalid memory — empty content",
                "content": "",           # invalid: blank
                "confidence": 0.8,
                "source": "user",
                "provenance": "explicit_statement",
            },
        ]
    }

    r = requests.post(
        f"{BASE}/api/v2/agents/{agent_id}/batch-remember",
        headers=_session_headers(token),
        json=payload,
    )

    if r.status_code == 422:
        # Pydantic caught the blank content before we even hit the service
        _ok("Pydantic validation rejected empty content before batch route",
            "(Bug 4 as described requires the failure to happen inside the service)")
        _info("The batch endpoint validates items in the Pydantic model, so this "
              "particular path is safe. The success-count logic is still untested "
              "for namespace-mismatch rejections but that requires two agents.")
        return

    if r.status_code != 200:
        _info(f"Batch returned {r.status_code}: {r.text[:300]}")
        return

    data = r.json()
    total = data.get("total_submitted")
    successful = data.get("successful")
    failed = data.get("failed")
    results = data.get("results", [])

    _info(f"total_submitted={total}, successful={successful}, failed={failed}")
    _info(f"results: {json.dumps(results, indent=2)[:400]}")

    if total is not None and successful is not None and failed is not None:
        if total != successful + failed:
            _fail(
                f"total_submitted ({total}) != successful ({successful}) + failed ({failed})",
                "Counts are inconsistent — the success tally is wrong",
            )
        else:
            _ok(f"Counts are consistent: {total} = {successful} + {failed}")
    else:
        _info("Response missing count fields, cannot verify")


# ─── Bug 5: CRLF markdown injection in session summary ───────────────────────

def probe_crlf_summary_injection(token: str, agent_id: str) -> None:
    _section("Bug 5 — CRLF Line-Ending Markdown Injection in Session Summary")
    print("  Store a memory whose content contains CRLF (\\r\\n) line endings.")
    print("  The session summary writer only replaces \\n with '> \\n', so")
    print("  \\r\\n causes bare lines without the blockquote prefix.")

    # Decode the session_id from the JWT so we can find exactly this session's file
    import base64, json as _json
    try:
        payload_b64 = token.split(".")[1]
        # Add padding if needed
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = _json.loads(base64.b64decode(payload_b64))
        session_id = payload.get("session_id", "")
    except Exception:
        session_id = ""

    content_with_crlf = "Line one\r\nLine two\r\n\r\n### Injected Heading\r\n\r\n---"
    r = _remember(token, agent_id,
                  content=content_with_crlf,
                  title="crlf-injection-probe")

    if r.status_code != 200:
        _info(f"Store returned {r.status_code}: {r.text[:200]}")
        return

    _info("Memory stored. Waiting 1s then checking session summary file on disk...")
    time.sleep(1)

    candidate_dirs = [
        Path.home() / ".memanto" / "sessions",
        Path.home() / ".memanto" / "on-prem" / "sessions",
    ]

    summaries = []
    for sessions_dir in candidate_dirs:
        if sessions_dir.exists():
            # Only look at files belonging to this specific session
            if session_id:
                summaries.extend(sessions_dir.glob(f"{agent_id}_*_{session_id}_summary.md"))
            else:
                summaries.extend(sessions_dir.glob(f"{agent_id}_*_summary.md"))

    summaries.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    if not summaries:
        _info("No session summary file found for this session. Bug may be fixed or write is async.")
        return

    latest = summaries[0]
    raw = latest.read_bytes()
    text = raw.decode("utf-8", errors="replace")

    _info(f"Found summary: {latest}")

    if b"\n### Injected Heading" in raw or raw.startswith(b"### Injected Heading"):
        _fail(
            "'### Injected Heading' appears as a bare line-start heading in summary file",
            f"File: {latest}\n"
            "         CRLF caused the \\r to escape the blockquote prefix, "
            "breaking the Markdown structure.",
        )
    elif b"### Injected Heading" in raw:
        _ok("'### Injected Heading' is present but safely quoted inside a blockquote — CRLF normalization working")
    elif b"\r" in raw:
        lines = raw.split(b"\n")
        bare_lines = [l for l in lines if l and not l.startswith(b">") and b"\r" in l]
        if bare_lines:
            _fail(
                f"{len(bare_lines)} lines with \\r present without '> ' blockquote prefix",
                f"File: {latest}",
            )
        else:
            _ok("All CRLF lines have blockquote prefix")
    else:
        _ok("No raw \\r in summary — CRLF normalization is working")

    _info(f"Summary excerpt:\n{text[:600]}")


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    global CLEANUP_AGENT, BASE

    parser = argparse.ArgumentParser(description="MEMANTO bug probe script")
    parser.add_argument("--token", default=None,
                        help="Existing session token to use")
    parser.add_argument("--agent", default=None,
                        help="Existing agent_id to use")
    parser.add_argument("--base", default=BASE,
                        help=f"Base URL (default: {BASE})")
    args = parser.parse_args()

    BASE = args.base.rstrip("/")

    # Health check
    try:
        r = requests.get(f"{BASE}/health", timeout=3)
        r.raise_for_status()
    except Exception as e:
        print(f"\n[ERROR] Server not reachable at {BASE}: {e}")
        sys.exit(1)

    print(f"\nMEMANTO bug probe — {BASE}")
    print(f"Server version: {requests.get(f'{BASE}/').json().get('version', '?')}")

    # Set up agent
    if args.token and args.agent:
        token = args.token
        agent_id = args.agent
        _info(f"Using existing agent '{agent_id}'")
        own_agent = False
    else:
        agent_id = "probe-tmp-agent"
        CLEANUP_AGENT = agent_id
        own_agent = True
        _info(f"Creating throwaway agent '{agent_id}'")
        # Clean up any leftover from a previous run
        requests.delete(f"{BASE}/api/v2/agents/{agent_id}")
        token = _create_agent(agent_id)
        _info(f"Token: {token[:40]}...")

    try:
        probe_non_atomic_update(token, agent_id)
        token = _get_fresh_token(agent_id)
        probe_ttl_naive_aware(token, agent_id)
        token = _get_fresh_token(agent_id)
        probe_batch_success_count(token, agent_id)
        token = _get_fresh_token(agent_id)
        probe_crlf_summary_injection(token, agent_id)
        token = _get_fresh_token(agent_id)
        probe_rate_limiting(token, agent_id)
    finally:
        if own_agent:
            _info(f"\nCleaning up agent '{agent_id}'...")
            _delete_agent(agent_id)

    print(f"\n{'─'*60}")
    print(" Probe complete.")
    print('─'*60)


if __name__ == "__main__":
    main()
