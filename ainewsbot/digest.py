"""
digest.py — Tạo và gửi bản tin tổng hợp hàng tuần và hàng tháng.
Đọc bài đã gửi từ SQLite, gọi Claude để tổng hợp, gửi lên channel.
"""

import json
import time
import asyncio
import logging
from datetime import datetime

from google import genai

from database import get_articles_since, log_digest
from sender import send_digest_async
from config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_MAX_RETRIES, TZ_GMT7

logger  = logging.getLogger(__name__)
_client = genai.Client(api_key=GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_WEEKLY_PROMPT = """\
Bạn là editor tổng hợp tin tức AI hàng tuần, viết cho kênh Telegram.

Dưới đây là {n} bài tin tức AI đã đăng trong tuần (từ {start_date} đến {end_date}):

{article_list}

Hãy viết BẢN TIN TỔNG KẾT TUẦN theo đúng 2 phiên bản JSON với key "vietnamese" và "english".

QUAN TRỌNG — Chỉ dùng HTML Telegram: <b>, <i>, <a href="...">
KHÔNG dùng Markdown, KHÔNG dùng HTML tags khác.

PHIÊN BẢN TIẾNG VIỆT:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 <b>TỔNG KẾT AI TUẦN {week_num} | {start_date} — {end_date}</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🏆 <b>TOP 5 SỰ KIỆN AI ĐÁNG CHÚ Ý NHẤT:</b>

1️⃣ [Tiêu đề sự kiện 1]
→ [1-2 câu tóm tắt + tại sao quan trọng]

2️⃣ [Tiêu đề sự kiện 2]
→ [1-2 câu tóm tắt + tại sao quan trọng]

3️⃣ [Tiêu đề sự kiện 3]
→ [1-2 câu tóm tắt + tại sao quan trọng]

4️⃣ [Tiêu đề sự kiện 4]
→ [1-2 câu tóm tắt + tại sao quan trọng]

5️⃣ [Tiêu đề sự kiện 5]
→ [1-2 câu tóm tắt + tại sao quan trọng]

📈 <b>XU HƯỚNG NỔI BẬT TUẦN NÀY:</b>
[2-3 câu nhận định xu hướng chung]

🔭 <b>DỰ BÁO TUẦN TỚI:</b>
[1-2 câu dự đoán những gì đáng theo dõi]

🏷️ #AIWeekly #TổngKếtTuần #AI2026
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PHIÊN BẢN TIẾNG ANH: (same structure, professional English)

Chỉ trả về JSON thuần túy, không markdown, không giải thích.
"""

_MONTHLY_PROMPT = """\
Bạn là editor tổng hợp tin tức AI hàng tháng, viết cho kênh Telegram về AI và crypto.

Dưới đây là {n} bài tin tức AI đã đăng trong tháng {month}/{year}:

{article_list}

Hãy viết BẢN TIN TỔNG KẾT THÁNG theo đúng 2 phiên bản JSON với key "vietnamese" và "english".

QUAN TRỌNG — Chỉ dùng HTML Telegram: <b>, <i>, <a href="...">
KHÔNG dùng Markdown, KHÔNG dùng HTML tags khác.

PHIÊN BẢN TIẾNG VIỆT:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🗓️ <b>TỔNG KẾT AI THÁNG {month}/{year}</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔥 <b>3 SỰ KIỆN ĐỊNH HÌNH THÁNG NÀY:</b>

🥇 [Sự kiện quan trọng nhất]
[3-4 câu phân tích sâu]

🥈 [Sự kiện quan trọng thứ 2]
[3-4 câu phân tích]

🥉 [Sự kiện quan trọng thứ 3]
[3-4 câu phân tích]

---

📌 <b>NHỮNG CON SỐ ĐÁNG NHỚ:</b>
• [Số liệu nổi bật 1]
• [Số liệu nổi bật 2]
• [Số liệu nổi bật 3]

---

🧭 <b>XU HƯỚNG AI THÁNG {month}:</b>
[3-4 câu tổng hợp xu hướng lớn]

🤖 <b>AI &amp; CRYPTO THÁNG NÀY:</b>
[2-3 câu về AI trong crypto/blockchain]

🔭 <b>NHÌN VỀ THÁNG TỚI:</b>
[2-3 câu dự báo]

🏷️ #AIMonthly #TổngKếtTháng #AI{year}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

PHIÊN BẢN TIẾNG ANH: (same structure, professional English)

Chỉ trả về JSON thuần túy, không markdown, không giải thích.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_article_list(articles: list) -> str:
    """Format danh sách bài để đưa vào prompt."""
    lines = []
    for i, a in enumerate(articles, 1):
        date_str = a.get("processed_date", "")[:10]
        lines.append(f"{i}. [{a.get('source', 'N/A')}] {a.get('title', 'N/A')} ({date_str})")
    return "\n".join(lines)


def _extract_json(raw: str) -> dict:
    """Trích xuất JSON từ response Gemini. Xử lý markdown code block nếu có."""
    if "```" in raw:
        raw = raw.split("```")[-2] if raw.count("```") >= 2 else raw
        raw = raw.lstrip("json").strip()
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError("Không tìm thấy JSON trong response")
    parsed = json.loads(raw[start:end])
    if "vietnamese" not in parsed or "english" not in parsed:
        raise ValueError(f"JSON thiếu key: {list(parsed.keys())}")
    return parsed


def _week_label(dt: datetime) -> str:
    """VD: 2026-W14"""
    return f"{dt.year}-W{dt.isocalendar()[1]:02d}"


def _month_label(dt: datetime) -> str:
    """VD: 2026-04"""
    return f"{dt.year}-{dt.month:02d}"


# ---------------------------------------------------------------------------
# Claude API call với retry
# ---------------------------------------------------------------------------

def _generate_digest(prompt: str) -> dict:
    """
    Gọi Gemini để tạo nội dung digest.
    Digest dài hơn daily post nên cần model xử lý tốt văn bản dài.
    """
    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            response = _client.models.generate_content(
                model    = GEMINI_MODEL,
                contents = prompt,
            )
            raw = response.text.strip()
            parsed   = _extract_json(raw)
            logger.info("Gemini tạo digest thành công (lần %d)", attempt)
            return parsed

        except json.JSONDecodeError as e:
            logger.warning("[Lần %d] JSON không hợp lệ: %s", attempt, e)
        except ValueError as e:
            logger.warning("[Lần %d] Validation error: %s", attempt, e)
        except Exception as e:
            err_str = str(e).lower()
            if "429" in err_str or "quota" in err_str or "rate" in err_str:
                wait = 60
                logger.warning("[Lần %d] Gemini rate limit — chờ %ds", attempt, wait)
                time.sleep(wait)
                continue
            logger.error("[Lần %d] Lỗi không mong đợi: %s", attempt, e)

        if attempt < GEMINI_MAX_RETRIES:
            wait = 2 ** attempt
            logger.info("Chờ %ds trước khi retry digest...", wait)
            time.sleep(wait)

    return {"vietnamese": None, "english": None, "error": True}


# ---------------------------------------------------------------------------
# Weekly digest
# ---------------------------------------------------------------------------

def run_weekly_digest() -> None:
    """
    Chạy digest tuần vào mỗi Chủ Nhật.
    Đọc bài từ 7 ngày qua, tổng hợp, gửi channel.
    """
    logger.info("=== Bắt đầu Weekly Digest ===")
    articles = get_articles_since(days=7)

    if not articles:
        logger.warning("Không có bài nào trong 7 ngày qua cho weekly digest")
        return

    now       = datetime.now(TZ_GMT7)
    week_num  = now.isocalendar()[1]
    end_dt    = now.strftime("%d/%m/%Y")
    start_dt  = (now.replace(hour=0, minute=0, second=0) - __import__("datetime").timedelta(days=6)).strftime("%d/%m/%Y")

    prompt = _WEEKLY_PROMPT.format(
        n            = len(articles),
        start_date   = start_dt,
        end_date     = end_dt,
        week_num     = week_num,
        article_list = _format_article_list(articles),
    )

    result = _generate_digest(prompt)
    label  = _week_label(now)

    if result.get("error") or not result.get("vietnamese"):
        logger.error("Weekly digest generation thất bại")
        log_digest("weekly", label, len(articles), "failed")
        return

    stats = {
        "article_count": len(articles),
        "period_label":  f"Tuần {week_num}/{now.year}",
    }
    asyncio.run(send_digest_async(result["vietnamese"], result["english"], "weekly", stats))
    log_digest("weekly", label, len(articles), "success")
    logger.info("Weekly digest gửi thành công, tổng hợp %d bài", len(articles))


# ---------------------------------------------------------------------------
# Monthly digest
# ---------------------------------------------------------------------------

def run_monthly_digest() -> None:
    """
    Chạy digest tháng vào ngày cuối tháng.
    Đọc bài từ 31 ngày qua, tổng hợp, gửi channel.
    """
    logger.info("=== Bắt đầu Monthly Digest ===")
    articles = get_articles_since(days=31)

    if not articles:
        logger.warning("Không có bài nào trong 31 ngày qua cho monthly digest")
        return

    now   = datetime.now(TZ_GMT7)
    month = now.month
    year  = now.year

    prompt = _MONTHLY_PROMPT.format(
        n            = len(articles),
        month        = month,
        year         = year,
        article_list = _format_article_list(articles),
    )

    result = _generate_digest(prompt)
    label  = _month_label(now)

    if result.get("error") or not result.get("vietnamese"):
        logger.error("Monthly digest generation thất bại")
        log_digest("monthly", label, len(articles), "failed")
        return

    stats = {
        "article_count": len(articles),
        "period_label":  f"Tháng {month}/{year}",
    }
    asyncio.run(send_digest_async(result["vietnamese"], result["english"], "monthly", stats))
    log_digest("monthly", label, len(articles), "success")
    logger.info("Monthly digest gửi thành công, tổng hợp %d bài", len(articles))
