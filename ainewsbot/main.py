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
    POST1_SEND_HOUR, POST2_SEND_HOUR, POST3_SEND_HOUR,
)
from database import init_db, cleanup_old_records
from fetcher import fetch_all
from processor import process
from writer import write_all
from sender import send_daily_articles, send_scheduled_post
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
    Chạy pipeline lúc 7AM: fetch → process → write → lưu pending_posts → gửi admin report.
    Bài sẽ được gửi channel theo lịch riêng (7h/12h/17h).
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

            enriched = write_all(selected)          # lưu vào pending_posts
            send_daily_articles(enriched, stats)    # chỉ gửi admin report
            cleanup_old_records()

            logger.info("========== Daily Pipeline Hoàn Thành — Bài sẽ gửi 7h/12h/17h ==========")

        except Exception as e:
            logger.error("Lỗi pipeline: %s", e, exc_info=True)
        finally:
            _pipeline_lock.release()

    thread = threading.Thread(target=_task, name="pipeline", daemon=True)
    thread.start()


def _run_send_post(post_type: str):
    """Gửi 1 bài theo lịch từ pending_posts."""
    def _task():
        try:
            logger.info("Đang gửi bài theo lịch: %s", post_type)
            send_scheduled_post(post_type)
        except Exception as e:
            logger.error("Lỗi gửi bài %s: %s", post_type, e, exc_info=True)

    thread = threading.Thread(target=_task, name=f"send-{post_type}", daemon=True)
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

    # Pipeline: 07:00 GMT+7 — fetch + write + lưu pending
    utc_pipeline = _gmt7_to_utc_hour(DAILY_SEND_HOUR)
    t_pipeline   = f"{utc_pipeline:02d}:{DAILY_SEND_MINUTE:02d}"
    schedule.every().day.at(t_pipeline).do(_run_pipeline_thread)

    # Gửi Morning Brief: 07:00 GMT+7 (sau pipeline ~1-2 phút nên đặt 07:05)
    utc_p1 = _gmt7_to_utc_hour(POST1_SEND_HOUR)
    t_p1   = f"{utc_p1:02d}:05"
    schedule.every().day.at(t_p1).do(lambda: _run_send_post("morning_brief"))

    # Gửi Deep Focus: 12:00 GMT+7
    utc_p2 = _gmt7_to_utc_hour(POST2_SEND_HOUR)
    t_p2   = f"{utc_p2:02d}:00"
    schedule.every().day.at(t_p2).do(lambda: _run_send_post("deep_focus"))

    # Gửi Brain Spark: 17:00 GMT+7
    utc_p3 = _gmt7_to_utc_hour(POST3_SEND_HOUR)
    t_p3   = f"{utc_p3:02d}:00"
    schedule.every().day.at(t_p3).do(lambda: _run_send_post("brain_spark"))

    # Sunday digest: 09:00 GMT+7
    utc_digest  = _gmt7_to_utc_hour(DIGEST_SEND_HOUR)
    time_digest = f"{utc_digest:02d}:00"
    schedule.every().sunday.at(time_digest).do(run_sunday_digest)

    # End-of-month check: 09:00 GMT+7 (mỗi ngày)
    schedule.every().day.at(time_digest).do(run_end_of_month_check)

    logger.info("Lịch chạy đã được thiết lập:")
    logger.info("  Pipeline       : 07:00 GMT+7 (%s UTC)", t_pipeline)
    logger.info("  Morning Brief  : 07:05 GMT+7 (%s UTC)", t_p1)
    logger.info("  Deep Focus     : 12:00 GMT+7 (%s UTC)", t_p2)
    logger.info("  Brain Spark    : 17:00 GMT+7 (%s UTC)", t_p3)
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
