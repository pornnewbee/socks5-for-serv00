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
def fetch_logs_grouped(days=7, limit=100, sleep_sec=0.2):
    """
    保留 requestId 分组的 invocations 日志
    """
    since, until = get_utc_timeframe(days)
    print(f"Querying last {days} days: {since} → {until} UTC (ms)")

    # Dry run 校验
    dry_run(since, until)

    offset = None
    all_invocations = {}
    page = 1

    pbar = tqdm(desc="Fetching invocations", unit="invocations")

    while True:
        payload = {
            "queryId": QUERY_ID,
            "timeframe": {"from": since, "to": until},
            "limit": limit,
            "view": "invocations"
        }
        if offset:
            payload["offset"] = offset

        r = requests.post(API_URL, headers=HEADERS, json=payload)
        if r.status_code != 200:
            print(f"HTTP ERROR {r.status_code}: {r.text}")
            break

        data = r.json()
        if not data.get("success"):
            print(f"API ERROR: {data}")
            break

        invocations = data.get("result", {}).get("invocations", {})
        count_invocations = len(invocations)
        if count_invocations == 0:
            print("No more invocations in this page.")
            break

        # 按 requestId 合并字典
        all_invocations.update(invocations)

        # 更新 offset 用最后一条日志的 $metadata.id
        # 找最后一个 requestId 的最后一条日志
        last_request_id = list(invocations.keys())[-1]
        last_logs = invocations[last_request_id]
        offset = last_logs[-1].get("$metadata", {}).get("id")

        pbar.update(count_invocations)
        print(f"Page {page} | got {count_invocations} invocations | total {len(all_invocations)} request IDs")

        page += 1
        time.sleep(sleep_sec)

    pbar.close()
    return all_invocations



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
