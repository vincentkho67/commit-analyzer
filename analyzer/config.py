"""Credential loading.

Keys live in a SHARED parent env file one level above the repo
(``secondtalent/.env``), not in the repo's own directory — so sibling
projects under ``secondtalent/`` can share one set of credentials.

The path is resolved relative to this file (not the current working
directory) so the tool works no matter where it is invoked from. It can be
overridden with the ``COMMIT_ANALYZER_DOTENV`` environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# analyzer/config.py -> analyzer/ -> commit-analyzer/ -> secondtalent/
# parents[2] is the shared "secondtalent/" parent that holds .env
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DOTENV = _REPO_ROOT.parent / ".env"

_loaded = False


def dotenv_path() -> Path:
    """The .env file we load from (override with COMMIT_ANALYZER_DOTENV)."""
    override = os.environ.get("COMMIT_ANALYZER_DOTENV")
    return Path(override).expanduser() if override else _DEFAULT_DOTENV


def load_parent_dotenv() -> None:
    """Load the shared parent .env exactly once. Pre-existing real environment
    variables win (we never override them)."""
    global _loaded
    if _loaded:
        return
    load_dotenv(dotenv_path=str(dotenv_path()), override=False)
    _loaded = True


def _require(name: str) -> str:
    load_parent_dotenv()
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"ERROR: required environment variable {name} is not set.\n"
            f"  Looked in: {dotenv_path()}\n"
            f"  Fix: copy .env.example to that path and fill in real values,\n"
            f"       or export {name} in your shell, or set COMMIT_ANALYZER_DOTENV."
        )
    return value


def github_token() -> str:
    return _require("GITHUB_TOKEN")


def openai_key() -> str:
    return _require("OPENAI_API_KEY")


def require_keys(need_openai: bool = True) -> None:
    """Validate required keys up front so the tool fails loudly and early."""
    github_token()
    if need_openai:
        openai_key()
