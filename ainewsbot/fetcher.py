"""
fetcher.py — Thu thập tin tức từ RSS feeds và Google News.
Mỗi nguồn được xử lý độc lập: 1 nguồn lỗi không ảnh hưởng các nguồn khác.
"""

import urllib.parse
import logging
from datetime import datetime
from email.utils import parsedate_to_datetime

import feedparser
import requests

import time

from config import (
    RSS_FEEDS,
    GOOGLE_NEWS_KEYWORDS,
    FETCH_TIMEOUT,
    MAX_ARTICLES_PER_SOURCE,
    MIN_ARTICLES_FETCH,
    FETCH_RETRY_DELAY_SEC,
)

logger = logging.getLogger(__name__)

# Header giả lập browser để tránh bị block bởi một số RSS server
_HEADERS = {"User-Agent": "Mozilla/5.0 AINewsBot/1.0 (+https://github.com/ainewsbot)"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date(entry) -> str:
    """
    Chuyển ngày RSS sang ISO format.
    Thử nhiều attribute khác nhau vì RSS không đồng nhất.
    """
    for attr in ("published", "updated", "created"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return parsedate_to_datetime(val).isoformat()
            except Exception:
                pass
    # feedparser cũng parse sang struct_time nếu có
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                import time
                return datetime(*val[:6]).isoformat()
            except Exception:
                pass
    return datetime.utcnow().isoformat()


def _clean_summary(entry) -> str:
    """Lấy summary từ entry, giới hạn 600 ký tự."""
    raw = entry.get("summary", "") or entry.get("description", "") or ""
    # Loại bỏ HTML tags đơn giản
    import re
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:600]


# ---------------------------------------------------------------------------
# Fetch một nguồn RSS
# ---------------------------------------------------------------------------

def fetch_rss_source(name: str, url: str) -> list:
    """
    Fetch và parse một RSS feed.

    Dùng requests để fetch content (kiểm soát timeout),
    sau đó pass content cho feedparser để parse.
    KHÔNG dùng feedparser.parse(url) trực tiếp vì không có timeout.
    """
    try:
        resp = requests.get(url, timeout=FETCH_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()

        feed     = feedparser.parse(resp.content)
        articles = []

        for entry in feed.entries[:MAX_ARTICLES_PER_SOURCE]:
            title = (entry.get("title") or "").strip()
            link  = entry.get("link") or ""

            if not title or not link:
                continue

            articles.append({
                "title":          title,
                "url":            link,
                "summary":        _clean_summary(entry),
                "source":         name,
                "published_date": _parse_date(entry),
            })

        logger.info("[%s] Lấy được %d bài", name, len(articles))
        return articles

    except requests.Timeout:
        logger.warning("[%s] Timeout sau %ds", name, FETCH_TIMEOUT)
        return []
    except requests.HTTPError as e:
        logger.warning("[%s] HTTP error: %s", name, e)
        return []
    except requests.RequestException as e:
        logger.warning("[%s] Request error: %s", name, e)
        return []
    except Exception as e:
        logger.error("[%s] Lỗi không mong đợi: %s", name, e)
        return []


# ---------------------------------------------------------------------------
# Fetch Google News RSS
# ---------------------------------------------------------------------------

def fetch_google_news(keyword: str) -> list:
    """
    Fetch Google News RSS cho một từ khóa tìm kiếm.
    Google News trả về các bài tổng hợp từ nhiều nguồn.
    """
    encoded = urllib.parse.quote(keyword)
    url     = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    name    = f"Google News: {keyword}"
    return fetch_rss_source(name, url)


# ---------------------------------------------------------------------------
# Entry point chính
# ---------------------------------------------------------------------------

def _fetch_all_sources() -> list:
    """Thu thập tất cả nguồn tin: RSS feeds + Google News keywords."""
    all_articles = []

    for name, url in RSS_FEEDS.items():
        articles = fetch_rss_source(name, url)
        all_articles.extend(articles)

    for keyword in GOOGLE_NEWS_KEYWORDS:
        articles = fetch_google_news(keyword)
        all_articles.extend(articles)

    return all_articles


def fetch_all() -> list:
    """
    Thu thập tất cả nguồn tin. Nếu < MIN_ARTICLES_FETCH → retry 1 lần sau delay.
    Trả về list tổng hợp tất cả articles (dedup by URL giữa 2 lần fetch).
    """
    articles = _fetch_all_sources()

    if len(articles) < MIN_ARTICLES_FETCH:
        logger.warning(
            "Chỉ fetch được %d bài (< %d) — retry sau %ds",
            len(articles), MIN_ARTICLES_FETCH, FETCH_RETRY_DELAY_SEC,
        )
        time.sleep(FETCH_RETRY_DELAY_SEC)
        retry = _fetch_all_sources()
        seen  = {a["url"] for a in retry}
        articles = retry + [a for a in articles if a["url"] not in seen]
        logger.info("Sau retry: %d bài", len(articles))

    logger.info("Tổng cộng fetch được %d bài từ tất cả nguồn", len(articles))
    return articles
