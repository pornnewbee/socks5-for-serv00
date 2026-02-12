import os
import requests
from datetime import datetime, timedelta, timezone
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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
def get_days(days=7):
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return [now - timedelta(days=i) for i in reversed(range(days))]

# ========================
# 拆分天为每 N 分钟 slice
# ========================
def split_day_to_minutes(day, interval=10):
    slices = []
    start_ms = int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    end_ms = int(day.replace(hour=23, minute=59, second=59, microsecond=999000).timestamp() * 1000)
    step = interval * 60 * 1000
    current = start_ms
    while current <= end_ms:
        slice_end = min(current + step - 1, end_ms)
        slices.append((current, slice_end))
        current += step
    return slices

# ========================
# API 查询函数（dry 查询写死）
# ========================
def query_logs(since, until, offset=None, limit=2000):
    payload = {
        "queryId": QUERY_ID,
        "limit": limit,
        "dry": True,  # dry 查询写死
        "view": "invocations",
        "timeframe": {"from": since, "to": until}
    }
    if offset:
        payload["offset"] = offset
        payload["offsetDirection"] = "next"

    r = requests.post(API_URL, headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()

# ========================
# 检查 invocation 是否截断
# ========================
def invocation_truncated(logs):
    return any(log.get("$workers", {}).get("truncated") for log in logs)

# ========================
# 拉取单个 slice
# ========================
def fetch_slice(since, until, limit=2000, sleep_sec=0.1):
    offset = None
    slice_data = {}
    while True:
        data = query_logs(since, until, offset=offset, limit=limit)
        invocations = data.get("result", {}).get("invocations", {})
        if not invocations:
            break

        # 检查截断
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

        # 合并数据，自动去重
        slice_data.update(invocations)

        # 更新 offset
        if truncated_offset:
            offset = truncated_offset
        else:
            last_rid = keys[-1]
            last_logs = invocations[last_rid]
            offset = last_logs[-1]["$metadata"]["id"]

        time.sleep(sleep_sec)

    return slice_data

# ========================
# 主流程
# ========================
def fetch_all_logs(days=7, limit=2000, max_workers=20, interval_min=10):
    day_list = get_days(days)

    for day in day_list:
        print(f"=== Fetching day {day.date()} ===")
        slices = split_day_to_minutes(day, interval=interval_min)

        day_data = {}  # 当天的日志

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_slice = {executor.submit(fetch_slice, s, e, limit): (s,e) for s,e in slices}
            for future in as_completed(future_to_slice):
                s,e = future_to_slice[future]
                try:
                    slice_data = future.result()
                    day_data.update(slice_data)
                    print(f"  ✅ {datetime.utcfromtimestamp(s/1000)} → {datetime.utcfromtimestamp(e/1000)} fetched {len(slice_data)} requestIDs")
                except Exception as ex:
                    print(f"  ❌ {datetime.utcfromtimestamp(s/1000)} → {datetime.utcfromtimestamp(e/1000)} failed: {ex}")

        # 写当天日志到单独文件
        output_file = f"/mnt/logs_{day.date()}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(day_data, f, ensure_ascii=False, indent=2)
        print(f"Saved {output_file} with {len(day_data)} requestIDs")

    return

# ========================
# MAIN
# ========================
if __name__ == "__main__":
    # 按天拉取日志并写文件
    fetch_all_logs(days=7, limit=2000, max_workers=8, interval_min=5)

