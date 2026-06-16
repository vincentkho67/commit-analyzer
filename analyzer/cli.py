"""CLI wiring + redact mode.

    python -m analyzer --repo owner/name [--repo owner/name2] --days 7 \
        [--author <you>] [--all-authors] [--redact] [--out report.md]

Pipeline: fetch (all authors) -> scope to one author -> (optional) redact at
the data layer -> cluster -> LLM analyze -> apply estimates -> render markdown.

Redaction happens BEFORE any LLM call or terminal output, so sensitive paths
and messages never leave this machine.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from datetime import datetime, timezone

import click

from . import analyze, cluster, config, fetch, report
from .fetch import Commit, FileChange


# --------------------------------------------------------------------------- #
# Redaction — performed at the data layer, before the LLM call
# --------------------------------------------------------------------------- #
def _mask_path(path: str) -> str:
    """Keep only the extension: 'src/auth/login.py' -> '<redacted>.py'."""
    _, ext = os.path.splitext(path)
    return f"<redacted>{ext}" if ext else "<redacted>"


def redact_commits(commits: list[Commit]) -> list[Commit]:
    """Mask paths (keep extension), redact messages, drop patch text.

    Returns new Commit objects; the originals are left untouched.
    """
    redacted = []
    for c in commits:
        files = [
            FileChange(_mask_path(f.path), f.status, f.additions, f.deletions, "")
            for f in c.files
        ]
        redacted.append(replace(c, message="[redacted]", files=files))
    return redacted


def _echo(msg: str = "") -> None:
    click.echo(msg, err=True)


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #
@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--repo", "repos", multiple=True, required=True,
              metavar="OWNER/NAME", help="Repository to analyze (repeatable).")
@click.option("--days", default=7, show_default=True, type=int,
              help="Look back this many days.")
@click.option("--author", default=None, metavar="LOGIN|EMAIL|NAME",
              help="Scope analysis to one author. Defaults to the authenticated "
                   "GitHub user ('you').")
@click.option("--all-authors", is_flag=True,
              help="Analyze every contributor, not just you.")
@click.option("--redact", is_flag=True,
              help="Mask paths, redact messages, skip evidence — before the LLM call.")
@click.option("--out", default="report.md", show_default=True,
              type=click.Path(dir_okay=False, writable=True),
              help="Markdown report output path.")
@click.option("--model", default=analyze.MODEL, show_default=True,
              help="OpenAI model for analysis.")
def main(repos, days, author, all_authors, redact, out, model):
    """Analyze a developer's GitHub commits: quality + estimated active hours."""
    repos = list(repos)

    # Fail loudly and early if credentials are missing.
    config.require_keys(need_openai=True)

    window_end = datetime.now(timezone.utc)
    since = fetch.since_iso(days, now=window_end)
    window_start = datetime.fromisoformat(since.replace("Z", "+00:00"))

    # 1) Fetch ALL commits (every author) across the repos.
    _echo(f"Fetching commits from {', '.join(repos)} (last {days} days)…")
    with fetch.make_client() as client:
        all_commits: list[Commit] = []
        for repo in repos:
            all_commits.extend(fetch.fetch_repo_commits(client, repo, since))

        # 2) Resolve the author scope ('you' by default) and filter.
        if all_authors:
            author_label = None
            scoped = all_commits
        else:
            resolved = author or fetch.get_authenticated_login(client)
            if not resolved:
                raise SystemExit(
                    "ERROR: could not resolve an author. Pass --author or --all-authors."
                )
            author_label = resolved
            scoped = fetch.filter_by_author(all_commits, resolved)

    others = len(all_commits) - len(scoped)
    _echo(f"  {len(all_commits)} commit(s) total"
          + (f"; {len(scoped)} by {author_label} ({others} by others)"
             if not all_authors else " (all authors)"))

    if not scoped:
        _echo("No matching commits — writing an empty report.")

    # 3) Redact at the data layer, before anything reaches the LLM/terminal.
    if redact:
        scoped = redact_commits(scoped)
        _echo("  redaction ON — paths masked, messages withheld, evidence skipped.")

    # 4) Cluster into sessions (timestamps only; unaffected by redaction).
    sessions = cluster.cluster_sessions(scoped)

    # 5) Per-commit LLM analysis.
    analyses = {}
    if scoped:
        oa = analyze.make_openai_client()

        def _progress(i, n, commit):
            label = commit.short_sha if redact else f"{commit.short_sha} {commit.summary[:48]}"
            _echo(f"  analyzing {i}/{n}: {label}")

        analyses = analyze.analyze_commits(
            scoped, redact=redact, model=model, client=oa, progress=_progress
        )

        # 6) Apply LLM estimates to sessions (lead-in + single-commit fallback).
        cluster.apply_llm_estimates(sessions, analyze.minutes_by_sha(analyses))

    # 7) Aggregate weekly summary + top-3 recommendations.
    inferred = cluster.total_active_minutes(sessions)
    traditional = analyze.total_traditional_minutes(analyses)
    weekly = analyze.WeeklySummary("", [])
    if scoped:
        stats = {
            "commits": len(scoped),
            "sessions": len(sessions),
            "inferred_active_minutes": round(inferred),
            "traditional_estimate_minutes": round(traditional),
            "leverage_multiplier": round(traditional / inferred, 2) if inferred else None,
            "average_quality": analyze.average_quality(analyses),
        }
        _echo("  generating weekly summary…")
        weekly = analyze.aggregate_summary(scoped, analyses, stats, model=model, client=oa)

    # 8) Render + write the markdown report.
    md = report.build_report(
        repos=repos, days=days, window_start=window_start, window_end=window_end,
        author=author_label, commits=scoped, sessions=sessions, analyses=analyses,
        weekly=weekly, redact=redact,
    )
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(md)

    # Terminal summary (kept free of sensitive text even without --redact:
    # only aggregate numbers are printed here).
    mult = f"{traditional / inferred:.1f}×" if inferred else "n/a"
    _echo("")
    _echo(f"✓ Report written to {out}")
    _echo(f"  commits: {len(scoped)} | sessions: {len(sessions)} "
          f"| active: {report.fmt_hm(inferred)} "
          f"| leverage: {mult} | avg quality: {analyze.average_quality(analyses)}/10")


if __name__ == "__main__":  # pragma: no cover
    main()
