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
CF_ACCOUNT_ID = os.environ["CF_ACCOUNT_ID"]
QUERY_ID = "gbax5izkb3b4b1y4ne9hgrja"

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
# 启动 run
# ========================
def start_run(since, until):
    payload = {
        "queryId": QUERY_ID,
        "timeframe": {"from": since, "to": until}
    }
    r = requests.post(API_URL, headers=HEADERS, json=payload)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise Exception(f"Start run failed: {data}")
    run_id = data["result"]["run"]["id"]
    print(f"Started run_id: {run_id}")
    return run_id


# ========================
# 拉取 run 日志（分页）
# ========================
def fetch_run_logs(run_id, limit=2000, sleep_sec=0.2):
    all_logs = []
    offset = 0
    page = 1

    # tqdm 进度条（注意：只是估算，真实总条数未知）
    pbar = tqdm(desc="Fetching logs", unit="logs")

    while True:
        url = f"{API_URL}/run/{run_id}?limit={limit}&offset={offset}"
        r = requests.get(url, headers=HEADERS)
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
    DAYS = 7
    LIMIT = 2000

    since, until = get_utc_timeframe(days=DAYS)
    print(f"Querying last {DAYS} days: {since} → {until} UTC (ms)")

    # 1. Dry run 校验
    dry_run(since, until)

    # 2. 启动 run
    run_id = start_run(since, until)

    # 3. 拉取 run 日志
    logs = fetch_run_logs(run_id, limit=LIMIT)

    print(f"Total logs fetched: {len(logs)}")

    # 4. 保存 JSON 文件
    filename = f"worker_logs_run_{DAYS}d.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

    print(f"Saved logs to {filename}")
