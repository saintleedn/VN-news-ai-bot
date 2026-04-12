"""
sender.py — Gửi bài lên Telegram channel và báo cáo admin.

Lưu ý kiến trúc async:
- python-telegram-bot v20+ là fully async
- Bot() phải được dùng trong 'async with Bot(...) as bot:' context
- Mỗi lần gọi asyncio.run() tạo một event loop mới — Bot không được tái sử dụng
  qua nhiều asyncio.run() calls (sẽ gây "Event loop is closed" error)
"""

import asyncio
import logging
from datetime import datetime, timedelta

from telegram import Bot
from telegram.error import TelegramError, RetryAfter, TimedOut

from database import mark_sent, pop_pending_post
from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHANNEL_ID,
    TELEGRAM_ADMIN_CHAT_ID,
    SEND_DELAY_BETWEEN_LANG,
    TZ_GMT7,
)

logger = logging.getLogger(__name__)

_MAX_SEND_RETRIES  = 3
_RETRY_DELAY_SEC   = 10


# ---------------------------------------------------------------------------
# Low-level send với retry
# ---------------------------------------------------------------------------

async def _send_msg(bot: Bot, chat_id: str, text: str, retries: int = _MAX_SEND_RETRIES) -> bool:
    """
    Gửi một tin nhắn Telegram với retry logic.
    Xử lý rate-limit (RetryAfter) và timeout tự động.
    """
    for attempt in range(1, retries + 1):
        try:
            await bot.send_message(
                chat_id                  = chat_id,
                text                     = text,
                parse_mode               = "HTML",
                disable_web_page_preview = False,
            )
            return True

        except RetryAfter as e:
            # Telegram yêu cầu chờ trước khi gửi tiếp
            wait = e.retry_after + 1
            logger.warning("Rate limited — chờ %ds", wait)
            await asyncio.sleep(wait)

        except TimedOut:
            logger.warning("[Lần %d] Telegram timeout, thử lại sau %ds", attempt, _RETRY_DELAY_SEC)
            await asyncio.sleep(_RETRY_DELAY_SEC)

        except TelegramError as e:
            logger.error("[Lần %d] Telegram error: %s", attempt, e)
            if attempt < retries:
                await asyncio.sleep(_RETRY_DELAY_SEC)

    return False


# ---------------------------------------------------------------------------
# Gửi một bài (VI + EN)
# ---------------------------------------------------------------------------

async def _send_article(bot: Bot, article: dict, index: int, total: int) -> bool:
    """
    Gửi một bài lên channel — chỉ tiếng Việt.
    Trả về True nếu gửi thành công.
    """
    post_type   = article.get("post_type", f"bài {index}")
    title_short = article.get("title", "Article")[:50]
    vi_text     = article.get("vi_text")

    if not vi_text:
        logger.error("Bài %d/%d (%s) không có nội dung — bỏ qua", index, total, post_type)
        return False

    logger.info("Đang gửi bài %d/%d [%s]: %s", index, total, post_type, title_short)

    ok = await _send_msg(bot, TELEGRAM_CHANNEL_ID, vi_text)
    if not ok:
        logger.error("Thất bại khi gửi bài %d/%d (%s)", index, total, post_type)
        await _send_msg(
            bot, TELEGRAM_ADMIN_CHAT_ID,
            f"⚠️ <b>Lỗi gửi bài</b>\nBài {index}/{total} [{post_type}] thất bại sau {_MAX_SEND_RETRIES} lần thử."
        )
        return False

    logger.info("Bài %d/%d [%s] gửi thành công", index, total, post_type)
    return True


# ---------------------------------------------------------------------------
# Admin report
# ---------------------------------------------------------------------------

