#!/usr/bin/env python3
# coding: utf-8

import os, sys, json, copy, asyncio, aiohttp
from datetime import datetime, timedelta, timezone

SEGMENTS_PER_DAY = 24   # 每日划分的段数（保持你的原设定）

ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON")
if not ACCOUNTS_JSON:
    print("❌ 未检测到环境变量 ACCOUNTS_JSON")
    sys.exit(1)

try:
    ACCOUNTS = json.loads(ACCOUNTS_JSON)
    if not isinstance(ACCOUNTS, dict):
        raise ValueError("ACCOUNTS_JSON must be dict")
except Exception as e:
    print("❌ ACCOUNTS_JSON 内容无效：", e)
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


# ==========================================================
# 核心：单段抓取（无限并发版本）
# ==========================================================
async def fetch_segment(session, account_id, service_name, segment):
    seg_id = segment["seg_id"]
    s = segment["start_ms"]
    e = segment["end_ms"]

    all_logs = {}
    offset = None
    attempt = 1
    page = 0

    while True:
        data = {
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
            "timeframe": {"from": s, "to": e},
        }

        if offset:
            data["offset"] = offset

        try:
            async with session.post(
                URL_TEMPLATE.format(account_id=account_id),
                headers=HEADERS,
                json=data,
                timeout=30
            ) as resp:
                status = resp.status
                text = await resp.text()

                if status == 200:
                    attempt = 1
                    try:
                        result = await resp.json()
                    except:
                        print(f"❌ {account_id}/{service_name} 段{seg_id} JSON错误")
                        break

                    inv = result.get("result", {}).get("invocations", {})
                    new_cnt = 0

                    for req_id, entries in inv.items():
                        if req_id not in all_logs:
                            all_logs[req_id] = entries
                            new_cnt += len(entries)

                    page += 1
                    print(
                        f"✅ {account_id}/{service_name} 段{seg_id} 第{page}页 获取 {new_cnt} 条日志"
                    )

                    # 获取下一页 offset
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
                    print(f"⛔ {account_id}/{service_name} 段{seg_id} 429 重试 {delay:.1f}s")
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue

                else:
                    print(
                        f"⚠️ {account_id}/{service_name} 段{seg_id} HTTP {status}: {text[:200]}"
                    )
                    delay = linear_delay(attempt)
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue

        except Exception as e:
            delay = linear_delay(attempt)
            print(
                f"❌ {account_id}/{service_name} 段{seg_id} 网络异常 {e}，{delay:.1f}s 后重试"
            )
            await asyncio.sleep(delay)
            attempt += 1
            continue

    # 保存
    segment["data"] = all_logs


# ==========================================================
# 无限并发：日期 → 所有 segment 直接并发
# ==========================================================
async def fetch_account(account_id, service_name, dates):
    timeout = aiohttp.ClientTimeout(
        total=60,        # 整个请求生命周期最多 30 秒
        sock_connect=10, # TCP 连接阶段最多 10 秒
        sock_read=10     # 和服务器建立连接后，单次读操作等待最多 10 秒
    )

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for date_str in dates:
            print(f"\n===== {account_id}/{service_name} {date_str} =====")

            ranges = split_timeframes(date_str)
            segments = []

            for i, (s, e) in enumerate(ranges):
                segments.append(
                    {
                        "seg_id": i + 1,
                        "start_ms": s,
                        "end_ms": e,
                        "data": {},
                    }
                )

            # 🔥 直接无限并发所有分段
            tasks = [
                asyncio.create_task(
                    fetch_segment(session, account_id, service_name, seg)
                )
                for seg in segments
            ]

            await asyncio.gather(*tasks)

            # 合并所有段
            all_logs = {}
            for seg in segments:
                all_logs.update(seg["data"])

            out = f"{account_id}_invocations_{date_str}.json"
            with open(out, "w", encoding="utf8") as f:
                json.dump({"invocations": all_logs}, f, ensure_ascii=False, indent=2)

            print(f"📦 {account_id} 保存 {len(all_logs)} 条日志 → {out}")



# ==========================================================
# 主程序
# ==========================================================
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

    dates = get_date_list(selected_days) if len(selected_days) != 8 else [selected_days]

    print(f"📅 查询日期: {dates}")
    print(f"👥 账户数: {len(accounts)}")

    for acc_id, svc in accounts.items():
        await fetch_account(acc_id, svc, dates)


if __name__ == "__main__":
    asyncio.run(main_async())
