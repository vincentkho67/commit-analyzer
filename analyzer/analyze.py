"""LLM analysis — quality scoring + per-commit time estimation via GPT-4o.

One call per commit, plus one aggregate call for the weekly summary. All calls
use ``response_format={"type": "json_object"}`` for reliable parsing and retry
once on a parse failure.

The key idea (baked into the system prompt): the developer uses AI tooling, so
time tracks the NOVELTY of a change, not its size. A 2000-line generated diff
can be minutes; a 20-line algorithm can be an afternoon.

  * ``estimated_active_minutes``      — what it actually took (with AI).
  * ``traditional_estimate_minutes``  — what a solo dev WITHOUT AI would take.

The ratio of the two, measured against clustered actual time, is the leverage
metric the report surfaces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

from .fetch import Commit

MODEL = "gpt-4o"

SYSTEM_PROMPT = (
    "You are a senior engineering manager auditing one week of a developer's "
    "commits. The developer uses AI coding tools, so estimate time from the "
    "NOVELTY of the change, not its size — large mechanical or generated diffs "
    "are fast; small algorithmic or architectural changes are slow. For the "
    "commit you receive (message, files, additions/deletions, truncated patch) "
    "return STRICT JSON, no prose, no markdown fences:\n"
    "{\n"
    '  "quality": {"score": 1-10, "strengths": [..], "issues": [..], '
    '"evidence": [".. short quoted line .."]},\n'
    '  "complexity": "trivial|routine|complex|novel",\n'
    '  "estimated_active_minutes": <int>,\n'
    '  "traditional_estimate_minutes": <int, how long a solo dev WITHOUT AI '
    "tools would take>,\n"
    '  "reasoning": "<one sentence>"\n'
    "}\n"
    "Judge quality on: correctness risk, readability & naming, structure & "
    "modularity, error handling, test presence, and commit hygiene. The "
    '"evidence" array must hold short lines quoted verbatim from the diff that '
    "justify the score. estimated_active_minutes and traditional_estimate_minutes "
    "must be positive integers (minutes)."
)

# Extra instruction appended in redact mode (no identifying text is sent).
_REDACT_NOTE = (
    "\nPRIVACY MODE: file paths are masked to their extension and the commit "
    "message is withheld. Do not guess or invent identifying details, and "
    'return "evidence" as an empty array [].'
)

AGGREGATE_SYSTEM_PROMPT = (
    "You are a senior engineering manager. You are given per-commit audit "
    "results for one developer's week (quality scores, complexity, estimated "
    "vs. traditional minutes, and notable issues) plus aggregate time/leverage "
    "figures. Return STRICT JSON, no prose, no fences:\n"
    "{\n"
    '  "weekly_summary": "<2-4 sentences on output, quality trend, and AI '
    'leverage>",\n'
    '  "recommendations": ["<rec 1>", "<rec 2>", "<rec 3>"]\n'
    "}\n"
    "Exactly three concrete, actionable recommendations aimed at this developer."
)


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass
class CommitAnalysis:
    sha: str
    quality_score: int
    strengths: list[str]
    issues: list[str]
    evidence: list[str]
    complexity: str
    estimated_active_minutes: int
    traditional_estimate_minutes: int
    reasoning: str
    parse_failed: bool = False
    raw: dict = field(default_factory=dict)


@dataclass
class WeeklySummary:
    summary: str
    recommendations: list[str]
    raw: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #
def build_commit_prompt(commit: Commit, redact: bool = False) -> str:
    """Render one commit into the user message for the model."""
    lines = [f"Commit message:\n{commit.message or '(empty)'}", ""]
    lines.append(f"Files changed ({len(commit.files)}):")
    for f in commit.files:
        lines.append(f"- {f.status} {f.path} (+{f.additions}/-{f.deletions})")
    lines.append("")
    lines.append(f"Totals: +{commit.additions}/-{commit.deletions}")

    if not redact:
        lines.append("\nDiffs (patches truncated to 3000 chars each):")
        for f in commit.files:
            if f.patch:
                lines.append(f"\n### {f.path}\n{f.patch}")
            else:
                lines.append(f"\n### {f.path}\n(no patch available — binary or too large)")
    return "\n".join(lines)


def _coerce_int(value, default: int = 0) -> int:
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return default


def _parse_commit_json(sha: str, text: str) -> CommitAnalysis:
    """Parse the model's JSON into a CommitAnalysis (raises on bad JSON/shape)."""
    data = json.loads(text)
    quality = data.get("quality") or {}
    score = _coerce_int(quality.get("score"), 0)
    score = min(10, max(0, score))  # clamp to rubric range
    return CommitAnalysis(
        sha=sha,
        quality_score=score,
        strengths=[str(s) for s in (quality.get("strengths") or [])],
        issues=[str(s) for s in (quality.get("issues") or [])],
        evidence=[str(s) for s in (quality.get("evidence") or [])],
        complexity=str(data.get("complexity", "routine")),
        estimated_active_minutes=_coerce_int(data.get("estimated_active_minutes")),
        traditional_estimate_minutes=_coerce_int(
            data.get("traditional_estimate_minutes")
        ),
        reasoning=str(data.get("reasoning", "")),
        raw=data,
    )


