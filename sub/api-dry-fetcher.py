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
# 时间窗口拆分
# ========================
def split_day_to_hours(date):
    """
    date: datetime 对象（UTC），只用日期部分
    返回 24 个 (start_ms, end_ms) tuple
    """
    slices = []
    for h in range(24):
        start = date.replace(hour=h, minute=0, second=0, microsecond=0)
        end = start.replace(minute=59, second=59, microsecond=999000)
        slices.append((int(start.timestamp()*1000), int(end.timestamp()*1000)))
    return slices

def get_days(days=7):
    now = datetime.now(timezone.utc)
    day_list = []
    for i in range(days):
        day = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_list.append(day)
    return list(reversed(day_list))  # 早到晚顺序

# ========================
# API 查询函数（dry 查询写死 + offsetDirection）
# ========================
def query_logs(since, until, offset=None, limit=2000):
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

    r = requests.post(API_URL, headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()

# ========================
# 检查 invocation 是否截断
# ========================
def invocation_truncated(logs):
    return any(log.get("$workers", {}).get("truncated") for log in logs)

# ========================
# 拉取一个时间片的所有 logs
# ========================
def fetch_slice(since, until, limit=2000, sleep_sec=0.2):
    offset = None
    all_data = {}
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

        all_data.update(invocations)

        # 更新 offset
        if truncated_offset:
            offset = truncated_offset
        else:
            last_rid = keys[-1]
            last_logs = invocations[last_rid]
            offset = last_logs[-1]["$metadata"]["id"]

        time.sleep(sleep_sec)

    return all_data

# ========================
# 主流程：多天 + 每天24小时 + 并发
# ========================
def fetch_all_logs(days=7, limit=2000, max_workers=8):
    all_data = {}

    day_list = get_days(days)

    for day in day_list:
        print(f"=== Fetching day {day.date()} ===")
        slices = split_day_to_hours(day)

        # 并发拉取 24 个小时 slice
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_slice = {executor.submit(fetch_slice, s, e, limit): (s,e) for s,e in slices}
            for future in as_completed(future_to_slice):
                s,e = future_to_slice[future]
                try:
                    slice_data = future.result()
                    all_data.update(slice_data)
                    print(f"  ✅ {datetime.utcfromtimestamp(s/1000)} → {datetime.utcfromtimestamp(e/1000)} fetched {len(slice_data)} requestIDs")
                except Exception as ex:
                    print(f"  ❌ {datetime.utcfromtimestamp(s/1000)} → {datetime.utcfromtimestamp(e/1000)} failed: {ex}")

    return all_data

# ========================
# MAIN
# ========================
if __name__ == "__main__":
    logs = fetch_all_logs(days=7, limit=2000, max_workers=8)

    total_logs = sum(len(v) for v in logs.values())
    print(f"Total requestIDs: {len(logs)}")
    print(f"Total logs: {total_logs}")

    with open("logs_dry_7days.json", "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

    print("Saved logs_dry_7days.json")
