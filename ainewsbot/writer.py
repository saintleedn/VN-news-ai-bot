"""
writer.py — Tạo 3 loại bài theo format cố định:
  Bài 1: Morning Brief — điểm tin nhanh tất cả headlines
  Bài 2: Deep Focus — phân tích sâu 1 tin quan trọng nhất
  Bài 3: Brain Spark — câu hỏi/vote/tip/prediction (xoay vòng theo ngày)

Chỉ tiếng Việt. Không dịch sang EN.
"""

import json
import time
import logging
from datetime import datetime

from google import genai

from config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_MAX_RETRIES, TZ_GMT7
from database import save_pending_post, clear_pending_posts

logger  = logging.getLogger(__name__)
_client = genai.Client(api_key=GEMINI_API_KEY)

# ---------------------------------------------------------------------------
# Quy tắc HTML Telegram
# ---------------------------------------------------------------------------
_HTML_RULES = """
QUY TẮC ĐỊNH DẠNG BẮT BUỘC:
- Dùng <b>text</b> để in đậm — viết ĐÚNG nguyên văn ký tự < và >, KHÔNG escape thành &lt; hay &gt;
- Dùng <a href="url">text</a> cho link — tương tự, viết < và > nguyên văn
- KHÔNG dùng Markdown (không **, không __, không ```)
- Ký tự & trong NỘI DUNG TEXT (không phải trong tag) phải viết là &amp;
- Trả về text thuần, KHÔNG wrap trong JSON hay code block
- Ký tự & trong nội dung viết là &amp;, nhưng trong HTML tag <b> và <a> thì viết bình thường
"""

# ---------------------------------------------------------------------------
# PROMPT BÀI 1 — MORNING BRIEF
# ---------------------------------------------------------------------------
_PROMPT_MORNING_BRIEF = """\
Bạn là editor kênh Telegram AI có 500K follower. Viết bài MORNING BRIEF cho ngày {date}.

Tin tức hôm nay:
{headlines}

{html_rules}

Viết CHÍNH XÁC theo format sau. Chỉ thay phần trong []:

🌅 <b>AI MORNING BRIEF | {date}</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

[1 câu hook mở đầu — nêu sự kiện lớn nhất, gây tò mò ngay, tối đa 15 từ]

🔵 [Tin 1: động từ mạnh + kết quả cụ thể, tối đa 10 từ]
🟣 [Tin 2: động từ mạnh + kết quả cụ thể, tối đa 10 từ]
🟠 [Tin 3: động từ mạnh + kết quả cụ thể, tối đa 10 từ]

💬 Tin nào đáng lo nhất?
👇 Comment số bên dưới

#AIMorningBrief #AINews #CôngNghệAI

NGUYÊN TẮC VIẾT:
- Câu hook phải tạo cảm giác "chuyện gì đang xảy ra" — KHÔNG được dùng "Hôm nay đáng chú ý" hay câu mở nhàm
- Mỗi bullet dùng động từ mạnh đứng đầu: "Bùng nổ", "Sụp đổ", "Vượt mặt", "Cảnh báo", "Lật ngược"
- CTA cuối hỏi thứ gây lo lắng hoặc tò mò, không hỏi chung chung
- KHÔNG giải thích thêm, chỉ trả về text bài post
"""

# ---------------------------------------------------------------------------
# PROMPT BÀI 2 — DEEP FOCUS
# ---------------------------------------------------------------------------
_PROMPT_DEEP_FOCUS = """\
Bạn là editor kênh Telegram AI có 500K follower. Viết bài DEEP FOCUS cho ngày {date}.

Tin quan trọng nhất:
Tiêu đề: {title}
Nguồn: {source}
Tóm tắt: {summary}
URL: {url}

{html_rules}

Viết CHÍNH XÁC theo format sau. Chỉ thay phần trong []:

🔍 <b>DEEP FOCUS | {date}</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

📌 <b>[TIÊU ĐỀ VIẾT HOA — mạnh, gây sốc hoặc tạo FOMO, tối đa 10 từ]</b>

[1-2 câu mở — đập thẳng vào vấn đề, KHÔNG giới thiệu lan man. Ví dụ: "X triệu người dùng vừa bị lộ dữ liệu." hoặc "Ví của bạn có thể không còn an toàn."]

→ [Điểm mấu chốt 1 — con số cụ thể hoặc hành động]
→ [Điểm mấu chốt 2 — ai bị ảnh hưởng / lợi gì / thiệt gì]
→ [Điểm mấu chốt 3 — điều ít ai để ý / góc nhìn bất ngờ]

⚡ <b>Tại sao quan trọng với bạn ngay lúc này:</b>
[2 câu — kết nối trực tiếp với người đọc. "Nếu bạn đang dùng X, hãy làm Y ngay." Tạo FOMO hoặc urgency thực sự.]

🔗 <a href="{url}">Đọc đầy đủ</a>

#DeepFocus #[2 hashtag chủ đề]

NGUYÊN TẮC VIẾT:
- Câu mở phải "đập vào mặt" ngay — KHÔNG dùng "Vấn đề thực sự là gì?" hay câu hỏi mở nhàm
- Mỗi → phải có số liệu hoặc hành động cụ thể, KHÔNG nói chung chung
- CTA phải tạo FOMO: "Nếu bạn chưa làm điều này, bạn đang bỏ lỡ / đang có rủi ro"
- Tổng bài KHÔNG quá 200 từ
- KHÔNG giải thích thêm, chỉ trả về text bài post
"""

