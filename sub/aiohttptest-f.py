#!/usr/bin/env python3
# coding: utf-8
"""
异步日志抓取脚本（主线程动态调度 + 从线程暂停恢复机制）
支持：
  - 正数参数 N：最近 N 天（包含今天）
  - 负数参数 -N：N 天前当天
环境变量：
  - ACCOUNTS_JSON: JSON 字符串，形如 {"acctid1": "service1", ...}
  - CF_COOKIE: Cloudflare cookie 字符串
用法：
  python fetcher.py 7          # 查询最近7天（包含今天）
  python fetcher.py -2         # 查询2天前当天
  python fetcher.py 20251101   # 指定某天 YYYYMMDD
  python fetcher.py -<account_id> 7  # 也支持 -<account_id> 选择特定账户
"""

import os, sys, json, copy, asyncio, aiohttp, contextlib
from datetime import datetime, timedelta, timezone

# ===================== 配置区 =====================
SEGMENTS_PER_DAY = 24
MAX_CONCURRENT_ACCOUNTS = 1
FOLLOWER_START_INTERVAL = 3
FOLLOWER_RECOVERY_INTERVAL = 3
# ===================================================

ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON")
if not ACCOUNTS_JSON:
    print("❌ 未检测到环境变量 ACCOUNTS_JSON")
    sys.exit(1)
try:
    ACCOUNTS = json.loads(ACCOUNTS_JSON)
    if not isinstance(ACCOUNTS, dict):
        raise ValueError("ACCOUNTS_JSON must be a JSON object")
except Exception as e:
    print("❌ ACCOUNTS_JSON 内容无效：", e)
    sys.exit(1)

CF_COOKIE = os.getenv("CF_COOKIE") or ""
if not CF_COOKIE or len(CF_COOKIE) < 20:
    print("❌ 未检测到有效 CF_COOKIE")
    sys.exit(1)

URL_TEMPLATE = "https://dash.cloudflare.com/api/v4/accounts/{account_id}/workers/observability/telemetry/query"
HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://dash.cloudflare.com",
    "referer": "https://dash.cloudflare.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "workers-observability-origin": "workers-logs",
    "x-cross-site-security": "dash",
    "cookie": CF_COOKIE,
}

# ===================== 工具函数 =====================
def get_date_list(arg: str):
    """解析参数，支持正数查询最近N天，负数查询N天前当天"""
    today = datetime.now(timezone.utc).date()
    if arg and arg.isdigit() and len(arg) == 8:
        return [arg]  # 指定日期 YYYYMMDD
    try:
        n = int(arg)
    except:
        n = 7

    if n >= 0:
        # 最近 N 天，包括今天
        return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]
    else:
        # N 天前当天
        target = today + timedelta(days=n)
        return [target.strftime("%Y%m%d")]

def split_timeframes(date_str, segments=SEGMENTS_PER_DAY):
    dt = datetime.strptime(date_str, "%Y%m%d")
    start = datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    step = (end_ms - start_ms) // segments
    arr = []
    for i in range(segments):
        s = start_ms + i * step
        e = s + step if i < segments - 1 else end_ms
        arr.append((s, e))
    return arr

def linear_delay(attempt: int):
    return min(0.5 * attempt, 10.0)

