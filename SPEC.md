# SPEC — Commit Analyzer

A CLI tool that pulls a developer's commits from GitHub over a time window and produces a markdown report on (a) code quality and (b) estimated active hours spent writing the code. Assume the developer uses AI coding tools heavily.

## Build order (do these in sequence, commit after each works)

1. **Fetcher** — get commits + diffs from GitHub.
2. **Session clustering** — the time-estimation core.
3. **LLM analysis** — quality scoring + per-commit estimation.
4. **Report generator** — markdown out.
5. **CLI wiring + redact mode.**

Commit at every working checkpoint with a clear message. Test against THIS repo as you build (it accumulates real commits during the session).

## Stack

- **Python via conda on WSL (Ubuntu).** Assume an activated conda env; install deps with `pip` inside it. Document the env name in the README and the exact `conda create`/`activate` lines.
- Python 3.11+, single package: `fetch.py`, `cluster.py`, `analyze.py`, `report.py`, `cli.py`.
- `httpx` for GitHub API, `openai` SDK for the LLM (GPT-4o), `click` or argparse for CLI.
- Read `GITHUB_TOKEN` and `OPENAI_API_KEY` from a **shared parent env file at `../secondtalent/.env`** (the repo lives inside `secondtalent/`). Load it explicitly: `load_dotenv(dotenv_path="../.env")` (adjust depth to where the repo sits relative to `secondtalent/`). Do NOT assume keys are in the repo's own dir. Fail loudly with a clear message if either var is missing.
- Ensure `.env` is gitignored. Commit only `.env.example` with placeholder values.

## 1. Fetcher (`fetch.py`)

- Input: one or more `owner/repo` strings, a `--days` window (default 7).
- Use GitHub REST: `GET /repos/{owner}/{repo}/commits?since=<iso>` for the list, then `GET /repos/{owner}/{repo}/commits/{sha}` per commit for file stats + patch.
- Authenticate with `GITHUB_TOKEN` (raises rate limit to 5000/hr, enables private repos).
- For each commit collect: sha, author timestamp, message, files (path, additions, deletions, status), and per-file patch text.
- **Truncate each file patch to ~3000 chars** with a `[...truncated]` marker before it ever leaves this module. This prevents context blowups on lockfiles / generated code.

## 2. Session clustering (`cluster.py`)

This is the part git can't tell you directly, so it's the most important.

- Sort all commits across all repos by author timestamp ascending.
- Split into sessions wherever the gap between consecutive commits exceeds **90 minutes** (make it a constant, `SESSION_GAP_MIN = 90`).
- Session active time = (last commit ts − first commit ts) within the session.
- **Lead-in problem:** the first commit of a session has invisible work before it (you coded before committing). Add an LLM-estimated lead-in, **capped at 60 min**, so it can't hallucinate huge values.
- **Single-commit sessions:** no duration signal at all — fall back to pure LLM estimate from the diff.
- Output per session: duration, commit count, list of commits.

## 3. LLM analysis (`analyze.py`)

One call per commit. Use **GPT-4o with `response_format={"type": "json_object"}`** for reliable parsing; retry once on parse failure.

System prompt:
> You are a senior engineering manager auditing one week of a developer's commits. The developer uses AI coding tools, so estimate time from the NOVELTY of the change, not its size — large mechanical or generated diffs are fast; small algorithmic or architectural changes are slow. For the commit you receive (message, files, additions/deletions, truncated patch) return STRICT JSON, no prose, no markdown fences:
> ```
> {
>   "quality": {"score": 1-10, "strengths": [..], "issues": [..], "evidence": [".. short quoted line .."]},
>   "complexity": "trivial|routine|complex|novel",
>   "estimated_active_minutes": <int>,
>   "traditional_estimate_minutes": <int, how long a solo dev WITHOUT AI tools would take>,
>   "reasoning": "<one sentence>"
> }
> ```

- Quality rubric to bake into the prompt: correctness risk, readability/naming, structure & modularity, error handling, test presence, commit hygiene.
- `traditional_estimate_minutes` vs the session-clustered actual = the **leverage metric**. Surface it.
- After per-commit calls, make ONE aggregate call: weekly summary + top 3 recommendations.

## 4. Report (`report.py`)

Markdown `report.md`:

- **Header:** repos analyzed, window, total commits, total sessions.
- **Time summary:** inferred active hours (from clustering + lead-ins), per-day breakdown, session list with durations.
- **Leverage:** sum of `traditional_estimate_minutes` vs inferred actual, expressed as a multiplier.
- **Quality:** weekly average score, per-commit table (sha, message, score, complexity, est. minutes), with evidence under each.
- **Recommendations:** the aggregate top 3.
- **Limitations:** state plainly what the tool cannot know — squashed commits hide sessions, end-of-session committers inflate per-commit estimates, timestamps can be amended, thinking/reading time is invisible, lead-in is an estimate.

## 5. CLI + redact (`cli.py`)

- `python -m analyzer --repo owner/name [--repo owner/name2] --days 7 [--author <you>] [--redact] [--out report.md]`
- `--author`: filter commits to a single author, **defaulting to you**. The fetcher pulls all commits on the repo (including peers'), but analysis scopes to your authored commits unless told otherwise — so the hours/quality/leverage numbers are genuinely yours. Demo this filtering on a shared repo to show multi-contributor awareness.
- `--redact`: mask file paths (keep extension), replace commit messages with `[redacted]`, and skip evidence extraction — **redact at the data layer, before the LLM call**, not on the finished report. This keeps sensitive text out of the API request and the terminal.
- Default (no `--redact`) = full clear-text output.

## Deliverables

- Working tool.
- `sample_output.md` — a clear-text run against the assessment repos themselves.
- README with usage + the `docs/approach.png` diagram.