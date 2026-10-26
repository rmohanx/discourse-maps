"""Settings from the environment (and an optional, git-ignored .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    database_url: str
    scholarly_mailto: str | None
    openalex_api_key: str | None
    embedding_provider: str
    roam_graph: str | None
    roam_api_token: str | None


def get_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
        ),
        scholarly_mailto=os.environ.get("SCHOLARLY_MAILTO") or None,
        openalex_api_key=os.environ.get("OPENALEX_API_KEY") or None,
        embedding_provider=os.environ.get("EMBEDDING_PROVIDER", "qwen3"),
        roam_graph=os.environ.get("ROAM_GRAPH") or None,
        roam_api_token=os.environ.get("ROAM_API_TOKEN") or None,
    )