# ---------------------------------------------------------------------------
# PROMPT BÀI 3 — BRAIN SPARK (3 variant xoay vòng theo ngày)
# ---------------------------------------------------------------------------

# Thứ 2=0, Thứ 3=1, Thứ 4=2, Thứ 5=3, Thứ 6=4, Thứ 7=5, CN=6
# Variant: 0,2,4 → Vote | 1,3 → Tip | 5 → Prediction | 6 → Vote
_BRAIN_SPARK_VARIANT = {
    0: "vote",       # Thứ 2
    1: "tip",        # Thứ 3
    2: "vote",       # Thứ 4
    3: "tip",        # Thứ 5
    4: "vote",       # Thứ 6
    5: "prediction", # Thứ 7
    6: "vote",       # Chủ Nhật
}

_PROMPT_BRAIN_SPARK_VOTE = """\
Bạn là editor kênh Telegram AI có 500K follower. Viết bài BRAIN SPARK dạng VOTE cho ngày {date}.

Tin tức hôm nay làm context:
{context}

{html_rules}

Viết CHÍNH XÁC theo format sau. Chỉ thay phần trong []:

🧠 <b>BRAIN SPARK | {date}</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

[1 câu setup gây sốc hoặc nghịch lý — KHÔNG hơn 15 từ. Ví dụ: "AI vừa thay thế 10.000 lập trình viên. Bạn nghĩ ai vui nhất?"]

[Câu hỏi tranh cãi — phải chia rẽ người đọc, không có đáp án "đúng". Ví dụ: "Nếu AI làm tốt hơn bạn, bạn sẽ làm gì?"]

🔴 A — [Option cực đoan / mạo hiểm]
🟡 B — [Option thực dụng / an toàn]
🟢 C — [Option bất ngờ / ngược đời]
🔵 D — [Option "thú nhận thật" / hài hước]

[1 câu chốt gây tranh cãi — đứng về một phía rõ ràng, có thể gây phản ứng. Ví dụ: "Thật ra C mới là đáp án của người thông minh."]

👇 Vote A/B/C/D

#BrainSpark #[1-2 hashtag chủ đề]

NGUYÊN TẮC VIẾT:
- Câu setup phải có mâu thuẫn hoặc nghịch lý — KHÔNG mở đầu bằng "Hôm nay..." hay "Trong bối cảnh..."
- 4 options phải represent 4 quan điểm THỰC SỰ KHÁC NHAU — không được na ná nhau
- Câu chốt cuối phải "chọc" người đọc phản bác hoặc đồng ý mạnh
- KHÔNG giải thích thêm, chỉ trả về text bài post
"""

_PROMPT_BRAIN_SPARK_TIP = """\
Bạn là editor kênh Telegram AI có 500K follower. Viết bài BRAIN SPARK dạng MẸO THỰC TẾ cho ngày {date}.

Chủ đề AI đang hot hôm nay:
{context}

{html_rules}

Viết CHÍNH XÁC theo format sau. Chỉ thay phần trong []:

🧠 <b>BRAIN SPARK | {date}</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ <b>[Tiêu đề mẹo — bắt đầu bằng kết quả, không bắt đầu bằng "Cách". Ví dụ: "Tiết kiệm 2 giờ/ngày với prompt này:" hoặc "Làm CV chuẩn HR trong 3 phút:"]</b>

"[Prompt hoặc tip cụ thể — copy-paste được ngay, không cần chỉnh sửa]"

→ Tool: [ChatGPT / Claude / Gemini / Midjourney — chọn 1]
→ Kết quả: [Mô tả output cụ thể — "ra bản CV 1 trang, đúng format ATS"]

💬 [Câu hỏi kéo reply — phải cụ thể. Ví dụ: "Bạn đang dùng AI để làm gì mà tiết kiệm thời gian nhất?"]

#BrainSpark #AITip #[1 hashtag phù hợp]

NGUYÊN TẮC VIẾT:
- Tiêu đề phải nêu LỢI ÍCH TRƯỚC (không phải method) — "Tiết kiệm 2h" > "Cách dùng AI"
- Prompt phải thực sự hoạt động, không phải ví dụ giả
- Câu hỏi cuối phải dẫn đến reply thật, không hỏi chung chung "Bạn nghĩ sao?"
- KHÔNG giải thích thêm, chỉ trả về text bài post
"""

