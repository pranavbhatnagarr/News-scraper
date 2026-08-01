"""AI enrichment via the Claude API.

Two stages, split deliberately by cost:

1. `classify_batch` — cheap. Sends only the headline and lead paragraph for a
   batch of articles in a single call and gets back a topic, an
   Australia-relevance flag, and an importance score for each. This is what
   separates the feed into topics.

2. `summarise` — expensive. Runs per article against the full extracted body,
   and only for articles the classifier scored highly enough. Most articles
   never reach this stage, which is the point.

Both stages use structured outputs, so the response is a validated Pydantic
object rather than free text that needs parsing.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence

import anthropic
from pydantic import BaseModel, Field

from .config import TOPICS
from .util import clip

log = logging.getLogger("newsau.enrich")

# Character budgets. Bodies are clipped before they reach the API — the lead
# and first few paragraphs of a news article carry essentially all of the
# summary-relevant information, and sending 8000 words of a liveblog is pure
# cost with no quality gain.
MAX_LEAD_CHARS = 400
MAX_BODY_CHARS = 6000


class Classification(BaseModel):
    index: int = Field(description="The index number of the article being classified.")
    topic: str = Field(description="One of the allowed topic slugs.")
    australian: bool = Field(
        description="True if the story is about Australia, Australians, or has "
        "direct consequences for an Australian audience."
    )
    importance: int = Field(
        description="Newsworthiness from 1 (routine filler) to 5 (major "
        "national or global story)."
    )


class ClassificationBatch(BaseModel):
    results: List[Classification]


class Summary(BaseModel):
    summary: str = Field(description="A single sentence stating what happened.")
    key_points: List[str] = Field(
        description="Two to four short factual bullet points from the article."
    )
    entities: List[str] = Field(
        description="Key people, organisations, or places named in the article."
    )


CLASSIFY_SYSTEM = """You classify Australian news articles for a topic-separated news feed.

For each article you receive an index, a headline, the publisher, the section \
of the site it came from, and a lead paragraph.

Assign each article exactly one topic from this list:
{topics}

Topic guidance:
- ai-tech: artificial intelligence, software, hardware, telecommunications, \
cybersecurity, startups, science and research with a technology angle.
- sports: matches, results, transfers, sporting bodies, athletes.
- business: markets, companies, economics, property, employment, tax, mining, energy markets.
- politics: government, elections, policy, legislation, courts where politically salient.
- health: medicine, public health, hospitals, mental health, disease.
- world: international news with no direct Australian angle.
- entertainment: film, television, music, celebrity, arts, gaming as culture.
- lifestyle: food, travel, fashion, relationships, consumer advice.
- other: anything that genuinely fits none of the above.

The site section is a hint, not the answer. Publishers file stories loosely — a \
story in a "technology" section about a telco's share price is business, and a \
story in "general" about a data breach is ai-tech. Judge by what the article is \
actually about.

Set `australian` to true when the story concerns Australia, Australians, or has \
direct consequences for an Australian audience. A wire story about a US \
election is not Australian; the same election analysed for its effect on \
Australian trade is.

Score `importance` from 1 to 5:
- 5: major national or global story, leads the bulletin
- 4: significant, most readers would want to know
- 3: solid news, matters to people following that topic
- 2: minor or incremental
- 1: routine filler, listicles, promotional content

Return one result per article, using the index you were given. Classify every \
article, including ones where you are unsure — pick the closest fit."""


SUMMARISE_SYSTEM = """You summarise news articles for a briefing feed.

Write a one-sentence summary that states what actually happened — the event, \
who it involves, and the outcome. Lead with the substance, not with framing \
like "This article discusses". Someone reading only that sentence should know \
the news.

Then give two to four short factual bullet points carrying the specifics: \
numbers, dates, names, stated positions. No speculation and nothing that is not \
in the article.

Then list the key named entities: people, organisations, and places.

Use Australian English spelling. Be factual and neutral — no editorialising, no \
adjectives the article did not use. If the article is thin or mostly \
promotional, say so plainly in the summary rather than inflating it."""


def build_client(api_key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key)


def _valid_topic(value: str) -> str:
    """Guard against a topic outside the vocabulary reaching the database."""
    slug = (value or "").strip().lower()
    return slug if slug in TOPICS else "other"


def _clamp_importance(value: int) -> int:
    try:
        return max(1, min(5, int(value)))
    except (TypeError, ValueError):
        return 3


def classify_batch(
    client: anthropic.Anthropic,
    model: str,
    articles: Sequence[dict],
) -> Dict[int, Classification]:
    """Classify a batch of articles in one call, keyed by article id.

    `articles` are dict-like rows with id, title, publisher, feed_topic, lead.
    """
    if not articles:
        return {}

    # Index by position rather than database id: small integers cost fewer
    # tokens and keep the model from trying to interpret ids as meaningful.
    lines = []
    for position, article in enumerate(articles):
        lines.append(
            "[{idx}] headline: {title}\n"
            "    publisher: {publisher}\n"
            "    site section: {section}\n"
            "    lead: {lead}".format(
                idx=position,
                title=article["title"],
                publisher=article["publisher"] or "unknown",
                section=article["feed_topic"] or "general",
                lead=clip(article["lead"] or "", MAX_LEAD_CHARS) or "(none)",
            )
        )

    prompt = "Classify these {n} articles:\n\n{body}".format(
        n=len(articles), body="\n\n".join(lines)
    )

    response = client.messages.parse(
        model=model,
        max_tokens=4000,
        system=[
            {
                "type": "text",
                "text": CLASSIFY_SYSTEM.format(topics=", ".join(TOPICS)),
                # The system prompt is byte-identical on every call, so it
                # caches across the whole run and every subsequent poll.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": prompt}],
        output_format=ClassificationBatch,
    )

    if response.stop_reason == "refusal":
        log.warning("Classification refused by safety classifier; skipping batch")
        return {}

    parsed = response.parsed_output
    if parsed is None:
        log.warning("Classification returned no parsed output; skipping batch")
        return {}

    out: Dict[int, Classification] = {}
    for result in parsed.results:
        if not (0 <= result.index < len(articles)):
            continue
        article_id = articles[result.index]["id"]
        out[article_id] = Classification(
            index=result.index,
            topic=_valid_topic(result.topic),
            australian=bool(result.australian),
            importance=_clamp_importance(result.importance),
        )

    missing = len(articles) - len(out)
    if missing:
        log.warning("Classifier skipped %d of %d articles", missing, len(articles))
    return out


def summarise(
    client: anthropic.Anthropic,
    model: str,
    title: str,
    publisher: str,
    body: str,
) -> Optional[Summary]:
    """Summarise a single article. Returns None if the model declined."""
    prompt = (
        "Headline: {title}\n"
        "Publisher: {publisher}\n\n"
        "Article:\n{body}"
    ).format(title=title, publisher=publisher or "unknown", body=clip(body, MAX_BODY_CHARS))

    response = client.messages.parse(
        model=model,
        max_tokens=2000,
        system=[
            {
                "type": "text",
                "text": SUMMARISE_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": prompt}],
        output_format=Summary,
    )

    if response.stop_reason == "refusal":
        log.warning("Summary refused for %r", clip(title, 80))
        return None
    return response.parsed_output
