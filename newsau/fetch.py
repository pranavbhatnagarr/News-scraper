"""Network layer: polite fetching, feed parsing, and article body extraction."""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import feedparser
import httpx
import trafilatura

from .config import Settings, Source
from .util import canonical_url, clip, now_utc_iso, parse_date, strip_html, title_key

# One request per domain per second. Publishers are generous with feeds but
# unkind to anything that hammers article pages, and getting IP-blocked is
# the failure mode that ends a scraper project.
MIN_SECONDS_BETWEEN_REQUESTS = 1.0
REQUEST_TIMEOUT = 20.0


class DomainRateLimiter:
    """Serialises requests per domain without serialising across domains."""

    def __init__(self, min_interval: float = MIN_SECONDS_BETWEEN_REQUESTS):
        self.min_interval = min_interval
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        while True:
            with self._lock:
                now = time.monotonic()
                earliest = self._last.get(host, 0.0) + self.min_interval
                if now >= earliest:
                    self._last[host] = now
                    return
                delay = earliest - now
            time.sleep(delay)


def make_client(settings: Settings) -> httpx.Client:
    return httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-AU,en;q=0.9",
        },
    )


def _get(
    client: httpx.Client, url: str, limiter: DomainRateLimiter
) -> Tuple[Optional[str], Optional[str]]:
    """Fetch a URL. Returns (text, error)."""
    limiter.wait(url)
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        return None, "network: {}".format(exc)
    if response.status_code >= 400:
        return None, "http {}".format(response.status_code)
    return response.text, None


# --------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------


def poll_feed(
    client: httpx.Client, source: Source, limiter: DomainRateLimiter
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Fetch one feed and return rows ready for the database."""
    text, error = _get(client, source.url, limiter)
    if error:
        return [], error

    parsed = feedparser.parse(text)
    if not parsed.entries:
        # bozo means feedparser hit malformed XML. Worth surfacing, because
        # it usually means the publisher swapped the feed for an HTML page.
        detail = getattr(parsed, "bozo_exception", None)
        return [], "no entries{}".format(" ({})".format(detail) if detail else "")

    discovered_at = now_utc_iso()
    rows = []
    for entry in parsed.entries[: source.max_items]:
        link = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not link or not title:
            continue

        published = parse_date(
            entry.get("published") or entry.get("updated") or entry.get("created")
        )
        # Feed summaries are frequently HTML. Strip to text so the classifier
        # sees prose rather than markup.
        lead = strip_html(entry.get("summary") or entry.get("description") or "")
        author = entry.get("author") or ""

        rows.append(
            {
                "canonical_url": canonical_url(link),
                "url": link,
                "title": title,
                "title_key": title_key(title),
                "publisher": source.publisher,
                "source": source.name,
                "author": author[:200],
                "published_at": published,
                "discovered_at": discovered_at,
                "feed_topic": source.topic_hint,
                "lead": clip(lead, 600),
            }
        )
    return rows, None


def poll_all_feeds(
    client: httpx.Client,
    sources: List[Source],
    limiter: DomainRateLimiter,
    max_workers: int = 6,
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str]]]:
    """Poll every feed concurrently. Returns (rows, [(source_name, error)])."""
    rows: List[Dict[str, Any]] = []
    failures: List[Tuple[str, str]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(poll_feed, client, s, limiter): s for s in sources}
        for future in as_completed(futures):
            source = futures[future]
            try:
                got, error = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad feed must not kill the run
                failures.append((source.name, "unexpected: {}".format(exc)))
                continue
            if error:
                failures.append((source.name, error))
            rows.extend(got)

    return rows, failures


# --------------------------------------------------------------------------
# Article bodies
# --------------------------------------------------------------------------


def extract_body(
    client: httpx.Client,
    url: str,
    limiter: DomainRateLimiter,
    settings: Settings,
) -> Tuple[Optional[str], Optional[str]]:
    """Fetch an article page and pull the readable body out of it.

    trafilatura handles boilerplate removal (nav, ads, related-links) across
    almost every publisher without per-site rules, which is why there are no
    per-site selectors in this project.
    """
    html, error = _get(client, url, limiter)
    if error:
        return None, error

    if settings.keep_raw_html:
        _archive_html(url, html, settings)

    body = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
        url=url,
    )
    if not body or len(body.strip()) < 200:
        # Short output almost always means a paywall interstitial or a
        # JS-rendered shell rather than a genuinely short article.
        return None, "body too short ({} chars) - likely paywalled or JS-rendered".format(
            len(body.strip()) if body else 0
        )
    return body.strip(), None


def _archive_html(url: str, html: str, settings: Settings) -> None:
    """Keep the raw HTML so a parser regression can be debugged after the fact."""
    try:
        settings.raw_dir.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20] + ".html"
        (settings.raw_dir / name).write_text(html, encoding="utf-8")
    except OSError:
        pass  # archiving is best-effort; never fail a run over it
