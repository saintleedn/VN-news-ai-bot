# AI News Telegram Bot

Bot tự động thu thập tin tức AI, viết lại bằng Claude API (song ngữ Việt/Anh), và gửi lên Telegram channel hằng ngày lúc 7 giờ sáng. Chạy 24/7 trên Railway.app.

## Tính năng

- Thu thập tin từ 6 RSS feeds + Google News (10 từ khóa)
- Lọc bỏ tin trùng lặp tự động
- Gộp bài cùng chủ đề bằng Jaccard similarity
- Gọi Claude API (`claude-sonnet-4-20250514`) viết lại content song ngữ
- Gửi 3 bài/ngày lên channel lúc 07:00 GMT+7 (cách nhau 30 phút/bài)
- Báo cáo admin ngay sau khi gen xong content
- Weekly digest mỗi Chủ Nhật 09:00
- Monthly digest ngày cuối tháng 09:00

---

## Cài đặt & Deploy

### Bước 1 — Clone và cấu hình `.env`

```bash
git clone <your-repo-url>
cd "bot AI news"
cp .env.example .env
```

Mở file `.env` và điền 4 giá trị:

| Biến | Mô tả |
|------|-------|
| `ANTHROPIC_API_KEY` | API key từ console.anthropic.com |
| `TELEGRAM_BOT_TOKEN` | Token từ @BotFather |
| `TELEGRAM_CHANNEL_ID` | `@username` hoặc numeric ID của channel |
| `TELEGRAM_ADMIN_CHAT_ID` | Chat ID của bạn để nhận báo cáo |

**Lưu ý Telegram:**
- Bot phải là **Admin** của channel với quyền "Post Messages"
- Để lấy `TELEGRAM_ADMIN_CHAT_ID`: nhắn bất kỳ tin nhắn cho bot, sau đó truy cập `https://api.telegram.org/bot<TOKEN>/getUpdates`

### Bước 2 — Test local

```bash
pip install -r requirements.txt
python ainewsbot/main.py
```

Bot sẽ khởi động và in ra lịch chạy. Để test ngay không cần chờ đến 7 giờ sáng, tạm thời sửa `main.py` đổi giờ schedule thành 1 phút từ hiện tại.

Kiểm tra output trong terminal — bot sẽ in log mỗi bước: fetch → process → write → send.

### Bước 3 — Push lên GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

> **Bảo mật:** File `.env` đã có trong `.gitignore`. Không commit file `.env` lên GitHub.

### Bước 4 — Deploy lên Railway

1. Truy cập [railway.app](https://railway.app) → **New Project**
2. Chọn **Deploy from GitHub repo**
3. Chọn repo vừa push
4. Railway sẽ tự detect `Procfile` và build với Nixpacks

### Bước 5 — Cấu hình biến môi trường trên Railway

1. Trong Railway Dashboard → chọn service của bạn
2. Tab **Variables** → **Add Variables**
3. Thêm đủ 4 biến (copy từ file `.env` local)
4. Click **Deploy** — Railway sẽ restart với config mới

### Bước 6 — Kiểm tra

- Tab **Logs** trên Railway: tìm dòng `AI News Bot đang khởi động...`
- Telegram admin chat: nhận được báo cáo hằng ngày sau 7 giờ sáng
- Telegram channel: nhận được 3 bài cách nhau 30 phút

---

## Cấu trúc project

```
.
├── ainewsbot/
│   ├── main.py        # Scheduler chính, vòng lặp 24/7
│   ├── config.py      # Load .env, constants
│   ├── database.py    # SQLite operations
│   ├── fetcher.py     # RSS + Google News
│   ├── processor.py   # Dedup, grouping, chọn bài
│   ├── writer.py      # Claude API → bilingual content
│   ├── sender.py      # Telegram send + admin report
│   └── digest.py      # Weekly + Monthly digest
├── requirements.txt
├── Procfile
├── railway.json
├── .env.example
└── README.md
```

---

## Lưu ý quan trọng

### SQLite và Railway

Railway dùng **ephemeral filesystem** — file `data/bot.db` sẽ bị xóa mỗi khi deploy hoặc restart. Điều này có nghĩa là lịch sử dedup sẽ reset, và trong ngày đầu sau restart có thể có bài trùng lặp.

Nếu muốn persistence, cần:
1. Thêm **Railway Volume** (persistent disk)
2. Mount tại `/data`
3. Thêm biến môi trường `DB_PATH=/data/bot.db`

### Timezone

Bot schedule theo **GMT+7 (Asia/Ho_Chi_Minh)**. Trên Railway container (UTC), lịch tự động được convert:
- 07:00 GMT+7 → 00:00 UTC (daily pipeline)
- 09:00 GMT+7 → 02:00 UTC (digest)

### Chi phí ước tính

| Dịch vụ | Chi phí |
|---------|---------|
| Railway Hobby plan | ~$5/tháng |
| Claude API (~3 bài × 30 ngày) | ~$2-5/tháng tùy độ dài |
| **Tổng** | ~$7-10/tháng |
