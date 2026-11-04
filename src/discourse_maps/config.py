"""Settings from the environment (and an optional, git-ignored .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

load_dotenv(PROJECT_ROOT / ".env")

LOCAL_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
TARGETS = ("local", "cloud")


def target() -> str:
    """Which database the pipeline uses: `local` (default) or `cloud` (hosted Supabase).

    Set with `dm --target cloud ...` or the `DM_TARGET` environment variable. Cloud settings come
    from the git-ignored `.env.cloud` file (`DATABASE_URL_CLOUD`).
    """
    value = os.environ.get("DM_TARGET", "local").strip().lower()
    if value not in TARGETS:
        raise ValueError(f"DM_TARGET must be one of {TARGETS}, not {value!r}")
    return value


@dataclass(frozen=True)
class Settings:
    target: str
    database_url: str
    scholarly_mailto: str | None
    openalex_api_key: str | None
    embedding_provider: str
    roam_graph: str | None
    roam_api_token: str | None


def _database_url(which: str) -> str:
    if which == "local":
        return os.environ.get("DATABASE_URL", LOCAL_DATABASE_URL)
    load_dotenv(PROJECT_ROOT / ".env.cloud")
    url = os.environ.get("DATABASE_URL_CLOUD")
    if not url:
        raise RuntimeError("Target is cloud, but DATABASE_URL_CLOUD is not set (expected in .env.cloud).")
    return url


def get_settings() -> Settings:
    which = target()
    return Settings(
        target=which,
        database_url=_database_url(which),
        scholarly_mailto=os.environ.get("SCHOLARLY_MAILTO") or None,
        openalex_api_key=os.environ.get("OPENALEX_API_KEY") or None,
        embedding_provider=os.environ.get("EMBEDDING_PROVIDER", "qwen3"),
        roam_graph=os.environ.get("ROAM_GRAPH") or None,
        roam_api_token=os.environ.get("ROAM_API_TOKEN") or None,
    )
