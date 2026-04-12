"""
processor.py — Lọc trùng lặp, nhóm bài liên quan, score và xếp hạng.

Scoring mỗi bài:
  - Recency       (0.35): bài mới trong 48h
  - Source quality(0.20): TechCrunch > VentureBeat > ...
  - Coverage      (0.20): nhiều nguồn cover = trending proxy
  - Keyword boost (0.15): AI agent, LLM, funding, launch...
  - Crypto boost  (0.10): bitcoin, defi, token... (chỉ crypto)
"""

import re
import logging
from datetime import datetime, timezone

from database import is_duplicate, save_article
from config import (
    KEYWORD_OVERLAP_THRESHOLD,
    SCORE_RECENCY_WEIGHT, SCORE_SOURCE_WEIGHT,
    SCORE_COVERAGE_WEIGHT, SCORE_KEYWORD_WEIGHT, SCORE_CRYPTO_WEIGHT,
    SCORE_BOOST_KEYWORDS, SCORE_CRYPTO_KEYWORDS,
    TREND_MIN_COUNT, TREND_TOP_N,
)

logger = logging.getLogger(__name__)

_SOURCE_PRIORITY = [
    "TechCrunch AI",
    "VentureBeat AI",
    "The Verge AI",
    "VnExpress CN",
    "Decrypt",
    "CoinDesk",
]

_STOPWORDS = frozenset({
    "the", "a", "an", "in", "of", "to", "and", "or", "for", "is", "are",
    "was", "were", "with", "on", "at", "by", "from", "as", "that", "this",
    "it", "be", "has", "have", "will", "can", "new", "after", "about",
    "its", "how", "what", "why", "when", "who", "which", "but", "into",
    "not", "all", "more", "over", "now", "just", "also", "out", "up",
})

_CRYPTO_SOURCES = {"CoinDesk", "Decrypt"}
_CRYPTO_RE = re.compile(
    r"\b(crypto|bitcoin|btc|ethereum|eth|blockchain|coin|defi|nft|token|web3|xrp)\b",
    re.IGNORECASE,
)

# Trend topic keyword map (dùng cho count_trend_topics)
_TREND_TOPICS = {
    "AI agents":       ["agent", "agentic", "autonomous"],
    "Open source AI":  ["open source", "open-source", "llama", "mistral", "qwen"],
    "AI funding":      ["raises", "series", "million", "billion"],
    "Regulation":      ["regulation", "ban", "law", "policy", "congress", "eu"],
    "AI crypto/DePIN": ["depin", "ai crypto", "tokenize"],
}


# ---------------------------------------------------------------------------
# Keyword helpers
# ---------------------------------------------------------------------------

def _extract_keywords(title: str) -> frozenset:
    """Trích xuất từ khóa — normalize hyphen và possessives để bắt reordering."""
    normalized = title.lower().replace("-", " ").replace("_", " ")
    words = re.findall(r"\b[a-zA-Z]{3,}\b", normalized)
    words = [w[:-2] if w.endswith("'s") else w for w in words]
    return frozenset(w for w in words if w not in _STOPWORDS)


def _jaccard(title_a: str, title_b: str) -> float:
    kw_a = _extract_keywords(title_a)
    kw_b = _extract_keywords(title_b)
    if not kw_a or not kw_b:
        return 0.0
    return len(kw_a & kw_b) / len(kw_a | kw_b)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def _group_articles(articles: list) -> list:
    groups = []
    used   = set()

    for i, article in enumerate(articles):
        if i in used:
            continue
        group = [article]
        used.add(i)

        for j, other in enumerate(articles):
            if j in used:
                continue
            if _jaccard(article["title"], other["title"]) >= KEYWORD_OVERLAP_THRESHOLD:
                group.append(other)
                used.add(j)

        groups.append(group)

    return groups


# ---------------------------------------------------------------------------
# Source priority + representative
# ---------------------------------------------------------------------------

def _source_priority(source: str) -> int:
    try:
        return _SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(_SOURCE_PRIORITY)


def _pick_representative(group: list) -> dict:
    best = min(group, key=lambda a: _source_priority(a["source"]))
    best = dict(best)
    best["group_size"]     = len(group)
    best["related_titles"] = [a["title"] for a in group if a is not best][:3]
    return best


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _is_crypto(article: dict) -> bool:
    if article.get("source") in _CRYPTO_SOURCES:
        return True
    text = article.get("title", "") + " " + article.get("source", "")
    return bool(_CRYPTO_RE.search(text))


