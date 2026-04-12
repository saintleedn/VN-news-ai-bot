"""
main.py — Entry point cho Railway worker.
Vòng lặp 24/7 với scheduler:
  - 07:00 GMT+7 hằng ngày: chạy daily pipeline
  - 09:00 GMT+7 mỗi Chủ Nhật: chạy weekly digest (bỏ qua nếu cuối tháng)
  - 09:00 GMT+7 hằng ngày: kiểm tra và chạy monthly digest nếu cuối tháng

Lưu ý Railway: container chạy UTC. Tất cả schedule được convert từ GMT+7 → UTC.
"""

import calendar
import logging
import signal
import sys
import threading
from datetime import datetime, date

import schedule

from config import (
    TZ_GMT7, DAILY_SEND_HOUR, DAILY_SEND_MINUTE, DIGEST_SEND_HOUR,
)
from database import init_db, cleanup_old_records
from fetcher import fetch_all
from processor import process, count_trend_topics
from sender import send_all_immediately
from digest import run_weekly_digest, run_monthly_digest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

# Event dùng để dừng vòng lặp chính khi nhận SIGTERM
_shutdown_event = threading.Event()


def _handle_signal(signum, frame):
    """Xử lý SIGTERM / SIGINT để tắt bot nhẹ nhàng thay vì crash."""
    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    logger.info("Nhận %s — đang dừng bot gracefully...", sig_name)
    _shutdown_event.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ---------------------------------------------------------------------------
# Pipeline lock — ngăn 2 pipeline chạy đồng thời
# ---------------------------------------------------------------------------
_pipeline_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Scheduling helpers
# ---------------------------------------------------------------------------

def _gmt7_to_utc_hour(gmt7_hour: int) -> int:
    """
    Convert giờ GMT+7 sang UTC để schedule đúng trên Railway (container chạy UTC).
    VD: 07:00 GMT+7 → 00:00 UTC
    """
    return (gmt7_hour - 7) % 24


def _is_last_day_of_month() -> bool:
    """Kiểm tra hôm nay có phải ngày cuối tháng không (theo GMT+7)."""
    today    = datetime.now(TZ_GMT7).date()
    last_day = calendar.monthrange(today.year, today.month)[1]
    return today.day == last_day


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def _run_pipeline_thread():
    """
    Chạy pipeline lúc 7AM: fetch → process → gửi tất cả bài ngay lập tức.
    Không dùng Gemini, không queue theo giờ.
    """
    if not _pipeline_lock.acquire(blocking=False):
        logger.warning("Pipeline đang chạy rồi — bỏ qua trigger này")
        return

    def _task():
        try:
            logger.info("========== Daily Pipeline Bắt Đầu ==========")

            raw_articles = fetch_all()
            selected, stats = process(raw_articles)

            if not selected:
                logger.warning("Không có bài nào được chọn — pipeline kết thúc sớm")
                return

            trend_counts = count_trend_topics(raw_articles)
            send_all_immediately(selected, stats, trend_counts)
            cleanup_old_records()

            logger.info("========== Daily Pipeline Hoàn Thành — %d bài đã gửi ==========", len(selected))

        except Exception as e:
            logger.error("Lỗi pipeline: %s", e, exc_info=True)
        finally:
            _pipeline_lock.release()

    thread = threading.Thread(target=_task, name="pipeline", daemon=True)
    thread.start()


def run_sunday_digest():
    """
    Chạy weekly digest mỗi Chủ Nhật 09:00 GMT+7.
    Bỏ qua nếu hôm nay là ngày cuối tháng (nhường cho monthly digest).
    """
    if _is_last_day_of_month():
        logger.info("Chủ Nhật cuối tháng — bỏ qua weekly digest, nhường monthly")
        return
    thread = threading.Thread(target=run_weekly_digest, name="digest-weekly", daemon=True)
    thread.start()


def run_end_of_month_check():
    """
    Kiểm tra mỗi ngày lúc 09:00 GMT+7.
    Nếu là ngày cuối tháng → chạy monthly digest.
    """
    if _is_last_day_of_month():
        logger.info("Ngày cuối tháng — bắt đầu monthly digest")
        thread = threading.Thread(target=run_monthly_digest, name="digest-monthly", daemon=True)
        thread.start()


# ---------------------------------------------------------------------------
# Schedule setup
# ---------------------------------------------------------------------------

def _setup_schedule():
    """Đăng ký tất cả jobs với giờ đã convert sang UTC."""

    # Pipeline: 07:00 GMT+7 — fetch + gửi tất cả bài ngay
    utc_pipeline = _gmt7_to_utc_hour(DAILY_SEND_HOUR)
    t_pipeline   = f"{utc_pipeline:02d}:{DAILY_SEND_MINUTE:02d}"
    schedule.every().day.at(t_pipeline).do(_run_pipeline_thread)

    # Sunday digest: 09:00 GMT+7
    utc_digest  = _gmt7_to_utc_hour(DIGEST_SEND_HOUR)
    time_digest = f"{utc_digest:02d}:00"
    schedule.every().sunday.at(time_digest).do(run_sunday_digest)

    # End-of-month check: 09:00 GMT+7 (mỗi ngày)
    schedule.every().day.at(time_digest).do(run_end_of_month_check)

    logger.info("Lịch chạy đã được thiết lập:")
    logger.info("  Pipeline   : 07:00 GMT+7 (%s UTC) — fetch + gửi ngay", t_pipeline)
    logger.info("  Sunday digest  : 09:00 GMT+7 (%s UTC, Chủ Nhật)", time_digest)
    logger.info("  EOM check      : 09:00 GMT+7 (%s UTC, hằng ngày)", time_digest)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """Entry point — khởi tạo DB, thiết lập lịch, chạy vòng lặp 24/7."""
    logger.info("AI News Bot đang khởi động...")

    # Khởi tạo database (tạo bảng nếu chưa có)
    init_db()

    # Đăng ký schedule jobs
    _setup_schedule()

    logger.info("Bot đang chạy. Đang chờ trigger lúc 07:00 GMT+7...")

    # Vòng lặp chính — chờ tối đa 30s mỗi lần, dừng ngay nếu có SIGTERM
    while not _shutdown_event.is_set():
        schedule.run_pending()
        _shutdown_event.wait(timeout=30)

    logger.info("Bot đã dừng.")
    sys.exit(0)


if __name__ == "__main__":
    main()
