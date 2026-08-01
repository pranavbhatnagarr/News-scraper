"""Command line interface."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import db, fetch, pipeline
from .config import TOPICS, load_settings, load_sources
from .util import clip, strip_tracking

log = logging.getLogger("newsau")

_SINCE_RE = re.compile(r"^(\d+)\s*([hdm])$", re.IGNORECASE)
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_since(value: Optional[str]) -> Optional[str]:
    """Turn '24h' / '3d' / '90m' into an ISO-8601 UTC cutoff."""
    if not value:
        return None
    match = _SINCE_RE.match(value.strip())
    if not match:
        raise SystemExit("Invalid --since {!r}. Use forms like 90m, 24h, 3d.".format(value))
    amount, unit = int(match.group(1)), match.group(2).lower()
    cutoff = datetime.now(timezone.utc) - timedelta(**{_UNITS[unit]: amount})
    return cutoff.isoformat()


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx logs every request at INFO, which drowns out the pipeline.
    logging.getLogger("httpx").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_init(args) -> int:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init(conn)
    conn.close()
    sources = load_sources()
    print("Initialised {} with {} sources configured.".format(settings.db_path, len(sources)))
    if not settings.has_api_key:
        print(
            "\nNo ANTHROPIC_API_KEY found. Scraping will work; topic "
            "classification and summaries will be skipped.\n"
            "Copy .env.example to .env and add your key to enable them."
        )
    return 0


def cmd_run(args) -> int:
    settings = load_settings()
    sources = load_sources()
    counts = pipeline.run_once(settings, sources, skip_ai=args.no_ai)
    print(
        "run complete: {discovered} new, {extracted} bodies, "
        "{classified} classified, {summarised} summarised, {errors} feed errors".format(
            **counts
        )
    )
    return 0


def cmd_watch(args) -> int:
    settings = load_settings()
    sources = load_sources()
    interval = (args.interval or settings.poll_interval_minutes) * 60

    print(
        "Watching {} sources every {} minutes. Ctrl-C to stop.".format(
            len(sources), interval // 60
        )
    )
    while True:
        started = time.monotonic()
        try:
            counts = pipeline.run_once(settings, sources, skip_ai=args.no_ai)
            log.info(
                "cycle done: %d new, %d bodies, %d classified, %d summarised",
                counts["discovered"],
                counts["extracted"],
                counts["classified"],
                counts["summarised"],
            )
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
        except Exception as exc:  # noqa: BLE001 - the watcher must survive a bad cycle
            log.exception("cycle failed: %s", exc)

        # Sleep the remainder of the interval so a slow cycle doesn't drift
        # the schedule later and later.
        elapsed = time.monotonic() - started
        remaining = max(5.0, interval - elapsed)
        try:
            time.sleep(remaining)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


def cmd_list(args) -> int:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init(conn)
    rows = db.query(
        conn,
        topic=args.topic,
        since_iso=parse_since(args.since),
        australian_only=args.au,
        min_importance=args.min_importance,
        limit=args.limit,
        collapse_duplicates=not args.all_copies,
    )
    conn.close()

    if args.json:
        print(json.dumps([_row_to_dict(r) for r in rows], indent=2, ensure_ascii=False))
        return 0

    if not rows:
        print("No matching articles. Run `python -m newsau run` first.")
        return 0

    for row in rows:
        stars = "*" * (row["importance"] or 0)
        flag = " [AU]" if row["australian"] else ""
        print("\n{}  {:<12} {:<5}{}".format(
            (row["published_at"] or row["discovered_at"] or "")[:16],
            row["topic"] or "?",
            stars,
            flag,
        ))
        print("  {}".format(clip(row["title"], 110)))
        print("  {}".format(row["publisher"] or ""))
        if row["summary"]:
            print("  -> {}".format(clip(row["summary"], 200)))
        print("  {}".format(strip_tracking(row["url"])))
    print()
    return 0


def cmd_digest(args) -> int:
    """Print a Markdown briefing grouped by topic."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init(conn)
    since_iso = parse_since(args.since)

    topics = [args.topic] if args.topic else TOPICS
    window = args.since or "all time"
    lines = ["# Australian news digest", "", "_Window: {}_".format(window), ""]
    found = False

    for topic in topics:
        rows = db.query(
            conn,
            topic=topic,
            since_iso=since_iso,
            australian_only=args.au,
            min_importance=args.min_importance,
            limit=args.per_topic,
            collapse_duplicates=not args.all_copies,
        )
        if not rows:
            continue
        found = True
        lines.append("## {}".format(topic))
        lines.append("")
        for row in rows:
            lines.append(
                "### [{}]({})".format(
                    row["title"].replace("]", ")"), strip_tracking(row["url"])
                )
            )
            lines.append(
                "_{} · importance {}/5_".format(
                    row["publisher"] or "unknown", row["importance"] or "?"
                )
            )
            lines.append("")
            if row["summary"]:
                lines.append(row["summary"])
                lines.append("")
            if row["key_points"]:
                try:
                    for point in json.loads(row["key_points"]):
                        lines.append("- {}".format(point))
                    lines.append("")
                except (ValueError, TypeError):
                    pass
        lines.append("")

    conn.close()

    if not found:
        print("Nothing to report in that window.")
        return 0

    text = "\n".join(lines)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print("Wrote {}".format(args.out))
    else:
        print(text)
    return 0