async def _send_admin_report(bot: Bot, articles: list, stats: dict) -> None:
    """
    Gửi báo cáo tổng hợp cho admin TRƯỚC khi gửi bài lên channel.
    Bao gồm: số liệu fetch, nguồn hôm nay, lịch đăng, lỗi nếu có.
    """
    now = datetime.now(TZ_GMT7).strftime("%d/%m/%Y %H:%M GMT+7")

    # Thống kê nguồn
    sources_str = "\n".join(
        f"  • {src}: {count} bài"
        for src, count in stats.get("sources_breakdown", {}).items()
    ) or "  • Không có"

    # Lịch đăng ước tính
    base_hour   = 7
    base_minute = 0
    schedule_lines = []
    for i in range(len(articles)):
        total_minutes = base_hour * 60 + base_minute + i * 30
        h = total_minutes // 60
        m = total_minutes % 60
        schedule_lines.append(f"  • Bài {i + 1}: {h:02d}:{m:02d}")
    schedule_str = "\n".join(schedule_lines) or "  • Không có bài nào"

    write_errors = sum(1 for a in articles if a.get("write_error"))

    report = (
        f"✅ <b>BOT REPORT | {now}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📰 Gen xong: Morning Brief + Deep Focus + Brain Spark\n\n"
        f"📊 <b>Thống kê fetch:</b>\n"
        f"  • Tổng fetch: {stats.get('total_fetched', 0)}\n"
        f"  • Bị lọc trùng: {stats.get('duplicates_filtered', 0)}\n"
        f"  • Sau dedup: {stats.get('after_dedup', 0)}\n"
        f"  • Nhóm bài: {stats.get('groups_formed', 0)}\n\n"
        f"🗞️ <b>Nguồn hôm nay:</b>\n{sources_str}\n\n"
        f"⏭️ <b>Lịch đăng:</b>\n{schedule_str}\n\n"
        f"⚠️ <b>Lỗi write:</b> {write_errors if write_errors else 'Không có'}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    await _send_msg(bot, TELEGRAM_ADMIN_CHAT_ID, report)


# ---------------------------------------------------------------------------
# Daily pipeline send (lưu vào pending, gửi admin report)
# ---------------------------------------------------------------------------

async def _send_daily_articles_async(articles: list, stats: dict) -> None:
    """
    Gửi admin report sau khi pipeline viết xong.
    Bài đã được lưu vào pending_posts — sẽ gửi channel theo lịch riêng.
    """
    async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
        await _send_admin_report(bot, articles, stats)

    logger.info("Admin report đã gửi. Bài sẽ được gửi theo lịch 7h/12h/17h.")


# ---------------------------------------------------------------------------
# Gửi 1 bài theo lịch (gọi từ scheduler)
# ---------------------------------------------------------------------------

async def _send_scheduled_post_async(post_type: str) -> None:
    """Lấy bài từ pending_posts và gửi lên channel."""
    post = pop_pending_post(post_type)
    if post is None:
        logger.warning("Không tìm thấy bài pending cho post_type='%s' — bỏ qua", post_type)
        return

    article = {
        "post_type": post["post_type"],
        "title":     post["title"],
        "vi_text":   post["vi_text"],
        "db_id":     post["article_id"],
    }

    async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
        success = await _send_article(bot, article, 1, 1)
        db_id = article.get("db_id")
        if db_id is not None:
            mark_sent(db_id, "success" if success else "failed")


# ---------------------------------------------------------------------------
# Digest send
# ---------------------------------------------------------------------------

async def send_digest_async(
    vi_text: str,
    en_text: str,
    digest_type: str,
    stats: dict,
) -> None:
    """
    Gửi digest (weekly/monthly) lên channel.
    VI trước → 3s → EN, sau đó báo admin.
    """
    async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
        # Thông báo admin trước
        article_count = stats.get("article_count", 0)
        type_label    = "Tuần" if digest_type == "weekly" else "Tháng"
        await _send_msg(
            bot, TELEGRAM_ADMIN_CHAT_ID,
            f"📋 <b>Đang gửi {type_label} Digest...</b>\n"
            f"Tổng hợp từ {article_count} bài đã đăng."
        )

        # Gửi lên channel
        vi_ok = await _send_msg(bot, TELEGRAM_CHANNEL_ID, vi_text)
        await asyncio.sleep(SEND_DELAY_BETWEEN_LANG)
        en_ok = await _send_msg(bot, TELEGRAM_CHANNEL_ID, en_text)

        # Báo cáo kết quả cho admin
        now = datetime.now(TZ_GMT7).strftime("%d/%m/%Y %H:%M")
        status_emoji = "✅" if (vi_ok and en_ok) else "⚠️"
        period = stats.get("period_label", "N/A")

        await _send_msg(
            bot, TELEGRAM_ADMIN_CHAT_ID,
            f"{status_emoji} <b>DIGEST REPORT | {type_label.upper()}</b>\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"📊 Loại: {type_label} Digest ({period})\n"
            f"📰 Tổng hợp từ: {article_count} bài\n"
            f"✅ Đã gửi channel lúc: {now}\n"
            f"━━━━━━━━━━━━━━━━"
        )

        logger.info("%s digest gửi thành công (%s)", digest_type, period)


# ---------------------------------------------------------------------------
# Synchronous entry points (gọi từ scheduler thread)
# ---------------------------------------------------------------------------

def send_daily_articles(articles: list, stats: dict) -> None:
    """Entry point đồng bộ — gửi admin report sau khi pipeline viết xong."""
    asyncio.run(_send_daily_articles_async(articles, stats))


def send_scheduled_post(post_type: str) -> None:
    """Entry point đồng bộ — gửi 1 bài theo lịch (7h/12h/17h)."""
    asyncio.run(_send_scheduled_post_async(post_type))


# ---------------------------------------------------------------------------
# Gửi tất cả bài ngay lập tức (không queue, không AI)
# ---------------------------------------------------------------------------

async def _send_all_immediately_async(articles: list, stats: dict, trend_counts: dict = None) -> None:
    """Gửi toàn bộ articles thành tin nhắn Telegram ngay lập tức."""
    from template_writer import build_messages
    from config import MIN_ARTICLES_POST

    if trend_counts is None:
        trend_counts = {}

    # --- Resilience: quá ít bài → alert admin, bỏ qua channel ---
    if len(articles) < MIN_ARTICLES_POST:
        logger.warning("Chỉ có %d bài sau dedup (< %d) — bỏ qua gửi channel", len(articles), MIN_ARTICLES_POST)
        async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
            await _send_msg(
                bot, TELEGRAM_ADMIN_CHAT_ID,
                f"⚠️ <b>LOW CONTENT ALERT</b>\n"
                f"Chỉ có <b>{len(articles)} bài</b> sau dedup (ngưỡng: {MIN_ARTICLES_POST}).\n"
                f"Bỏ qua gửi channel hôm nay.\n"
                f"Tổng fetch: {stats.get('total_fetched', 0)} | Bị lọc: {stats.get('duplicates_filtered', 0)}"
            )
        return

    messages = build_messages(articles, trend_counts)
    if not messages:
        logger.warning("Không có bài nào để gửi")
        return

    async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
        # Admin report
        now = datetime.now(TZ_GMT7).strftime("%d/%m/%Y %H:%M GMT+7")
        sources_str = "\n".join(
            f"  • {src}: {count} bài"
            for src, count in stats.get("sources_breakdown", {}).items()
        ) or "  • Không có"
        trend_str = "\n".join(
            f"  • {t}: ↑{c}" for t, c in trend_counts.items()
        ) or "  • Không có"

        report = (
            f"✅ <b>BOT REPORT | {now}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📰 Gửi: {len(articles)} bài → {len(messages)} tin nhắn\n\n"
            f"📊 <b>Thống kê fetch:</b>\n"
            f"  • Tổng fetch: {stats.get('total_fetched', 0)}\n"
            f"  • Bị lọc trùng: {stats.get('duplicates_filtered', 0)}\n"
            f"  • Sau dedup: {stats.get('after_dedup', 0)}\n"
            f"  • Nhóm bài: {stats.get('groups_formed', 0)}\n\n"
            f"🗞️ <b>Nguồn hôm nay:</b>\n{sources_str}\n\n"
            f"🔥 <b>Trend signals:</b>\n{trend_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        await _send_msg(bot, TELEGRAM_ADMIN_CHAT_ID, report)

        # Gửi lên channel
        for i, msg in enumerate(messages, 1):
            ok = await _send_msg(bot, TELEGRAM_CHANNEL_ID, msg)
            if not ok:
                logger.error("Thất bại khi gửi tin nhắn %d/%d", i, len(messages))
                await _send_msg(
                    bot, TELEGRAM_ADMIN_CHAT_ID,
                    f"⚠️ <b>Lỗi gửi tin nhắn {i}/{len(messages)}</b>"
                )
            else:
                logger.info("Tin nhắn %d/%d gửi thành công", i, len(messages))
            if i < len(messages):
                await asyncio.sleep(1)

    logger.info("Đã gửi xong %d bài trong %d tin nhắn", len(articles), len(messages))


def send_all_immediately(articles: list, stats: dict, trend_counts: dict = None) -> None:
    """Entry point đồng bộ — gửi tất cả articles ngay lập tức (không queue)."""
    asyncio.run(_send_all_immediately_async(articles, stats, trend_counts))
