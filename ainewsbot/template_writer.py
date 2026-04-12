"""
template_writer.py — Format bài thành tin nhắn Telegram (không dùng AI).

Format mỗi nhóm (AI chung / AI crypto):
  🔥 Highlights  — top 2 bài có snippet 80 ký tự + tag
  📋 Top Stories — bài 3–10, chỉ link + tag
  📊 Signals     — trend topics từ raw articles

Phân loại: CoinDesk, Decrypt, bài có keyword crypto → AI crypto.
           Còn lại → AI chung.
"""

import html
import logging
import re
from datetime import datetime

from config import TZ_GMT7, TREND_TOP_N

logger = logging.getLogger(__name__)

_TG_MAX_LEN = 4096
_SEP = "━━━━━━━━━━━━━━━━━━━━━━━━━━━"

_NUM_EMOJI = [
    "1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟",
    "1️⃣1️⃣","1️⃣2️⃣","1️⃣3️⃣","1️⃣4️⃣","1️⃣5️⃣","1️⃣6️⃣","1️⃣7️⃣","1️⃣8️⃣","1️⃣9️⃣","2️⃣0️⃣",
]

# ---------------------------------------------------------------------------
# Category detection
# ---------------------------------------------------------------------------

_CRYPTO_SOURCES = {"CoinDesk", "Decrypt"}
_CRYPTO_RE = re.compile(
    r"\b(crypto|bitcoin|btc|ethereum|eth|blockchain|coin|defi|nft|token|web3|xrp)\b",
    re.IGNORECASE,
)


def _is_crypto(article: dict) -> bool:
    if article.get("source") in _CRYPTO_SOURCES:
        return True
    text = article.get("title", "") + " " + article.get("source", "")
    return bool(_CRYPTO_RE.search(text))


# ---------------------------------------------------------------------------
# Tag classifier — first match wins (priority order)
# ---------------------------------------------------------------------------

_TAG_RULES = [
    ("[Incident]", re.compile(
        r"\b(attack|breach|hack|hacked|vulnerability|arrested|scam|fraud|exploit|stolen)\b", re.I)),
    ("[Funding]",  re.compile(
        r"\b(raises|raise|raised|million|billion|series\s+[a-e]|investment|ipo|funding|backed|valuation)\b", re.I)),
    ("[Policy]",   re.compile(
        r"\b(regulation|regulates|ban|banned|law|government|eu|congress|senate|legislation|legal|court)\b", re.I)),
    ("[Research]", re.compile(
        r"\b(study|paper|research|breakthrough|discovered|discovers|found|published|arxiv|benchmark)\b", re.I)),
    ("[Market]",   re.compile(
        r"\b(price|drops?|rises?|crash|crashed|bull|bear|rally|surge|plunge|ath|dump|pump)\b", re.I)),
    ("[Product]",  re.compile(
        r"\b(launch(?:es|ed)?|release[sd]?|update[sd]?|introduces?|unveils?|announces?|new\s+\w+\s+(?:model|app|tool|feature))\b", re.I)),
]
_TAG_DEFAULT = "[Update]"


def _classify_tag(article: dict) -> str:
    text = article.get("title", "") + " " + article.get("summary", "")
    for tag, pattern in _TAG_RULES:
        if pattern.search(text):
            return tag
    return _TAG_DEFAULT


# ---------------------------------------------------------------------------
# Snippet helper
# ---------------------------------------------------------------------------

