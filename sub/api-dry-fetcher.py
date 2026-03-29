import os
import requests
from datetime import datetime, timedelta, timezone
import json
import time

CF_API_TOKEN = os.environ["CF_API_TOKEN"]
CF_ACCOUNT_ID = os.environ["API_ACCOUNT_ID"]
QUERY_ID = "gbax5izkb3b4b1y4ne9hgrja"

API_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/workers/observability/telemetry/query"

HEADERS = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}

# ========================
# API 查询函数（带 429 自适应重试）
# ========================
def query_logs(since, until, offset=None, limit=2000, max_retries=99):
    sleep_time = 5
    retries = 0
    while True:
        payload = {
            "queryId": QUERY_ID,
            "limit": limit,
            "dry": True,
            "view": "invocations",
            "timeframe": {"from": since, "to": until}
        }
        if offset:
            payload["offset"] = offset
            payload["offsetDirection"] = "next"
        try:
            r = requests.post(API_URL, headers=HEADERS, json=payload)
            if r.status_code == 429:
                print(f"  ⚠️ 429 Rate Limit, sleeping {sleep_time}s...")
                time.sleep(sleep_time)
                sleep_time = min(sleep_time + 1, 50)
                continue
            elif r.status_code >= 500:
                if retries < max_retries:
                    retries += 1
                    wait = min(2, 10)
                    print(f"  ⚠️ {r.status_code} Server Error, retry {retries}/{max_retries} after {wait}s...")
                    time.sleep(wait)
                    continue
                else:
                    r.raise_for_status()
            r.raise_for_status()
            return r.json()
        except requests.RequestException as ex:
            if retries < max_retries:
                retries += 1
                wait = min(sleep_time + retries, 10)
                print(f"  ⚠️ RequestException, retry {retries}/{max_retries} after {wait}s...")
                time.sleep(wait)
                continue
            else:
                raise ex

# ========================
# 检查 invocation 是否截断
# ========================
def invocation_truncated(logs):
    return any(log.get("$workers", {}).get("truncated") for log in logs)

# ========================
# 拉取指定时间范围的所有日志（分页）
# ========================
def fetch_logs_range(since, until, limit=2000, sleep_sec=0.1):
    offset = None
    all_data = {}
    while True:
        data = query_logs(since, until, offset=offset, limit=limit)
        invocations = data.get("result", {}).get("invocations", {})
        if not invocations:
            break

        # 检查截断并更新 offset
        truncated_offset = None
        keys = list(invocations.keys())
        for idx, rid in enumerate(keys):
            logs = invocations[rid]
            if invocation_truncated(logs):
                if idx > 0:
                    prev_rid = keys[idx - 1]
                    prev_logs = invocations[prev_rid]
                    truncated_offset = prev_logs[-1]["$metadata"]["id"]
                break

        all_data.update(invocations)

        if truncated_offset:
            offset = truncated_offset
        else:
            last_rid = keys[-1]
            last_logs = invocations[last_rid]
            offset = last_logs[-1]["$metadata"]["id"]

        time.sleep(sleep_sec)

    return all_data

# ========================
# MAIN
# ========================
if __name__ == "__main__":
    # 定义时间范围（示例：过去7天）
    start_time = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    end_time = datetime.now(timezone.utc).isoformat()

    print(f"Fetching logs from {start_time} → {end_time} ...")
    logs = fetch_logs_range(start_time, end_time, limit=2000)
    print(f"Fetched {len(logs)} requestIDs")

    output_file = f"/mnt/logs_{datetime.now(timezone.utc).date()}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)
    print(f"Saved {output_file}")
