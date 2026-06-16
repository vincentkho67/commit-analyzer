"""Report generator — render the analysis as markdown.

Pure rendering: it takes already-fetched commits, clustered sessions (with LLM
estimates applied) and per-commit analyses, and returns a markdown string. No
network, no LLM calls here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from . import analyze as _an
from . import cluster as _cl
from .analyze import CommitAnalysis, WeeklySummary
from .cluster import Session
from .fetch import Commit


def fmt_hm(minutes: float) -> str:
    """Render a minute count as e.g. '2h 05m' (or '0m')."""
    minutes = max(0, round(minutes))
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h}h {m:02d}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _hours(minutes: float) -> str:
    return f"{minutes / 60.0:.1f} h"


def _esc(text: str) -> str:
    """Escape a string for use inside a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _multiplier(traditional: float, actual: float) -> str:
    if actual <= 0:
        return "n/a"
    return f"{traditional / actual:.1f}×"


def build_report(
    *,
    repos: Sequence[str],
    days: int,
    window_start: datetime,
    window_end: datetime,
    author: str | None,
    commits: Sequence[Commit],
    sessions: Sequence[Session],
    analyses: Mapping[str, CommitAnalysis],
    weekly: WeeklySummary,
    redact: bool = False,
) -> str:
    by_sha = {c.sha: c for c in commits}
    ordered = sorted(commits, key=lambda c: c.authored_at)

    inferred_actual = _cl.total_active_minutes(sessions)
    traditional_total = _an.total_traditional_minutes(analyses)
    estimated_total = _an.total_estimated_minutes(analyses)
    avg_quality = _an.average_quality(analyses)
    per_day = _cl.per_day_minutes(sessions)

    out: list[str] = []
    w = out.append

    # --- Header ----------------------------------------------------------- #
    w("# Commit Analysis Report\n")
    if redact:
        w("> **Redacted run** — file paths masked, messages withheld, no diff "
          "evidence. Sensitive text was never sent to the LLM.\n")
    w("| | |")
    w("|---|---|")
    w(f"| **Repos analyzed** | {', '.join(f'`{r}`' for r in repos)} |")
    w(f"| **Window** | {window_start:%Y-%m-%d %H:%M} → {window_end:%Y-%m-%d %H:%M} "
      f"UTC (last {days} day{'s' if days != 1 else ''}) |")
    if author:
        w(f"| **Author scope** | `{author}` |")
    w(f"| **Commits analyzed** | {len(commits)} |")
    w(f"| **Sessions** | {len(sessions)} |")
    w(f"| **Generated** | {window_end:%Y-%m-%d %H:%M} UTC |")
    w("")

    if not commits:
        w("_No commits found in this window for the selected author._")
        return "\n".join(out) + "\n"

    # --- Time summary ----------------------------------------------------- #
    w("## ⏱️ Time Summary\n")
    w(f"**Inferred active time: {fmt_hm(inferred_actual)}** "
      f"({_hours(inferred_actual)}), reconstructed from {len(sessions)} "
      f"work session(s) plus lead-in / single-commit estimates.\n")

    w("**Per-day breakdown**\n")
    w("| Day | Active time |")
    w("|---|---|")
    for day, mins in per_day.items():
        w(f"| {day:%Y-%m-%d (%a)} | {fmt_hm(mins)} |")
    w("")

    w("**Sessions**\n")
    w("| # | Day | Time (UTC) | Commits | Span | Lead-in | Active |")
    w("|---|---|---|---|---|---|---|")
    for i, s in enumerate(sorted(sessions, key=lambda x: x.start), 1):
        if s.is_single:
            lead = "—"
            note = " (single)"
        else:
            lead = fmt_hm(s.lead_in_minutes)
            note = ""
        timerange = f"{s.start:%H:%M}–{s.end:%H:%M}"
        w(f"| {i} | {s.start:%Y-%m-%d} | {timerange} | {s.commit_count}{note} "
          f"| {fmt_hm(s.span_minutes)} | {lead} | {fmt_hm(s.active_minutes)} |")
    w("")

    # --- Leverage --------------------------------------------------------- #
    w("## 🚀 AI Leverage\n")
    w(f"- **Traditional estimate (no AI):** {fmt_hm(traditional_total)} "
      f"({_hours(traditional_total)})")
    w(f"- **Inferred actual (clustered):** {fmt_hm(inferred_actual)} "
      f"({_hours(inferred_actual)})")
    w(f"- **LLM per-commit estimate (with AI):** {fmt_hm(estimated_total)} "
      f"({_hours(estimated_total)})")
    w("")
    w(f"**Leverage multiplier: {_multiplier(traditional_total, inferred_actual)}** "
      f"— a developer without AI tooling is estimated to need "
      f"{_multiplier(traditional_total, inferred_actual)} the inferred actual time "
      f"for the same output.")
    w("")

    # --- Quality ---------------------------------------------------------- #
    w("## 🧐 Code Quality\n")
    w(f"**Weekly average score: {avg_quality} / 10** "
      f"(across {len([a for a in analyses.values() if not a.parse_failed])} "
      f"scored commit(s)).\n")

    w("| Commit | Message | Score | Complexity | Est. active |")
    w("|---|---|---|---|---|")
    for c in ordered:
        a = analyses.get(c.sha)
        if not a:
            continue
        msg = "[redacted]" if redact else _esc(c.summary)
        w(f"| `{c.short_sha}` | {msg or '(empty)'} | {a.quality_score}/10 "
          f"| {a.complexity} | {fmt_hm(a.estimated_active_minutes)} |")
    w("")

    w("### Evidence & notes\n")
    for c in ordered:
        a = analyses.get(c.sha)
        if not a:
            continue
        title = "[redacted]" if redact else _esc(c.summary)
        w(f"**`{c.short_sha}` — {title or '(empty)'}**  "
          f"_(score {a.quality_score}/10, {a.complexity}, "
          f"est. {fmt_hm(a.estimated_active_minutes)} vs "
          f"{fmt_hm(a.traditional_estimate_minutes)} traditional)_")
        if a.reasoning:
            w(f"- _Reasoning:_ {a.reasoning}")
        if a.strengths:
            w(f"- _Strengths:_ {'; '.join(a.strengths)}")
        if a.issues:
            w(f"- _Issues:_ {'; '.join(a.issues)}")
        if a.evidence and not redact:
            w("- _Evidence:_")
            for ev in a.evidence:
                w(f"  - `{_esc(ev)}`")
        w("")

    # --- Recommendations -------------------------------------------------- #
    w("## ✅ Recommendations\n")
    if weekly.summary:
        w(f"{weekly.summary}\n")
    if weekly.recommendations:
        for i, rec in enumerate(weekly.recommendations, 1):
            w(f"{i}. {rec}")
    else:
        w("_No aggregate recommendations were produced._")
    w("")

    # --- Limitations ------------------------------------------------------ #
    w("## ⚠️ Limitations\n")
    w("This tool infers time from commit timestamps and an LLM; it cannot know "
      "the ground truth. Read the numbers as estimates:\n")
    w("- **Squashed / rebased commits hide sessions** — squashing a day's work "
      "into one commit collapses its session to a single point.")
    w("- **End-of-session committers inflate per-commit estimates** — if you "
      "commit everything at the end, early work is invisible to clustering and "
      "leans entirely on the LLM estimate.")
    w("- **Timestamps can be amended or rebased**, so author time may not equal "
      "wall-clock time.")
    w("- **Thinking, reading, and debugging time between commits is invisible** "
      "unless it falls inside a measured session span.")
    w(f"- **Lead-in is an estimate**, capped at {_cl.LEAD_IN_CAP_MIN} min per "
      "session, and single-commit sessions rest entirely on the LLM's read of "
      "the diff.")
    w("- **Per-day time is attributed to a session's start day (UTC)**; a "
      "session spanning midnight is counted on the day it began.")
    w("- **LLM estimates are judgment, not measurement** — they vary run to run "
      "and can misjudge novelty from a truncated diff.")
    w("")

    return "\n".join(out) + "\n"
