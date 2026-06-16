"""Render docs/approach.png — the commit-analyzer pipeline.

Run with an environment that has matplotlib:
    python docs/make_diagram.py
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

# Palette (muted, non-default)
INK = "#1f2933"
MUTED = "#52606d"
STAGE_FILL = "#eef2f7"
STAGE_EDGE = "#3e5c76"
CLI_FILL = "#3e5c76"
EXT_FILL = "#fbeedb"
EXT_EDGE = "#c08a2d"
OUT_FILL = "#e4f1e8"
OUT_EDGE = "#3f7d52"
ARROW = "#3e5c76"

fig, ax = plt.subplots(figsize=(13, 6.2), dpi=160)
ax.set_xlim(0, 100)
ax.set_ylim(0, 100)
ax.axis("off")


def box(x, y, w, h, fill, edge, lw=1.6):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.6,rounding_size=2.2",
        linewidth=lw, edgecolor=edge, facecolor=fill, mutation_aspect=1))


def stage(x, title, bullets, y=40, w=20, h=30):
    box(x, y, w, h, STAGE_FILL, STAGE_EDGE)
    ax.text(x + w / 2, y + h - 5.5, title, ha="center", va="center",
            fontsize=12.5, fontweight="bold", color=INK)
    ax.text(x + w / 2, y + h - 11, "·" * 0, ha="center")
    ax.text(x + w / 2, y + (h - 13) / 2 + 1, "\n".join(bullets), ha="center",
            va="center", fontsize=8.8, color=MUTED, linespacing=1.5)


def arrow(x1, y1, x2, y2, color=ARROW, lw=2.2, style="-|>"):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style,
                 mutation_scale=18, linewidth=lw, color=color,
                 shrinkA=0, shrinkB=0))


# Title
ax.text(50, 96, "commit-analyzer — pipeline", ha="center", fontsize=17,
        fontweight="bold", color=INK)
ax.text(50, 91, "GitHub commits  →  work sessions  →  LLM audit  →  markdown report",
        ha="center", fontsize=10.5, color=MUTED)

# CLI orchestration band
box(4, 78, 92, 8, CLI_FILL, CLI_FILL)
ax.text(50, 82, "cli.py   ·   python -m analyzer --repo O/N --days 7 [--author] "
        "[--all-authors] [--redact] [--out]",
        ha="center", va="center", fontsize=10.5, color="white", fontweight="bold")

# External inputs / output
box(4, 64, 20, 8, EXT_FILL, EXT_EDGE, lw=1.4)
ax.text(14, 68, "GitHub REST API", ha="center", va="center", fontsize=9.2, color=INK)
box(54, 64, 16, 8, EXT_FILL, EXT_EDGE, lw=1.4)
ax.text(62, 68, "OpenAI GPT-4o", ha="center", va="center", fontsize=9.2, color=INK)
box(78, 8, 18, 8, OUT_FILL, OUT_EDGE, lw=1.6)
ax.text(87, 12, "report.md", ha="center", va="center", fontsize=10.5,
        color=INK, fontweight="bold")

# Four core stages
stage(4, "fetch.py", [
    "GitHub REST",
    "list + per-commit diffs",
    "patch ≤ 3000 chars",
    "ALL authors",
])
stage(28, "cluster.py", [
    "sort by author time",
    "split on > 90-min gap",
    "active = span + lead-in",
    "lead-in ≤ 60 min",
    "1-commit → LLM only",
])
stage(52, "analyze.py", [
    "GPT-4o, JSON mode",
    "novelty, not size",
    "quality 1–10 + evidence",
    "est. vs traditional min",
])
stage(76, "report.py", [
    "time + per-day",
    "leverage multiplier",
    "quality table",
    "limitations",
])

# Stage-to-stage arrows
arrow(24, 55, 28, 55)
arrow(48, 55, 52, 55)
arrow(72, 55, 76, 55)

# Filter + redact happens between fetch and cluster/analyze (data layer)
ax.text(26, 33, "scope to author\n+ optional redact\n(data layer)",
        ha="center", va="center", fontsize=7.8, color="#9a3b3b",
        fontstyle="italic", linespacing=1.4)

# Input/output connector arrows
arrow(14, 78, 14, 70.2)          # CLI -> implies invocation (visual)
arrow(14, 64, 14, 56)            # GitHub API -> fetch
arrow(62, 64, 62, 56)            # OpenAI -> analyze
arrow(86, 40, 86.5, 16.2)        # report stage -> report.md

# CLI band feeds the pipeline
arrow(50, 78, 50, 70.5, color="#90a4b8", lw=1.4)
ax.text(50, 74.4, "loads ../​.env keys · orchestrates", ha="center",
        fontsize=8, color=MUTED)

# Footer note
ax.text(50, 3, "Leverage = Σ traditional-estimate ÷ inferred-actual (clustered)   ·   "
        "redaction strips paths/messages/diffs BEFORE any LLM call",
        ha="center", fontsize=8.4, color=MUTED)

plt.tight_layout()
fig.savefig("docs/approach.png", bbox_inches="tight", facecolor="white")
print("wrote docs/approach.png")
