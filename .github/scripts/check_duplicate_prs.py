"""Flag pull requests that appear to duplicate other open/merged PRs.

Runs in GitHub Actions. Reads only PR metadata (title, body, changed file
paths) via the REST API -- it never checks out PR code, so it is safe to run
under `pull_request_target`.

Pipeline:
  1. Cheap heuristics (shared linked issue, changed-file overlap, title token
     similarity) narrow the field to a few candidate PRs.
  2. Mistral semantically compares the current PR against each candidate to
     catch "same bug, different approach" duplicates that heuristics miss.
  3. If anything scores above the confidence threshold, post/update a comment
     and apply the `possible-duplicate` label.

Stdlib only -- no third-party packages, so the workflow needs no `pip install`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

GITHUB_API = "https://api.github.com"
MISTRAL_API = "https://api.mistral.ai/v1/chat/completions"

# Tunables (override via workflow env if you want).
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "6"))
HEURISTIC_MIN = float(os.environ.get("HEURISTIC_MIN", "0.12"))
CONFIDENCE_MIN = float(os.environ.get("CONFIDENCE_MIN", "0.6"))
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
LABEL = os.environ.get("DUPLICATE_LABEL", "possible-duplicate")
COMMENT_MARKER = "<!-- duplicate-pr-check -->"
# When set, print what would be posted instead of commenting/labelling.
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}

_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "fix",
    "fixes", "bug", "add", "update", "remove", "support", "feat", "feature",
    "with", "when", "use", "make", "allow", "wip", "pr", "issue",
}
_ISSUE_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE
)
_ISSUE_ANY = re.compile(r"#(\d+)")


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _request(method: str, url: str, headers: dict, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else None


def gh(method: str, path: str, token: str, payload: dict | None = None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }
    url = path if path.startswith("http") else f"{GITHUB_API}{path}"
    return _request(method, url, headers, payload)


# --------------------------------------------------------------------------- #
# Heuristics
# --------------------------------------------------------------------------- #
def title_tokens(title: str) -> set[str]:
    raw = re.split(r"[^a-z0-9]+", title.lower())
    return {t for t in raw if len(t) > 2 and t not in _STOPWORDS}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def closing_issues(text: str) -> set[int]:
    return {int(n) for n in _ISSUE_CLOSING.findall(text or "")}


def referenced_issues(text: str) -> set[int]:
    return {int(n) for n in _ISSUE_ANY.findall(text or "")}


def changed_files(repo: str, number: int, token: str) -> set[str]:
    files = gh("GET", f"/repos/{repo}/pulls/{number}/files?per_page=100", token) or []
    return {f["filename"] for f in files}


# --------------------------------------------------------------------------- #
# Mistral
# --------------------------------------------------------------------------- #
def mistral_compare(api_key: str, current: dict, candidate: dict) -> dict:
    """Ask Mistral whether two PRs solve the same problem. Returns a verdict."""
    prompt = (
        "You compare two GitHub pull requests and decide whether they address "
        "the same underlying bug, feature, or problem -- even if they take "
        "different technical approaches or touch different files.\n\n"
        f"PR A (#{current['number']}): {current['title']}\n"
        f"Description: {(current['body'] or '')[:1500]}\n"
        f"Changed files: {', '.join(sorted(current['files'])[:40]) or 'unknown'}\n\n"
        f"PR B (#{candidate['number']}): {candidate['title']}\n"
        f"Description: {(candidate['body'] or '')[:1500]}\n"
        f"Changed files: {', '.join(sorted(candidate['files'])[:40]) or 'unknown'}\n\n"
        "Respond with JSON only: "
        '{"duplicate": true|false, "confidence": 0.0-1.0, "reason": "one sentence"}. '
        "Set duplicate=true only if they target the same problem; overlapping "
        "files alone is not enough."
    )
    payload = {
        "model": MISTRAL_MODEL,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = _request("POST", MISTRAL_API, headers, payload)
        verdict = json.loads(resp["choices"][0]["message"]["content"])
    except (urllib.error.HTTPError, KeyError, json.JSONDecodeError) as exc:
        print(f"  ! Mistral comparison failed for #{candidate['number']}: {exc}")
        return {"duplicate": False, "confidence": 0.0, "reason": ""}
    return verdict


# --------------------------------------------------------------------------- #
# Comment / label output
# --------------------------------------------------------------------------- #
def build_comment(hits: list[dict]) -> str:
    lines = [
        COMMENT_MARKER,
        "### Possible duplicate PRs",
        "",
        "This PR may overlap with existing work. Please review before merging:",
        "",
    ]
    for h in sorted(hits, key=lambda x: x["confidence"], reverse=True):
        state = "merged" if h["merged"] else h["state"]
        pct = round(h["confidence"] * 100)
        lines.append(f"- #{h['number']} ({state}, ~{pct}% match) — {h['reason']}")
    lines += [
        "",
        "_Automated by the duplicate-PR check; close this comment if it's a false positive._",
    ]
    return "\n".join(lines)


def upsert_comment(repo: str, number: int, token: str, body: str) -> None:
    comments = gh("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100", token) or []
    for c in comments:
        if COMMENT_MARKER in (c.get("body") or ""):
            gh("PATCH", f"/repos/{repo}/issues/comments/{c['id']}", token, {"body": body})
            return
    gh("POST", f"/repos/{repo}/issues/{number}/comments", token, {"body": body})


def add_label(repo: str, number: int, token: str) -> None:
    gh("POST", f"/repos/{repo}/issues/{number}/labels", token, {"labels": [LABEL]})


def gather_pool(repo: str, token: str, exclude: int) -> list[dict]:
    """Open PRs + recently updated closed/merged PRs, minus the PR itself."""
    others = gh("GET", f"/repos/{repo}/pulls?state=open&per_page=100", token) or []
    closed = gh(
        "GET",
        f"/repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=50",
        token,
    ) or []
    return [p for p in (others + closed) if p["number"] != exclude]


def process_pr(repo: str, token: str, mistral_key: str | None, pr: dict) -> None:
    number = pr["number"]
    current = {
        "number": number,
        "title": pr["title"] or "",
        "body": pr.get("body") or "",
        "files": changed_files(repo, number, token),
    }
    cur_tokens = title_tokens(current["title"])
    cur_close = closing_issues(current["title"] + " " + current["body"])
    cur_ref = referenced_issues(current["title"] + " " + current["body"])

    pool = gather_pool(repo, token, number)

    # Heuristic scoring -> candidate shortlist.
    scored = []
    for p in pool:
        text = (p["title"] or "") + " " + (p.get("body") or "")
        shared_close = cur_close & closing_issues(text)
        shared_ref = cur_ref & referenced_issues(text)
        score = jaccard(cur_tokens, title_tokens(p["title"] or ""))
        if shared_close:
            score = max(score, 0.9)
        elif shared_ref:
            score = max(score, 0.4)
        if score >= HEURISTIC_MIN:
            scored.append((score, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    shortlist = scored[:MAX_CANDIDATES]

    print(f"PR #{number}: {len(pool)} other PRs, {len(shortlist)} candidates after heuristics")

    if not shortlist:
        print("  No candidates; nothing to flag.")
        return
    if not mistral_key:
        print("  MISTRAL_API_KEY not set; skipping semantic check.")
        return

    hits = []
    for score, p in shortlist:
        candidate = {
            "number": p["number"],
            "title": p["title"] or "",
            "body": p.get("body") or "",
            "files": changed_files(repo, p["number"], token),
        }
        verdict = mistral_compare(mistral_key, current, candidate)
        conf = float(verdict.get("confidence", 0))
        print(f"  #{p['number']}: heuristic={score:.2f} dup={verdict.get('duplicate')} conf={conf:.2f}")
        if verdict.get("duplicate") and conf >= CONFIDENCE_MIN:
            hits.append({
                "number": p["number"],
                "state": p["state"],
                "merged": bool(p.get("merged_at")),
                "confidence": conf,
                "reason": verdict.get("reason", ""),
            })

    if not hits:
        print("  No semantic duplicates above threshold.")
        return

    comment = build_comment(hits)
    if DRY_RUN:
        print(f"  [dry-run] would flag {len(hits)} duplicate(s) and add '{LABEL}':\n{comment}")
        return

    upsert_comment(repo, number, token, comment)
    add_label(repo, number, token)
    print(f"  Flagged {len(hits)} possible duplicate(s).")


def resolve_prs(repo: str, token: str) -> list[dict]:
    """Decide which PR(s) to scan: explicit number, the triggering PR, or all open."""
    pr_number_env = os.environ.get("PR_NUMBER")
    if pr_number_env:
        return [gh("GET", f"/repos/{repo}/pulls/{pr_number_env}", token)]

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        with open(event_path, encoding="utf-8") as fh:
            event = json.load(fh)
        if "pull_request" in event:
            return [event["pull_request"]]

    # No specific PR (e.g. a manual all-open backfill run): scan every open PR.
    print("No specific PR given; scanning all open PRs.")
    return gh("GET", f"/repos/{repo}/pulls?state=open&per_page=100", token) or []


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    mistral_key = os.environ.get("MISTRAL_API_KEY")

    prs = resolve_prs(repo, token)
    if not prs:
        print("No PRs to scan.")
        return 0

    for pr in prs:
        process_pr(repo, token, mistral_key, pr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