_PROMPT_BRAIN_SPARK_PREDICTION = """\
Bạn là editor kênh Telegram AI có 500K follower. Viết bài BRAIN SPARK dạng DỰ ĐOÁN cho ngày {date}.

Tin tức AI trong tuần:
{context}

{html_rules}

Viết CHÍNH XÁC theo format sau. Chỉ thay phần trong []:

🧠 <b>BRAIN SPARK | {date}</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎯 <b>Dự đoán táo bạo của tuần:</b>

[1 câu setup — sự kiện đang xảy ra, tối đa 15 từ]

<b>[Dự đoán chính — phải gây tranh cãi, không mơ hồ. Ví dụ: "GPT-5 sẽ bị cấm tại EU trong 6 tháng tới." hoặc "Bitcoin sẽ về 40K trước khi lên 150K."]</b>

Lý do mình tin điều này:
→ [Bằng chứng/dấu hiệu 1 — có số liệu hoặc tên cụ thể]
→ [Bằng chứng/dấu hiệu 2]
→ [Điều người khác đang bỏ qua / không ai nói đến]

[1 câu kết mạnh — đứng về 1 phía rõ ràng, KHÔNG hedge. Ví dụ: "Ai không chuẩn bị bây giờ sẽ hối hận sau 12 tháng."]

Bạn đồng ý không? 👇

#BrainSpark #[1-2 hashtag chủ đề] #Prediction

NGUYÊN TẮC VIẾT:
- Dự đoán phải CỤ THỂ (có thời gian, có đối tượng, có con số) — KHÔNG nói "AI sẽ thay đổi mọi thứ"
- Lý do thứ 3 phải là góc nhìn counter-intuitive — thứ người khác chưa nghĩ đến
- Câu kết phải tạo FOMO hoặc urgency — không kết bằng câu hỏi mở nhàm
- KHÔNG giải thích thêm, chỉ trả về text bài post
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today_vi() -> str:
    """Ngày hôm nay dạng dd/mm/yyyy theo GMT+7."""
    return datetime.now(TZ_GMT7).strftime("%d/%m/%Y")


def _extract_text(raw: str) -> str:
    """
    Lấy text thuần từ response Gemini.
    Bỏ markdown code block nếu Gemini vô tình thêm vào.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        # Bỏ dòng đầu (```...) và dòng cuối (```)
        raw = "\n".join(lines[1:-1]).strip()
    return raw


def _call_gemini(prompt: str, label: str) -> str | None:
    """
    Gọi Gemini với retry. Trả về text hoặc None nếu thất bại.
    """
    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        start = time.time()
        try:
            response = _client.models.generate_content(
                model    = GEMINI_MODEL,
                contents = prompt,
            )
            text    = _extract_text(response.text)
            elapsed = time.time() - start
            logger.info("Gemini viết xong '%s' trong %.1fs (lần %d)", label, elapsed, attempt)
            return text

        except Exception as e:
            err = str(e)
            if "429" in err or "quota" in err.lower() or "rate" in err.lower():
                logger.warning("[Lần %d] Rate limit — chờ 60s", attempt)
                time.sleep(60)
                continue
            logger.warning("[Lần %d] Lỗi Gemini cho '%s': %s", attempt, label, err[:100])

        if attempt < GEMINI_MAX_RETRIES:
            time.sleep(2 ** attempt)

    logger.error("Gemini thất bại sau %d lần cho '%s'", GEMINI_MAX_RETRIES, label)
    return None


# ---------------------------------------------------------------------------
# Viết từng loại bài
# ---------------------------------------------------------------------------

def _write_morning_brief(articles: list) -> str | None:
    """Bài 1: Morning Brief — tóm tắt tất cả tin trong 1 post."""
    date = _today_vi()

    # Tạo danh sách headlines từ tất cả articles
    headlines = "\n".join(
        f"{i+1}. [{a['source']}] {a['title']}"
        for i, a in enumerate(articles)
    )

    prompt = _PROMPT_MORNING_BRIEF.format(
        date      = date,
        headlines = headlines,
        html_rules = _HTML_RULES,
    )
    return _call_gemini(prompt, "Morning Brief")