# --------------------------------------------------------------------------- #
# OpenAI client
# --------------------------------------------------------------------------- #
def make_openai_client():
    from openai import OpenAI

    from . import config

    return OpenAI(api_key=config.openai_key())


def _chat_json(client, system: str, user: str, model: str) -> str:
    resp = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


# --------------------------------------------------------------------------- #
# Per-commit analysis
# --------------------------------------------------------------------------- #
def analyze_commit(
    client, commit: Commit, *, redact: bool = False, model: str = MODEL
) -> CommitAnalysis:
    """Analyze one commit; retry once on a JSON parse failure."""
    system = SYSTEM_PROMPT + (_REDACT_NOTE if redact else "")
    user = build_commit_prompt(commit, redact=redact)

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            text = _chat_json(client, system, user, model)
            analysis = _parse_commit_json(commit.sha, text)
            if redact:
                analysis.evidence = []  # belt-and-suspenders: never leak quotes
            return analysis
        except (json.JSONDecodeError, KeyError, TypeError) as err:
            last_err = err
            # Nudge the model harder on the retry.
            user = (
                user
                + "\n\nIMPORTANT: your previous reply was not valid JSON matching "
                "the required schema. Reply with ONLY the JSON object."
            )

    # Both attempts failed to parse — record a sentinel so one bad commit
    # cannot sink the whole report.
    return CommitAnalysis(
        sha=commit.sha,
        quality_score=0,
        strengths=[],
        issues=[f"analysis unavailable (LLM parse failure: {last_err})"],
        evidence=[],
        complexity="unknown",
        estimated_active_minutes=0,
        traditional_estimate_minutes=0,
        reasoning="Model did not return parseable JSON after one retry.",
        parse_failed=True,
    )


def analyze_commits(
    commits: Iterable[Commit],
    *,
    redact: bool = False,
    model: str = MODEL,
    client=None,
    progress: Callable[[int, int, Commit], None] | None = None,
) -> dict[str, CommitAnalysis]:
    """Analyze every commit, returning {sha: CommitAnalysis}."""
    commits = list(commits)
    client = client or make_openai_client()
    results: dict[str, CommitAnalysis] = {}
    for i, commit in enumerate(commits, 1):
        if progress:
            progress(i, len(commits), commit)
        results[commit.sha] = analyze_commit(
            client, commit, redact=redact, model=model
        )
    return results


# --------------------------------------------------------------------------- #
# Aggregate weekly summary
# --------------------------------------------------------------------------- #
def _aggregate_digest(
    commits: list[Commit],
    analyses: Mapping[str, CommitAnalysis],
    stats: Mapping[str, float],
) -> str:
    lines = ["Aggregate figures:"]
    for k, v in stats.items():
        lines.append(f"- {k}: {v}")
    lines.append("\nPer-commit results:")
    by_sha = {c.sha: c for c in commits}
    for sha, a in analyses.items():
        summary = by_sha[sha].summary if sha in by_sha else sha[:7]
        issue = f" | issue: {a.issues[0]}" if a.issues else ""
        lines.append(
            f"- {sha[:7]} score={a.quality_score} {a.complexity} "
            f"est={a.estimated_active_minutes}m trad={a.traditional_estimate_minutes}m"
            f" :: {summary[:60]}{issue}"
        )
    return "\n".join(lines)


def aggregate_summary(
    commits: Iterable[Commit],
    analyses: Mapping[str, CommitAnalysis],
    stats: Mapping[str, float],
    *,
    model: str = MODEL,
    client=None,
) -> WeeklySummary:
    """One call: weekly narrative + top-3 recommendations."""
    commits = list(commits)
    client = client or make_openai_client()
    user = _aggregate_digest(commits, analyses, stats)
    try:
        text = _chat_json(client, AGGREGATE_SYSTEM_PROMPT, user, model)
        data = json.loads(text)
        recs = [str(r) for r in (data.get("recommendations") or [])][:3]
        return WeeklySummary(
            summary=str(data.get("weekly_summary", "")),
            recommendations=recs,
            raw=data,
        )
    except (json.JSONDecodeError, KeyError, TypeError) as err:
        return WeeklySummary(
            summary=f"(weekly summary unavailable: {err})",
            recommendations=[],
        )


# --------------------------------------------------------------------------- #
# Convenience reducers
# --------------------------------------------------------------------------- #
def minutes_by_sha(analyses: Mapping[str, CommitAnalysis]) -> dict[str, float]:
    """sha -> estimated_active_minutes, for cluster.apply_llm_estimates."""
    return {sha: float(a.estimated_active_minutes) for sha, a in analyses.items()}


def total_traditional_minutes(analyses: Mapping[str, CommitAnalysis]) -> float:
    return float(sum(a.traditional_estimate_minutes for a in analyses.values()))


def total_estimated_minutes(analyses: Mapping[str, CommitAnalysis]) -> float:
    return float(sum(a.estimated_active_minutes for a in analyses.values()))


def average_quality(analyses: Mapping[str, CommitAnalysis]) -> float:
    scored = [a.quality_score for a in analyses.values() if not a.parse_failed]
    return round(sum(scored) / len(scored), 1) if scored else 0.0
