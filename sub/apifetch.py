import os
import requests
from datetime import datetime, timedelta, timezone
import json
import time

CF_API_TOKEN = os.environ["CF_API_TOKEN"]
CF_ACCOUNT_ID = os.environ["API_ACCOUNT_ID"]

QUERY_ID = "gbax5izkb3b4b1y4ne9hgrja"

API_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/workers/observability/telemetry/query"

headers = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}


# ================================
# UTC 时间范围（最近 N 天完整 UTC 天）
# ================================
def get_utc_timeframe(days=7):
    now = datetime.now(timezone.utc)

    start = (now - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    end = now.replace(
        hour=23, minute=59, second=59, microsecond=999000
    )

    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


# ================================
# Dry Run 校验
# ================================
def dry_run_check(since, until):
    payload = {
        "queryId": QUERY_ID,
        "dry": True,
        "timeframe": {
            "from": since,
            "to": until
        }
    }

    r = requests.post(API_URL, headers=headers, json=payload)
    print("Dry run result:", r.text)


# ================================
# 主查询（自动分页）
# ================================
def fetch_all_logs(days=7, limit=2000, sleep_sec=0.2):

    since, until = get_utc_timeframe(days)

    print("========== Query Config ==========")
    print("Query ID:", QUERY_ID)
    print("Timeframe:", since, "→", until)
    print("Limit per page:", limit)
    print("==================================")

    dry_run_check(since, until)

    offset = 0
    total_logs = []
    page = 1

    while True:

        payload = {
            "queryId": QUERY_ID,
            "timeframe": {
                "from": since,
                "to": until
            },
            "limit": limit,
            "offset": offset
        }

        r = requests.post(API_URL, headers=headers, json=payload)

        if r.status_code != 200:
            print("HTTP ERROR:", r.status_code, r.text)
            break

        data = r.json()

        if not data.get("success"):
            print("API ERROR:", data)
            break

        rows = data.get("result", {}).get("data", [])

        count = len(rows)

        print(f"Page {page} | offset={offset} | got={count}")

        if count == 0:
            break

        total_logs.extend(rows)

        # 停止条件
        if count < limit:
            print("Reached last page.")
            break

        offset += limit
        page += 1

        time.sleep(sleep_sec)

    return total_logs


# ================================
# MAIN
# ================================
if __name__ == "__main__":

    logs = fetch_all_logs(days=7, limit=2000)

    print("==================================")
    print("Total logs fetched:", len(logs))

    with open("worker_logs.json", "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

    print("Saved to worker_logs.json")
