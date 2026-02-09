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
        "timeframe": {"from": since, "to": until},
        "view": "invocations"   # Worker 调用日志
    }
    r = requests.post(API_URL, headers=HEADERS, json=payload)
    r.raise_for_status()
    data = r.json()
    
    # =======================
    # 1️⃣ 输出完整 Dry Run 返回
    # =======================
    print("=== Dry Run Response ===")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print("========================")

    if not data.get("success"):
        raise Exception(f"Dry run failed: {data}")

    # =======================
    # 2️⃣ 输出 Dry Run 总体情况
    # =======================
    invocations = data.get("result", {}).get("invocations", {})
    total_logs = 0
    min_ts, max_ts = None, None
    truncated_count = 0

    for req_id, logs_list in invocations.items():
        total_logs += len(logs_list)
        for log in logs_list:
            ts = log.get("timestamp")
            if ts is not None:
                if min_ts is None or ts < min_ts:
                    min_ts = ts
                if max_ts is None or ts > max_ts:
                    max_ts = ts
            if log.get("$workers", {}).get("truncated"):
                truncated_count += 1

    print("=== Dry Run Response Summary ===")
    print(f"Total invocations in dry run: {total_logs}")
    if min_ts and max_ts:
        print(f"Timestamp range: {min_ts} → {max_ts}")
    if truncated_count:
        print(f"Truncated logs count: {truncated_count}")
    print("===============================")
    
    print("Dry run successful ✅")
    return data

# ========================
# 拉取日志（offset + limit 分页）
# ========================
def fetch_logs(days=7, limit=100, sleep_sec=0.2):
    """
    拉取最近 N 天 Worker 调用日志（invocations view）
    分页处理，返回完整日志列表
    """
    since, until = get_utc_timeframe(days)
    print(f"Querying last {days} days: {since} → {until} UTC (ms)")

    # 1. Dry run 校验
    dry_run(since, until)

    offset = None  # 第一次请求不用 offset
    all_logs = []
    page = 1

    pbar = tqdm(desc="Fetching logs", unit="logs")

    while True:
        payload = {
            "queryId": QUERY_ID,
            "timeframe": {"from": since, "to": until},
            "limit": limit,
            "view": "invocations"
        }
        if offset:
            payload["offset"] = offset  # 使用上一页最后一条日志 id 翻页

        r = requests.post(API_URL, headers=HEADERS, json=payload)
        if r.status_code != 200:
            print(f"HTTP ERROR {r.status_code}: {r.text}")
            break

        data = r.json()
        if not data.get("success"):
            print(f"API ERROR: {data}")
            break

        # 从 result.invocations 获取日志
        invocations = data.get("result", {}).get("invocations", {})
        rows = []
        for req_id, logs_list in invocations.items():
            rows.extend(logs_list)

        count = len(rows)
        if count == 0:
            print("No more logs in this page.")
            break

        all_logs.extend(rows)
        pbar.update(count)
        print(f"Page {page} | got {count} logs | total {len(all_logs)}")

        # 翻页用最后一条日志的 $metadata.id
        last_log = rows[-1]
        offset = last_log.get("$metadata", {}).get("id")
        if not offset:
            print("No offset for next page, reached last page.")
            break

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
