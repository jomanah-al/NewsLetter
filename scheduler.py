"""
Weekly Newsletter Scheduler
Run this as a background process: python scheduler.py
It will auto-send newsletters to all active subscribers every Sunday at 9AM.
"""
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import requests
import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
BASE_URL = os.getenv("APP_URL", "http://localhost:5050")

def send_weekly_newsletters():
    print(f"\n{'='*50}")
    print(f"🗞️  Weekly send triggered: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")

    try:
        res = requests.post(
            f"{BASE_URL}/api/admin/send-all",
            headers={"X-Admin-Password": ADMIN_PASSWORD},
            timeout=300  # 5 min timeout for large lists
        )
        data = res.json()
        print(f"✅ Sent {data.get('sent', 0)} newsletters successfully")
    except Exception as e:
        print(f"❌ Error: {e}")

scheduler = BlockingScheduler()

# Every Sunday at 9:00 AM
scheduler.add_job(
    send_weekly_newsletters,
    CronTrigger(day_of_week='sun', hour=9, minute=0),
    id='weekly_newsletter',
    name='Weekly Arabic Newsletter Send'
)

print("📅 Scheduler started — newsletters send every Sunday at 9:00 AM")
print("   Press Ctrl+C to stop\n")

# Uncomment to test immediately:
# send_weekly_newsletters()

scheduler.start()
