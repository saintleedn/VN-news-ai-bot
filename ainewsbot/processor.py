"""
processor.py — Lọc trùng lặp, nhóm bài liên quan, chọn 3 bài tốt nhất.
Dùng Jaccard similarity trên từ khóa tiêu đề để nhóm bài cùng chủ đề.
"""

import re
import logging

from database import is_duplicate, save_article
from config import KEYWORD_OVERLAP_THRESHOLD, TARGET_ARTICLE_COUNT

logger = logging.getLogger(__name__)

# Nguồn được xếp hạng uy tín (index nhỏ hơn = ưu tiên cao hơn)
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


# ---------------------------------------------------------------------------
# Keyword helpers
# ---------------------------------------------------------------------------

def _extract_keywords(title: str) -> frozenset:
    """Trích xuất từ khóa có nghĩa từ tiêu đề, bỏ stopwords và từ ngắn."""
    words = re.findall(r"\b[a-zA-Z]{3,}\b", title.lower())
    return frozenset(w for w in words if w not in _STOPWORDS)


def _jaccard(title_a: str, title_b: str) -> float:
    """Tính Jaccard similarity giữa 2 tập từ khóa."""
    kw_a = _extract_keywords(title_a)
    kw_b = _extract_keywords(title_b)
    if not kw_a or not kw_b:
        return 0.0
    return len(kw_a & kw_b) / len(kw_a | kw_b)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

def _group_articles(articles: list) -> list:
    """
    Nhóm các bài có nội dung liên quan.
    Bài nào có Jaccard similarity >= KEYWORD_OVERLAP_THRESHOLD sẽ vào cùng nhóm.
    """
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
# Scoring & selection
# ---------------------------------------------------------------------------

def _source_priority(source: str) -> int:
    """Trả về chỉ số ưu tiên của nguồn (nhỏ hơn = uy tín hơn)."""
    try:
        return _SOURCE_PRIORITY.index(source)
    except ValueError:
        return len(_SOURCE_PRIORITY)  # Nguồn không biết xếp cuối


def _group_sort_key(group: list) -> tuple:
    """
    Key để sắp xếp nhóm: (ngày mới nhất DESC, nguồn uy tín nhất).
    Trả về tuple để sorted() có thể so sánh.
    """
    newest_date   = max(a.get("published_date", "") for a in group)
    best_priority = min(_source_priority(a["source"]) for a in group)
    # Dùng âm priority để sort ascending = ưu tiên nguồn tốt nhất
    return (newest_date, -best_priority)


def _pick_representative(group: list) -> dict:
    """Chọn bài đại diện cho nhóm: bài từ nguồn uy tín nhất."""
    best = min(group, key=lambda a: _source_priority(a["source"]))
    # Gắn thêm context về các bài liên quan
    best = dict(best)  # copy để không mutate input
    best["group_size"]     = len(group)
    best["related_titles"] = [a["title"] for a in group if a is not best][:3]
    return best


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def process(raw_articles: list) -> tuple:
    """
    Pipeline xử lý: lọc duplicate → nhóm → chọn top 3.

    Returns:
        (selected: list[dict], stats: dict)
        selected: list bài đã chọn, có thêm field 'db_id'
        stats: thống kê cho admin report
    """
    stats = {
        "total_fetched":      len(raw_articles),
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
            stats["sources_breakdown"][src] = (
                stats["sources_breakdown"].get(src, 0) + 1
            )

    stats["after_dedup"] = len(fresh)
    logger.info(
        "Sau dedup: %d bài mới (%d bị lọc trùng)",
        len(fresh),
        stats["duplicates_filtered"],
    )

    if not fresh:
        logger.warning("Không có bài mới nào sau dedup")
        return [], stats

    # --- Bước 2: Nhóm bài liên quan ---
    groups = _group_articles(fresh)
    stats["groups_formed"] = len(groups)
    logger.info("Tạo được %d nhóm bài", len(groups))

    # --- Bước 3: Sắp xếp nhóm và chọn top N ---
    sorted_groups = sorted(groups, key=_group_sort_key, reverse=True)
    top_groups    = sorted_groups[:TARGET_ARTICLE_COUNT]
    selected      = [_pick_representative(g) for g in top_groups]
    stats["selected"] = len(selected)

    # --- Bước 4: Lưu vào DB để đánh dấu đã xử lý ---
    for article in selected:
        try:
            db_id = save_article(
                article["url"],
                article["title"],
                article["source"],
            )
            article["db_id"] = db_id
        except Exception as e:
            logger.error("Lỗi lưu bài vào DB: %s", e)
            article["db_id"] = None

    logger.info("Chọn được %d bài để viết nội dung", len(selected))
    return selected, stats
