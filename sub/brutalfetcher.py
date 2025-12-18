#!/usr/bin/env python3
# coding: utf-8 

import os, sys, json, asyncio, aiohttp, time
from datetime import datetime, timedelta, timezone

SEGMENTS_PER_DAY = 8
# 同时查询的账户数量
# 0 表示所有账户同时查询
MAX_CONCURRENT_ACCOUNTS = 0

ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON")
if not ACCOUNTS_JSON:
    print("❌ 未检测到环境变量 ACCOUNTS_JSON")
    sys.exit(1)

try:
    ACCOUNTS = json.loads(ACCOUNTS_JSON)
    if not isinstance(ACCOUNTS, dict):
        raise ValueError("ACCOUNTS_JSON must be dict")
except Exception as err:
    print("❌ ACCOUNTS_JSON 内容无效：", err)
    sys.exit(1)

CF_COOKIE = os.getenv("CF_COOKIE") or ""
if not CF_COOKIE:
    print("❌ 未检测到 CF_COOKIE")
    sys.exit(1)

URL_TEMPLATE = (
    "https://dash.cloudflare.com/api/v4/accounts/"
    "{account_id}/workers/observability/telemetry/query"
)

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

# ==========================================================
# 工具函数
# ==========================================================
def get_date_list(arg: str):
    today = datetime.now(timezone.utc).date()

    if arg.isdigit() and len(arg) == 8:
        return [arg]

    try:
        n = int(arg)
    except:
        n = 7

    if n >= 0:
        return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]
    else:
        target = today + timedelta(days=n)
        return [target.strftime("%Y%m%d")]


def split_timeframes(date_str, segments=SEGMENTS_PER_DAY):
    dt = datetime.strptime(date_str, "%Y%m%d")
    start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
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
    
async def fetch_account_with_limit(semaphore, account_id, service_name, dates):
    async with semaphore:
        await fetch_account(account_id, service_name, dates)
# ==========================================================
# 单段抓取
# ==========================================================
async def fetch_segment(session, account_id, service_name, segment):
    seg_id = segment["seg_id"]
    start_ms = segment["start_ms"]
    end_ms = segment["end_ms"]

    all_logs = {}
    offset = None
    attempt = 1
    page = 0

    while True:
        payload = {
            "view": "invocations",
            "queryId": "workers-logs-invocations",
            "limit": 100,
            "parameters": {
                "datasets": ["cloudflare-workers"],
                "filters": [
                    {
                        "key": "$metadata.service",
                        "type": "string",
                        "value": service_name,
                        "operation": "eq",
                    }
                ],
                "calculations": [],
                "groupBys": [],
                "havings": [],
            },
            "timeframe": {"from": start_ms, "to": end_ms},
        }

        if offset:
            payload["offset"] = offset

        req_start = time.monotonic()

        try:
            async with session.post(
                URL_TEMPLATE.format(account_id=account_id),
                headers=HEADERS,
                json=payload
            ) as resp:
                elapsed = time.monotonic() - req_start
                status = resp.status
                text = await resp.text()

                if status == 200:
                    attempt = 1
                    result = json.loads(text)

                    inv = result.get("result", {}).get("invocations", {})
                    new_cnt = 0

                    for req_id, entries in inv.items():
                        if req_id not in all_logs:
                            all_logs[req_id] = entries
                            new_cnt += len(entries)

                    page += 1
                    print(
                        f"✅ {account_id}/{service_name} 段{seg_id} "
                        f"第{page}页 获取 {new_cnt} 条日志 "
                        f"({elapsed:.2f}s)"
                    )

                    offset = None
                    for req_id in reversed(list(inv.keys())):
                        last_meta = inv[req_id][-1].get("$metadata", {})
                        offset = last_meta.get("id")
                        if offset:
                            break

                    if not offset:
                        break

                elif status == 429:
                    delay = linear_delay(attempt)
                    print(
                        f"⛔ {account_id}/{service_name} 段{seg_id} "
                        f"429 ({elapsed:.2f}s)，{delay:.1f}s 后重试"
                    )
                    await asyncio.sleep(delay)
                    attempt += 1

                else:
                    delay = linear_delay(attempt)
                    print(
                        f"⚠️ {account_id}/{service_name} 段{seg_id} "
                        f"HTTP {status} ({elapsed:.2f}s): {text[:120]}，"
                        f"{delay:.1f}s 后重试"
                    )
                    await asyncio.sleep(delay)
                    attempt += 1

        except asyncio.TimeoutError as err:
            delay = linear_delay(attempt)
            print(
                f"⏱ {account_id}/{service_name} 段{seg_id} 请求超时: {err}，"
                f"{delay:.1f}s 后重试"
            )
            await asyncio.sleep(delay)
            attempt += 1

        except aiohttp.ClientError as err:
            delay = linear_delay(attempt)
            print(
                f"❌ {account_id}/{service_name} 段{seg_id} 网络异常: {err}，"
                f"{delay:.1f}s 后重试"
            )
            await asyncio.sleep(delay)
            attempt += 1

    segment["data"] = all_logs


async def fetch_account(account_id, service_name, dates):
    timeout = aiohttp.ClientTimeout(
        total=60,
        sock_connect=10,
        sock_read=10
    )

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for date_str in dates:
            print(f"\n===== {account_id}/{service_name} {date_str} =====")

            ranges = split_timeframes(date_str)
            segments = [
                {"seg_id": i + 1, "start_ms": s, "end_ms": e, "data": {}}
                for i, (s, e) in enumerate(ranges)
            ]

            tasks = [
                asyncio.create_task(
                    fetch_segment(session, account_id, service_name, seg)
                )
                for seg in segments
            ]

            await asyncio.gather(*tasks)

            all_logs = {}
            for seg in segments:
                all_logs.update(seg["data"])

            out = f"{account_id}_invocations_{date_str}.json"
            with open(out, "w", encoding="utf-8") as f:
                json.dump({"invocations": all_logs}, f, ensure_ascii=False, indent=2)

            print(f"📦 {account_id} 保存 {len(all_logs)} 条日志 → {out}")


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
    else:
        accounts = ACCOUNTS

    dates = get_date_list(selected_days)

    print(f"📅 查询日期: {dates}")
    print(f"👥 账户数: {len(accounts)}")

    # 根据配置决定并发数
    if MAX_CONCURRENT_ACCOUNTS and MAX_CONCURRENT_ACCOUNTS > 0:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_ACCOUNTS)
    else:
    # 0 = 不限并发（账户数即并发数）
    semaphore = asyncio.Semaphore(len(accounts))

    tasks = []
    for acc_id, svc in accounts.items():
        tasks.append(
            asyncio.create_task(
                fetch_account_with_limit(semaphore, acc_id, svc, dates)
        )
    )

    await asyncio.gather(*tasks)



if __name__ == "__main__":
    asyncio.run(main_async())
