"""
config.py — Load biến môi trường và định nghĩa hằng số toàn cục.
Được import bởi tất cả các module khác, validate ngay khi khởi động.
"""

import os
import logging
import pytz
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Cấu hình logging chuẩn — dùng chung cho toàn bộ project
# ---------------------------------------------------------------------------
LOG_FORMAT = "[%(asctime)s] %(levelname)s — %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validate biến môi trường bắt buộc
# ---------------------------------------------------------------------------
_REQUIRED_VARS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHANNEL_ID",
    "TELEGRAM_ADMIN_CHAT_ID",
]


def _validate_env() -> dict:
    """Kiểm tra đủ 4 biến môi trường bắt buộc. Raise lỗi rõ ràng nếu thiếu."""
    config = {}
    missing = []
    for var in _REQUIRED_VARS:
        val = os.getenv(var, "").strip()
        if not val:
            missing.append(var)
        else:
            config[var] = val
    if missing:
        raise EnvironmentError(
            f"Thiếu biến môi trường bắt buộc: {', '.join(missing)}\n"
            f"Vui lòng điền đầy đủ vào file .env hoặc Railway Variables."
        )
    return config


# Gọi ngay khi import — fail fast nếu thiếu config
_ENV = _validate_env()

GEMINI_API_KEY         = os.getenv("GEMINI_API_KEY", "")  # optional — dùng cho digest
TELEGRAM_BOT_TOKEN     = _ENV["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID    = _ENV["TELEGRAM_CHANNEL_ID"]
TELEGRAM_ADMIN_CHAT_ID = _ENV["TELEGRAM_ADMIN_CHAT_ID"]

# ---------------------------------------------------------------------------
# Múi giờ & lịch gửi bài
# ---------------------------------------------------------------------------
TZ_GMT7 = pytz.timezone("Asia/Ho_Chi_Minh")  # UTC+7

DAILY_SEND_HOUR   = 7    # 07:00 GMT+7 — pipeline fetch + viết bài
DAILY_SEND_MINUTE = 0
DIGEST_SEND_HOUR  = 9    # 09:00 GMT+7 — gửi digest tuần/tháng

# Lịch gửi từng bài lên channel (GMT+7)
POST1_SEND_HOUR = 7   # Morning Brief — 07:00
POST2_SEND_HOUR = 12  # Deep Focus    — 12:00
POST3_SEND_HOUR = 17  # Brain Spark   — 17:00

# ---------------------------------------------------------------------------
# Nguồn RSS
# ---------------------------------------------------------------------------
RSS_FEEDS = {
    "TechCrunch AI":  "https://techcrunch.com/category/artificial-intelligence/feed/",
    "VentureBeat AI": "https://venturebeat.com/category/ai/feed/",
    "The Verge AI":   "https://www.theverge.com/rss/index.xml",
    "Decrypt":        "https://decrypt.co/feed",
    "CoinDesk":       "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "VnExpress CN":   "https://vnexpress.net/rss/khoa-hoc-cong-nghe.rss",
}

# Từ khóa tìm kiếm trên Google News RSS
GOOGLE_NEWS_KEYWORDS = [
    "AI agent 2026",
    "artificial intelligence breakthrough",
    "AI crypto blockchain",
    "large language model",
]

# ---------------------------------------------------------------------------
# Cấu hình fetch
# ---------------------------------------------------------------------------
FETCH_TIMEOUT           = 10   # giây timeout cho mỗi request HTTP
MAX_ARTICLES_PER_SOURCE = 10   # số bài tối đa lấy từ mỗi nguồn

# ---------------------------------------------------------------------------
# Gemini API (Google AI Studio — free tier)
# ---------------------------------------------------------------------------
GEMINI_MODEL       = "gemini-2.5-flash"   # miễn phí: 15 req/phút, 1500 req/ngày
GEMINI_MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# Độ trễ gửi Telegram
# ---------------------------------------------------------------------------
SEND_DELAY_BETWEEN_LANG  = 3     # giây giữa bản VI và EN của cùng 1 bài
SEND_DELAY_BETWEEN_POSTS = 1800  # 30 phút giữa các bài (giây)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH      = "data/bot.db"
CLEANUP_DAYS = 30  # xóa bản ghi cũ hơn N ngày

# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------
KEYWORD_OVERLAP_THRESHOLD = 0.40  # ngưỡng Jaccard để gộp bài liên quan
TARGET_ARTICLE_COUNT      = 3     # số bài gửi mỗi ngày (legacy — không dùng nữa)

# ---------------------------------------------------------------------------
# Scoring weights (tổng = 1.0)
# ---------------------------------------------------------------------------
SCORE_RECENCY_WEIGHT  = 0.35
SCORE_SOURCE_WEIGHT   = 0.20
SCORE_COVERAGE_WEIGHT = 0.20
SCORE_KEYWORD_WEIGHT  = 0.15
SCORE_CRYPTO_WEIGHT   = 0.10

SCORE_BOOST_KEYWORDS = [
    "agent", "llm", "gpt", "claude", "gemini", "open source",
    "funding", "launch", "breakthrough", "raises", "billion",
    "million", "series", "release", "introduces",
]
SCORE_CRYPTO_KEYWORDS = [
    "bitcoin", "ethereum", "defi", "solana", "bnb", "xrp",
    "token", "nft", "depin", "stablecoin", "layer2",
]

# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------
MIN_ARTICLES_FETCH    = 20   # retry fetch nếu tổng bài < ngưỡng này
MIN_ARTICLES_POST     = 5    # alert admin + bỏ qua channel nếu sau dedup < ngưỡng
FETCH_RETRY_DELAY_SEC = 60   # giây chờ trước khi retry fetch

# ---------------------------------------------------------------------------
# Trend signals
# ---------------------------------------------------------------------------
TREND_MIN_COUNT = 3   # topic phải xuất hiện >= N lần để hiển thị
TREND_TOP_N     = 3   # hiển thị tối đa N topic trong signals block
