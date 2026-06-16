"""Fetcher — pull commits and their diffs from GitHub.

Uses the GitHub REST API:
  * ``GET /repos/{owner}/{repo}/commits?since=<iso>`` to list commits, then
  * ``GET /repos/{owner}/{repo}/commits/{sha}`` per commit for file stats + patch.

Every per-file patch is truncated to ~3000 chars *inside this module* so that
lockfiles and generated code can never blow up downstream context.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Iterator

import httpx

from . import config

GITHUB_API = "https://api.github.com"

# Truncate each file patch to this many characters before it leaves the module.
PATCH_CHAR_LIMIT = 3000
TRUNCATION_MARKER = "\n[...truncated]"

# GitHub returns at most 100 items per page.
PER_PAGE = 100

_REQUEST_TIMEOUT = httpx.Timeout(30.0)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class FileChange:
    """One file touched by a commit. ``patch`` is already truncated."""

    path: str
    status: str
    additions: int
    deletions: int
    patch: str = ""


@dataclass
class Commit:
    repo: str  # "owner/repo"
    sha: str
    authored_at: datetime  # timezone-aware (UTC)
    message: str
    author_name: str
    author_email: str
    author_login: str | None  # GitHub login, may be None for unlinked emails
    files: list[FileChange] = field(default_factory=list)

    @property
    def short_sha(self) -> str:
        return self.sha[:7]

    @property
    def summary(self) -> str:
        """First line of the commit message."""
        return self.message.splitlines()[0] if self.message else ""

    @property
    def additions(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def deletions(self) -> int:
        return sum(f.deletions for f in self.files)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def truncate_patch(patch: str | None, limit: int = PATCH_CHAR_LIMIT) -> str:
    """Cap a patch at ``limit`` chars, appending a marker when cut."""
    if not patch:
        return ""
    if len(patch) <= limit:
        return patch
    return patch[:limit] + TRUNCATION_MARKER


def since_iso(days: int, *, now: datetime | None = None) -> str:
    """ISO-8601 timestamp ``days`` ago (UTC), for the API ``since`` param."""
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(iso: str) -> datetime:
    """Parse a GitHub ISO timestamp into a tz-aware UTC datetime."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def make_client(token: str | None = None) -> httpx.Client:
    """Build an authenticated GitHub API client."""
    token = token or config.github_token()
    return httpx.Client(
        base_url=GITHUB_API,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "commit-analyzer",
        },
        timeout=_REQUEST_TIMEOUT,
        follow_redirects=True,
    )


def _get(client: httpx.Client, url: str, params: dict | None = None) -> httpx.Response:
    """GET with one retry on transient rate-limit / 5xx, and clear errors."""
    for attempt in range(2):
        resp = client.get(url, params=params)
        if resp.status_code < 400:
            return resp
        # Primary rate limit: 403/429 with remaining == 0 -> wait until reset once.
        if resp.status_code in (403, 429) and resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
            wait = max(0, reset - int(time.time())) + 1
            if attempt == 0 and wait <= 60:
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"GitHub rate limit exhausted on {url}; resets in ~{wait}s."
            )
        if resp.status_code >= 500 and attempt == 0:
            time.sleep(1.5)
            continue
        # 404 / 401 / other: surface a useful message.
        detail = ""
        try:
            detail = resp.json().get("message", "")
        except Exception:
            detail = resp.text[:200]
        raise RuntimeError(
            f"GitHub API {resp.status_code} for {url}: {detail or 'request failed'}"
        )
    raise RuntimeError(f"GitHub API request failed after retries: {url}")


def _iter_commit_pages(client: httpx.Client, repo: str, since: str) -> Iterator[dict]:
    """Yield raw commit summaries for ``repo`` since ``since``, following pagination."""
    url = f"/repos/{repo}/commits"
    params: dict | None = {"since": since, "per_page": PER_PAGE}
    while url:
        resp = _get(client, url, params=params)
        for item in resp.json():
            yield item
        # Follow the Link header's rel="next"; params are baked into that URL.
        next_link = resp.links.get("next")
        url = next_link["url"] if next_link else None
        params = None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def get_commit_detail(client: httpx.Client, repo: str, sha: str) -> Commit:
    """Fetch full per-file stats + (truncated) patch for one commit."""
    resp = _get(client, f"/repos/{repo}/commits/{sha}")
    data = resp.json()

    commit_meta = data["commit"]
    author_meta = commit_meta.get("author") or {}
    gh_author = data.get("author") or {}

    files = [
        FileChange(
            path=f.get("filename", "?"),
            status=f.get("status", "modified"),
            additions=int(f.get("additions", 0)),
            deletions=int(f.get("deletions", 0)),
            patch=truncate_patch(f.get("patch")),
        )
        for f in data.get("files", [])
    ]

    return Commit(
        repo=repo,
        sha=data["sha"],
        authored_at=_parse_ts(author_meta.get("date")),
        message=commit_meta.get("message", ""),
        author_name=author_meta.get("name", ""),
        author_email=author_meta.get("email", ""),
        author_login=gh_author.get("login"),
        files=files,
    )


def fetch_repo_commits(
    client: httpx.Client, repo: str, since: str
) -> list[Commit]:
    """All commits (with diffs) for one ``owner/repo`` since ``since``."""
    if repo.count("/") != 1 or not all(repo.split("/")):
        raise ValueError(f"Expected 'owner/repo', got: {repo!r}")
    shas = [item["sha"] for item in _iter_commit_pages(client, repo, since)]
    return [get_commit_detail(client, repo, sha) for sha in shas]


def fetch_commits(
    repos: Iterable[str],
    days: int = 7,
    *,
    token: str | None = None,
    since: str | None = None,
) -> list[Commit]:
    """Fetch commits across one or more repos within the time window.

    Returns every commit (all authors); author scoping happens downstream.
    """
    repos = list(repos)
    if not repos:
        raise ValueError("At least one 'owner/repo' is required.")
    since = since or since_iso(days)
    out: list[Commit] = []
    with make_client(token) as client:
        for repo in repos:
            out.extend(fetch_repo_commits(client, repo, since))
    return out


def filter_by_author(commits: list[Commit], author: str | None) -> list[Commit]:
    """Keep commits whose login, email, or name matches ``author`` (case-insensitive).

    ``None`` returns all commits unchanged.
    """
    if not author:
        return commits
    needle = author.lower()
    kept = []
    for c in commits:
        candidates = {
            (c.author_login or "").lower(),
            c.author_email.lower(),
            c.author_name.lower(),
        }
        if needle in candidates:
            kept.append(c)
    return kept


if __name__ == "__main__":  # pragma: no cover - ad-hoc debugging
    import sys

    repos = sys.argv[1:] or ["vincentkho67/commit-analyzer"]
    commits = fetch_commits(repos, days=7)
    print(f"Fetched {len(commits)} commit(s) from {', '.join(repos)}")
    for c in sorted(commits, key=lambda x: x.authored_at):
        print(
            f"  {c.short_sha} {c.authored_at.isoformat()} "
            f"[{c.author_login or c.author_email}] "
            f"+{c.additions}/-{c.deletions} {len(c.files)}f  {c.summary[:60]}"
        )
