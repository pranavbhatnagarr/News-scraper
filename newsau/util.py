"""Small shared helpers: URL canonicalisation and time handling."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from html import unescape
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dateutil import parser as date_parser

# Query parameters that identify a campaign or referrer rather than the
# article itself. Stripping them is what makes deduplication actually work —
# the same story shared from three places otherwise looks like three rows.
_TRACKING_PREFIXES = ("utm_", "pk_", "at_", "ito", "mc_")
_TRACKING_EXACT = {
    "cmpid", "CMP", "cmp", "fbclid", "gclid", "igshid", "ref", "smid",
    "spref", "s_cid", "sr_share", "taid", "__twitter_impression",
}


def canonical_url(url: str) -> str:
    """Normalise a URL so the same article always maps to the same string.

    Lowercases scheme/host, drops the fragment, removes tracking parameters,
    sorts what remains, and strips a trailing slash.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())

    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Drop default ports so :443 and bare host don't split into two rows.
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    elif netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k not in _TRACKING_EXACT
        and not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ]
    query = urlencode(sorted(kept))

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    return urlunsplit((scheme, netloc, path, query, ""))


def strip_tracking(url: str) -> str:
    """Remove tracking parameters but leave the URL otherwise untouched.

    Unlike `canonical_url` this is safe to click: the host keeps its `www.`
    and the path keeps its trailing slash, so it is what we show to humans.
    `canonical_url` is more aggressive and exists only as a dedup key.
    """
    if not url:
        return ""
    parts = urlsplit(url.strip())
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in _TRACKING_EXACT
        and not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )


_TITLE_NOISE_RE = re.compile(r"[^a-z0-9 ]+")


def title_key(title: str) -> str:
    """A fingerprint for cross-publisher duplicate detection.

    Nine Entertainment runs the Herald, The Age, WAtoday and Brisbane Times off
    identical copy, so the same article arrives four times under four domains.
    URL dedup cannot see that. Hashing the normalised headline can.

    This is intentionally strict — exact headline match after normalisation.
    Fuzzy matching would start collapsing genuinely different stories that
    share a stock headline like "Markets wrap".
    """
    if not title:
        return ""
    normalised = _WS_RE.sub(" ", _TITLE_NOISE_RE.sub(" ", title.lower())).strip()
    if not normalised:
        return ""
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:16]


def parse_date(value: Optional[str]) -> Optional[str]:
    """Parse any publisher date format into an ISO-8601 UTC string.

    Every publisher formats dates differently; normalising once at the edge
    means nothing downstream ever has to think about it again.
    """
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(value: Optional[str]) -> str:
    """Flatten a feed's HTML summary into plain text.

    Feed <summary> fields are HTML often enough that passing them straight to
    the classifier wastes tokens on markup. This is deliberately crude — it
    only ever sees short summary blurbs, never article bodies.
    """
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    return _WS_RE.sub(" ", unescape(text)).strip()


def clip(text: str, limit: int) -> str:
    """Trim text to a character budget on a word boundary where possible."""
    if not text or len(text) <= limit:
        return text or ""
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut + "…"
