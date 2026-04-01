"""Script test: viết 3 bài, lưu pending, gửi từng bài lên Telegram (delay 5s)."""
import sys, io, asyncio, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, 'ainewsbot')

from database import init_db
from fetcher import fetch_all
from processor import process
from writer import write_all
from sender import _send_daily_articles_async, _send_scheduled_post_async

async def main():
    init_db()
    print("Fetching news...")
    articles = fetch_all()
    selected, stats = process(articles)
    print(f"\nSelected {len(selected)} articles:")
    for a in selected:
        print(f"  [{a['source']}] {a['title'][:70]}")

    print("\nWriting 3 posts (Morning Brief / Deep Focus / Brain Spark)...")
    posts = write_all(selected)

    print("\n--- PREVIEW ---")
    for p in posts:
        print(f"\n[{p['post_type'].upper()}]")
        print(p['vi_text'][:300] if p['vi_text'] else "ERROR")
        print("...")

    print("\nSending admin report...")
    await _send_daily_articles_async(posts, stats)

    print("\nSending Morning Brief to @VNAInews...")
    await _send_scheduled_post_async("morning_brief")

    print("Waiting 5s...")
    await asyncio.sleep(5)

    print("Sending Deep Focus...")
    await _send_scheduled_post_async("deep_focus")

    print("Waiting 5s...")
    await asyncio.sleep(5)

    print("Sending Brain Spark...")
    await _send_scheduled_post_async("brain_spark")

    print("\nDONE!")

asyncio.run(main())
