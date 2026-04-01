"""
database.py — Tất cả thao tác SQLite cho bot.
Tự tạo bảng nếu chưa có. Dùng WAL mode cho phép đọc/ghi đồng thời.
"""

import os
import sqlite3
import hashlib
import logging
from datetime import datetime, timedelta

from config import DB_PATH, CLEANUP_DAYS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kết nối
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    """Tạo kết nối SQLite mới với WAL mode và row_factory."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")  # Cho phép đọc/ghi đồng thời từ nhiều thread
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Khởi tạo schema
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Tạo tất cả các bảng nếu chưa tồn tại."""
    conn = _get_conn()
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash       TEXT UNIQUE NOT NULL,
                title          TEXT NOT NULL,
                source         TEXT NOT NULL,
                processed_date TEXT NOT NULL,
                language       TEXT DEFAULT 'en'
            );

            CREATE TABLE IF NOT EXISTS sent_posts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER NOT NULL REFERENCES articles(id),
                sent_date  TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'success'
            );

            CREATE TABLE IF NOT EXISTS pending_posts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                post_type  TEXT NOT NULL,
                title      TEXT NOT NULL,
                vi_text    TEXT NOT NULL,
                article_id INTEGER,
                created_at TEXT NOT NULL,
                sent       INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS digest_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                digest_type   TEXT NOT NULL,
                period_label  TEXT NOT NULL,
                sent_date     TEXT NOT NULL,
                article_count INTEGER NOT NULL,
                status        TEXT NOT NULL DEFAULT 'success'
            );
        """)
    conn.close()
    logger.info("Database initialized: %s", DB_PATH)


# ---------------------------------------------------------------------------
# Hash helper
# ---------------------------------------------------------------------------

def _url_hash(url: str) -> str:
    """SHA256 hash của URL để dedup theo link."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _title_hash(title: str) -> str:
    """SHA256 hash của title đã normalize để dedup theo nội dung."""
    normalized = title.lower().strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def is_duplicate(url: str, title: str) -> bool:
    """Kiểm tra bài đã tồn tại theo URL hash hoặc title hash."""
    url_h   = _url_hash(url)
    title_h = _title_hash(title)
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM articles WHERE url_hash = ? OR url_hash = ?",
            (url_h, title_h),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def save_article(url: str, title: str, source: str, language: str = "en") -> int:
    """
    Lưu bài viết vào DB. Trả về id của row mới.
    Raise sqlite3.IntegrityError nếu URL đã tồn tại (duplicate).
    """
    url_h = _url_hash(url)
    now   = datetime.utcnow().isoformat()
    conn  = _get_conn()
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO articles (url_hash, title, source, processed_date, language) "
                "VALUES (?, ?, ?, ?, ?)",
                (url_h, title, source, now, language),
            )
            return cursor.lastrowid
    finally:
        conn.close()


def mark_sent(article_id: int, status: str = "success") -> None:
    """Ghi nhận bài đã được gửi lên channel."""
    now  = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        with conn:
            conn.execute(
                "INSERT INTO sent_posts (article_id, sent_date, status) VALUES (?, ?, ?)",
                (article_id, now, status),
            )
    finally:
        conn.close()


def save_pending_post(post_type: str, title: str, vi_text: str, article_id: int | None) -> int:
    """Lưu bài đã viết xong vào hàng chờ để gửi sau."""
    now  = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO pending_posts (post_type, title, vi_text, article_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (post_type, title, vi_text, article_id, now),
            )
            return cursor.lastrowid
    finally:
        conn.close()


def pop_pending_post(post_type: str) -> dict | None:
    """
    Lấy bài chờ gửi theo post_type (chưa gửi, mới nhất trong ngày).
    Đánh dấu sent=1 ngay khi lấy ra (optimistic lock).
    Trả về dict hoặc None nếu không có.
    """
    today = datetime.utcnow().date().isoformat()
    conn  = _get_conn()
    try:
        with conn:
            row = conn.execute(
                "SELECT * FROM pending_posts "
                "WHERE post_type = ? AND sent = 0 AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT 1",
                (post_type, today),
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE pending_posts SET sent = 1 WHERE id = ?", (row["id"],))
            return dict(row)
    finally:
        conn.close()


def clear_pending_posts() -> None:
    """Xóa toàn bộ bài chờ chưa gửi (dọn dẹp khi pipeline chạy lại)."""
    conn = _get_conn()
    try:
        with conn:
            conn.execute("DELETE FROM pending_posts WHERE sent = 0")
    finally:
        conn.close()


def log_digest(
    digest_type: str,
    period_label: str,
    article_count: int,
    status: str = "success",
) -> None:
    """Ghi log digest đã gửi (weekly/monthly)."""
    now  = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        with conn:
            conn.execute(
                "INSERT INTO digest_log (digest_type, period_label, sent_date, article_count, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (digest_type, period_label, now, article_count, status),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def get_articles_since(days: int) -> list:
    """
    Lấy tất cả bài đã gửi thành công trong N ngày gần nhất.
    Dùng để tạo weekly/monthly digest.
    """
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn  = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT a.title, a.source, a.processed_date
            FROM articles a
            JOIN sent_posts sp ON sp.article_id = a.id
            WHERE sp.status = 'success' AND sp.sent_date >= ?
            ORDER BY a.processed_date DESC
            """,
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def cleanup_old_records(days: int = CLEANUP_DAYS) -> int:
    """
    Xóa bản ghi cũ hơn N ngày để giữ DB gọn nhẹ.
    Trả về số bài đã xóa.
    """
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn   = _get_conn()
    try:
        with conn:
            # Xóa sent_posts trước vì có foreign key tới articles
            conn.execute("DELETE FROM sent_posts WHERE sent_date < ?", (cutoff,))
            result = conn.execute(
                "DELETE FROM articles WHERE processed_date < ?", (cutoff,)
            )
            deleted = result.rowcount
        logger.info("Cleanup: đã xóa %d bản ghi cũ hơn %d ngày", deleted, days)
        return deleted
    finally:
        conn.close()