# ===================== 异步抓取函数 =====================
async def fetch_segment(session, account_id, service_name, segment, main_ok_event: asyncio.Event = None, is_main=False):
    all_logs = segment.get("partial_logs", {})
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
            "calculations": [], "groupBys": [], "havings": []
        },
        "timeframe": {"from": segment["start_ms"], "to": segment["end_ms"]}
    }

    offset = segment.get("offset")
    attempt = 1
    while True:
        data = copy.deepcopy(base_data)
        if offset:
            data["offset"] = offset
        try:
            async with session.post(URL_TEMPLATE.format(account_id=account_id), headers=HEADERS, json=data, timeout=30) as resp:
                status = resp.status
                text = await resp.text()
                if status == 200:
                    if is_main and main_ok_event is not None and not main_ok_event.is_set():
                        main_ok_event.set()
                        print(f"🔔 {account_id}/{service_name} 主线程已恢复（HTTP 200）")
                    try:
                        result = await resp.json()
                    except Exception as e:
                        print(f"❌ {account_id}/{service_name} 第{segment['seg_id']}段 JSON 解码异常: {e}")
                        result = None
                    if not result or "result" not in result or "invocations" not in result["result"]:
                        print(f"❌ {account_id}/{service_name} 第{segment['seg_id']}段 收到空或异常响应结构")
                        break
                    invocations = result["result"].get("invocations", {})
                    new_entries = 0
                    for req_id, entries in invocations.items():
                        if req_id not in all_logs:
                            all_logs[req_id] = entries
                            new_entries += len(entries)
                    if new_entries == 0 and not offset:
                        break
                    page += 1
                    print(f"✅ {account_id}/{service_name} 第{segment['seg_id']}段 第{page}页 获取 {new_entries} 条日志")
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
                    attempt = 1
                elif status == 429:
                    if is_main and main_ok_event is not None:
                        if main_ok_event.is_set():
                            main_ok_event.clear()
                            print(f"⛔ {account_id}/{service_name} 主线程检测到 429，切换退避模式")
                    else:
                        segment["status"] = "paused"
                        segment["offset"] = offset
                        segment["partial_logs"] = all_logs
                        print(f"♻️ {account_id}/{service_name} 第{segment['seg_id']}段 从线程遇到 429，暂停")
                        return
                else:
                    print(f"⚠️ {account_id}/{service_name} 第{segment['seg_id']}段 HTTP {status} {text[:200]}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"❌ {account_id}/{service_name} 第{segment['seg_id']}段 网络/请求异常: {e}")

        if status != 200:
            delay = linear_delay(attempt)
            print(f"⏳ {account_id}/{service_name} 第{segment['seg_id']}段 第{attempt}次重试，等待 {delay:.1f}s")
            await asyncio.sleep(delay)
            attempt += 1

    segment["status"] = "done"
    segment["partial_logs"] = all_logs

# ===================== 账户抓取流程 =====================
async def fetch_account(account_id, service_name, dates):
    async with aiohttp.ClientSession() as session:
        for date_str in dates:
            print(f"\n===== 抓取 {account_id}/{service_name} 的 {date_str} 日日志（UTC） =====")
            ranges = split_timeframes(date_str)
            segments = [
                {"seg_id": i+1, "start_ms": s, "end_ms": e, "status": "pending", "offset": None, "partial_logs": {}}
                for i, (s, e) in enumerate(ranges)
            ]
            main_ok_event = asyncio.Event()
            main_ok_event.set()
            tasks = []

            async def main_loop():
                while True:
                    pending = [seg for seg in segments if seg["status"] == "pending"]
                    paused = [seg for seg in segments if seg["status"] == "paused"]
                    if pending:
                        seg = pending[0]
                    elif paused:
                        seg = paused[0]
                        seg["status"] = "running"
                    else:
                        break
                    seg["status"] = "running"
                    await fetch_segment(session, account_id, service_name, seg, main_ok_event, is_main=True)
            
            async def follower_loop():
                follower_segments = [seg for seg in segments if seg["status"] == "pending"]
                tasks = []
                for seg in follower_segments:
                    await asyncio.sleep(FOLLOWER_START_INTERVAL)
                    t = asyncio.create_task(fetch_segment(session, account_id, service_name, seg, main_ok_event, is_main=False))
                    tasks.append(t)
                if tasks:
                    await asyncio.gather(*tasks)

            await asyncio.gather(main_loop(), follower_loop())

            all_logs = {}
            for seg in segments:
                all_logs.update(seg["partial_logs"])
            out_file = f"{account_id}_invocations_{date_str}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump({"invocations": all_logs}, f, ensure_ascii=False, indent=2)
            print(f"📦 {account_id} 已保存 {len(all_logs)} 条日志 -> {out_file}")

# ===================== 主程序 =====================
async def main_async():
    args = sys.argv[1:]
    selected_days = None
    selected_accounts = []
    for a in args:
        if a.startswith("-") and not a[1:].isdigit():
            selected_accounts.append(a[1:])
        elif a.lstrip("-").isdigit():
            selected_days = a

    if selected_days is None:
        selected_days = "7"

    if selected_accounts:
        accounts = {k: v for k, v in ACCOUNTS.items() if k in selected_accounts}
        if not accounts:
            print("❌ 没有匹配的账户ID")
            return
    else:
        accounts = ACCOUNTS

    dates = get_date_list(selected_days) if len(selected_days) != 8 else [selected_days]
    print(f"📅 查询天数: {dates}")
    print(f"👥 目标账户: {', '.join(accounts.keys())}")

    account_list = list(accounts.items())
    for i in range(0, len(account_list), MAX_CONCURRENT_ACCOUNTS):
        batch = account_list[i:i + MAX_CONCURRENT_ACCOUNTS]
        tasks = [fetch_account(acc_id, svc_name, dates) for acc_id, svc_name in batch]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main_async())
