import os
import sys
import json
import time
import copy
import requests
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

# ===================== 配置区 =====================
SEGMENTS_PER_DAY = 48              # 每天拆成几段
MAX_RETRIES = 5                   # 单页请求最大重试次数
BACKOFF = 1                        # 重试间隔秒，0 表示不限速
MAX_CONCURRENT_ACCOUNTS = 1        # 同时查询账户数
THREADS_PER_ACCOUNT = 1            # 每个账户内部线程数
# ==================================================

# 从环境变量读取 ACCOUNTS
ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON")
if not ACCOUNTS_JSON:
    print("❌ 未检测到环境变量 ACCOUNTS_JSON，请在 GitHub Secrets 设置")
    sys.exit(1)

try:
    ACCOUNTS = json.loads(ACCOUNTS_JSON)
except json.JSONDecodeError:
    print("❌ ACCOUNTS_JSON 内容不是合法 JSON")
    sys.exit(1)

URL_TEMPLATE = "https://dash.cloudflare.com/api/v4/accounts/{account_id}/workers/observability/telemetry/query"
LOCAL_COOKIE = os.getenv("CF_COOKIE") or ""
if not LOCAL_COOKIE or len(LOCAL_COOKIE) < 20:
    print("❌ 未检测到有效 CF_COOKIE，请在环境变量 CF_COOKIE 中设置")
    sys.exit(1)

HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://dash.cloudflare.com",
    "referer": "https://dash.cloudflare.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "workers-observability-origin": "workers-logs",
    "x-cross-site-security": "dash",
    "cookie": LOCAL_COOKIE,
}


def get_date_list(arg: str):
    n = int(arg) if arg and arg.isdigit() else 1
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def split_timeframes(date_str, segments=SEGMENTS_PER_DAY):
    dt = datetime.strptime(date_str, "%Y%m%d")
    start = datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    step = (end_ms - start_ms) // segments
    ranges = []
    for i in range(segments):
        seg_start = start_ms + i * step
        seg_end = seg_start + step
        if i == segments - 1:
            seg_end = end_ms
        ranges.append((seg_start, seg_end))
    return ranges


def fetch_segment(account_id, service_name, seg_id, start_ms, end_ms):
    """抓取单段日志（分页 + 自动重试，任何错误都会重试）"""
    all_logs = {}
    offset = None
    page = 0

    base_data = {
        "view": "invocations",
        "queryId": "workers-logs-invocations",
        "limit": 100,
        "parameters": {
            "datasets": ["cloudflare-workers"],
            "filters": [
                {"key": "$metadata.service", "type": "string", "value": service_name, "operation": "eq"}
            ],
            "calculations": [],
            "groupBys": [],
            "havings": []
        },
        "timeframe": {"from": start_ms, "to": end_ms}
    }

    while True:
        data = copy.deepcopy(base_data)
        if offset:
            data["offset"] = offset

        attempt = 1
        while True:
            try:
                resp = requests.post(
                    URL_TEMPLATE.format(account_id=account_id),
                    headers=HEADERS,
                    json=data,
                    timeout=15
                )
                if resp.ok:
                    break  # 正常返回才退出重试循环
                else:
                    print(f"⚠️ {account_id}/{service_name} 第{seg_id}段 第{page+1}页 HTTP {resp.status_code}")
                    print(f"⚠️ 返回内容: {resp.text[:300]}")
            except requests.RequestException as e:
                print(f"❌ {account_id}/{service_name} 第{seg_id}段 第{page+1}页 网络错误: {e}")

            # 所有错误都重试（包括 400）
            delay = min(0.5 * attempt, 10)
            print(f"⏳ 等待 {delay:.1f}s 后重试 (第 {attempt} 次)")
            time.sleep(delay)
            attempt += 1

        # 尝试解析 JSON
        try:
            result = resp.json()
        except Exception as e:
            print(f"❌ {account_id}/{service_name} 第{seg_id}段 JSON 解析失败: {e}")
            delay = min(0.5 * attempt, 10)
            print(f"⏳ 等待 {delay:.1f}s 后重试 (第 {attempt} 次)")
            time.sleep(delay)
            attempt += 1
            continue  # 重新进入请求循环

        invocations = result.get("result", {}).get("invocations", {})
        if not invocations:
            break

        all_logs.update(invocations)
        page += 1
        print(f"✅ {account_id}/{service_name} 第{seg_id}段 第{page}页 {len(invocations)}条日志")

        # 提取 offset
        offset = None
        for req_id in reversed(list(invocations.keys())):
            logs_list = invocations[req_id]
            if isinstance(logs_list, list) and logs_list:
                metadata = logs_list[-1].get("$metadata", {})
                offset = metadata.get("id")
                if offset:
                    break
        if not offset:
            break

    return all_logs




def fetch_account(account_id, service_name, dates):
    """每个账户多线程抓取"""
    for date_str in dates:
        print(f"\n===== 抓取 {account_id}/{service_name} 的 {date_str} 日日志（UTC） =====")
        ranges = split_timeframes(date_str)
        all_logs = {}

        with ThreadPoolExecutor(max_workers=THREADS_PER_ACCOUNT) as executor:
            futures = []
            for seg_id, (start_ms, end_ms) in enumerate(ranges, 1):
                futures.append(executor.submit(fetch_segment, account_id, service_name, seg_id, start_ms, end_ms))
            for f in as_completed(futures):
                all_logs.update(f.result())

        out_file = f"{account_id}_invocations_{date_str}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({"invocations": all_logs}, f, ensure_ascii=False, indent=2)
        print(f"📦 {account_id} 已保存 {len(all_logs)} 条日志 -> {out_file}")


def main():
    args = sys.argv[1:]
    selected_days = next((int(a) for a in args if a.isdigit()), 1)
    selected_accounts = [a[1:] for a in args if a.startswith("-")]
    if selected_accounts:
        accounts = {k: v for k, v in ACCOUNTS.items() if k in selected_accounts}
    else:
        accounts = ACCOUNTS

    print(f"📅 查询天数: {selected_days}")
    print(f"👥 目标账户: {', '.join(accounts.keys())}")
    dates = get_date_list(str(selected_days))

    # 控制同时查询账户数
    account_list = list(accounts.items())
    for i in range(0, len(account_list), MAX_CONCURRENT_ACCOUNTS):
        batch = account_list[i:i + MAX_CONCURRENT_ACCOUNTS]
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_ACCOUNTS) as executor:
            futures = [executor.submit(fetch_account, acc_id, svc_name, dates) for acc_id, svc_name in batch]
            for f in as_completed(futures):
                f.result()


if __name__ == "__main__":
    main()
