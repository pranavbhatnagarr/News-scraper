"""SQLite storage.

The schema is deliberately one table plus a run log. `canonical_url` carries a
UNIQUE constraint, which is what makes the whole pipeline safely re-runnable:
every insert is an INSERT OR IGNORE, so polling the same feed every 15 minutes
costs nothing but a few no-op writes.

`status` tracks only the body-extraction stage:
    new        -> discovered in a feed, body not fetched yet
    extracted  -> body text pulled from the article page
    failed     -> body extraction gave up (paywall, JS shell); kept as a
                  tombstone so we never retry it forever

Classification and summarisation are tracked by column nullity rather than by
status, because they are independent of extraction. A paywalled article has no
body but still has a headline and lead, so it can be classified into a topic
even though it can never be summarised. Gating both stages on one linear status
would silently drop those articles out of the topic feeds entirely.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id             INTEGER PRIMARY KEY,
    canonical_url  TEXT NOT NULL UNIQUE,
    url            TEXT NOT NULL,
    title          TEXT NOT NULL,
    title_key      TEXT,
    publisher      TEXT,
    source         TEXT,
    author         TEXT,
    published_at   TEXT,
    discovered_at  TEXT NOT NULL,
    feed_topic     TEXT,
    lead           TEXT,
    body           TEXT,
    body_chars     INTEGER DEFAULT 0,
    topic          TEXT,
    australian     INTEGER,
    importance     INTEGER,
    summary        TEXT,
    key_points     TEXT,
    entities       TEXT,
    enriched_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'new',
    error          TEXT
);

CREATE INDEX IF NOT EXISTS idx_articles_status       ON articles(status);
CREATE INDEX IF NOT EXISTS idx_articles_topic        ON articles(topic);
CREATE INDEX IF NOT EXISTS idx_articles_published    ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_topic_pub    ON articles(topic, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_title_key    ON articles(title_key);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    discovered    INTEGER DEFAULT 0,
    extracted     INTEGER DEFAULT 0,
    classified    INTEGER DEFAULT 0,
    summarised    INTEGER DEFAULT 0,
    errors        INTEGER DEFAULT 0,
    note          TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets `list`/`digest` read while `watch` is mid-write.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created.

    Cheap forward-only migration so an existing news.db keeps working instead
    of failing with 'no such column' on the next run.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(articles)")}
    for column, ddl in (("title_key", "ALTER TABLE articles ADD COLUMN title_key TEXT"),):
        if column not in existing:
            conn.execute(ddl)


def insert_discovered(conn: sqlite3.Connection, rows: Iterable[Dict[str, Any]]) -> int:
    """Insert newly-seen feed entries. Returns how many were actually new."""
    sql = """
        INSERT OR IGNORE INTO articles
            (canonical_url, url, title, title_key, publisher, source, author,
             published_at, discovered_at, feed_topic, lead, status)
        VALUES
            (:canonical_url, :url, :title, :title_key, :publisher, :source, :author,
             :published_at, :discovered_at, :feed_topic, :lead, 'new')
    """
    added = 0
    for row in rows:
        cur = conn.execute(sql, row)
        added += cur.rowcount
    conn.commit()
    return added


def pending(conn: sqlite3.Connection, status: str, limit: int) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM articles WHERE status = ? ORDER BY published_at DESC LIMIT ?",
            (status, limit),
        )
    )


def save_body(
    conn: sqlite3.Connection,
    article_id: int,
    body: Optional[str],
    error: Optional[str] = None,
) -> None:
    if body:
        conn.execute(
            "UPDATE articles SET body = ?, body_chars = ?, status = 'extracted',"
            " error = NULL WHERE id = ?",
            (body, len(body), article_id),
        )
    else:
        conn.execute(
            "UPDATE articles SET status = 'failed', error = ? WHERE id = ?",
            (error or "no body extracted", article_id),
        )
    conn.commit()


def needs_classification(conn: sqlite3.Connection, limit: int) -> List[sqlite3.Row]:
    """Articles whose body stage has finished (either way) but have no topic."""
    return list(
        conn.execute(
            """
            SELECT * FROM articles
            WHERE topic IS NULL AND status IN ('extracted', 'failed')
            ORDER BY COALESCE(published_at, discovered_at) DESC
            LIMIT ?
            """,
            (limit,),
        )
    )


def save_classification(
    conn: sqlite3.Connection,
    article_id: int,
    topic: str,
    australian: bool,
    importance: int,
    enriched_at: str,
) -> None:
    conn.execute(
        "UPDATE articles SET topic = ?, australian = ?, importance = ?,"
        " enriched_at = ? WHERE id = ?",
        (topic, 1 if australian else 0, importance, enriched_at, article_id),
    )
    conn.commit()


def save_summary(
    conn: sqlite3.Connection,
    article_id: int,
    summary: str,
    key_points: List[str],
    entities: List[str],
) -> None:
    conn.execute(
        "UPDATE articles SET summary = ?, key_points = ?, entities = ? WHERE id = ?",
        (summary, json.dumps(key_points), json.dumps(entities), article_id),
    )
    conn.commit()


def needs_summary(
    conn: sqlite3.Connection, min_importance: int, limit: int
) -> List[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT * FROM articles
            WHERE topic IS NOT NULL
              AND summary IS NULL
              AND status = 'extracted'
              AND importance >= ?
              AND body_chars > 400
            ORDER BY importance DESC, published_at DESC
            LIMIT ?
            """,
            (min_importance, limit),
        )
    )


