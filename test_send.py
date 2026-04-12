"""Script test: fetch → process → gửi tất cả bài ngay lên Telegram (không AI)."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, 'ainewsbot')

from database import init_db
from fetcher import fetch_all
from processor import process
from sender import send_all_immediately

def main():
    init_db()
    print("Fetching news...")
    raw = fetch_all()
    print(f"Fetched {len(raw)} articles")

    selected, stats = process(raw)
    print(f"\nSau dedup/grouping: {len(selected)} bài")
    for a in selected:
        print(f"  [{a['source']}] {a['title'][:70]}")

    print(f"\nGửi {len(selected)} bài lên Telegram...")
    send_all_immediately(selected, stats)
    print("DONE!")

main()
