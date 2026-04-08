"""
config.py — Load configuration from .env file or environment variables.
"""

import os
from pathlib import Path


def _load_env(path: str = ".env") -> None:
    """Minimal .env loader — supports KEY=VALUE and KEY="VALUE" syntax."""
    env_file = Path(path)
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_env()

# ── Required ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN: str = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID: str = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_TOKEN: str = os.environ["GITHUB_TOKEN"]
GITHUB_REPO: str = os.environ["GITHUB_REPO"]          # e.g. george1-adel/open-redirect-workflow

# ── Optional ──────────────────────────────────────────────────────────────────
GITHUB_WORKFLOW_ID: str = os.environ.get("GITHUB_WORKFLOW_ID", "scan.yml")
GITHUB_BRANCH: str = os.environ.get("GITHUB_BRANCH", "main")
BATCH_SIZE: int = int(os.environ.get("BATCH_SIZE", "50"))
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL", "30"))   # seconds
POLL_TIMEOUT: int = int(os.environ.get("POLL_TIMEOUT", "900"))    # 15 minutes
