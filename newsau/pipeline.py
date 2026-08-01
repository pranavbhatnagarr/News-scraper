"""One pass of the pipeline: poll -> extract -> classify -> summarise.

Every stage is bounded and independently resumable. If a run is killed halfway
through, the next run picks up whatever is still pending — nothing is held in
memory between stages, it all lives in SQLite.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from . import db, enrich, fetch
from .config import Settings, Source
from .util import now_utc_iso

log = logging.getLogger("newsau.pipeline")

# Per-run ceilings. These bound the cost and wall time of a single pass so a
# backlog drains over several polls instead of one enormous run.
MAX_EXTRACT_PER_RUN = 120
MAX_CLASSIFY_PER_RUN = 120
MAX_SUMMARISE_PER_RUN = 25
EXTRACT_WORKERS = 8


def run_once(
    settings: Settings,
    sources: List[Source],
    skip_ai: bool = False,
) -> Dict[str, int]:
    conn = db.connect(settings.db_path)
    db.init(conn)

    started = now_utc_iso()
    run_id = db.start_run(conn, started)
    counts = {
        "discovered": 0,
        "extracted": 0,
        "classified": 0,
        "summarised": 0,
        "errors": 0,
    }

    limiter = fetch.DomainRateLimiter()
    client = fetch.make_client(settings)

    try:
        # --- Stage 1: poll every feed -------------------------------------
        rows, failures = fetch.poll_all_feeds(client, sources, limiter)
        for name, error in failures:
            log.warning("feed %s: %s", name, error)
        counts["errors"] += len(failures)

        counts["discovered"] = db.insert_discovered(conn, rows)
        log.info(
            "polled %d feeds, saw %d entries, %d new",
            len(sources),
            len(rows),
            counts["discovered"],
        )

        # --- Stage 2: fetch article bodies --------------------------------
        counts["extracted"] = _extract_bodies(conn, client, limiter, settings)

        # --- Stages 3 & 4: AI enrichment ----------------------------------
        if skip_ai:
            log.info("skipping AI enrichment (--no-ai)")
        elif not settings.has_api_key:
            log.warning(
                "ANTHROPIC_API_KEY not set - articles stored but not classified. "
                "Set it in .env to enable topic separation and summaries."
            )
        else:
            ai = enrich.build_client(settings.api_key)
            counts["classified"] = _classify(conn, ai, settings)
            counts["summarised"] = _summarise(conn, ai, settings)

    finally:
        client.close()
        db.finish_run(conn, run_id, now_utc_iso(), **counts)
        conn.close()

    return counts


def _extract_bodies(conn, client, limiter, settings: Settings) -> int:
    todo = db.pending(conn, "new", MAX_EXTRACT_PER_RUN)
    if not todo:
        return 0

    log.info("extracting %d article bodies", len(todo))
    done = 0

    def work(row):
        body, error = fetch.extract_body(client, row["url"], limiter, settings)
        return row["id"], body, error

    with ThreadPoolExecutor(max_workers=EXTRACT_WORKERS) as pool:
        futures = [pool.submit(work, row) for row in todo]
        for future in as_completed(futures):
            try:
                article_id, body, error = future.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("extraction crashed: %s", exc)
                continue
            # Writes are serialised here on the main thread; SQLite does not
            # want concurrent writers from a pool.
            db.save_body(conn, article_id, body, error)
            if body:
                done += 1

    log.info("extracted %d bodies (%d failed)", done, len(todo) - done)
    return done


def _classify(conn, ai, settings: Settings) -> int:
    todo = db.needs_classification(conn, MAX_CLASSIFY_PER_RUN)
    if not todo:
        return 0

    log.info("classifying %d articles", len(todo))
    size = max(1, settings.classify_batch_size)
    total = 0

    for start in range(0, len(todo), size):
        batch = todo[start : start + size]
        try:
            results = enrich.classify_batch(ai, settings.model, batch)
        except Exception as exc:  # noqa: BLE001 - a bad batch must not kill the run
            log.warning("classification batch failed: %s", exc)
            continue

        stamp = now_utc_iso()
        for article_id, result in results.items():
            db.save_classification(
                conn,
                article_id,
                result.topic,
                result.australian,
                result.importance,
                stamp,
            )
            total += 1

    log.info("classified %d articles", total)
    return total


def _summarise(conn, ai, settings: Settings) -> int:
    todo = db.needs_summary(
        conn, settings.min_importance_to_summarise, MAX_SUMMARISE_PER_RUN
    )
    if not todo:
        return 0

    log.info("summarising %d articles", len(todo))
    total = 0

    for row in todo:
        try:
            result = enrich.summarise(
                ai, settings.model, row["title"], row["publisher"], row["body"]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("summary failed for %s: %s", row["url"], exc)
            continue
        if result is None:
            continue
        db.save_summary(
            conn, row["id"], result.summary, result.key_points, result.entities
        )
        total += 1

    log.info("summarised %d articles", total)
    return total
