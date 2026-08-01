# News-scraper

Australia-focused news scraper. Polls 26 Australian RSS feeds every 15 minutes,
extracts article text, and uses Claude to sort each story into a topic
(`ai-tech`, `sports`, `business`, `politics`, …), score how newsworthy it is,
and summarise the ones that matter.

Everything lands in a single SQLite file, so it is safely re-runnable — polling
the same feed twice costs nothing.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
cp .env.example .env   # then add your ANTHROPIC_API_KEY
```

```bash
.venv/bin/python -m newsau init
```

```bash
.venv/bin/python -m newsau run
```

Then read what it found:

```bash
.venv/bin/python -m newsau list --topic ai-tech --since 24h
```

To keep it running on the 15-minute cadence:

```bash
.venv/bin/python -m newsau watch
```

## Commands

| Command | What it does |
|---|---|
| `init` | Create the database |
| `run` | One full pass: poll → extract → classify → summarise |
| `run --no-ai` | Scrape and store only; no API calls, no cost |
| `watch` | `run` on a loop (`--interval` minutes, default 15) |
| `list` | Show stored articles (`--topic`, `--since 24h`, `--au`, `--min-importance`, `--json`) |
| `digest` | Markdown briefing grouped by topic (`--out brief.md`) |
| `stats` | Pipeline counts, per-topic totals, recent run history |
| `verify-feeds` | Re-check every feed still returns items |

`--since` takes `90m`, `24h`, `3d`.

## How it works

```
sources.yaml → poll feeds → dedup → fetch article → extract body
                                                          ↓
                              topic + importance ← classify (batched)
                                                          ↓
                                      summary ← summarise (important only)
```

**Feeds first.** Every source is an RSS/Atom feed, not a scraped homepage. Feeds
are stable, cheap, and explicitly published for this purpose. The 26 in
`sources.yaml` were each verified to return parseable items — re-check any time
with `verify-feeds`.

**Two-stage AI, split by cost.** Classification is batched: 12 articles per call
using only headline and lead, which is what separates the feed into topics.
Summarisation runs per article against the full body, and only for articles
scoring at or above `MIN_IMPORTANCE_TO_SUMMARISE`. Most articles never reach
stage two — that is the point.

Both stages use structured outputs, so responses come back as validated objects
rather than free text needing regex. The classifier is constrained to the topic
list in `newsau/config.py`; anything outside it is coerced to `other` before it
can reach the database.

**The site section is a hint, not the answer.** A story in a publisher's
"technology" section about a telco's share price is business. The feed's
`topic_hint` is passed to the classifier as a prior, and the model decides.

## Deduplication

Two different problems, handled separately:

- **Same URL twice** — `canonical_url` strips tracking parameters (`utm_*`,
  `fbclid`, `ref`, …), lowercases the host, and drops `www.`. A UNIQUE
  constraint on that column makes every insert an `INSERT OR IGNORE`.
- **Same story, different mastheads** — Nine Entertainment runs the *Sydney
  Morning Herald*, *The Age*, *WAtoday* and *Brisbane Times* off identical
  copy, so one article arrives four times under four domains. URL dedup cannot
  see this. `title_key` hashes the normalised headline, and `list`/`digest`
  collapse to one row per fingerprint — preferring the copy that actually
  extracted. On a typical run this is ~11% of everything ingested.

Pass `--all-copies` to see every copy.

## Paywalls

The Nine mastheads and the AFR paywall a good share of their articles. When
body extraction fails, the article is marked `failed` rather than dropped — it
still gets classified from its headline and lead, so it still shows up in the
right topic. It just never gets a summary. Roughly a quarter of fetched
articles land this way, almost all of them Nine.

Nothing here bypasses a paywall, and nothing should.

## Cost

The model is set by `NEWS_MODEL` and defaults to `claude-opus-5`. At a 15-minute
cadence across 26 feeds this is the single biggest cost lever in the project, so
it is worth understanding before leaving `watch` running overnight.

Two things already keep it down:

- **Batching.** Classification sends 12 articles per call, on headline and lead
  only — not full bodies.
- **The importance gate.** Only articles scoring `MIN_IMPORTANCE_TO_SUMMARISE`
  or higher (default 3) get a full-body summary.
- **Prompt caching.** Both system prompts are byte-identical on every call and
  carry a cache breakpoint, so they are read from cache rather than reprocessed.

If it is still more than you want to spend, in rough order of effect: raise
`MIN_IMPORTANCE_TO_SUMMARISE` to 4, raise `CLASSIFY_BATCH_SIZE`, lengthen
`POLL_INTERVAL_MINUTES`, cut sources from `sources.yaml`, or set `NEWS_MODEL` to
a smaller model such as `claude-haiku-4-5` — classification is a simple enough
task that a smaller model may well be sufficient. Measure before deciding.

Run with `--no-ai` any time to scrape with zero API spend.

## Being a good citizen

- One request per second per domain, enforced across threads.
- A real `User-Agent` with a contact address from `CONTACT_EMAIL`. Publishers
  are far more tolerant of a crawler that identifies itself.
- Feeds only; no homepage scraping, no paywall circumvention.
- The database stores links, metadata, and extracted text for local analysis.
  Republishing full article text is a copyright problem rather than a technical
  one — share the links and your own summaries.

## Configuration

All in `.env` (see `.env.example`): `ANTHROPIC_API_KEY`, `NEWS_MODEL`,
`POLL_INTERVAL_MINUTES`, `MIN_IMPORTANCE_TO_SUMMARISE`, `CLASSIFY_BATCH_SIZE`,
`CONTACT_EMAIL`, `DB_PATH`, `KEEP_RAW_HTML`.

Set `KEEP_RAW_HTML=true` to archive every fetched page under `raw/`. Costs disk,
but when a parser breaks you will want the input that broke it.

## Adding a source

Add an entry to `sources.yaml` and run `verify-feeds`:

```yaml
  - name: my-source
    publisher: My Publisher
    url: https://example.com/feed
    topic_hint: ai-tech
```

Before writing any custom scraper for a site, check for `/feed`, `/rss`, or
`/sitemap.xml`, and open DevTools → Network → XHR to look for a JSON endpoint
behind the page. Most Australian publishers still have one, and a feed never
breaks the way a CSS selector does.

## Layout

```
newsau/
  config.py    settings, topic vocabulary, source loading
  db.py        SQLite schema and queries
  fetch.py     rate-limited HTTP, feed parsing, body extraction
  enrich.py    Claude classification and summarisation
  pipeline.py  one pass: poll → extract → classify → summarise
  cli.py       command line interface
  util.py      URL canonicalisation, dates, headline fingerprints
sources.yaml   the 26 feeds
```

## Known limits

- **Only 3 sports feeds and no dedicated health feed.** Health and
  entertainment stories arrive through the general feeds and get classified
  correctly, but coverage is thinner than for tech or business.
- **No full-text search.** Add a SQLite FTS5 virtual table over `body` when you
  want it.
- **`watch` is a foreground loop.** For unattended operation use launchd, cron
  calling `run`, or the GitHub Actions workflow in `.github/workflows/`.
- **Classification is not verified against a labelled set.** Spot-check the
  topic assignments before trusting them for anything that matters.