def _write_deep_focus(article: dict) -> str | None:
    """Bài 2: Deep Focus — phân tích sâu bài quan trọng nhất (articles[0])."""
    date = _today_vi()
    prompt = _PROMPT_DEEP_FOCUS.format(
        date       = date,
        title      = article.get("title", ""),
        source     = article.get("source", ""),
        summary    = article.get("summary", ""),
        url        = article.get("url", ""),
        html_rules = _HTML_RULES,
    )
    return _call_gemini(prompt, "Deep Focus")


def _write_brain_spark(articles: list) -> str | None:
    """
    Bài 3: Brain Spark — xoay vòng 3 variant theo ngày trong tuần.
    Thứ 2,4,6,CN → Vote | Thứ 3,5 → Tip | Thứ 7 → Prediction
    """
    date    = _today_vi()
    weekday = datetime.now(TZ_GMT7).weekday()  # 0=Thứ 2, 6=CN
    variant = _BRAIN_SPARK_VARIANT.get(weekday, "vote")

    # Context: dùng tất cả headlines làm nguyên liệu
    context = "\n".join(
        f"- [{a['source']}] {a['title']}: {a.get('summary', '')[:100]}"
        for a in articles
    )

    if variant == "vote":
        prompt = _PROMPT_BRAIN_SPARK_VOTE.format(
            date=date, context=context, html_rules=_HTML_RULES
        )
        label = "Brain Spark (Vote)"
    elif variant == "tip":
        prompt = _PROMPT_BRAIN_SPARK_TIP.format(
            date=date, context=context, html_rules=_HTML_RULES
        )
        label = "Brain Spark (Tip)"
    else:
        prompt = _PROMPT_BRAIN_SPARK_PREDICTION.format(
            date=date, context=context, html_rules=_HTML_RULES
        )
        label = "Brain Spark (Prediction)"

    logger.info("Brain Spark variant hôm nay: %s", label)
    return _call_gemini(prompt, label)


# ---------------------------------------------------------------------------
# Entry point chính
# ---------------------------------------------------------------------------

def write_all(articles: list) -> list:
    """
    Tạo 3 bài từ danh sách articles:
      [0] Morning Brief  — dùng tất cả articles làm headlines
      [1] Deep Focus     — phân tích articles[0] (quan trọng nhất)
      [2] Brain Spark    — dùng tất cả articles làm context

    Trả về list 3 dict với key 'vi_text' và 'write_error'.
    """
    if not articles:
        logger.error("Không có articles để viết")
        return []

    # Xóa bài pending cũ chưa gửi (tránh tồn đọng từ hôm trước)
    clear_pending_posts()

    results = []

    # --- Bài 1: Morning Brief ---
    logger.info("Đang viết Bài 1 — Morning Brief...")
    text1 = _write_morning_brief(articles)
    post1 = {
        "post_type":   "morning_brief",
        "title":       "Morning Brief",
        "vi_text":     text1,
        "write_error": text1 is None,
        "db_id":       articles[0].get("db_id"),
    }
    if text1:
        save_pending_post("morning_brief", "Morning Brief", text1, articles[0].get("db_id"))
    results.append(post1)
    time.sleep(5)

    # --- Bài 2: Deep Focus ---
    logger.info("Đang viết Bài 2 — Deep Focus: %s", articles[0].get("title", "")[:60])
    text2 = _write_deep_focus(articles[0])
    post2 = {
        "post_type":   "deep_focus",
        "title":       articles[0].get("title", ""),
        "vi_text":     text2,
        "write_error": text2 is None,
        "db_id":       articles[1].get("db_id") if len(articles) > 1 else None,
    }
    if text2:
        save_pending_post("deep_focus", articles[0].get("title", ""), text2,
                          articles[1].get("db_id") if len(articles) > 1 else None)
    results.append(post2)
    time.sleep(5)

    # --- Bài 3: Brain Spark ---
    logger.info("Đang viết Bài 3 — Brain Spark...")
    text3 = _write_brain_spark(articles)
    post3 = {
        "post_type":   "brain_spark",
        "title":       "Brain Spark",
        "vi_text":     text3,
        "write_error": text3 is None,
        "db_id":       articles[2].get("db_id") if len(articles) > 2 else None,
    }
    if text3:
        save_pending_post("brain_spark", "Brain Spark", text3,
                          articles[2].get("db_id") if len(articles) > 2 else None)
    results.append(post3)

    errors = sum(1 for r in results if r["write_error"])
    logger.info("Hoàn thành viết 3 bài (%d lỗi) — đã lưu vào pending_posts", errors)
    return results
