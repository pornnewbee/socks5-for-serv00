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
# 时间窗口函数（按天）
# ========================
def get_days(days=7):
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return [now - timedelta(days=i) for i in reversed(range(days))]

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
# 拉取单天日志（单线程、按 offset 分页）
# ========================
def fetch_day(day, limit=2000, sleep_sec=0.1):
    since = int(day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    until = int(day.replace(hour=23, minute=59, second=59, microsecond=999000).timestamp() * 1000)

    offset = None
    day_data = {}
    attempt = 0

    while True:
        attempt += 1
        data = query_logs(since, until, offset=offset, limit=limit)
        invocations = data.get("result", {}).get("invocations", {})
        if not invocations:
            print(f"  ℹ️ Day {day.date()} slice empty, finishing")
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

        # 合并数据
        day_data.update(invocations)

        # 打印 offset 调试信息
        if truncated_offset:
            print(f"  🔹 Day {day.date()}, attempt {attempt}, truncated_offset={truncated_offset}")
            offset = truncated_offset
        else:
            last_rid = keys[-1]
            last_logs = invocations[last_rid]
            offset = last_logs[-1]["$metadata"]["id"]
            print(f"  🔸 Day {day.date()}, attempt {attempt}, next_offset={offset}")

        time.sleep(sleep_sec)

    return day_data

# ========================
# 主流程（单线程）
# ========================
def fetch_all_logs(days=7, limit=2000):
    day_list = get_days(days)

    for day in day_list:
        print(f"=== Fetching day {day.date()} ===")
        day_data = fetch_day(day, limit=limit)

        # 写文件
        output_file = f"/mnt/logs_{day.date()}.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(day_data, f, ensure_ascii=False, indent=2)
        print(f"Saved {output_file} with {len(day_data)} requestIDs")

if __name__ == "__main__":
    fetch_all_logs(days=7, limit=2000)
