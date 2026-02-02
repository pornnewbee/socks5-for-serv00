import os
import requests
from datetime import datetime, timedelta, timezone
import json

CF_API_TOKEN = os.environ["CF_API_TOKEN"]
CF_ACCOUNT_ID = os.environ["API_ACCOUNT_ID"]
WORKER_NAME = "sub"  # 注意加引号

API_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/workers/observability/telemetry/query"

headers = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}

def get_utc_timeframe(days=7):
    """返回最近 days 天的 UTC 时间段（秒级 UNIX 时间戳），包含今天完整一天"""
    now_utc = datetime.now(timezone.utc)
    start = (now_utc - timedelta(days=days-1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now_utc.replace(hour=23, minute=59, second=59, microsecond=0)
    return int(start.timestamp()), int(end.timestamp())

def fetch_logs():
    since, until = get_utc_timeframe(days=7)
    print(f"Querying logs from {since} to {until} UTC for Worker '{WORKER_NAME}'")

    cursor = None
    all_logs = []

    while True:
        payload = {
        "timeframe": {
            "from": since,
            "to": until
        },
        "filter": f'Worker == "{WORKER_NAME}"',
        "limit": 100
    }

        if cursor:
            payload["cursor"] = cursor

        resp = requests.post(API_URL, headers=headers, json=payload)
        data = resp.json()

        if not data.get("success"):
            print("Error:", data)
            break

        results = data.get("result", {}).get("data", [])
        all_logs.extend(results)

        cursor = data.get("result", {}).get("meta", {}).get("nextCursor")
        if not cursor:
            break

    return all_logs

if __name__ == "__main__":
    logs = fetch_logs()
    print(f"Fetched {len(logs)} logs for Worker '{WORKER_NAME}'")
    # 保存到文件
    with open(f"{WORKER_NAME}_logs.json", "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)