def query(
    conn: sqlite3.Connection,
    topic: Optional[str] = None,
    since_iso: Optional[str] = None,
    australian_only: bool = False,
    min_importance: int = 1,
    limit: int = 50,
    collapse_duplicates: bool = True,
) -> List[sqlite3.Row]:
    clauses = ["topic IS NOT NULL"]
    params: List[Any] = []
    if topic:
        clauses.append("topic = ?")
        params.append(topic)
    if since_iso:
        # COALESCE so articles with no publisher date still surface by
        # discovery time rather than vanishing from every time-bounded query.
        clauses.append("COALESCE(published_at, discovered_at) >= ?")
        params.append(since_iso)
    if australian_only:
        clauses.append("australian = 1")
    if min_importance > 1:
        clauses.append("importance >= ?")
        params.append(min_importance)
    where = " AND ".join(clauses)
    params.append(limit)

    if collapse_duplicates:
        # Syndicated copy appears under several mastheads with different URLs.
        # Keep one row per headline fingerprint, preferring the copy we
        # actually managed to extract and summarise so the digest is not
        # silently anchored to a paywalled sibling.
        #
        # COALESCE on the id keeps rows with no fingerprint from collapsing
        # into a single bucket.
        sql = (
            "SELECT * FROM articles WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, COALESCE(NULLIF(title_key, ''), CAST(id AS TEXT)) AS k,"
            "           ROW_NUMBER() OVER ("
            "             PARTITION BY COALESCE(NULLIF(title_key, ''), CAST(id AS TEXT))"
            "             ORDER BY (summary IS NOT NULL) DESC, body_chars DESC, id ASC"
            "           ) AS rn"
            "    FROM articles WHERE " + where +
            "  ) ranked WHERE rn = 1"
            ") ORDER BY importance DESC, COALESCE(published_at, discovered_at) DESC"
            " LIMIT ?"
        )
    else:
        sql = (
            "SELECT * FROM articles WHERE " + where
            + " ORDER BY importance DESC, COALESCE(published_at, discovered_at) DESC"
            " LIMIT ?"
        )
    return list(conn.execute(sql, params))


def topic_counts(conn: sqlite3.Connection, since_iso: Optional[str] = None):
    sql = "SELECT topic, COUNT(*) AS n FROM articles WHERE topic IS NOT NULL"
    params: List[Any] = []
    if since_iso:
        sql += " AND COALESCE(published_at, discovered_at) >= ?"
        params.append(since_iso)
    sql += " GROUP BY topic ORDER BY n DESC"
    return list(conn.execute(sql, params))


def stats(conn: sqlite3.Connection) -> Dict[str, int]:
    out = {}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM articles GROUP BY status"):
        out[row["status"]] = row["n"]
    scalars = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(topic IS NOT NULL) AS classified,"
        " SUM(summary IS NOT NULL) AS summarised"
        " FROM articles"
    ).fetchone()
    out["total"] = scalars["total"] or 0
    out["classified"] = scalars["classified"] or 0
    out["summarised"] = scalars["summarised"] or 0
    return out


def start_run(conn: sqlite3.Connection, started_at: str) -> int:
    cur = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (started_at,))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, finished_at: str, **counts) -> None:
    conn.execute(
        """
        UPDATE runs SET finished_at = ?, discovered = ?, extracted = ?,
            classified = ?, summarised = ?, errors = ?, note = ?
        WHERE id = ?
        """,
        (
            finished_at,
            counts.get("discovered", 0),
            counts.get("extracted", 0),
            counts.get("classified", 0),
            counts.get("summarised", 0),
            counts.get("errors", 0),
            counts.get("note"),
            run_id,
        ),
    )
    conn.commit()