def _snippet(article: dict, max_chars: int = 80) -> str:
    """Lấy tối đa max_chars ký tự đầu summary, cắt tại word boundary."""
    raw = article.get("summary", "") or article.get("title", "")
    clean = re.sub(r"<[^>]+>", " ", raw)
    clean = re.sub(r"\s+", " ", clean).strip()
    if len(clean) <= max_chars:
        return clean
    cut = clean[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > max_chars // 2:
        cut = cut[:last_space]
    return cut + "…"


# ---------------------------------------------------------------------------
# Block builders
# ---------------------------------------------------------------------------

def _num(i: int) -> str:
    return _NUM_EMOJI[i] if i < len(_NUM_EMOJI) else f"{i + 1}."


def _format_highlight(article: dict) -> str:
    tag     = _classify_tag(article)
    title   = html.escape(article.get("title", ""))
    source  = html.escape(article.get("source", ""))
    snip    = html.escape(_snippet(article))
    url     = article.get("url", "")
    return f'• {tag} <a href="{url}">{title}</a> <i>({source})</i>\n  ↳ {snip}'


def _format_story(i: int, article: dict) -> str:
    tag    = _classify_tag(article)
    title  = html.escape(article.get("title", ""))
    url    = article.get("url", "")
    source = html.escape(article.get("source", ""))
    return f'{_num(i)} {tag} <a href="{url}">{title}</a> — <i>{source}</i>'


def _build_signals_block(trend_counts: dict) -> str:
    if not trend_counts:
        return ""
    top = list(trend_counts.items())[:TREND_TOP_N]
    lines = [f"• {topic} (↑ {count} articles)" for topic, count in top]
    return "\n\n📊 <b>Today's Signals</b>\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Message assembler
# ---------------------------------------------------------------------------

def _build_group_message(
    articles: list,
    header: str,
    footer: str,
    trend_counts: dict,
) -> list[str]:
    """
    Tạo 1–2 tin nhắn cho 1 nhóm (AI chung hoặc AI crypto).
    Highlights = top 2, Top Stories = bài 3–10.
    Nếu vượt 4096: tin 1 = Highlights, tin 2 = Stories + Signals.
    """
    highlights = articles[:2]
    stories    = articles[2:]

    hl_lines = [_format_highlight(a) for a in highlights]
    hl_block = "🔥 <b>Highlights</b>\n" + "\n\n".join(hl_lines)

    st_lines = [_format_story(i, a) for i, a in enumerate(stories)]
    st_block = ("📋 <b>Top Stories</b>\n" + "\n".join(st_lines)) if st_lines else ""

    signals = _build_signals_block(trend_counts)

    body = header + hl_block
    if st_block:
        body += "\n\n" + st_block
    body += signals + "\n\n" + footer

    if len(body) <= _TG_MAX_LEN:
        return [body]

    # Tách thành 2 tin
    msg1 = header + hl_block + "\n\n" + footer
    msg2 = header.replace("|", "| (2/2) |", 1) + st_block + signals + "\n\n" + footer
    # Đơn giản hơn: dùng label (1/2) / (2/2)
    h1 = header.rstrip("\n") + " <i>(1/2)</i>\n\n"
    h2 = header.rstrip("\n") + " <i>(2/2)</i>\n\n"
    msg1 = h1 + hl_block + "\n\n" + footer
    msg2 = h2 + st_block + signals + "\n\n" + footer
    return [msg1, msg2]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_messages(articles: list, trend_counts: dict = None) -> list[str]:
    """
    Phân loại articles → AI chung + AI crypto, top 10 mỗi nhóm.
    Tạo tin nhắn với Highlights / Top Stories / Signals.

    Args:
        articles:     list bài đã score + sort từ processor.process()
        trend_counts: dict {topic: count} từ processor.count_trend_topics()

    Returns:
        list[str]: [tin AI chung, tin AI crypto] — mỗi cái <= 4096 ký tự
    """
    if not articles:
        return []
    if trend_counts is None:
        trend_counts = {}

    now = datetime.now(TZ_GMT7).strftime("%d/%m/%Y")

    ai_general = [a for a in articles if not _is_crypto(a)][:10]
    ai_crypto  = [a for a in articles if _is_crypto(a)][:10]

    logger.info("Phân loại: %d bài AI chung, %d bài AI crypto", len(ai_general), len(ai_crypto))

    messages = []

    if ai_general:
        header = (
            f"🤖 <b>AI NEWS | {now}</b>\n"
            f"<i>Công nghệ · Nghiên cứu · Sản phẩm AI</i>\n"
            f"{_SEP}\n\n"
        )
        footer = "#AINews #CôngNghệAI #MachineLearning"
        msgs = _build_group_message(ai_general, header, footer, trend_counts)
        messages.extend(msgs)
        logger.info("AI chung → %d tin nhắn", len(msgs))

    if ai_crypto:
        header = (
            f"₿ <b>AI × CRYPTO | {now}</b>\n"
            f"<i>Blockchain · DeFi · Token · Thị trường</i>\n"
            f"{_SEP}\n\n"
        )
        footer = "#AICrypto #Blockchain #Bitcoin #DeFi"
        msgs = _build_group_message(ai_crypto, header, footer, trend_counts)
        messages.extend(msgs)
        logger.info("AI crypto → %d tin nhắn", len(msgs))

    return messages
