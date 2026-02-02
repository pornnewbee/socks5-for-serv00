import os
import requests
from datetime import datetime, timedelta, timezone
import json
import time
from tqdm import tqdm

# ========================
# 环境变量
# ========================
CF_API_TOKEN = os.environ["CF_API_TOKEN"]
CF_ACCOUNT_ID = os.environ["API_ACCOUNT_ID"]
QUERY_ID = "gbax5izkb3b4b1y4ne9hgrja"  # 你的 Saved Query ID

API_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/workers/observability/telemetry/query"
HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}


# ========================
# 获取最近 N 天完整 UTC 时间戳（毫秒）
# ========================
def get_utc_timeframe(days=7):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


# ========================
# Dry Run 校验
# ========================
def dry_run(since, until):
    payload = {
        "queryId": QUERY_ID,
        "dry": True,
        "timeframe": {"from": since, "to": until}
    }
    r = requests.post(API_URL, headers=HEADERS, json=payload)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise Exception(f"Dry run failed: {data}")
    print("Dry run successful ✅")
    return data


# ========================
# 拉取日志（offset + limit 分页）
# ========================
def fetch_logs(days=7, limit=2000, sleep_sec=0.2):
    since, until = get_utc_timeframe(days)
    print(f"Querying last {days} days: {since} → {until} UTC (ms)")

    # 1. Dry run
    dry_run(since, until)

    offset = 0
    all_logs = []
    page = 1

    pbar = tqdm(desc="Fetching logs", unit="logs")

    while True:
        payload = {
            "queryId": QUERY_ID,
            "timeframe": {"from": since, "to": until},
            "limit": limit,
            "offset": offset
        }

        r = requests.post(API_URL, headers=HEADERS, json=payload)
        if r.status_code != 200:
            print(f"HTTP ERROR {r.status_code}: {r.text}")
            break

        data = r.json()
        if not data.get("success"):
            print(f"API ERROR: {data}")
            break

        rows = data.get("result", {}).get("data", [])
        count = len(rows)
        if count == 0:
            break

        all_logs.extend(rows)
        pbar.update(count)
        print(f"Page {page} | offset={offset} | got={count}")

        if count < limit:
            print("Reached last page.")
            break

        offset += limit
        page += 1
        time.sleep(sleep_sec)

    pbar.close()
    return all_logs


# ========================
# MAIN
# ========================
if __name__ == "__main__":
    logs = fetch_logs(days=7, limit=2000)
    print(f"Total logs fetched: {len(logs)}")

    filename = "worker_logs_savedquery.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

    print(f"Saved logs to {filename}")
