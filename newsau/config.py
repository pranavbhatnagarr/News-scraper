"""Configuration: environment, source list, and the topic vocabulary."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, NamedTuple

import yaml

ROOT = Path(__file__).resolve().parent.parent

# The topic vocabulary. This is a closed set on purpose: the classifier is
# constrained to these values, so downstream queries never have to cope with
# a topic the model invented. Add a topic here and it becomes available
# everywhere — schema, CLI filters, digests.
TOPICS = [
    "ai-tech",
    "sports",
    "business",
    "politics",
    "health",
    "world",
    "entertainment",
    "lifestyle",
    "other",
]

# Feeds may use "general" as a hint, but it is never a final topic — every
# article gets classified into one of TOPICS.
GENERAL_HINT = "general"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Real environment variables always win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Settings(NamedTuple):
    model: str
    api_key: str
    db_path: Path
    poll_interval_minutes: int
    min_importance_to_summarise: int
    classify_batch_size: int
    contact_email: str
    keep_raw_html: bool
    raw_dir: Path

    @property
    def user_agent(self) -> str:
        return "newsau/0.1 (+{})".format(self.contact_email)

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)


def load_settings() -> Settings:
    return Settings(
        model=os.environ.get("NEWS_MODEL", "claude-opus-5"),
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        db_path=Path(os.environ.get("DB_PATH", str(ROOT / "news.db"))),
        poll_interval_minutes=_env_int("POLL_INTERVAL_MINUTES", 15),
        min_importance_to_summarise=_env_int("MIN_IMPORTANCE_TO_SUMMARISE", 3),
        classify_batch_size=_env_int("CLASSIFY_BATCH_SIZE", 12),
        contact_email=os.environ.get("CONTACT_EMAIL", "unknown@example.com"),
        keep_raw_html=_env_bool("KEEP_RAW_HTML", False),
        raw_dir=ROOT / "raw",
    )


class Source(NamedTuple):
    name: str
    publisher: str
    url: str
    topic_hint: str
    max_items: int


def load_sources(path: Path = None) -> List[Source]:
    path = path or (ROOT / "sources.yaml")
    data: Dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = data.get("defaults") or {}
    default_max = int(defaults.get("max_items_per_poll", 40))

    sources = []
    for entry in data.get("sources") or []:
        sources.append(
            Source(
                name=entry["name"],
                publisher=entry.get("publisher", entry["name"]),
                url=entry["url"],
                topic_hint=entry.get("topic_hint", GENERAL_HINT),
                max_items=int(entry.get("max_items_per_poll", default_max)),
            )
        )
    return sources