def cmd_stats(args) -> int:
    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.init(conn)
    counts = db.stats(conn)
    print("Database: {}".format(settings.db_path))
    print("  total articles : {}".format(counts.get("total", 0)))
    print("  awaiting body  : {}".format(counts.get("new", 0)))
    print("  body extracted : {}".format(counts.get("extracted", 0)))
    print("  body failed    : {}".format(counts.get("failed", 0)))
    print("  classified     : {}".format(counts.get("classified", 0)))
    print("  summarised     : {}".format(counts.get("summarised", 0)))

    rows = db.topic_counts(conn, parse_since(args.since))
    if rows:
        print("\nBy topic{}:".format(" (last {})".format(args.since) if args.since else ""))
        for row in rows:
            print("  {:<14} {}".format(row["topic"], row["n"]))

    recent = list(
        conn.execute(
            "SELECT * FROM runs WHERE finished_at IS NOT NULL"
            " ORDER BY id DESC LIMIT 5"
        )
    )
    if recent:
        print("\nRecent runs:")
        for row in recent:
            print(
                "  {}  new={:<4} bodies={:<4} classified={:<4} summarised={:<3} errors={}".format(
                    row["started_at"][:16],
                    row["discovered"],
                    row["extracted"],
                    row["classified"],
                    row["summarised"],
                    row["errors"],
                )
            )
    conn.close()
    return 0


def cmd_verify_feeds(args) -> int:
    """Re-check every configured feed. Run this when a source goes quiet."""
    settings = load_settings()
    sources = load_sources()
    limiter = fetch.DomainRateLimiter()
    client = fetch.make_client(settings)

    broken = 0
    try:
        for source in sources:
            rows, error = fetch.poll_feed(client, source, limiter)
            if error:
                broken += 1
                print("FAIL  {:<32} {}".format(source.name, error))
            else:
                print("ok    {:<32} {} items".format(source.name, len(rows)))
    finally:
        client.close()

    print("\n{}/{} feeds healthy.".format(len(sources) - broken, len(sources)))
    return 1 if broken else 0


def _row_to_dict(row) -> dict:
    out = dict(row)
    for field in ("key_points", "entities"):
        if out.get(field):
            try:
                out[field] = json.loads(out[field])
            except (ValueError, TypeError):
                pass
    out.pop("body", None)  # too large to be useful in JSON output
    return out


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="newsau",
        description="Australia-focused news scraper with topic separation and AI enrichment.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)

    run = sub.add_parser("run", help="run one full pass")
    run.add_argument("--no-ai", action="store_true", help="scrape only, skip enrichment")
    run.set_defaults(func=cmd_run)

    watch = sub.add_parser("watch", help="poll on a loop")
    watch.add_argument("--interval", type=int, help="minutes between polls")
    watch.add_argument("--no-ai", action="store_true", help="scrape only, skip enrichment")
    watch.set_defaults(func=cmd_watch)

    listing = sub.add_parser("list", help="list stored articles")
    listing.add_argument("--topic", choices=TOPICS, help="filter by topic")
    listing.add_argument("--since", help="time window, e.g. 90m, 24h, 3d")
    listing.add_argument("--au", action="store_true", help="Australian stories only")
    listing.add_argument("--min-importance", type=int, default=1, help="1-5")
    listing.add_argument("--limit", type=int, default=30)
    listing.add_argument(
        "--all-copies",
        action="store_true",
        help="show every syndicated copy instead of one row per headline",
    )
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    listing.set_defaults(func=cmd_list)

    digest = sub.add_parser("digest", help="print a Markdown briefing by topic")
    digest.add_argument("--topic", choices=TOPICS, help="single topic only")
    digest.add_argument("--since", default="24h", help="time window, default 24h")
    digest.add_argument("--au", action="store_true", help="Australian stories only")
    digest.add_argument("--min-importance", type=int, default=3, help="1-5, default 3")
    digest.add_argument("--per-topic", type=int, default=5)
    digest.add_argument(
        "--all-copies",
        action="store_true",
        help="show every syndicated copy instead of one row per headline",
    )
    digest.add_argument("--out", help="write to a file instead of stdout")
    digest.set_defaults(func=cmd_digest)

    stats = sub.add_parser("stats", help="pipeline and topic counts")
    stats.add_argument("--since", help="time window for topic counts")
    stats.set_defaults(func=cmd_stats)

    sub.add_parser(
        "verify-feeds", help="check every configured feed still returns items"
    ).set_defaults(func=cmd_verify_feeds)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
