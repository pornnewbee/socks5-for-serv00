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
# 时间窗口
# ========================
def get_utc_timeframe(days=1):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


# ========================
# DRY 查询
# ========================
def dry_query(since, until, limit=2000):
    payload = {
        "queryId": QUERY_ID,
        "dry": True,
        "limit": 2000,
        "view": "invocations",
        "timeframe": {"from": since, "to": until}
    }

    r = requests.post(API_URL, headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


# ========================
# 正式分页查询
# ========================
def real_query(since, until, offset=None, limit=2000):
    payload = {
        "queryId": QUERY_ID,
        "limit": limit,
        "view": "invocations",
        "timeframe": {"from": since, "to": until}
    }

    if offset:
        payload["offset"] = offset

    r = requests.post(API_URL, headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()


# ========================
# 检查 invocation 是否 truncated
# ========================
def invocation_truncated(logs):
    for log in logs:
        if log.get("$workers", {}).get("truncated"):
            return True
    return False


# ========================
# 主流程
# ========================
def dry_with_offset_recovery(days=1):

    since, until = get_utc_timeframe(days)

    print("=== DRY QUERY ===")
    dry_data = dry_query(since, until)

    invocations = dry_data["result"]["invocations"]

    print("Dry invocation count:", len(invocations))

    request_ids = list(invocations.keys())

    last_good_offset = None
    truncated_found = False

    for i, rid in enumerate(request_ids):

        logs = invocations[rid]

        if invocation_truncated(logs):
            print(f"⚠ Found truncated invocation: {rid}")
            truncated_found = True

            if i > 0:
                prev_rid = request_ids[i - 1]
                prev_logs = invocations[prev_rid]
                last_good_offset = prev_logs[-1]["$metadata"]["id"]
                print("Use previous invocation offset:", last_good_offset)

            break

    # ========================
    # 如果没有截断
    # ========================
    if not truncated_found:
        print("✅ Dry data looks complete")
        return invocations

    # ========================
    # 从 offset 继续真实拉取
    # ========================
    print("\n=== REAL QUERY FROM OFFSET ===")

    all_data = {}
    all_data.update(invocations)

    offset = last_good_offset

    page = 1

    while True:

        data = real_query(since, until, offset)

        page_inv = data["result"]["invocations"]

        if not page_inv:
            print("No more data.")
            break

        print(f"Real Page {page}: {len(page_inv)} invocations")

        all_data.update(page_inv)

        # 更新 offset
        last_rid = list(page_inv.keys())[-1]
        last_logs = page_inv[last_rid]
        offset = last_logs[-1]["$metadata"]["id"]

        page += 1
        time.sleep(0.3)

    return all_data


# ========================
# MAIN
# ========================
if __name__ == "__main__":

    data = dry_with_offset_recovery(days=1)

    total = sum(len(v) for v in data.values())

    print("Total requestIDs:", len(data))
    print("Total logs:", total)

    with open("dry_recovery_logs.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("Saved dry_recovery_logs.json")
