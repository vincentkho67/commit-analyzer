"""Session clustering — the time-estimation core.

Git records *when* you committed, not *how long* you worked. We recover active
time by grouping commits into work sessions and measuring each session's span:

  1. Sort every commit (across all repos) by author timestamp.
  2. Start a new session whenever the gap to the previous commit exceeds
     SESSION_GAP_MIN (90 min) — a long gap means you stopped working.
  3. A session's measurable span = last commit ts − first commit ts.

Two things that pure timestamps cannot see, filled in later from LLM estimates
(see analyze.py) via ``apply_llm_estimates``:

  * **Lead-in** — work done *before* the first commit of a session is invisible.
    We add an LLM estimate for the first commit, capped at LEAD_IN_CAP_MIN so a
    hallucinated value can't dominate.
  * **Single-commit sessions** — one commit has zero measurable span, so there
    is no timing signal at all; we fall back to the pure LLM estimate.

This module stays free of any network/LLM dependency so the clustering is
deterministic and unit-testable; the LLM numbers are injected by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping

from .fetch import Commit

# A gap larger than this between consecutive commits ends a session.
SESSION_GAP_MIN = 90

# Maximum minutes of pre-first-commit "lead-in" we will credit a session,
# so an over-eager LLM estimate can't balloon the total.
LEAD_IN_CAP_MIN = 60


@dataclass
class Session:
    """A run of commits with no >90-min gap between consecutive entries."""

    commits: list[Commit]  # sorted ascending by authored_at

    # Populated after LLM estimation; 0 until then.
    lead_in_minutes: float = 0.0  # multi-commit sessions only
    single_commit_minutes: float = 0.0  # single-commit sessions only

    @property
    def first(self) -> Commit:
        return self.commits[0]

    @property
    def last(self) -> Commit:
        return self.commits[-1]

    @property
    def commit_count(self) -> int:
        return len(self.commits)

    @property
    def is_single(self) -> bool:
        return self.commit_count == 1

    @property
    def start(self) -> datetime:
        return self.first.authored_at

    @property
    def end(self) -> datetime:
        return self.last.authored_at

    @property
    def day(self) -> date:
        """Calendar day a session is attributed to (its start, UTC)."""
        return self.start.date()

    @property
    def repos(self) -> list[str]:
        return sorted({c.repo for c in self.commits})

    @property
    def span_minutes(self) -> float:
        """Measurable span between first and last commit (0 for single-commit)."""
        return (self.end - self.start).total_seconds() / 60.0

    @property
    def active_minutes(self) -> float:
        """Best estimate of active time, including lead-in / single-commit fallback."""
        if self.is_single:
            return self.single_commit_minutes
        return self.span_minutes + self.lead_in_minutes


def cluster_sessions(
    commits: Iterable[Commit], gap_min: int = SESSION_GAP_MIN
) -> list[Session]:
    """Group commits into sessions, splitting on gaps larger than ``gap_min``."""
    ordered = sorted(commits, key=lambda c: c.authored_at)
    sessions: list[Session] = []
    current: list[Commit] = []
    threshold = timedelta(minutes=gap_min)

    for commit in ordered:
        if current and (commit.authored_at - current[-1].authored_at) > threshold:
            sessions.append(Session(current))
            current = []
        current.append(commit)
    if current:
        sessions.append(Session(current))
    return sessions


def apply_llm_estimates(
    sessions: Iterable[Session],
    minutes_by_sha: Mapping[str, float],
    lead_in_cap: int = LEAD_IN_CAP_MIN,
) -> list[Session]:
    """Attach LLM-derived lead-in / single-commit minutes to each session.

    ``minutes_by_sha`` maps a commit sha to its LLM ``estimated_active_minutes``.
    Mutates and returns the sessions for convenience.
    """
    sessions = list(sessions)
    for s in sessions:
        if s.is_single:
            s.single_commit_minutes = float(minutes_by_sha.get(s.first.sha, 0.0))
        else:
            first_est = float(minutes_by_sha.get(s.first.sha, 0.0))
            s.lead_in_minutes = min(first_est, float(lead_in_cap))
    return sessions


def total_active_minutes(sessions: Iterable[Session]) -> float:
    return sum(s.active_minutes for s in sessions)


def per_day_minutes(sessions: Iterable[Session]) -> dict[date, float]:
    """Active minutes attributed to each calendar day (session start, UTC)."""
    out: dict[date, float] = {}
    for s in sessions:
        out[s.day] = out.get(s.day, 0.0) + s.active_minutes
    return dict(sorted(out.items()))


if __name__ == "__main__":  # pragma: no cover - ad-hoc debugging
    import sys

    from .fetch import fetch_commits

    repos = sys.argv[1:] or ["vincentkho67/commit-analyzer"]
    commits = fetch_commits(repos, days=7)
    sessions = cluster_sessions(commits)
    print(f"{len(commits)} commits -> {len(sessions)} session(s) "
          f"(gap threshold {SESSION_GAP_MIN} min)\n")
    for i, s in enumerate(sessions, 1):
        kind = "single" if s.is_single else f"{s.commit_count} commits"
        print(f"Session {i}: {kind}, span {s.span_minutes:.0f} min")
        print(f"  {s.start.isoformat()} -> {s.end.isoformat()}  repos={s.repos}")
        for c in s.commits:
            print(f"    {c.short_sha} {c.authored_at.strftime('%H:%M')} {c.summary[:54]}")
