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
# 时间窗口函数
# ========================
def get_utc_timeframe(days=1):
    now = datetime.now(timezone.utc)
    start_day = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = now.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(start_day.timestamp() * 1000), int(end_day.timestamp() * 1000)


# ========================
# API 查询函数（dry 或真实）
# ========================
def query_logs(since, until, offset=None, limit=2000, dry=True):
    payload = {
        "queryId": QUERY_ID,
        "limit": limit,
        "view": "invocations",
        "timeframe": {"from": since, "to": until}
    }
    if offset:
        payload["offset"] = offset
    if dry:
        payload["dry"] = True

    r = requests.post(API_URL, headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


# ========================
# 检查 invocation 是否截断
# ========================
def invocation_truncated(logs):
    return any(log.get("$workers", {}).get("truncated") for log in logs)


# ========================
# 主流程
# ========================
def fetch_all_logs(days=1, limit=2000, use_dry=True, sleep_sec=0.3):
    since, until = get_utc_timeframe(days)
    offset = None
    all_data = {}
    page = 1

    print("=== START QUERY ===")

    while True:
        data = query_logs(since, until, offset=offset, limit=limit, dry=use_dry)
        invocations = data.get("result", {}).get("invocations", {})

        if not invocations:
            print("No more invocations.")
            break

        print(f"Page {page}: {len(invocations)} requestIDs")

        # 检查截断
        truncated_offset = None
        for rid in invocations:
            logs = invocations[rid]
            if invocation_truncated(logs):
                print(f"⚠ Found truncated invocation: {rid}")
                # 用上一个 requestID 的最后一条日志作为 offset
                keys = list(invocations.keys())
                idx = keys.index(rid)
                if idx > 0:
                    prev_rid = keys[idx - 1]
                    prev_logs = invocations[prev_rid]
                    truncated_offset = prev_logs[-1]["$metadata"]["id"]
                break

        all_data.update(invocations)

        # 更新 offset
        if truncated_offset:
            offset = truncated_offset
        else:
            last_rid = list(invocations.keys())[-1]
            last_logs = invocations[last_rid]
            offset = last_logs[-1]["$metadata"]["id"]

        page += 1
        time.sleep(sleep_sec)

    return all_data


# ========================
# MAIN
# ========================
if __name__ == "__main__":
    logs = fetch_all_logs(days=7, limit=2000, use_dry=True)

    total_logs = sum(len(v) for v in logs.values())
    print(f"Total requestIDs: {len(logs)}")
    print(f"Total logs: {total_logs}")

    with open("logs_dry_real.json", "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

    print("Saved logs_dry_real.json")