def _score_article(article: dict) -> float:
    """
    Composite score [0.0, 1.0] dùng để xếp hạng bài.
    Không expose ra ngoài Telegram — chỉ dùng để sort.
    """
    # --- Recency: decay tuyến tính, 0h→1.0, 48h→0.0 ---
    recency = 0.0
    try:
        pub = datetime.fromisoformat(article.get("published_date", ""))
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - pub).total_seconds() / 3600
        recency = max(0.0, 1.0 - age_hours / 48.0)
    except Exception:
        recency = 0.0

    # --- Source quality: normalize index → [0,1] inverted ---
    priority     = _source_priority(article["source"])
    max_priority = len(_SOURCE_PRIORITY)
    source_score = 1.0 - priority / (max_priority + 1)

    # --- Coverage: group_size 1→0.0, 2→0.5, 3+→1.0 ---
    coverage = min(1.0, (article.get("group_size", 1) - 1) / 2.0)

    # --- Keyword boost: hits capped at 3 ---
    text = (article.get("title", "") + " " + article.get("summary", "")).lower()
    kw_hits      = sum(1 for kw in SCORE_BOOST_KEYWORDS if kw in text)
    keyword_score = min(1.0, kw_hits / 3.0)

    # --- Crypto relevance boost ---
    crypto_score = 0.0
    if _is_crypto(article):
        crypto_hits  = sum(1 for kw in SCORE_CRYPTO_KEYWORDS if kw in text)
        crypto_score = min(1.0, crypto_hits / 2.0)

    return (
        SCORE_RECENCY_WEIGHT  * recency
        + SCORE_SOURCE_WEIGHT   * source_score
        + SCORE_COVERAGE_WEIGHT * coverage
        + SCORE_KEYWORD_WEIGHT  * keyword_score
        + SCORE_CRYPTO_WEIGHT   * crypto_score
    )


# ---------------------------------------------------------------------------
# Trend counter (chạy trên raw articles, trước dedup)
# ---------------------------------------------------------------------------

def count_trend_topics(raw_articles: list) -> dict:
    """
    Đếm tần suất topic trên toàn bộ raw articles (trước dedup).
    Dùng raw thay vì selected để signal mạnh hơn — nhiều nguồn cover = trending.

    Returns:
        dict {topic_label: count} đã lọc >= TREND_MIN_COUNT, sorted by count desc.
    """
    counts = {topic: 0 for topic in _TREND_TOPICS}

    for article in raw_articles:
        text = (article.get("title", "") + " " + article.get("summary", "")).lower()
        for topic, keywords in _TREND_TOPICS.items():
            if any(kw in text for kw in keywords):
                counts[topic] += 1

    filtered = {t: c for t, c in counts.items() if c >= TREND_MIN_COUNT}
    sorted_counts = dict(sorted(filtered.items(), key=lambda x: x[1], reverse=True))

    if sorted_counts:
        logger.info("Trend topics: %s", sorted_counts)
    return sorted_counts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def process(raw_articles: list) -> tuple:
    """
    Pipeline: lọc duplicate → nhóm → score → sort → trả về tất cả bài xếp hạng.

    Returns:
        (selected: list[dict], stats: dict)
    """
    stats = {
        "total_fetched":       len(raw_articles),
        "duplicates_filtered": 0,
        "after_dedup":         0,
        "groups_formed":       0,
        "selected":            0,
        "sources_breakdown":   {},
    }

    # --- Bước 1: Lọc trùng lặp ---
    fresh = []
    for article in raw_articles:
        if is_duplicate(article["url"], article["title"]):
            stats["duplicates_filtered"] += 1
        else:
            fresh.append(article)
            src = article["source"]
            stats["sources_breakdown"][src] = stats["sources_breakdown"].get(src, 0) + 1

    stats["after_dedup"] = len(fresh)
    logger.info("Sau dedup: %d bài mới (%d bị lọc trùng)", len(fresh), stats["duplicates_filtered"])

    if not fresh:
        logger.warning("Không có bài mới nào sau dedup")
        return [], stats

    # --- Bước 2: Nhóm bài liên quan ---
    groups = _group_articles(fresh)
    stats["groups_formed"] = len(groups)
    logger.info("Tạo được %d nhóm bài", len(groups))

    # --- Bước 3: Chọn đại diện → score → sort ---
    candidates = [_pick_representative(g) for g in groups]
    for article in candidates:
        article["_score"] = _score_article(article)

    selected = sorted(candidates, key=lambda a: a["_score"], reverse=True)
    stats["selected"] = len(selected)

    # --- Bước 4: Lưu vào DB ---
    for article in selected:
        try:
            db_id = save_article(article["url"], article["title"], article["source"])
            article["db_id"] = db_id
        except Exception as e:
            logger.error("Lỗi lưu bài vào DB: %s", e)
            article["db_id"] = None

    logger.info(
        "Chọn %d bài | Top score: %.3f | Bottom score: %.3f",
        len(selected),
        selected[0]["_score"] if selected else 0,
        selected[-1]["_score"] if selected else 0,
    )
    return selected, stats
